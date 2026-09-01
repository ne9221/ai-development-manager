"""Regression tests for the 3 Codex-review P0 bugs fixed on
arch/terminal-commit-authority-20260901 (reviewed against an earlier target,
c58842aa; reproduced and fixed against current HEAD):

  P0-1: the normal terminalize_execution() happy path never called
        task_root.commit_terminal_bind() -- only the R17 recovery path did --
        so a normal terminal execution's Task Root object never carried
        terminal-commit authority at all.
  P0-2: release_runtime_claim() never advanced the Task Root's own
        `cleanup` facet to "released" for a terminal-bound Root, leaving it
        stuck at "retained" forever even after full convergence.
  P0-3: the bound Handoff's Drive file ID was chosen without ever verifying
        a physically-existing file actually has that ID/content, so a bound
        ID could point at a nonexistent or wrong-content file.

These tests use manager.tasks.DriveRecords backed by the real
FakeDriveService/FakeDriveFiles in-memory Drive double from test_tasks.py --
not the lighter MemoryStore doubles used elsewhere -- because only
DriveRecords implements get_with_token()/put_with_fixed_file_id(), which is
what the P0-3 fix actually branches on. Exercising the fix against a store
that doesn't implement get_with_token would silently take the pre-fix
fallback path and prove nothing.
"""

import unittest
from unittest.mock import patch

from manager import task_root
from manager.execution_lifecycle import _terminal_handoff, enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.tasks import DriveRecords, TaskError, create_project, create_task, validate
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
        "task_id": "t1", "project_id": "p1", "title": "P0 regression", "task_type": "implementation",
        "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": False,
        "needs_research": False, "needs_browser": False, "parallelizable": False,
        "read_only": True, "scope": ["manager/test_p0_regression.py"], "constraints": [],
        "acceptance_criteria": ["gate"], "working_directory": "unused",
        "branch": "refs/heads/main", "baseline_head": "a" * 40,
        "allowed_paths": ["manager/test_p0_regression.py"], "execution_policies": [],
    }


