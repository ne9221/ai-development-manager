"""Comprehensive unit, integration, and adversarial tests for ADM Minimal Autopilot
Slice 1 (manager/autopilot.py) addressing all 4 AG Preflight P0 findings."""

import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from manager.autopilot import (
    STATE_ATTENTION_REQUIRED,
    STATE_BLOCKED,
    STATE_DISPATCHED,
    STATE_DONE,
    STATE_ELIGIBLE,
    STATE_NEXT_READY,
    STATE_RETRY_ELIGIBLE,
    STATE_REVIEW_REQUIRED,
    evaluate_circuit_breaker,
    evaluate_dependencies,
    evaluate_repository_transition,
    find_next_candidate_task,
    step_autopilot,
    verify_predecessor_barrier,
)
from manager.test_autopilot_continuations import MemoryClaimRegistryWithCAS
from manager.test_task_claims import MemoryClaimRegistry


class MemoryStore:
    def __init__(self):
        self.data: dict[tuple[str, str, str], dict] = {}

    def get(self, area: str, project_id: str, name: str) -> dict:
        key = (area, project_id, name)
        if key not in self.data:
            from manager.tasks import TaskError
            raise TaskError(f"not found: {key}")
        return dict(self.data[key])

    def put(self, area: str, project_id: str, name: str, document: dict) -> dict:
        key = (area, project_id, name)
        self.data[key] = dict(document)
        return document

    def list_records(self, area: str, project_id: str) -> list[dict]:
        return [dict(v) for (a, p, _), v in self.data.items() if a == area and p == project_id]

    def project_folder(self, area: str, project_id: str, create: bool = False) -> str:
        return f"{area}/{project_id}"

    def latest(self, area: str, project_id: str, task_id: str) -> dict:
        items = [v for (a, p, _), v in self.data.items() if a == area and p == project_id and v.get("task_id") == task_id]
        if not items:
            from manager.tasks import TaskError
            raise TaskError(f"no {area} record for task {task_id}")
        return max(items, key=lambda item: item.get("created_at") or "")


def sample_project(project_id="p1", working_directory=None):
    return {
        "project_id": project_id,
        "name": "Test Project",
        "default_branch": "refs/heads/main",
        "repo": "https://github.com/example/repo",
        "working_directory": working_directory or os.getcwd(),
        "active_tasks": [],
        "runtime_ssot": "drive",
        "current_phase": "Phase 1",
        "important_constraints": [],
        "project_rules": [],
        "updated_at": "2026-08-19T00:00:00Z",
    }


def sample_task(project_id="p1", task_id="t1", title="Task 1", status="ready",
                depends_on=None, read_only=True, needs_repo_edit=False,
                execution_policies=None):
    return {
        "task_id": task_id,
        "project_id": project_id,
        "title": title,
        "status": status,
        "priority": "normal",
        "created_at": "2026-08-19T00:00:00Z",
        "updated_at": "2026-08-19T00:00:00Z",
        "task_type": "general",
        "complexity": "medium",
        "expected_minutes": 20,
        "read_only": read_only,
        "needs_repo_edit": needs_repo_edit,
        "needs_research": False,
        "needs_browser": False,
        "parallelizable": False,
        "working_directory": os.getcwd(),
        "branch": "refs/heads/main",
        "baseline_head": None,
        "worktree_id": None,
        "allowed_paths": [],
        "preferred_provider": None,
        "excluded_provider": None,
        "execution_policies": execution_policies or ["disposable", "no_external_writes", "no_repo_writes", "read_only"],
        "scope": [],
        "constraints": [],
        "acceptance_criteria": [],
        "recommended_provider": "codex",
        "assigned_provider": "codex",
        "account_id": None,
        "mode": "standard",
        "effort": "medium",
        "depends_on": depends_on or [],
        "blocked_reason": None,
        "source_context": {},
        "quota_evidence": None,
        "current_progress": "Not started",
        "next_action": "Begin",
    }


def sample_execution(project_id="p1", task_id="t1", execution_id="exec-1",
                     status="completed", access="read_only",
                     persistence="complete", task_claim_release="released",
                     writer_release="not_required"):
    lease_evidence = None
    if access == "production_write":
        lease_evidence = {
            "authority": "acquired",
            "lock_id": "repo-" + "a" * 64,
            "generation": 1,
            "repository": "github:example/repo",
            "branch": "refs/heads/main",
            "scope": ["src"],
            "baseline_head": "0" * 40,
        }
    return {
        "execution_id": execution_id,
        "task_id": task_id,
        "project_id": project_id,
        "provider": "codex",
        "mode": "standard",
        "effort": "medium",
        "reserved_at": "2026-08-19T00:00:00Z",
        "started_at": "2026-08-19T00:00:01Z",
        "completed_at": "2026-08-19T00:05:00Z",
        "finished_at": "2026-08-19T00:05:00Z",
        "session_id": "codex:sess-1",
        "provider_session_id": "sess-1",
        "account_id": None,
        "elapsed_minutes": 5.0,
        "status": status,
        "retry_count": 0,
        "retry_of_execution_id": None,
        "quota_before": {"status": "known", "confidence": "high"},
        "quota_after": {"status": "known", "confidence": "high"},
        "quota_delta": None,
        "source_confidence": "high",
        "access": access,
        "lease_evidence": lease_evidence,
        "cleanup_evidence": {
            "persistence": persistence,
            "task_claim_release": task_claim_release,
            "writer_release": writer_release,
            "errors": [],
        },
        "notes": [],
        "task_snapshot": sample_task(project_id, task_id),
    }


def sample_quota_document(now=None):
    now_str = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "contract_version": "1.0",
        "schema_version": "1.0",
        "generated_at": now_str,
        "providers": [
            {
                "provider": "codex",
                "display_name": "Codex",
                "status": "known",
                "source": "codex_app_server",
                "source_type": "official",
                "confidence": "official",
                "last_updated": now_str,
                "windows": [{"name": "primary", "remaining_percent": 85.0}],
            },
            {
                "provider": "claude",
                "display_name": "Claude",
                "status": "known",
                "source": "claude_code_statusline_rate_limits",
                "source_type": "official",
                "confidence": "official",
                "last_updated": now_str,
                "windows": [{"name": "5h", "remaining_percent": 90.0}],
            },
        ],
        "accounts": [],
    }


class DependencyProgressionTests(unittest.TestCase):
    """AG Finding 1: task.depends_on deterministic progression tests."""

    def setUp(self):
        self.store = MemoryStore()

    def test_no_dependencies_is_immediately_satisfied(self):
        task = sample_task("p1", "t1", depends_on=[])
        res = evaluate_dependencies(self.store, "p1", task)
        self.assertTrue(res["satisfied"])
        self.assertEqual(STATE_ELIGIBLE, res["state"])

    def test_all_dependencies_completed_with_evidence_is_satisfied(self):
        dep_task = sample_task("p1", "dep-1", status="completed")
        self.store.put("tasks", "p1", "dep-1", dep_task)
        exec_record = sample_execution("p1", "dep-1", "exec-dep-1", status="completed")
        self.store.put("executions", "p1", "exec-dep-1", exec_record)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertTrue(res["satisfied"])
        self.assertEqual(STATE_ELIGIBLE, res["state"])
        self.assertEqual(["dep-1"], res["completed_dependencies"])

    def test_missing_dependency_fails_closed(self):
        target = sample_task("p1", "t2", depends_on=["nonexistent-task"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertIn("dependency_missing:nonexistent-task", res["reason"])

    def test_incomplete_dependency_fails_closed(self):
        dep_task = sample_task("p1", "dep-1", status="in_progress")
        self.store.put("tasks", "p1", "dep-1", dep_task)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertIn("dependency_incomplete:dep-1:in_progress", res["reason"])

    def test_dependency_completed_but_execution_evidence_missing_fails_closed(self):
        dep_task = sample_task("p1", "dep-1", status="completed")
        self.store.put("tasks", "p1", "dep-1", dep_task)
        # No execution in store

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertIn("dependency_execution_evidence_missing:dep-1", res["reason"])

    def test_dependency_completed_but_claim_unreleased_fails_attention(self):
        dep_task = sample_task("p1", "dep-1", status="completed")
        self.store.put("tasks", "p1", "dep-1", dep_task)
        exec_record = sample_execution("p1", "dep-1", "exec-dep-1", status="completed",
                                       task_claim_release="retained")
        self.store.put("executions", "p1", "exec-dep-1", exec_record)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])


