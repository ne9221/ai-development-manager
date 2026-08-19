"""Comprehensive unit, integration, and adversarial tests for ADM Minimal Autopilot
Slice 1 (manager/autopilot.py) addressing all 4 AG Preflight P0 findings."""

import os
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

        claim_reg = MemoryClaimRegistry()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(
            self.store, None, "p1", "exec-1", session_start,
            continuation_count=0, bucket="test-bucket",
            continuation_registry_factory=lambda *_: claim_reg,
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

        claim_reg = MemoryClaimRegistry()
        session_start = datetime.now(timezone.utc)

        # First run
        first = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                               bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
                               quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("dispatched", first["status"])

        # Second duplicate run
        second = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                                bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
                                quota_document=self.quota_doc, git_checker=lambda _: (True, ""))
        self.assertEqual("already_claimed", second["status"])
        self.assertEqual(STATE_DISPATCHED, second["state"])
        self.assertEqual("t2", second["existing_claim"]["next_task_id"])

    def test_write_capable_candidate_next_task_halts_at_review_required(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        # Task 2 is write-capable (needs_repo_edit=True)
        task2 = sample_task("p1", "t2", title="Task 2 Write Code", status="ready",
                            depends_on=["t1"], read_only=False, needs_repo_edit=True)
        self.store.put("tasks", "p1", "t2", task2)

        claim_reg = MemoryClaimRegistry()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                             bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))

        self.assertEqual("halted", res["status"])
        self.assertEqual(STATE_REVIEW_REQUIRED, res["state"])
        self.assertIn("slice1_write_task_requires_slice2_activation", res["reason"])

    def test_all_tasks_completed_terminates_with_done(self):
        task1 = sample_task("p1", "t1", status="completed", read_only=True, needs_repo_edit=False)
        self.store.put("tasks", "p1", "t1", task1)
        exec1 = sample_execution("p1", "t1", "exec-1", status="completed")
        self.store.put("executions", "p1", "exec-1", exec1)

        claim_reg = MemoryClaimRegistry()
        session_start = datetime.now(timezone.utc)

        res = step_autopilot(self.store, None, "p1", "exec-1", session_start, continuation_count=0,
                             bucket="test-bucket", continuation_registry_factory=lambda *_: claim_reg,
                             quota_document=self.quota_doc, git_checker=lambda _: (True, ""))

        self.assertEqual("done", res["status"])
        self.assertEqual(STATE_DONE, res["state"])
        self.assertEqual("all_tasks_completed", res["reason"])


if __name__ == "__main__":
    unittest.main()