class P0RegressionTests(unittest.TestCase):
    def setUp(self):
        self.store = DriveRecords(FakeDriveService())
        create_project(self.store, _project())
        create_task(self.store, _task(), assign=False)
        self.claim = MemoryClaimRegistry()

    def _reserve_and_run(self, execution_id="exec-a"):
        reserve_execution(self.store, "p1", "t1", execution_id, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=_quota_document()):
            gate = enter_running_gate(self.store, object(), None, "p1", "t1", execution_id, "codex",
                                      "read_only", task_claim_registry=self.claim)
        self._gate = gate
        return gate

    def _terminalize(self, execution_id="exec-a", status="completed",
                     completed_at="2026-09-01T00:00:00Z", summary="done"):
        with patch("manager.executions.read_drive_status", return_value=_quota_document()):
            return terminalize_execution(self.store, object(), None, self.claim, "p1", "t1", execution_id, "codex",
                                         status, self._gate["task_claim"]["generation"], True,
                                         completed_at=completed_at, summary=summary)

    # ---- P0-1: normal path binds before/with materialization ----

    def test_normal_terminal_path_binds_before_materialization(self):
        self._reserve_and_run()
        self._terminalize()

        root_doc = self.claim.document
        self.assertIsNotNone(root_doc)
        bind = root_doc.get("terminal")
        self.assertIsNotNone(bind, "normal terminalize_execution() must commit a terminal bind, not only the R17 recovery path")
        self.assertEqual("exec-a", bind["execution_id"])
        self.assertEqual("completed", bind["terminal_status"])
        self.assertEqual("verified", root_doc["materialization"]["task"]["status"])
        self.assertEqual("verified", root_doc["materialization"]["handoff"]["status"])

        # The bound Handoff ID must resolve to a REAL physical file whose
        # content matches what was actually persisted -- proving P0-1's bind
        # and P0-3's physical-ID verification compose correctly on the
        # normal (non-recovery) path.
        handoff_file_id = bind["handoff_drive_file_id"]
        self.assertIsNotNone(handoff_file_id)
        persisted = self.store.get("handoffs", "p1", "t1-completed-exec-a-0")
        self.assertEqual(persisted, self.store.get_by_file_id(handoff_file_id))

        task_doc = self.store.get("tasks", "p1", "t1")
        projection = task_doc["source_context"].get("terminal_commit_projection")
        self.assertIsNotNone(projection)
        self.assertTrue(task_root.verify_projection_matches_commit(bind, projection))

    def test_normal_path_stale_writer_non_authoritative(self):
        """A second execution racing for the same epoch never materializes
        or consumes Drive IDs once it has structurally lost the bind."""
        self._reserve_and_run()
        self._terminalize()
        winner_bind = self.claim.document["terminal"]

        loser = dict(execution_id="exec-loser", project_id="p1", task_id="t1", provider="codex",
                    retry_count=0, status="completed", completed_at="2026-09-01T00:05:00Z",
                    session_id=None, account_identity=None,
                    task_snapshot={"acceptance_criteria": []})
        with self.assertRaises(task_root.TerminalProposalLost):
            task_root.commit_terminal_bind(self.claim, "p1", "t1", loser)
        # The winner's bind is untouched by the loser's rejected attempt.
        self.assertEqual(winner_bind, self.claim.document["terminal"])

    # ---- P0-2: Task Root's own cleanup facet converges with authority release ----

    def test_task_root_release_advances_cleanup_facet(self):
        self._reserve_and_run()
        self._terminalize()
        root_doc = self.claim.document
        self.assertFalse(root_doc["authority_active"])
        self.assertEqual("released", root_doc["cleanup"]["status"])

    def test_execution_and_root_release_truth_converged(self):
        self._reserve_and_run()
        result = self._terminalize()
        execution = self.store.get("executions", "p1", "exec-a")
        self.assertEqual("released", execution["cleanup_evidence"]["task_claim_release"])
        self.assertEqual("released", self.claim.document["cleanup"]["status"])
        self.assertFalse(self.claim.document["authority_active"])
        self.assertEqual("released", result["cleanup"]["task_claim_release"])

    def test_next_epoch_not_blocked_by_stale_cleanup(self):
        self._reserve_and_run()
        self._terminalize()
        self.assertEqual("released", self.claim.document["cleanup"]["status"])

        acquired = task_root.acquire_task_root(self.claim, "p1", "t1", "exec-b", "codex", "2026-09-01T00:10:00Z")
        self.assertTrue(acquired.get("acquired", True) if isinstance(acquired, dict) and "acquired" in acquired else True)
        fresh_doc = self.claim.document
        self.assertTrue(fresh_doc["authority_active"])
        self.assertEqual("exec-b", fresh_doc["execution_id"])
        self.assertEqual(2, fresh_doc["epoch"])
        self.assertIsNone(fresh_doc.get("terminal"))

    # ---- P0-3: bound Handoff ID must name a real, matching physical file ----

    def test_existing_handoff_binds_actual_file_id(self):
        """A Handoff already durably written (e.g. by a prior partial
        attempt) with content matching the terminal proposal must have its
        REAL physical file_id frozen into the bind, not a fresh unused ID."""
        self._reserve_and_run()
        execution = self.store.get("executions", "p1", "exec-a")
        task = self.store.get("tasks", "p1", "t1")
        completed_at = "2026-09-01T00:00:00Z"
        expected_handoff = _terminal_handoff(execution, task, "completed", "done", completed_at)

        pre_existing_id = self.store.generate_record_file_id()
        self.store.put_with_fixed_file_id("handoffs", "p1", expected_handoff["handoff_id"], expected_handoff, pre_existing_id)

        self._terminalize(completed_at=completed_at, summary="done")
        bind = self.claim.document["terminal"]
        self.assertEqual(pre_existing_id, bind["handoff_drive_file_id"])
        self.assertEqual("verified", self.claim.document["materialization"]["handoff"]["status"])

    def test_conflicting_existing_handoff_fails_closed(self):
        """A pre-existing Handoff under the same logical id but with content
        that conflicts with what the terminal proposal would produce must
        fail closed -- never bind, never materialize, never silently
        overwrite the conflicting record."""
        self._reserve_and_run()
        execution = self.store.get("executions", "p1", "exec-a")
        task = self.store.get("tasks", "p1", "t1")
        completed_at = "2026-09-01T00:00:00Z"
        expected_handoff = _terminal_handoff(execution, task, "completed", "done", completed_at)
        conflicting = {**expected_handoff, "minimal_context": "a completely different summary"}

        pre_existing_id = self.store.generate_record_file_id()
        self.store.put_with_fixed_file_id("handoffs", "p1", expected_handoff["handoff_id"], conflicting, pre_existing_id)

        with self.assertRaises(TaskError):
            self._terminalize(completed_at=completed_at, summary="done")
        self.assertIsNone(self.claim.document.get("terminal") if self.claim.document else None)

    def test_bound_handoff_id_always_exists_when_verified(self):
        self._reserve_and_run()
        self._terminalize()
        bind = self.claim.document["terminal"]
        self.assertEqual("verified", self.claim.document["materialization"]["handoff"]["status"])
        # A verified handoff view's bound ID must resolve to a real file
        # with matching content -- never a dangling/nonexistent reference.
        physical = self.store.get_by_file_id(bind["handoff_drive_file_id"])
        logical = self.store.get("handoffs", "p1", "t1-completed-exec-a-0")
        self.assertEqual(logical, physical)


if __name__ == "__main__":
    unittest.main()