class DirtyTreeTransitionGateTests(unittest.TestCase):
    """AG Finding 2: Dirty-tree and baseline transition safety gate."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())

    def test_clean_working_directory_evaluates_clean(self):
        task = sample_task("p1", "t1")
        res = evaluate_repository_transition(self.store, self.project, task,
                                             git_checker=lambda _: (True, ""))
        self.assertTrue(res["clean"])
        self.assertEqual(STATE_ELIGIBLE, res["state"])

    def test_dirty_working_tree_blocks_with_zero_dispatch(self):
        task = sample_task("p1", "t1")
        res = evaluate_repository_transition(self.store, self.project, task,
                                             git_checker=lambda _: (False, " M file.py\n?? untracked.txt"))
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("working_tree_dirty", res["reason"])
        self.assertIn("M file.py", res["details"])

    def test_invalid_working_directory_blocks(self):
        task = sample_task("p1", "t1")
        task["working_directory"] = "relative/nonexistent"
        res = evaluate_repository_transition(self.store, self.project, task)
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_BLOCKED, res["state"])

    def test_active_unresolved_writer_lease_requires_attention(self):
        task = sample_task("p1", "t1")
        fake_registry = lambda: {
            "locks": {
                "repo-123": {
                    "effective_status": "active",
                    "execution_id": "other-exec",
                }
            }
        }
        res = evaluate_repository_transition(self.store, self.project, task,
                                             git_checker=lambda _: (True, ""),
                                             writer_registry_reader=fake_registry)
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("active_writer_lease_conflict", res["reason"])


class PredecessorBarrierTests(unittest.TestCase):
    """AG Finding 3: Post-execution verification / review barrier."""

    def setUp(self):
        self.store = MemoryStore()

    def test_read_only_completed_and_fully_persisted_advances_to_next_ready(self):
        task = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="read_only")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertTrue(res["passed"])
        self.assertEqual(STATE_NEXT_READY, res["state"])

    def test_completed_but_persistence_incomplete_halts(self):
        task = sample_task("p1", "t1", status="completed")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", persistence="incomplete")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("persistence_incomplete", res["reason"])

    def test_write_capable_predecessor_stops_at_review_required(self):
        task = sample_task("p1", "t1", status="completed", read_only=False, needs_repo_edit=True)
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="production_write",
                                       writer_release="released")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_REVIEW_REQUIRED, res["state"])
        self.assertIn("requires_manual_review", res["reason"])

    def test_failed_predecessor_stops_at_retry_eligible_or_blocked(self):
        task = sample_task("p1", "t1", status="blocked")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="failed")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_RETRY_ELIGIBLE, res["state"])


class RunawayCircuitBreakerTests(unittest.TestCase):
    """AG Finding 4: Runaway circuit breaker & budget guard."""

    def test_max_continuation_steps_reached_halts_cleanly(self):
        start = datetime.now(timezone.utc)
        res = evaluate_circuit_breaker(start, continuation_count=1, quota_summary={"providers": [{"has_reliable_quota": True}]}, max_steps=1)
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_DONE, res["state"])
        self.assertIn("max_continuation_steps_reached:1", res["reason"])

    def test_time_budget_exceeded_halts_with_attention(self):
        start = datetime.now(timezone.utc) - timedelta(minutes=70)
        res = evaluate_circuit_breaker(start, continuation_count=0, quota_summary={"providers": [{"has_reliable_quota": True}]}, max_minutes=60)
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("autopilot_time_budget_exceeded", res["reason"])

    def test_quota_unreliable_fails_closed(self):
        start = datetime.now(timezone.utc)
        unreliable_quota = {"providers": [{"has_reliable_quota": False}]}
        res = evaluate_circuit_breaker(start, continuation_count=0, quota_summary=unreliable_quota)
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("quota_unreliable", res["reason"])


class AutopilotStepIntegrationTests(unittest.TestCase):
    """End-to-end multi-gate continuation step execution tests."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()

    def test_successful_read_only_continuation_step(self):
        # 1. Setup completed predecessor task & execution
        task1 = sample_task("p1", "t1", title="Task 1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        # 2. Setup candidate next task dependent on t1
        task2 = sample_task("p1", "t2", title="Task 2 Read Only Analysis", status="ready",
                            depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

        claim_reg = MemoryClaimRegistryWithCAS()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(
            self.store, None, "p1", "exec-1", session_start,
            continuation_count=0, bucket="test-bucket",
            continuation_registry_factory=lambda *_: claim_reg,
            dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
            task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
            quota_document=self.quota_doc,
            git_checker=lambda _: (True, ""),
        )

        self.assertEqual("dispatched", res["status"])
        self.assertEqual(STATE_DISPATCHED, res["state"])
        self.assertEqual("t2", res["next_task_id"])
        self.assertEqual(1, res["continuation_count"])

        # Verify new Command was written to store
        cmd = self.store.get("commands", "p1", res["next_command_id"])
        self.assertEqual("t2", cmd["task_id"])
        self.assertEqual("queued", cmd["status"])
        self.assertEqual("direct_dispatch_ingress", cmd["created_via"])
        self.assertEqual("v1", cmd["admission_version"])

    def test_duplicate_step_is_idempotent_no_duplicate_command(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

        claim_reg = MemoryClaimRegistryWithCAS()
        session_start = datetime.now(timezone.utc)

        # First run
        first = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                               bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                               quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", first["status"])

        # Second duplicate run. Codex P1-1 now excludes t2 from candidacy
        # before the CAS claim step is ever reached, because the first run
        # already left it with a queued Command -- a more accurate halt
        # reason than "already_claimed", but it protects the exact same
        # invariant this test cares about: no duplicate Command is ever
        # created for the same predecessor execution.
        second = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                                bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                                quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("halted", second["status"])
        self.assertEqual(STATE_BLOCKED, second["state"])
        self.assertEqual("no_eligible_candidate", second["reason"])
        self.assertIn("t2", second["blocked_candidates"][0]["task_id"])
        self.assertIn("already_active", second["blocked_candidates"][0]["reason"])

        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(1, len(commands), "exactly one Command must exist across both polls")

    def test_write_capable_candidate_next_task_halts_at_review_required(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        # Task 2 is write-capable (needs_repo_edit=True)
        task2 = sample_task("p1", "t2", title="Task 2 Write Code", status="ready",
                            depends_on=["t1"], read_only=False, needs_repo_edit=True)
        self.store.put("tasks", "p1", "t2", task2)

        claim_reg = MemoryClaimRegistryWithCAS()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                             bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))

        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_REVIEW_REQUIRED, res["state"])
        self.assertIn("slice1_write_task_requires_slice2_activation", res["reason"])

    def test_all_tasks_completed_terminates_with_done(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        claim_reg = MemoryClaimRegistryWithCAS()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                             bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))

        self.assertEqual("done", res["status"])
        self.assertEqual(STATE_DONE, res["state"])
        self.assertEqual("all_tasks_completed", res["reason"])

    def test_already_dispatched_continuation_reports_already_claimed_not_no_eligible_candidate(self):
        """Distinct from the P1-1 duplicate-poll test above: when the
        already-dispatched task's Command has reached a TERMINAL status
        (so P1-1 no longer excludes it as "already active"), a duplicate
        poll must still be caught -- this time by the CAS claim record
        itself reporting `already_claimed`, proving that code path is not
        dead once P1-1 stops shadowing it."""
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

        claim_reg = MemoryClaimRegistryWithCAS()
        session_start = datetime.now(timezone.utc)

        first = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                               bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                               quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", first["status"])

        # Simulate the dispatched Command reaching a terminal status (as the
        # real command_watcher would once the provider run finishes), which
        # takes it out of P1-1's non-terminal exclusion set.
        cmd = self.store.get("commands", "p1", first["next_command_id"])
        cmd["status"] = "completed"
        self.store.put("commands", "p1", first["next_command_id"], cmd)

        second = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                                bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
 dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
 task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                                quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("already_claimed", second["status"])
        self.assertEqual(STATE_DISPATCHED, second["state"])
        self.assertEqual("t2", second["existing_claim"]["next_task_id"])

        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(1, len(commands))


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=10)


