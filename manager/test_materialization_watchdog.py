"""Permanent materialization failure watchdog tests.

Covers the durable, bounded-retry escalation on manager.task_root's
materialization facet (task_root.record_materialization_failure /
DEFAULT_MATERIALIZATION_ATTEMPTS) and its integration into
manager.execution_lifecycle._attention_note /
retry_incomplete_terminal_persistence:

  A. Below threshold: repeated failures stay "pending" with an
     incrementing durable retry_count; runtime claim authority is NOT
     released; Execution.cleanup_evidence.persistence never reaches
     "complete".
  B. At threshold: the view escalates to "attention" and runtime claim
     authority IS released in the same tick (authority_active=False,
     Task Root cleanup facet "released") -- never holding a running claim
     hostage to a permanently-broken Drive write.
  C. The Execution's own cleanup_evidence.task_claim_release converges to
     "released" in lockstep with the Task Root release (no split truth).
  D. The terminal bind itself is completely untouched by the escalation --
     the terminal winner stays immutable regardless of materialization
     failures.
  E. Execution.cleanup_evidence.persistence is never marked "complete"
     while stuck in attention, even after the claim has been released.
  F. Recovery: once the underlying failure clears, a later retry succeeds,
     resets retry_count to 0, reaches "verified", and completes
     persistence -- attention is not a dead end.
  G. Repair-after-release never needs to reacquire a running execution
     claim: the recovering retry call uses the SAME (already-released)
     claim_registry object with no fresh acquire_task_root() call, and
     task_root's own facet-advance functions only check execution_id
     ownership, never authority_active.
  H. A different, unrelated view failing repeatedly does not affect this
     view's own independent retry_count/threshold.
"""

import unittest
from unittest.mock import patch

from manager import task_root
from manager.execution_lifecycle import enter_running_gate, retry_incomplete_terminal_persistence, terminalize_execution
from manager.executions import reserve_execution
from manager.tasks import DriveRecords, TaskError, create_project, create_task
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_tasks import FakeDriveService


def _quota_document():
    return {"claude": {"freshness": "fresh"}, "codex": {"freshness": "fresh"}}


def _project():
    return {
        "project_id": "p1", "name": "Project", "repo": "github:owner/repo", "default_branch": "main",
        "runtime_ssot": "Drive", "project_rules": [], "active_tasks": ["t1"],
        "current_phase": "Phase 3C", "important_constraints": [],
    }


def _task():
    return {
        "task_id": "t1", "project_id": "p1", "title": "Watchdog", "task_type": "implementation",
        "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": False,
        "needs_research": False, "needs_browser": False, "parallelizable": False,
        "read_only": True, "scope": ["manager/test_materialization_watchdog.py"], "constraints": [],
        "acceptance_criteria": ["gate"], "working_directory": "unused",
        "branch": "refs/heads/main", "baseline_head": "a" * 40,
        "allowed_paths": ["manager/test_materialization_watchdog.py"], "execution_policies": [],
    }


class MaterializationWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.store = DriveRecords(FakeDriveService())
        create_project(self.store, _project())
        create_task(self.store, _task(), assign=False)
        self.claim = MemoryClaimRegistry()
        reserve_execution(self.store, "p1", "t1", "exec-a", "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=_quota_document()):
            self.gate = enter_running_gate(self.store, object(), None, "p1", "t1", "exec-a", "codex",
                                           "read_only", task_claim_registry=self.claim)

    def _seed_bind_with_broken_handoff(self):
        """Get to: terminal bind committed, materialization still "absent",
        runtime claim still active -- by forcing the FIRST terminalize_
        execution() attempt's Handoff write to fail. Mirrors
        test_execution_terminal.py's test_handoff_failure_preserves_outcome_
        and_retry_completes exactly, just against the fixed-ID-capable store."""
        with patch("manager.executions.read_drive_status", return_value=_quota_document()), \
             patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("handoff failed")):
            with self.assertRaises(TaskError):
                terminalize_execution(self.store, object(), None, self.claim, "p1", "t1", "exec-a", "codex",
                                      "completed", self.gate["task_claim"]["generation"], True,
                                      completed_at="2026-09-01T00:00:00Z", summary="done")
        self.assertIsNotNone(self.claim.document["terminal"], "bind must land before materialization is attempted")
        self.assertTrue(self.claim.document["authority_active"])

    def test_below_threshold_stays_pending_with_incrementing_retry_count_and_claim_held(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("still broken")):
            for expected_retry_count in range(1, task_root.DEFAULT_MATERIALIZATION_ATTEMPTS):
                result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
                self.assertFalse(result)
                view = self.claim.document["materialization"]["handoff"]
                self.assertEqual("pending", view["status"])
                self.assertEqual(expected_retry_count, view["retry_count"])
                self.assertTrue(self.claim.document["authority_active"], "claim must stay held below threshold")
                self.assertEqual("retained", self.claim.document["cleanup"]["status"])
        execution = self.store.get("executions", "p1", "exec-a")
        self.assertNotEqual("complete", execution["cleanup_evidence"]["persistence"])

    def test_at_threshold_escalates_to_attention_and_releases_claim(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("permanently broken")):
            for _ in range(task_root.DEFAULT_MATERIALIZATION_ATTEMPTS):
                retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)

        root_doc = self.claim.document
        self.assertEqual("attention", root_doc["materialization"]["handoff"]["status"])
        self.assertEqual(task_root.DEFAULT_MATERIALIZATION_ATTEMPTS, root_doc["materialization"]["handoff"]["retry_count"])
        self.assertFalse(root_doc["authority_active"], "runtime claim must be released once the bounded threshold is hit")
        self.assertEqual("released", root_doc["cleanup"]["status"])
        # D: the terminal bind itself is untouched by the escalation.
        self.assertEqual("exec-a", root_doc["terminal"]["execution_id"])
        self.assertEqual("completed", root_doc["terminal"]["terminal_status"])

    def test_execution_and_root_release_truth_converge_on_watchdog_escalation(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("permanently broken")):
            for _ in range(task_root.DEFAULT_MATERIALIZATION_ATTEMPTS):
                retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        execution = self.store.get("executions", "p1", "exec-a")
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertFalse(self.claim.document["authority_active"])
        self.assertNotEqual("complete", execution["cleanup_evidence"]["persistence"],
                            "persistence must never be marked complete while materialization is stuck in attention")

    def test_recovery_after_escalation_resets_retry_count_and_completes(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("permanently broken")):
            for _ in range(task_root.DEFAULT_MATERIALIZATION_ATTEMPTS):
                retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        self.assertEqual("attention", self.claim.document["materialization"]["handoff"]["status"])
        self.assertFalse(self.claim.document["authority_active"])

        # G: recovery reuses the SAME (already-released) claim_registry --
        # no fresh acquire_task_root()/reservation call is made here at all.
        result = retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        self.assertTrue(result)
        root_doc = self.claim.document
        self.assertEqual("verified", root_doc["materialization"]["handoff"]["status"])
        self.assertEqual(0, root_doc["materialization"]["handoff"]["retry_count"])
        self.assertEqual("verified", root_doc["materialization"]["task"]["status"])
        execution = self.store.get("executions", "p1", "exec-a")
        self.assertEqual("complete", execution["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], execution["cleanup_evidence"]["persisted"])

    def test_repair_after_release_does_not_reacquire_a_running_claim(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("permanently broken")):
            for _ in range(task_root.DEFAULT_MATERIALIZATION_ATTEMPTS):
                retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        self.assertFalse(self.claim.document["authority_active"])

        retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        # Repair must not flip a released Task Root back to "running"
        # authority -- it stays released; only materialization/terminal
        # truth advances.
        self.assertFalse(self.claim.document["authority_active"])
        self.assertEqual("released", self.claim.document["cleanup"]["status"])

    def test_unrelated_view_failure_does_not_share_retry_budget(self):
        self._seed_bind_with_broken_handoff()
        with patch("manager.execution_lifecycle.create_handoff", side_effect=TaskError("handoff only")):
            retry_incomplete_terminal_persistence(self.store, "p1", "t1", "exec-a", claim_registry=self.claim)
        root_doc = self.claim.document
        self.assertEqual(1, root_doc["materialization"]["handoff"]["retry_count"])
        self.assertEqual("absent", root_doc["materialization"]["task"]["status"])
        self.assertEqual(0, root_doc["materialization"]["task"].get("retry_count", 0))


if __name__ == "__main__":
    unittest.main()
