import hashlib
import json
import os
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from manager.claude_config_locks import (
    acquire_claude_config_lock,
    release_claude_config_lock,
    ConfigLockBusyError,
)
from manager.command_watcher import (
    _promote_waiting_quota_task,
    _reconcile_active,
    _claimed,
    _terminal,
    _result,
    CLAIM_TIMEOUT_SECONDS,
)
from manager.execution_lifecycle import (
    enter_running_gate,
    terminalize_execution,
    cleanup_execution,
)
from manager.execution_recovery import recover_task_claim
from manager.executions import (
    reserve_execution,
    heartbeat_execution,
)
from manager.execution_runner import _persist_session_link, LaunchRequest
from manager.gcs_lock_registry import GCSLockRegistry, RegistryConflict
from manager.session_identity import manager_session_key
from manager.task_claims import (
    claim_task_execution,
    check_task_execution_claim,
    release_task_execution_claim,
    TaskClaimConflict,
)
from manager.tasks import DriveRecords, TaskError, validate
from manager.worktree_locks import (
    acquire as acquire_lease,
    reconcile_unlinked_terminal_lease,
    release as release_lease,
    repository_lock_id,
)


class MemoryRegistry:
    def __init__(self):
        self.document = None
        self.generation = 0
        self._lock = threading.Lock()

    def read_if_exists(self):
        with self._lock:
            if self.document is None:
                return None
            return dict(self.document), self.generation, datetime.now(timezone.utc)

    def read(self):
        res = self.read_if_exists()
        if res is None:
            raise TaskError('not found')
        return res

    def create_if_absent(self, document):
        with self._lock:
            if self.document is not None:
                raise RegistryConflict('already exists')
            self.document = dict(document)
            self.generation += 1
            return self.generation

    def delete_if_generation_matches(self, generation):
        with self._lock:
            if self.document is None or self.generation != generation:
                raise RegistryConflict('generation mismatch')
            self.document = None
            self.generation += 1

    def cas(self, etag, updated_document):
        return self.compare_and_swap(etag, updated_document)

    def compare_and_swap(self, expected_generation, document):
        with self._lock:
            if self.generation != expected_generation:
                raise RegistryConflict('generation mismatch')
            self.document = dict(document)
            self.generation += 1
            return self.generation


class MemoryStore:
    def __init__(self):
        self.records = {}
        self._lock = threading.Lock()

    def get(self, area, project_id, name):
        with self._lock:
            key = (area, project_id, name)
            if key not in self.records:
                raise TaskError(f'{area} record not found: {name}')
            return dict(self.records[key])

    def put(self, area, project_id, name, document):
        with self._lock:
            key = (area, project_id, name)
            self.records[key] = dict(document)
            return dict(document)


def _valid_task(project_id='p1', task_id='t1', **kwargs):
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    base = {
        'project_id': project_id, 'task_id': task_id, 'title': 'Test Task',
        'task_type': 'general', 'complexity': 'medium', 'expected_minutes': 20,
        'needs_repo_edit': False, 'needs_research': False, 'needs_browser': False,
        'parallelizable': True, 'read_only': True, 'scope': ['.'], 'constraints': [],
        'acceptance_criteria': ['pass tests'], 'working_directory': None,
        'status': 'queued', 'assigned_provider': None, 'recommended_provider': None,
        'priority': 'normal', 'created_at': now, 'updated_at': now, 'completed_at': None,
        'mode': 'code', 'effort': 'medium', 'depends_on': [], 'account_id': None,
        'source_context': {}, 'blocked_reason': None, 'current_progress': '', 'next_action': '',
        'execution_policies': [], 'validation_command': None, 'allow_no_change_success': False,
    }
    base.update(kwargs)
    validate('task', base)
    return base


def _valid_execution(store, project_id='p1', task_id='t1', execution_id='exec-1', **kwargs):
    reserved = reserve_execution(
        store, project_id, task_id, execution_id, 'codex',
        quota_evidence={'status': 'known', 'windows': []},
    )
    if kwargs:
        reserved.update(kwargs)
        validate('execution', reserved)
        store.put('executions', project_id, execution_id, reserved)
    return reserved


def _valid_command(project_id='p1', task_id='t1', command_id=None, **kwargs):
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    cid = command_id or task_id
    base = {
        'command_id': cid, 'project_id': project_id, 'task_id': task_id, 'provider': 'codex',
        'model': 'gpt-4', 'fallback_model': None, 'mode': 'code', 'effort': 'medium',
        'selection_reason': [], 'quota_evidence': None, 'created_at': now,
        'status': 'queued', 'execution_id': None, 'claimed_at': None, 'completed_at': None, 'result': None,
    }
    base.update(kwargs)
    validate('command', base)
    return base