def _git_output(*args, cwd):
    res = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=10)
    return res.stdout.strip()


class RepositoryFailClosedTests(unittest.TestCase):
    """Codex P0-1: evaluate_repository_transition must be strictly
    fail-closed, using real git repositories (not stubs) wherever the
    scenario is about proving the DEFAULT implementation's own behavior,
    not just the injectable contract."""

    def setUp(self):
        self.store = MemoryStore()
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.realpath(self._tmp.name)
        _git("init", "-q", cwd=self.repo_dir)
        _git("config", "user.email", "test@example.com", cwd=self.repo_dir)
        _git("config", "user.name", "Test", cwd=self.repo_dir)
        with open(os.path.join(self.repo_dir, "file.txt"), "w") as fh:
            fh.write("hello\n")
        _git("add", "file.txt", cwd=self.repo_dir)
        _git("commit", "-q", "-m", "initial", cwd=self.repo_dir)
        self.head = _git_output("rev-parse", "HEAD", cwd=self.repo_dir)
        self.project = sample_project("p1", working_directory=self.repo_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_non_git_directory_blocks(self):
        with tempfile.TemporaryDirectory() as plain_dir:
            task = sample_task("p1", "t1")
            task["working_directory"] = os.path.realpath(plain_dir)
            res = evaluate_repository_transition(self.store, self.project, task)
            self.assertFalse(res["clean"])
            self.assertEqual(STATE_BLOCKED, res["state"])
            self.assertEqual("git_status_command_failed", res["reason"])

    def test_git_status_command_failure_blocks(self):
        task = sample_task("p1", "t1")
        task["working_directory"] = self.repo_dir
        res = evaluate_repository_transition(self.store, self.project, task,
                                             git_checker=lambda _: (False, "git_error:simulated transport failure"))
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("git_status_command_failed", res["reason"])

    def test_rev_parse_failure_is_not_swallowed_and_blocks(self):
        task = sample_task("p1", "t1")
        task["working_directory"] = self.repo_dir
        task["baseline_head"] = self.head

        def _raising_head_reader(cwd):
            raise OSError("simulated rev-parse failure")

        res = evaluate_repository_transition(self.store, self.project, task,
                                             git_checker=lambda _: (True, ""),
                                             head_reader=_raising_head_reader)
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertTrue(res["reason"].startswith("rev_parse_head_failed"))

    def test_worktree_git_file_with_valid_git_commands_is_allowed(self):
        with tempfile.TemporaryDirectory() as worktree_parent:
            worktree_dir = os.path.join(os.path.realpath(worktree_parent), "wt")
            _git("worktree", "add", "-q", worktree_dir, "-b", "autopilot-test-branch", cwd=self.repo_dir)
            try:
                self.assertTrue(os.path.isfile(os.path.join(worktree_dir, ".git")))
                task = sample_task("p1", "t1")
                task["working_directory"] = worktree_dir
                res = evaluate_repository_transition(self.store, self.project, task)
                self.assertTrue(res["clean"])
                self.assertEqual(STATE_ELIGIBLE, res["state"])
            finally:
                _git("worktree", "remove", "-f", worktree_dir, cwd=self.repo_dir)

    def test_dirty_tree_blocks(self):
        with open(os.path.join(self.repo_dir, "file.txt"), "a") as fh:
            fh.write("uncommitted change\n")
        try:
            task = sample_task("p1", "t1")
            task["working_directory"] = self.repo_dir
            res = evaluate_repository_transition(self.store, self.project, task)
            self.assertFalse(res["clean"])
            self.assertEqual(STATE_BLOCKED, res["state"])
            self.assertEqual("working_tree_dirty", res["reason"])
        finally:
            _git("checkout", "-q", "--", "file.txt", cwd=self.repo_dir)

    def test_wrong_baseline_blocks(self):
        task = sample_task("p1", "t1")
        task["working_directory"] = self.repo_dir
        task["baseline_head"] = "0" * 40
        res = evaluate_repository_transition(self.store, self.project, task)
        self.assertFalse(res["clean"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertTrue(res["reason"].startswith("baseline_head_mismatch"))

    def test_correct_clean_repo_with_matching_baseline_passes(self):
        task = sample_task("p1", "t1")
        task["working_directory"] = self.repo_dir
        task["baseline_head"] = self.head
        res = evaluate_repository_transition(self.store, self.project, task)
        self.assertTrue(res["clean"])
        self.assertEqual(STATE_ELIGIBLE, res["state"])


class DependencyActiveExecutionAuthorityTests(unittest.TestCase):
    """Codex P0-2: when a dependency task names an explicit
    source_context.active_execution_id, that execution is authoritative and
    must never be silently replaced by an older historical completed
    execution for the same task."""

    def setUp(self):
        self.store = MemoryStore()

    def _dep_task_with_active(self, active_execution_id):
        dep_task = sample_task("p1", "dep-1", status="completed")
        dep_task["source_context"] = {"active_execution_id": active_execution_id}
        self.store.put("tasks", "p1", "dep-1", dep_task)
        return dep_task

    def test_old_completed_execution_does_not_override_active_failed_execution(self):
        self._dep_task_with_active("exec-active")
        old_completed = sample_execution("p1", "dep-1", "exec-old", status="completed")
        old_completed["completed_at"] = "2026-08-19T05:00:00Z"
        self.store.put("executions", "p1", "exec-old", old_completed)
        active_failed = sample_execution("p1", "dep-1", "exec-active", status="failed")
        self.store.put("executions", "p1", "exec-active", active_failed)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("dependency_active_execution_not_completed:dep-1", res["reason"])

    def test_active_execution_completed_but_persistence_incomplete_blocks(self):
        self._dep_task_with_active("exec-active")
        active = sample_execution("p1", "dep-1", "exec-active", status="completed", persistence="incomplete")
        self.store.put("executions", "p1", "exec-active", active)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("dependency_active_execution_persistence_incomplete:dep-1", res["reason"])

    def test_active_execution_claim_unreleased_blocks(self):
        self._dep_task_with_active("exec-active")
        active = sample_execution("p1", "dep-1", "exec-active", status="completed", task_claim_release="retained")
        self.store.put("executions", "p1", "exec-active", active)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("dependency_task_claim_not_released:dep-1", res["reason"])

    def test_active_execution_task_mismatch_blocks(self):
        self._dep_task_with_active("exec-active")
        # exec-active actually belongs to a DIFFERENT task -- must not be
        # accepted as authoritative evidence for dep-1.
        mismatched = sample_execution("p1", "other-task", "exec-active", status="completed")
        self.store.put("executions", "p1", "exec-active", mismatched)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("dependency_active_execution_identity_mismatch:dep-1", res["reason"])

    def test_active_execution_missing_blocks(self):
        self._dep_task_with_active("exec-active")
        # No execution record at all for the named active_execution_id.
        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("dependency_active_execution_missing:dep-1", res["reason"])

    def test_valid_exact_active_execution_passes(self):
        self._dep_task_with_active("exec-active")
        active = sample_execution("p1", "dep-1", "exec-active", status="completed")
        self.store.put("executions", "p1", "exec-active", active)

        target = sample_task("p1", "t2", depends_on=["dep-1"])
        res = evaluate_dependencies(self.store, "p1", target)
        self.assertTrue(res["satisfied"])
        self.assertEqual(STATE_ELIGIBLE, res["state"])
        self.assertEqual(["dep-1"], res["completed_dependencies"])


class QuotaCircuitBreakerFailClosedTests(unittest.TestCase):
    """Codex P0-3: missing/malformed/empty quota evidence must fail closed
    in the actual circuit-breaker caller path, not just when a helper is
    tested in isolation with hand-built input."""

    def test_quota_summary_none_fails_closed(self):
        start = datetime.now(timezone.utc)
        res = evaluate_circuit_breaker(start, continuation_count=0, quota_summary=None)
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("quota_missing", res["reason"])

    def test_quota_summary_malformed_fails_closed(self):
        start = datetime.now(timezone.utc)
        res = evaluate_circuit_breaker(start, continuation_count=0, quota_summary="not-a-dict")
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("quota_malformed", res["reason"])

    def test_quota_summary_empty_providers_fails_closed(self):
        start = datetime.now(timezone.utc)
        res = evaluate_circuit_breaker(start, continuation_count=0, quota_summary={"providers": []})
        self.assertFalse(res["allowed"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("quota_providers_empty", res["reason"])

    def test_step_autopilot_halts_when_no_quota_document_and_no_service(self):
        """The actual caller path in step_autopilot: no quota_document and no
        service means quota_doc resolves to None, which must reach the
        circuit breaker as None and be rejected -- not silently skipped."""
        store = MemoryStore()
        project = sample_project("p1", working_directory=os.getcwd())
        store.put("projects", "p1", "p1", project)
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        store.put("executions", "p1", "exec-1", exec1)

        res = step_autopilot(store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, quota_document=None)
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("quota_missing", res["reason"])

    def test_step_autopilot_halts_when_quota_read_raises(self):
        store = MemoryStore()
        project = sample_project("p1", working_directory=os.getcwd())
        store.put("projects", "p1", "p1", project)
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        store.put("executions", "p1", "exec-1", exec1)

        class RaisingService:
            pass

        import manager.autopilot as autopilot_module

        def _raise(**kwargs):
            raise RuntimeError("Drive unreachable")

        original = autopilot_module.read_drive_status
        autopilot_module.read_drive_status = _raise
        try:
            res = step_autopilot(store, RaisingService(), "p1", "exec-1", datetime.now(timezone.utc),
                                 continuation_count=0, quota_document=None)
        finally:
            autopilot_module.read_drive_status = original
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertTrue(res["reason"].startswith("quota_read_failed"))


class ContinuationDispatchRecoveryIntegrationTests(unittest.TestCase):
    """Codex P0-4 exercised end-to-end through step_autopilot: known
    pre-dispatch failures recover automatically on the next poll; ambiguous
    post-dispatch-computation failures do not."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

    def _run(self, claim_reg, dispatcher_patch=None):
        import manager.autopilot as autopilot_module
        original = autopilot_module.dispatcher_dispatch
        if dispatcher_patch is not None:
            autopilot_module.dispatcher_dispatch = dispatcher_patch
        try:
            return step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                                  continuation_count=0, bucket="test-bucket",
                                  continuation_registry_factory=lambda *_: claim_reg,
                                  dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                                  task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                                  quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        finally:
            autopilot_module.dispatcher_dispatch = original

    def test_prelaunch_dispatch_failure_is_retry_eligible_and_recovers_on_next_poll(self):
        claim_reg = MemoryClaimRegistryWithCAS()

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated dispatcher failure before Command creation")

        first = self._run(claim_reg, dispatcher_patch=_raise)
        self.assertEqual("halted", first["status"])
        self.assertEqual(STATE_RETRY_ELIGIBLE, first["state"])
        self.assertTrue(first["reason"].startswith("continuation_dispatch_prelaunch_failure"))

        # No Command was ever created by the failed attempt.
        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(0, len(commands))

        second = self._run(claim_reg)  # real dispatcher this time
        self.assertEqual("dispatched", second["status"])
        self.assertEqual("t2", second["next_task_id"])
        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(1, len(commands))

    def test_ambiguous_post_dispatch_failure_is_attention_required_and_never_auto_retried(self):
        claim_reg = MemoryClaimRegistryWithCAS()

        original_put = self.store.put
        calls = {"n": 0}

        def _put_that_fails_for_commands(area, project_id, name, document):
            if area == "commands":
                calls["n"] += 1
                raise RuntimeError("simulated store timeout writing Command")
            return original_put(area, project_id, name, document)

        self.store.put = _put_that_fails_for_commands
        try:
            first = self._run(claim_reg)
        finally:
            self.store.put = original_put

        self.assertEqual("halted", first["status"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, first["state"])
        self.assertTrue(first["reason"].startswith("continuation_dispatch_evidence_write_failed"))
        self.assertEqual(1, calls["n"])

        # A second poll must NOT automatically retry an ATTENTION_REQUIRED
        # claim -- it must stay halted/unresolved, never silently dispatch.
        second = self._run(claim_reg)
        self.assertEqual("halted", second["status"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, second["state"])
        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(0, len(commands))


class RestartSafeContinuationDepthTests(unittest.TestCase):
    """Codex P0-5: a restarted caller passing continuation_count=0 must not
    be able to escape MAX_AUTOMATIC_CONTINUATION_DEPTH=1 when durable
    evidence on the predecessor task proves it was itself already created by
    a prior automatic continuation."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()

    def test_restart_with_continuation_count_reset_to_zero_is_still_blocked(self):
        # child-1 was itself created by a prior automatic continuation
        # (origin=direct_dispatch_ingress, continuation_count=1), and its
        # own execution has now completed -- simulating exactly the scenario
        # where a fresh/restarted process tries to treat it as a normal
        # continuation source with continuation_count reset to 0.
        child_task = sample_task("p1", "child-1", status="completed", read_only=True, needs_repo_edit=False)
        child_task["source_context"] = {
            "origin": "direct_dispatch_ingress",
            "admission_version": "v1",
            "source_execution_id": "exec-root",
            "source_task_id": "root-task",
            "continuation_count": 1,
            "decided_at": "2026-08-19T09:00:00Z",
        }
        self.store.put("tasks", "p1", "child-1", child_task)
        child_exec = sample_execution("p1", "child-1", "exec-child-1", status="completed")
        self.store.put("executions", "p1", "exec-child-1", child_exec)

        # A further candidate task exists and would otherwise be perfectly
        # eligible -- proving the halt comes from the depth guard, not from
        # having nothing left to do.
        grandchild = sample_task("p1", "grandchild-1", title="Grandchild", status="ready",
                                 depends_on=["child-1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "grandchild-1", grandchild)

        res = step_autopilot(self.store, None, "p1", "exec-child-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_DONE, res["state"])
        self.assertIn("max_continuation_steps_reached", res["reason"])

    def test_non_autopilot_task_with_incidental_continuation_count_field_is_not_treated_as_durable_depth(self):
        """A task whose source_context happens to carry a `continuation_count`
        key but was NOT created via the trusted-ingress autopilot origin must
        not be misread as durable depth evidence."""
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        task1["source_context"] = {"origin": "manual_entry", "continuation_count": 1}
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

        res = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", res["status"])


class CandidateEligibilityBeforeRankingTests(unittest.TestCase):
    """Codex P1-1 (exclude already-active candidates) and P1-3 (evaluate
    eligibility before ranking, so one ineligible high-priority candidate
    cannot starve a lower-priority eligible one or halt Autopilot outright)."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())

    def test_already_running_urgent_task_excluded_lower_priority_eligible_selected(self):
        urgent = sample_task("p1", "urgent-1", title="Urgent", status="ready")
        urgent["priority"] = "urgent"
        self.store.put("tasks", "p1", "urgent-1", urgent)
        running_exec = sample_execution("p1", "urgent-1", "exec-running", status="running")
        self.store.put("executions", "p1", "exec-running", running_exec)

        normal = sample_task("p1", "normal-1", title="Normal", status="ready")
        normal["priority"] = "normal"
        self.store.put("tasks", "p1", "normal-1", normal)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertIsNotNone(selection["task"])
        self.assertEqual("normal-1", selection["task"]["task_id"])
        self.assertTrue(any(b["task_id"] == "urgent-1" for b in selection["blocked"]))

    def test_queued_command_excludes_task_from_candidacy(self):
        task = sample_task("p1", "t1", title="Task", status="ready")
        self.store.put("tasks", "p1", "t1", task)
        cmd = {"command_id": "cmd-1", "project_id": "p1", "task_id": "t1", "status": "queued"}
        self.store.put("commands", "p1", "cmd-1", cmd)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertIsNone(selection["task"])
        self.assertEqual(1, len(selection["blocked"]))
        self.assertIn("already_active", selection["blocked"][0]["reason"])

    def test_urgent_eligible_beats_normal_eligible(self):
        urgent = sample_task("p1", "urgent-1", title="Urgent", status="ready")
        urgent["priority"] = "urgent"
        self.store.put("tasks", "p1", "urgent-1", urgent)
        normal = sample_task("p1", "normal-1", title="Normal", status="ready")
        normal["priority"] = "normal"
        self.store.put("tasks", "p1", "normal-1", normal)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertEqual("urgent-1", selection["task"]["task_id"])

    def test_urgent_dependency_blocked_normal_eligible_selected(self):
        urgent = sample_task("p1", "urgent-1", title="Urgent", status="ready", depends_on=["missing-dep"])
        urgent["priority"] = "urgent"
        self.store.put("tasks", "p1", "urgent-1", urgent)
        normal = sample_task("p1", "normal-1", title="Normal", status="ready")
        normal["priority"] = "normal"
        self.store.put("tasks", "p1", "normal-1", normal)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertEqual("normal-1", selection["task"]["task_id"])
        blocked_ids = [b["task_id"] for b in selection["blocked"]]
        self.assertIn("urgent-1", blocked_ids)

    def test_all_candidates_blocked_returns_none_with_evidence(self):
        urgent = sample_task("p1", "urgent-1", title="Urgent", status="ready", depends_on=["missing-dep"])
        self.store.put("tasks", "p1", "urgent-1", urgent)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertIsNone(selection["task"])
        self.assertEqual(1, len(selection["blocked"]))
        self.assertEqual("urgent-1", selection["blocked"][0]["task_id"])

    def test_step_autopilot_all_blocked_reports_blocked_not_done(self):
        """P1-3 correctness: a backlog that still has (ineligible) work left
        must never be reported as STATE_DONE/all_tasks_completed -- that
        would misrepresent a stalled/blocked backlog as finished."""
        store = MemoryStore()
        project = sample_project("p1", working_directory=os.getcwd())
        store.put("projects", "p1", "p1", project)
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        store.put("executions", "p1", "exec-1", exec1)
        # t2 depends on a task that will never complete.
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["never-completes"])
        store.put("tasks", "p1", "t2", task2)

        res = step_autopilot(store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=sample_quota_document(), git_checker=lambda _: (True, ""))
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("no_eligible_candidate", res["reason"])
        self.assertNotEqual(STATE_DONE, res["state"])
        self.assertNotEqual("done", res["status"])


class PredecessorLifecycleAgreementTests(unittest.TestCase):
    """Codex P1-2: a completed Execution is insufficient by itself -- the
    source Task's own lifecycle must independently agree."""

    def setUp(self):
        self.store = MemoryStore()

    def test_execution_completed_but_task_still_ready_halts(self):
        task = sample_task("p1", "t1", status="ready", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="read_only")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertIn("source_task_execution_lifecycle_mismatch:ready", res["reason"])

    def test_execution_completed_but_task_in_progress_halts(self):
        task = sample_task("p1", "t1", status="in_progress", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="read_only")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])

    def test_task_active_execution_id_naming_a_different_execution_halts(self):
        task = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        task["source_context"] = {"active_execution_id": "exec-other"}
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="read_only")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertEqual("source_task_active_execution_id_mismatch", res["reason"])

    def test_task_active_execution_id_matching_this_execution_passes(self):
        task = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        task["source_context"] = {"active_execution_id": "exec-1"}
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="completed", access="read_only")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertTrue(res["passed"])
        self.assertEqual(STATE_NEXT_READY, res["state"])


class RetryInvariantRegressionTests(unittest.TestCase):
    """Preserve existing conservative retry behavior: Autopilot itself never
    performs an automatic retry -- it only ever reports RETRY_ELIGIBLE /
    BLOCKED evidence for a failed or interrupted predecessor, and never
    advances to dispatching a next continuation off of one."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()

    def test_failed_predecessor_execution_never_reaches_dispatched(self):
        task = sample_task("p1", "t1", status="blocked")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="failed")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_RETRY_ELIGIBLE, res["state"])
        self.assertNotEqual("dispatched", res["status"])

    def test_interrupted_predecessor_execution_never_reaches_dispatched(self):
        task = sample_task("p1", "t1", status="blocked")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="interrupted")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_RETRY_ELIGIBLE, res["state"])
        self.assertEqual("source_execution_interrupted", res["reason"])

    def test_repeatedly_failed_predecessor_past_retry_budget_is_blocked_not_retried(self):
        task = sample_task("p1", "t1", status="blocked")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="failed")
        exec_record["retry_count"] = 2
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertEqual(STATE_BLOCKED, res["state"])

    def test_cancelled_predecessor_is_never_retried_or_dispatched(self):
        # sample_execution()'s fixture shape is only schema-valid for the
        # statuses the existing test suite already exercises (completed /
        # failed / interrupted); a "cancelled" execution has its own
        # required-field branch in execution.schema.json, so this
        # necessarily fails schema validation first. That is itself a valid
        # fail-closed outcome (ATTENTION_REQUIRED, never dispatched) and is
        # what this regression test actually protects: cancelled predecessors
        # are never advanced past, one way or the other.
        task = sample_task("p1", "t1", status="cancelled")
        self.store.put("tasks", "p1", "t1", task)
        exec_record = sample_execution("p1", "t1", "exec-1", status="cancelled")
        self.store.put("executions", "p1", "exec-1", exec_record)

        res = verify_predecessor_barrier(self.store, "p1", "exec-1")
        self.assertFalse(res["passed"])
        self.assertIn(res["state"], (STATE_BLOCKED, STATE_ATTENTION_REQUIRED))


class TrustedWatcherAdmissionIntegrationTests(unittest.TestCase):
    """AG AC-INGRESS-01: an Autopilot-generated Command must actually pass
    manager.command_watcher's real trusted-ingress admission path -- proven
    here by calling manager.trusted_ingress.verify_trusted_ingress_admission
    (the exact function command_watcher.process_command calls) against the
    Task/Command that step_autopilot produced, without modifying
    command_watcher.py or trusted_ingress.py at all."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)
        self.dispatch_reg = MemoryClaimRegistry()

    def _dispatch(self):
        return step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                              continuation_count=0, bucket="test-bucket",
                              continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                              dispatch_request_registry_factory=lambda *_: self.dispatch_reg,
                              task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                              quota_document=self.quota_doc, git_checker=lambda _: (True, ""))

    def test_valid_autopilot_continuation_passes_watcher_trusted_admission(self):
        from manager.trusted_ingress import verify_trusted_ingress_admission

        res = self._dispatch()
        self.assertEqual("dispatched", res["status"])
        command = self.store.get("commands", "p1", res["next_command_id"])

        admitted_task = verify_trusted_ingress_admission(
            self.store, command, "test-bucket", registry_factory=lambda *_: self.dispatch_reg)
        self.assertIsNotNone(admitted_task)
        self.assertEqual("t2", admitted_task["task_id"])

    def test_missing_trusted_evidence_is_rejected(self):
        """A Command that never went through step_autopilot's Task-stamping
        (e.g. hand-crafted, or written before this fix) carries no
        source_context evidence and must be rejected, not accidentally
        admitted."""
        from manager.trusted_ingress import verify_trusted_ingress_admission

        task2 = self.store.get("tasks", "p1", "t2")  # untouched: source_context == {}
        command = {
            "command_id": "cmd-bare", "project_id": "p1", "task_id": "t2",
            "provider": "codex", "model": None, "fallback_model": None, "mode": "standard",
            "effort": "medium", "selection_reason": [], "quota_evidence": None,
            "created_at": now_iso_for_tests(), "status": "queued", "execution_id": None,
            "claimed_at": None, "completed_at": None, "result": None,
            "created_via": "direct_dispatch_ingress", "admission_version": "v1",
            "request_id": "ap-bare",
        }
        self.store.put("commands", "p1", "cmd-bare", command)

        admitted_task = verify_trusted_ingress_admission(
            self.store, command, "test-bucket", registry_factory=lambda *_: self.dispatch_reg)
        self.assertIsNone(admitted_task)

    def test_forged_autopilot_origin_alone_is_rejected(self):
        """An origin string on the Task alone, with no corroborating
        dispatch-requests idempotency record, must not grant admission --
        proves the origin string alone never grants authority."""
        from manager.trusted_ingress import verify_trusted_ingress_admission

        task2 = self.store.get("tasks", "p1", "t2")
        task2["source_context"] = {
            "origin": "direct_dispatch_ingress", "admission_version": "v1",
            "external_request_id": "ap-forged", "read_only": True,
        }
        task2["read_only"] = True
        task2["execution_policies"] = ["disposable", "no_external_writes", "no_repo_writes", "read_only"]
        self.store.put("tasks", "p1", "t2", task2)
        command = {
            "command_id": "cmd-forged", "project_id": "p1", "task_id": "t2",
            "provider": "codex", "model": None, "fallback_model": None, "mode": "standard",
            "effort": "medium", "selection_reason": [], "quota_evidence": None,
            "created_at": now_iso_for_tests(), "status": "queued", "execution_id": None,
            "claimed_at": None, "completed_at": None, "result": None,
            "created_via": "direct_dispatch_ingress", "admission_version": "v1",
            "request_id": "ap-forged",
        }
        self.store.put("commands", "p1", "cmd-forged", command)

        # No dispatch-requests record exists for "ap-forged" -- the fake
        # registry is empty.
        admitted_task = verify_trusted_ingress_admission(
            self.store, command, "test-bucket", registry_factory=lambda *_: self.dispatch_reg)
        self.assertIsNone(admitted_task)

    def test_mismatched_identity_is_rejected(self):
        from manager.trusted_ingress import verify_trusted_ingress_admission

        res = self._dispatch()
        command = self.store.get("commands", "p1", res["next_command_id"])
        tampered = dict(command)
        tampered["task_id"] = "some-other-task-id"

        admitted_task = verify_trusted_ingress_admission(
            self.store, tampered, "test-bucket", registry_factory=lambda *_: self.dispatch_reg)
        self.assertIsNone(admitted_task)

    def test_duplicate_trusted_request_remains_idempotent(self):
        from manager.dispatch_requests import claim_dispatch_request

        first = claim_dispatch_request(self.dispatch_reg, "p1", "ap-x-y", "t2", "cmd-1", now_iso_for_tests())
        second = claim_dispatch_request(self.dispatch_reg, "p1", "ap-x-y", "t2", "cmd-1", now_iso_for_tests())
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertEqual(first["task_id"], second["task_id"])

    def test_existing_direct_dispatch_behavior_unchanged(self):
        """A genuine Direct Dispatch (not Autopilot) Task/Command, stamped
        exactly as cloud.dispatch_ingress would, must still be admitted --
        proves the Autopilot integration didn't alter or narrow the existing
        trusted-ingress contract."""
        from manager.trusted_ingress import verify_trusted_ingress_admission
        from manager.dispatch_requests import claim_dispatch_request

        dd_task = sample_task("p1", "dd-task", status="ready", read_only=True, needs_repo_edit=False)
        dd_task["source_context"] = {
            "origin": "direct_dispatch_ingress", "admission_version": "v1",
            "external_request_id": "dd-req-1",
        }
        self.store.put("tasks", "p1", "dd-task", dd_task)
        dd_command = {
            "command_id": "dd-cmd-1", "project_id": "p1", "task_id": "dd-task",
            "provider": "codex", "model": None, "fallback_model": None, "mode": "standard",
            "effort": "medium", "selection_reason": [], "quota_evidence": None,
            "created_at": now_iso_for_tests(), "status": "queued", "execution_id": None,
            "claimed_at": None, "completed_at": None, "result": None,
            "created_via": "direct_dispatch_ingress", "admission_version": "v1",
            "request_id": "dd-req-1",
        }
        self.store.put("commands", "p1", "dd-cmd-1", dd_command)
        claim_dispatch_request(self.dispatch_reg, "p1", "dd-req-1", "dd-task", "dd-cmd-1", now_iso_for_tests())

        admitted_task = verify_trusted_ingress_admission(
            self.store, dd_command, "test-bucket", registry_factory=lambda *_: self.dispatch_reg)
        self.assertIsNotNone(admitted_task)
        self.assertEqual("dd-task", admitted_task["task_id"])


class DeterministicCommandIdentityTests(unittest.TestCase):
    """AG AC-CAS-01: Command/request identity must be a deterministic
    function of stable chain inputs (project_id, source_execution_id,
    next_task_id), never wall-clock time -- the same continuation observed
    after a restart must resolve to the same command_id."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

    def test_same_source_execution_and_next_task_yields_same_command_id_after_restart(self):
        claim_reg = MemoryClaimRegistryWithCAS()
        dispatch_reg = MemoryClaimRegistry()

        first = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                               continuation_count=0, bucket="test-bucket",
                               continuation_registry_factory=lambda *_: claim_reg,
                               dispatch_request_registry_factory=lambda *_: dispatch_reg,
                               task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                               quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", first["status"])
        first_command_id = first["next_command_id"]

        # Advance the Command past P1-1's non-terminal exclusion window (as
        # command_watcher would once the provider run finishes) so this
        # second poll actually reaches the CAS claim / identity-recovery
        # path being tested here, rather than being short-circuited earlier
        # by "already active" candidate exclusion (a different, already
        # separately-tested safety path).
        cmd = self.store.get("commands", "p1", first_command_id)
        cmd["status"] = "completed"
        self.store.put("commands", "p1", first_command_id, cmd)

        # "Restart": brand-new claim registry instances would be a crash
        # scenario, not a normal replay -- what matters here is that
        # nothing about the identity computation depends on wall-clock
        # time, so re-running with the SAME (still-persisted) claim
        # registry after time has passed resolves to the identical id.
        second = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc) + timedelta(hours=3),
                                continuation_count=0, bucket="test-bucket",
                                continuation_registry_factory=lambda *_: claim_reg,
                                dispatch_request_registry_factory=lambda *_: dispatch_reg,
                                task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                                quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("already_claimed", second["status"])
        self.assertEqual(first_command_id, second["existing_command_id"])

    def test_command_id_does_not_embed_a_date_string(self):
        claim_reg = MemoryClaimRegistryWithCAS()
        dispatch_reg = MemoryClaimRegistry()
        res = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: claim_reg,
                             dispatch_request_registry_factory=lambda *_: dispatch_reg,
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertNotIn(today, res["next_command_id"])
        self.assertEqual("autopilot-exec-1-t2", res["next_command_id"])


class CasNotProofOfDispatchRecoveryTests(unittest.TestCase):
    """AG AC-CAS-01 refinement: an existing CAS claim record in DISPATCHED
    state is not, by itself, proof that a Command was actually created --
    durable Command evidence must be inspected before trusting it."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

    def test_dispatched_claim_with_matching_command_is_idempotent(self):
        claim_reg = MemoryClaimRegistryWithCAS()
        dispatch_reg = MemoryClaimRegistry()
        first = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                               continuation_count=0, bucket="test-bucket",
                               continuation_registry_factory=lambda *_: claim_reg,
                               dispatch_request_registry_factory=lambda *_: dispatch_reg,
                               task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                               quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", first["status"])

        cmd = self.store.get("commands", "p1", first["next_command_id"])
        cmd["status"] = "completed"  # out of P1-1's non-terminal exclusion set
        self.store.put("commands", "p1", first["next_command_id"], cmd)

        second = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                                continuation_count=0, bucket="test-bucket",
                                continuation_registry_factory=lambda *_: claim_reg,
                                dispatch_request_registry_factory=lambda *_: dispatch_reg,
                                task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                                quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("already_claimed", second["status"])
        self.assertEqual(STATE_DISPATCHED, second["state"])

    def test_dispatched_claim_with_missing_command_is_attention_required_not_dispatched(self):
        """Simulates a claim record that reached DISPATCHED (e.g. via a
        direct/manual write, or a Command later deleted) without a
        corresponding Command actually existing in the store."""
        from manager.autopilot_continuations import claim_autopilot_continuation, mark_continuation_dispatched, mark_continuation_dispatching

        claim_reg = MemoryClaimRegistryWithCAS()
        claim = claim_autopilot_continuation(claim_reg, "p1", "exec-1", "t1", "t2", "autopilot-exec-1-t2", 1, now_iso_for_tests())
        dispatching = mark_continuation_dispatching(claim_reg, claim)
        mark_continuation_dispatched(claim_reg, dispatching)
        # Deliberately no Command written to self.store for "autopilot-exec-1-t2".

        res = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: claim_reg,
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_ATTENTION_REQUIRED, res["state"])
        self.assertEqual("continuation_claimed_dispatched_but_command_missing", res["reason"])
        self.assertNotEqual("dispatched", res["status"])
        self.assertNotEqual("already_claimed", res["status"])


class DurableLineageFieldsTests(unittest.TestCase):
    """AG AC-CHAIN-01: durable lineage (root_execution_id,
    parent_execution_id, continuation_depth, autopilot_session_id) must be
    persisted onto the dispatched Task's source_context, not just passed as
    in-memory caller arguments, so it survives a Task/Execution reload."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()

    def test_lineage_fields_persisted_on_dispatched_task(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t2", task2)

        res = step_autopilot(self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
                             dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", res["status"])

        # Simulate a fresh process reloading the Task fresh from the store
        # (not reusing anything held in memory from the dispatch above).
        reloaded_task = self.store.get("tasks", "p1", "t2")
        sc = reloaded_task["source_context"]
        self.assertEqual("exec-1", sc["root_execution_id"])
        self.assertEqual("exec-1", sc["parent_execution_id"])
        self.assertEqual(1, sc["continuation_depth"])
        self.assertEqual("p1:exec-1", sc["autopilot_session_id"])
        self.assertEqual("exec-1", sc["source_execution_id"])

    def test_root_execution_id_threads_through_a_second_hop_unchanged(self):
        """Even though Slice 1 blocks a second automatic continuation
        (MAX_AUTOMATIC_CONTINUATION_DEPTH=1), the root-threading logic
        itself must correctly preserve the ORIGINAL root rather than
        re-rooting at each hop, so a future multi-hop slice can rely on it."""
        root_task = sample_task("p1", "root-1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "root-1", root_task)
        root_exec = sample_execution("p1", "root-1", "exec-root", status="completed")
        self.store.put("executions", "p1", "exec-root", root_exec)

        child_task = sample_task("p1", "child-1", status="ready", depends_on=["root-1"], read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "child-1", child_task)

        claim_reg = MemoryClaimRegistryWithCAS()
        dispatch_reg = MemoryClaimRegistry()
        res = step_autopilot(self.store, None, "p1", "exec-root", datetime.now(timezone.utc),
                             continuation_count=0, bucket="test-bucket",
                             continuation_registry_factory=lambda *_: claim_reg,
                             dispatch_request_registry_factory=lambda *_: dispatch_reg,
                             task_claim_registry_factory=lambda *_: MemoryClaimRegistry(),
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", res["status"])
        child_sc = self.store.get("tasks", "p1", "child-1")["source_context"]
        self.assertEqual("exec-root", child_sc["root_execution_id"])
        self.assertEqual("p1:exec-root", child_sc["autopilot_session_id"])


class SelfDependencyTests(unittest.TestCase):
    """AG AC-DEP-02 (P2): a task listing its own task_id in depends_on must
    fail closed with zero dispatch."""

    def setUp(self):
        self.store = MemoryStore()

    def test_self_dependency_blocks(self):
        task = sample_task("p1", "t1", depends_on=["t1"])
        self.store.put("tasks", "p1", "t1", task)
        res = evaluate_dependencies(self.store, "p1", task)
        self.assertFalse(res["satisfied"])
        self.assertEqual(STATE_BLOCKED, res["state"])
        self.assertEqual("self_dependency:t1", res["reason"])


class OptInTaskClaimExclusionTests(unittest.TestCase):
    """AG AC-SELECT-01 refinement: candidate selection can additionally
    exclude tasks covered by an active manager.task_claims lease, when the
    caller opts in with a task_claim_reader (never auto-activated merely
    because a bucket is configured -- see build_default_task_claim_reader's
    docstring in manager/autopilot.py for why)."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())

    def test_task_with_active_claim_is_excluded_when_reader_provided(self):
        task = sample_task("p1", "t1", title="Task", status="ready")
        self.store.put("tasks", "p1", "t1", task)

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""),
                                             task_claim_reader=lambda task_id: task_id == "t1")
        self.assertIsNone(selection["task"])
        self.assertIn("active_task_claim", selection["blocked"][0]["reason"])

    def test_task_claim_reader_error_excludes_candidate_fail_closed(self):
        task = sample_task("p1", "t1", title="Task", status="ready")
        self.store.put("tasks", "p1", "t1", task)

        def _raising_reader(task_id):
            raise RuntimeError("simulated backend unavailable")

        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""),
                                             task_claim_reader=_raising_reader)
        self.assertIsNone(selection["task"])

    def test_no_task_claim_reader_does_not_exclude_anything(self):
        task = sample_task("p1", "t1", title="Task", status="ready")
        self.store.put("tasks", "p1", "t1", task)
        selection = find_next_candidate_task(self.store, "p1", set(), project=self.project,
                                             git_checker=lambda _: (True, ""))
        self.assertIsNotNone(selection["task"])


