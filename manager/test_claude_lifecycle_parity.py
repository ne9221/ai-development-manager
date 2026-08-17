"""Phase 4E parity gate: prove provider="claude" gets byte-identical
lifecycle/recovery treatment to provider="codex" through the same, already
provider-neutral machinery -- no new production code, only new test coverage.
A grep across execution_lifecycle.py/execution_recovery.py/task_claims.py/
worktree_locks.py found zero "codex" string references at all; this file is
the empirical proof that absence of a hardcode actually means correct
provider-neutral behavior, not just untested code.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.execution_recovery import recover_task_claim
from manager.executions import execution_health, finish_execution, heartbeat_execution, prepare_task_retry
from manager.task_claims import check_task_execution_claim, claim_task_execution
from manager.tasks import TaskError
from manager.test_execution_lifecycle import build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import HEAD, MemoryRegistry


PROVIDER = "claude"


class ClaudeHeartbeatAndHealthParityTests(unittest.TestCase):
    """heartbeat_execution() / execution_health() take no provider branch at
    all -- this proves it, rather than trusting the grep alone."""

    def setUp(self):
        self.store = build_store(read_only=True, provider=PROVIDER)
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(self.store, object(), None, "p1", "t1", "exec-a", PROVIDER, "read_only",
                               task_claim_registry=MemoryClaimRegistry())
        self.assertEqual("running", self.store.get("executions", "p1", "exec-a")["status"])

    def test_heartbeat_updates_progress_for_a_claude_execution(self):
        heartbeat_execution(self.store, "p1", "exec-a", "provider_wait",
                            provider_evidence={"host": "test-host", "pid": 123, "started_at": "2026-08-13T00:00:00Z"})
        execution = self.store.get("executions", "p1", "exec-a")
        self.assertIsNotNone(execution["heartbeat_at"])
        self.assertEqual("provider_wait", execution["last_provider_event"])

    def test_heartbeat_requires_running_status_regardless_of_provider(self):
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            finish_execution(self.store, object(), "p1", "exec-a", "completed")
        with self.assertRaises(TaskError):
            heartbeat_execution(self.store, "p1", "exec-a", "provider_wait")

    def test_stale_progress_flags_attention_for_a_claude_execution(self):
        execution = self.store.get("executions", "p1", "exec-a")
        now = datetime.now(timezone.utc)
        started_ts = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        stale_ts = (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
        execution.update(started_at=started_ts, heartbeat_at=stale_ts, progress_updated_at=stale_ts)
        health = execution_health(execution, now=now)
        self.assertEqual("attention", health["state"])
        self.assertEqual("provider_progress_stale", health["reason"])

    def test_hard_timeout_exceeded_flags_attention_for_a_claude_execution(self):
        execution = self.store.get("executions", "p1", "exec-a")
        now = datetime.now(timezone.utc)
        recent = now.isoformat().replace("+00:00", "Z")
        past = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        execution.update(heartbeat_at=recent, progress_updated_at=recent, hard_timeout_at=past)
        health = execution_health(execution, now=now)
        self.assertEqual("attention", health["state"])
        self.assertEqual("hard_timeout_exceeded", health["reason"])

    def test_healthy_recent_progress_shows_running_for_a_claude_execution(self):
        execution = self.store.get("executions", "p1", "exec-a")
        now = datetime.now(timezone.utc)
        recent = now.isoformat().replace("+00:00", "Z")
        execution.update(heartbeat_at=recent, progress_updated_at=recent)
        health = execution_health(execution, now=now)
        self.assertEqual("healthy", health["state"])


class ClaudeTerminalizationParityTests(unittest.TestCase):
    """interrupted/failed terminalization, cleanup evidence, and task claim
    release for provider="claude" -- direct parity with test_execution_recovery.py's
    RecoveryTests.terminal_claim(), just with "claude" substituted for "codex"."""

    def terminal_claim(self, status="completed", read_only=True):
        store = build_store(read_only=read_only, provider=PROVIDER)
        claim = MemoryClaimRegistry()
        writer = None if read_only else MemoryRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), writer, "p1", "t1", "exec-a", PROVIDER,
                                      "read_only" if read_only else "production_write",
                                      baseline_head=None if read_only else HEAD, task_claim_registry=claim)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminal = terminalize_execution(store, object(), writer, claim, "p1", "t1", "exec-a", PROVIDER, status,
                                             gate["task_claim"]["generation"], True,
                                             lease_token=gate["lease"]["lease_token"] if gate["lease"] else None)
        return store, claim, terminal

    def test_interrupted_terminalization_completes_cleanly(self):
        store, claim, terminal = self.terminal_claim(status="interrupted")
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("interrupted", execution["status"])
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])
        self.assertEqual([], execution["cleanup_evidence"]["errors"])

    def test_failed_terminalization_completes_cleanly(self):
        store, claim, terminal = self.terminal_claim(status="failed")
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("failed", execution["status"])
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])

    def test_cleanup_evidence_shows_claim_released_for_read_only_claude_execution(self):
        store, claim, terminal = self.terminal_claim(status="completed", read_only=True)
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertEqual("not_required", execution["cleanup_evidence"]["writer_release"])
        self.assertIsNone(check_task_execution_claim(claim, "p1", "t1"))

    def test_cleanup_evidence_shows_writer_and_claim_released_for_production_write_claude_execution(self):
        store, claim, terminal = self.terminal_claim(status="completed", read_only=False)
        execution = store.get("executions", "p1", "exec-a")
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertEqual("released", execution["cleanup_evidence"]["writer_release"])

    # item: task claim release via the standalone recovery tool too, not just
    # the inline cleanup_execution() path -- both entry points must agree.
    def test_recover_task_claim_releases_a_terminal_claude_claim(self):
        store, claim, terminal = self.terminal_claim(status="completed")
        stale = claim_task_execution(claim, "p1", "t1", "exec-a", PROVIDER, "2026-08-13T01:00:00Z")
        result = recover_task_claim(store, claim, "p1", "t1")
        self.assertEqual("released", result["status"])
        self.assertEqual(stale["generation"], result["generation"])
        # idempotent: second call finds nothing left to release
        self.assertEqual({"status": "clean", "released": False, "reason": "no_active_claim"},
                         recover_task_claim(store, claim, "p1", "t1"))

    def test_terminalize_is_idempotent_for_a_claude_execution(self):
        store, claim, first = self.terminal_claim(status="completed")
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            second = terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", PROVIDER, "completed",
                                            first["execution"]["cleanup_evidence"].get("generation") or 0, True)
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["execution"]["status"], second["execution"]["status"])


class ClaudeRetryParityTests(unittest.TestCase):
    """prepare_task_retry() for a failed provider="claude" execution."""

    def test_failed_claude_execution_can_be_prepared_for_retry(self):
        store = build_store(read_only=True, provider=PROVIDER)
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            gate = enter_running_gate(store, object(), None, "p1", "t1", "exec-a", PROVIDER, "read_only",
                                      task_claim_registry=claim)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(store, object(), None, claim, "p1", "t1", "exec-a", PROVIDER, "failed",
                                  gate["task_claim"]["generation"], True)
        ready = prepare_task_retry(store, claim, "p1", "t1", "exec-a", retry_count=1)
        self.assertEqual("ready", ready["status"])
        self.assertEqual(1, ready["source_context"]["retry_count"])
        self.assertEqual("exec-a", ready["source_context"]["retry_of_execution_id"])

    def test_retry_requires_no_active_claim_regardless_of_provider(self):
        store = build_store(read_only=True, provider=PROVIDER)
        claim = MemoryClaimRegistry()
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(store, object(), None, "p1", "t1", "exec-a", PROVIDER, "read_only",
                               task_claim_registry=claim)
        # still "running": retry must refuse regardless of provider
        with self.assertRaises(TaskError):
            prepare_task_retry(store, claim, "p1", "t1", "exec-a", retry_count=1)


if __name__ == "__main__":
    unittest.main()