class TestConcurrencyReliabilityGate4(unittest.TestCase):
    def test_1_two_workers_simultaneously_claim_same_command(self):
        # Scenario 1: Two workers simultaneously enter running gate for the SAME Command / Task.
        # Exactly one must succeed as the owner, and the rival MUST receive TaskClaimConflict.
        registry = MemoryRegistry()
        store = MemoryStore()
        task = _valid_task('p1', 't1', status='ready')
        store.put('tasks', 'p1', 't1', task)
        _valid_execution(store, 'p1', 't1', 'exec-1')

        results, errors = [], []
        barrier = threading.Barrier(2)

        def worker(worker_id):
            barrier.wait(timeout=2)
            try:
                gate = enter_running_gate(
                    store, None, None, 'p1', 't1', 'exec-1', 'codex', 'read_only',
                    task_claim_registry=registry,
                )
                results.append((worker_id, gate))
            except Exception as exc:
                errors.append((worker_id, exc))

        t1 = threading.Thread(target=worker, args=('w1',))
        t2 = threading.Thread(target=worker, args=('w2',))
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)

        self.assertEqual(1, len(results), f'Expected exactly 1 winner, got {len(results)}')
        self.assertEqual(1, len(errors), f'Expected exactly 1 conflict error, got {len(errors)}')
        self.assertIsInstance(errors[0][1], (TaskClaimConflict, TaskError))

    def test_2_claim_crash_recovery_before_execution_created(self):
        # Scenario 2: Command claimed, but worker crashes before Execution record is created.
        # Reconcile after claim timeout must fail the command cleanly and unblock the task.
        registry = MemoryRegistry()
        store = MemoryStore()
        task = _valid_task('p1', 't1')
        store.put('tasks', 'p1', 't1', task)
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 10)).isoformat().replace('+00:00', 'Z')
        command = _valid_command(
            'p1', 't1', 't1', status='claimed', execution_id='t1', claimed_at=old_time, created_at=old_time,
        )
        store.put('commands', 'p1', 't1', command)

        res = _reconcile_active(store, None, command, lambda bucket, p, t: registry)
        self.assertEqual('failed', res['status'])
        self.assertTrue(res['reconciled'])

        final_cmd = store.get('commands', 'p1', 't1')
        self.assertEqual('failed', final_cmd['status'])
        self.assertEqual('claim_timeout', final_cmd['result']['error_kind'])

        final_task = store.get('tasks', 'p1', 't1')
        self.assertEqual('queued', final_task['status'])

    def test_3_two_processes_simultaneously_create_execution(self):
        # Scenario 3: Two processes simultaneously call reserve_execution with identical parameters.
        # Both must succeed idempotently without error.
        store = MemoryStore()
        task = _valid_task('p1', 't1')
        store.put('tasks', 'p1', 't1', task)
        results, errors = [], []
        barrier = threading.Barrier(2)

        def reserver(worker_id):
            barrier.wait(timeout=2)
            try:
                res = reserve_execution(
                    store, 'p1', 't1', 'exec-1', 'codex',
                    quota_evidence={'status': 'known', 'windows': []},
                )
                results.append((worker_id, res))
            except Exception as exc:
                errors.append((worker_id, exc))

        t1 = threading.Thread(target=reserver, args=('w1',))
        t2 = threading.Thread(target=reserver, args=('w2',))
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)

        self.assertEqual(2, len(results), f'Both should succeed, errors: {errors}')
        self.assertEqual(0, len(errors))
        self.assertEqual(results[0][1]['execution_id'], results[1][1]['execution_id'])

    def test_4_provider_launch_timeout_reconciliation(self):
        # Scenario 4: Provider launched, execution in running state, process dies (stopped).
        # Reconcile must safely terminalize as interrupted and release claims.
        registry = MemoryRegistry()
        store = MemoryStore()
        task = _valid_task('p1', 't1', source_context={'active_execution_id': 'exec-1'}, status='in_progress')
        store.put('tasks', 'p1', 't1', task)
        claim_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        claim = claim_task_execution(registry, 'p1', 't1', 'exec-1', 'codex', claim_time)
        host = socket.gethostname()[:100]
        execution = _valid_execution(
            store, 'p1', 't1', 'exec-1',
            status='running', access='read_only', started_at=claim_time, heartbeat_at=claim_time,
            progress_updated_at=claim_time, hard_timeout_at=claim_time, source_confidence='high',
            lease_evidence=None, cleanup_evidence=None,
            quota_before={'status': 'known', 'windows': [], 'captured_at': claim_time},
            provider_evidence={'host': host, 'pid': 999999, 'started_at': claim_time, 'creation_identity': 'old'},
        )
        command = _valid_command(
            'p1', 't1', 'cmd-1', status='running', execution_id='exec-1', claimed_at=claim_time, created_at=claim_time,
        )
        store.put('commands', 'p1', 'cmd-1', command)

        # Mocking process state as stopped (pid 999999 is dead)
        res = _reconcile_active(store, None, command, lambda bucket, p, t: registry)
        self.assertTrue(res.get('reconciled'))

        final_exec = store.get('executions', 'p1', 'exec-1')
        self.assertEqual('interrupted', final_exec['status'])

        final_task = store.get('tasks', 'p1', 't1')
        self.assertEqual('blocked', final_task['status'])

    def test_5_stale_lease_with_real_session_does_not_corrupt(self):
        # Scenario 5: Stale lease linked to session refuses unlinked reconciliation.
        lease_registry = MemoryRegistry()
        lock_id = repository_lock_id('github:org/repo')
        lease_registry.document = {
            'schema_version': '0.2.0',
            'locks': {
                lock_id: {
                    'lock_id': lock_id, 'repository': 'github:org/repo', 'branch': 'refs/heads/main',
                    'scope': ['.'], 'baseline_head': '0' * 40, 'access': 'production',
                    'project_id': 'p1', 'task_id': 't1', 'execution_id': 'exec-1',
                    'provider': 'codex', 'session_id': 'codex:sess-1',
                    'status': 'active', 'generation': 1, 'lease_token_hash': '0' * 64,
                    'created_at': '2026-08-01T00:00:00Z', 'updated_at': '2026-08-01T00:00:00Z',
                    'expires_at': '2026-08-01T01:00:00Z', 'released_at': None,
                }
            }
        }
        lease_registry.generation = 1

        # Unlinked reconciliation MUST refuse a lease with a linked session
        with self.assertRaisesRegex(TaskError, 'refuses a linked provider session'):
            reconcile_unlinked_terminal_lease(
                lease_registry, lock_id, 'p1', 't1', 'exec-1', 'codex', 'interrupted',
            )

    def test_6_stale_claim_recovery_refuses_active_execution(self):
        # Scenario 6: recover_task_claim must never release a claim for a running execution.
        registry = MemoryRegistry()
        store = MemoryStore()
        claim_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        claim = claim_task_execution(registry, 'p1', 't1', 'exec-1', 'codex', claim_time)

        task = _valid_task('p1', 't1', source_context={'active_execution_id': 'exec-1'}, status='in_progress')
        store.put('tasks', 'p1', 't1', task)
        _valid_execution(
            store, 'p1', 't1', 'exec-1', status='running', started_at=claim_time,
            access='read_only', source_confidence='high', progress_updated_at=claim_time,
            lease_evidence=None, cleanup_evidence=None,
            quota_before={'status': 'known', 'windows': []},
        )

        res = recover_task_claim(store, registry, 'p1', 't1')
        self.assertEqual('refused', res['status'])
        self.assertFalse(res['released'])
        self.assertEqual('running_execution_requires_provider_stop_and_terminal_recovery', res['reason'])

    def test_7_claude_config_lock_concurrency_slot_consistency(self):
        # Scenario 7: Claude config lock enforces mutual exclusion per config_dir.
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, 'state.json')
            lock_file = os.path.join(temp_dir, 'state.lock')
            cfg_dir = os.path.join(temp_dir, 'claude_cfg')
            os.makedirs(cfg_dir, exist_ok=True)

            lock1 = acquire_claude_config_lock(
                cfg_dir, account_id='acc1', project_id='p1', task_id='t1', execution_id='e1',
                state_path=state_file, lock_path=lock_file,
            )
            self.assertIsNotNone(lock1)

            # Second acquire for same dir by different execution in same process must fail closed
            with self.assertRaises(ConfigLockBusyError):
                acquire_claude_config_lock(
                    cfg_dir, account_id='acc1', project_id='p1', task_id='t1', execution_id='e2',
                    state_path=state_file, lock_path=lock_file,
                )

            # After release, second acquire succeeds
            rel = release_claude_config_lock(lock1, state_path=state_file, lock_path=lock_file)
            self.assertTrue(rel['released'])

            lock2 = acquire_claude_config_lock(
                cfg_dir, account_id='acc1', project_id='p1', task_id='t1', execution_id='e2',
                state_path=state_file, lock_path=lock_file,
            )
            self.assertIsNotNone(lock2)
            release_claude_config_lock(lock2, state_path=state_file, lock_path=lock_file)

    def test_8_waiting_quota_promotion_races_watcher_claim(self):
        # Scenario 8: Promotion and watcher claim race.
        # Promotion creates queued command; watcher claims it. Concurrent promotion must not overwrite claimed/running.
        store = MemoryStore()
        task = _valid_task(
            'p1', 't1',
            status='blocked', blocked_reason='waiting_quota',
            source_context={'origin': 'trusted_ingress', 'admission_version': 'v1'},
            preferred_provider='codex',
        )
        store.put('tasks', 'p1', 't1', task)

        # Simulate promotion created command
        cmd = _valid_command('p1', 't1', 't1', status='queued', created_via='trusted_ingress')
        store.put('commands', 'p1', 't1', cmd)

        # Watcher claims it
        claimed = _claimed(cmd)
        store.put('commands', 'p1', 't1', claimed)

        # A concurrent promotion sweep for the same task MUST observe existing command and NOT overwrite
        promoted = _promote_waiting_quota_task(store, None, task, {'providers': [{'provider': 'codex', 'status': 'known', 'windows': []}]})
        self.assertIsNone(promoted)

        # Command remains claimed by watcher
        current_cmd = store.get('commands', 'p1', 't1')
        self.assertEqual('claimed', current_cmd['status'])


if __name__ == '__main__':
    unittest.main()