_UNSET = object()


class DefaultPathTaskClaimWiringTests(unittest.TestCase):
    """Codex P1-1 wiring fix: task-claim exclusion must be ON BY DEFAULT in
    the real/default step_autopilot() invocation path -- bucket supplied,
    task_claim_reader NOT explicitly passed -- whenever real backend
    authority (bucket) is available, not only when a caller opts in with its
    own reader. Uses `task_claim_registry_factory` (a narrow backend seam,
    mirroring continuation_registry_factory / dispatch_request_registry_factory)
    to inject an in-memory double instead of touching real GCS -- production
    code never special-cases a bucket name/string."""

    def setUp(self):
        self.store = MemoryStore()
        self.project = sample_project("p1", working_directory=os.getcwd())
        self.store.put("projects", "p1", "p1", self.project)
        self.quota_doc = sample_quota_document()
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

    def _claimed_registry(self, task_id):
        reg = MemoryClaimRegistry()
        reg.document = {
            "schema_version": "0.1.0", "project_id": "p1", "task_id": task_id,
            "execution_id": "exec-other-owner", "provider": "codex",
            "claimed_at": now_iso_for_tests(),
        }
        reg.generation = 1
        return reg

    def _run(self, task_claim_registry_factory=None, task_claim_reader=_UNSET):
        kwargs = {}
        if task_claim_reader is not _UNSET:
            kwargs["task_claim_reader"] = task_claim_reader
        return step_autopilot(
            self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
            continuation_count=0, bucket="test-bucket",
            continuation_registry_factory=lambda *_: MemoryClaimRegistryWithCAS(),
            dispatch_request_registry_factory=lambda *_: MemoryClaimRegistry(),
            task_claim_registry_factory=task_claim_registry_factory or (lambda *_: MemoryClaimRegistry()),
            quota_document=self.quota_doc, git_checker=lambda _: (True, ""),
            **kwargs,
        )

    def test_default_real_path_skips_claimed_urgent_selects_lower_priority_eligible(self):
        urgent = sample_task("p1", "urgent-1", title="Urgent", status="ready", depends_on=["t1"])
        urgent["priority"] = "urgent"
        self.store.put("tasks", "p1", "urgent-1", urgent)
        normal = sample_task("p1", "normal-1", title="Normal", status="ready", depends_on=["t1"])
        normal["priority"] = "normal"
        self.store.put("tasks", "p1", "normal-1", normal)

        claimed = {"urgent-1": self._claimed_registry("urgent-1")}

        def registry_factory(bucket, project_id, task_id, session=None):
            return claimed.get(task_id, MemoryClaimRegistry())

        # task_claim_reader intentionally NOT passed -- this is the
        # default/real invocation path Codex flagged as unsafe.
        res = self._run(task_claim_registry_factory=registry_factory)
        self.assertEqual("dispatched", res["status"])
        self.assertEqual("normal-1", res["next_task_id"])

    def test_default_real_path_with_no_active_claim_proceeds(self):
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"])
        self.store.put("tasks", "p1", "t2", task2)

        res = self._run()  # empty registry everywhere -> no claim anywhere
        self.assertEqual("dispatched", res["status"])
        self.assertEqual("t2", res["next_task_id"])

    def test_default_real_path_backend_error_fails_closed_zero_dispatch(self):
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"])
        self.store.put("tasks", "p1", "t2", task2)

        def _raising_factory(bucket, project_id, task_id, session=None):
            raise RuntimeError("simulated GCS outage")

        res = self._run(task_claim_registry_factory=_raising_factory)
        self.assertEqual("halted", res["status"])
        self.assertNotEqual("dispatched", res["status"])
        commands = [v for (area, project_id, _), v in self.store.data.items()
                   if area == "commands" and project_id == "p1"]
        self.assertEqual(0, len(commands), "backend failure must never result in an unsafe dispatch")

    def test_explicit_task_claim_reader_still_overrides_default_wiring(self):
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"])
        self.store.put("tasks", "p1", "t2", task2)

        # The default-constructed reader (via registry_factory) would report
        # t2 as claimed; an explicitly-injected reader takes precedence.
        res = self._run(
            task_claim_registry_factory=lambda *_: self._claimed_registry("t2"),
            task_claim_reader=lambda task_id: False,
        )
        self.assertEqual("dispatched", res["status"])
        self.assertEqual("t2", res["next_task_id"])

    def test_no_bucket_default_path_does_not_construct_a_reader(self):
        """Without a bucket there is no backend authority to verify claims
        against (the same precondition the CAS/dispatch-request checks
        already require) -- this must not be treated as a NEW gap, just the
        existing bucket-optional degraded mode."""
        task2 = sample_task("p1", "t2", title="Task 2", status="ready", depends_on=["t1"])
        self.store.put("tasks", "p1", "t2", task2)

        res = step_autopilot(
            self.store, None, "p1", "exec-1", datetime.now(timezone.utc),
            continuation_count=0, bucket=None,
            quota_document=self.quota_doc, git_checker=lambda _: (True, ""),
        )
        self.assertEqual("dispatched", res["status"])


def now_iso_for_tests():
    from manager.tasks import now_iso
    return now_iso()


if __name__ == "__main__":
    unittest.main()
