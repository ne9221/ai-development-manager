"""Gap 1 architecture proofs: reader-side GCS terminal authority validation
actually wired into a real consumer (manager.dashboard_core), not just
existing as an unused primitive in manager.task_root.

Also documents the session_center.py inventory result: it judges terminal
truth purely from Execution.cleanup_evidence (already its own correctly
fail-closed lattice, unrelated to Task Drive projections), never from a
Task's own status/active_execution_id/Handoff-presence -- so it needs no
GCS Task Root integration at all. See
test_session_center_never_reads_task_projection_truth below.
"""

import unittest
from datetime import datetime, timezone

from manager import dashboard_core, task_root
from manager.dashboard_core import (
    TERMINAL_TRUTH_MISSING, TERMINAL_TRUTH_STALE, TERMINAL_TRUTH_TRUSTED, TERMINAL_TRUTH_UNVERIFIED,
    dashboard_terminal_state, resolve_terminal_truth,
)


def _bind(execution_id="exec-a", epoch=1, proposal_hash="hash-a", extra=None):
    bind = {
        "execution_id": execution_id, "epoch": epoch, "proposal_hash": proposal_hash,
        "task_id": "t1", "project_id": "p1", "retry_count": 0,
        "terminal_status": "completed", "provider_outcome": "completed",
        "terminal_reason": None, "completed_at": "2026-09-01T00:00:00Z",
        "terminal_committed_at": "2026-09-01T00:00:00Z", "provider_identity": "codex",
        "session_id": None, "account_identity": None, "schema_version": task_root.SCHEMA_VERSION,
        "canonicalization_version": task_root.CANONICALIZATION_VERSION,
        "canonical_proposal": {}, "terminal_fence_epoch": epoch,
        "task_projection_drive_id": None, "handoff_drive_file_id": None,
        "expected_task_projection_digest": None, "expected_handoff_projection_digest": None,
    }
    bind.update(extra or {})
    return bind


def _task_root_document(bind=None, materialization=None):
    return {
        "epoch": 1, "terminal": bind,
        "materialization": materialization or {"task": {"status": "absent"}, "handoff": {"status": "absent"}},
        "cleanup": {"status": "retained"},
    }


def _execution(status="completed", task_claim_release="released", writer_release="not_required", access="read_only"):
    return {"status": status, "access": access,
            "cleanup_evidence": {"task_claim_release": task_claim_release, "writer_release": writer_release}}


class ReaderProjectionValidationTests(unittest.TestCase):
    def test_A_valid_gcs_winner_and_matching_digest_trusted(self):
        # Mirrors real materialization order (execution_lifecycle.
        # retry_incomplete_terminal_persistence): the projection stamp is
        # part of the Task record BEFORE its digest is computed, since the
        # bind's expected digest covers the exact record that gets written.
        bind_without_digest = _bind()
        task_payload = {"status": "completed",
                        "source_context": {"terminal_commit_projection": task_root.projection_of(bind_without_digest)}}
        digest = task_root.projection_digest(task_payload)
        bind = _bind(extra={"expected_task_projection_digest": digest})
        self.assertEqual(TERMINAL_TRUTH_TRUSTED, resolve_terminal_truth(task_payload, _execution(), _task_root_document(bind)))

    def test_B_old_epoch_projection_rejected(self):
        bind = _bind(epoch=2)
        task_payload = {"status": "completed", "source_context": {
            "terminal_commit_projection": {"execution_id": "exec-a", "epoch": 1, "proposal_hash": "hash-a"}}}
        self.assertEqual(TERMINAL_TRUTH_STALE, resolve_terminal_truth(task_payload, _execution(), _task_root_document(bind)))

    def test_C_same_epoch_corrupted_content_digest_mismatch_rejected(self):
        original_payload = {"status": "completed", "source_context": {}}
        digest = task_root.projection_digest(original_payload)
        bind = _bind(extra={"expected_task_projection_digest": digest})
        projection = task_root.projection_of(bind)
        corrupted_payload = {"status": "blocked", "source_context": {"terminal_commit_projection": projection}}
        self.assertEqual(TERMINAL_TRUTH_STALE, resolve_terminal_truth(corrupted_payload, _execution(), _task_root_document(bind)))

    def test_D_gcs_committed_missing_drive_task_view_retains_terminal_truth_incomplete(self):
        bind = _bind()
        task_payload = {"status": "in_progress", "source_context": {}}  # never stamped -- Drive view absent
        self.assertEqual(TERMINAL_TRUTH_MISSING, resolve_terminal_truth(task_payload, _execution(), _task_root_document(bind)))
        state = dashboard_terminal_state(task_payload, _execution(), _task_root_document(bind))
        self.assertEqual("terminal_committed_materialization_incomplete", state)
        self.assertNotIn("running", state)

    def test_E_drive_says_completed_but_no_strengthened_bind_not_authoritative_alone(self):
        """Drive Task status=completed with cleanup_evidence NOT actually
        released must not be trusted just because status looks terminal --
        this is the pre-Design-A is_cleanup_confirmed() fallback path,
        proven unchanged."""
        task_payload = {"status": "completed", "source_context": {}}
        execution = _execution(task_claim_release="retained")
        self.assertEqual(TERMINAL_TRUTH_UNVERIFIED, resolve_terminal_truth(task_payload, execution, task_root_document=None))

    def test_F_legacy_task_no_task_root_document_uses_existing_compatibility_semantics(self):
        """A task never migrated to Strengthened Design A (no Task Root
        document passed at all) sees ZERO behavior change: the plain
        pre-Design-A cleanup_evidence check remains sole authority."""
        task_payload = {"status": "completed", "source_context": {}}
        execution = _execution(task_claim_release="released", writer_release="not_required")
        self.assertEqual(TERMINAL_TRUTH_TRUSTED, resolve_terminal_truth(task_payload, execution, task_root_document=None))
        state = dashboard_terminal_state(task_payload, execution, task_root_document=None)
        self.assertEqual("completed", state)

    def test_G_dashboard_state_never_regresses_to_running_when_projection_stale(self):
        bind = _bind()
        # Drive Task looks like it's still actively running -- but a GCS
        # terminal winner already exists for this task.
        task_payload = {"status": "in_progress", "source_context": {}}
        execution = {"status": "running", "access": "read_only", "provider_session_id": "sess-1",
                    "heartbeat_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "provider": "codex"}
        state = dashboard_terminal_state(task_payload, execution, _task_root_document(bind))
        self.assertNotEqual("running", state)
        self.assertNotEqual("waiting", state)
        self.assertNotEqual("correlating", state)
        self.assertTrue(state.startswith("terminal_committed"))

    def test_attention_materialization_surfaced_distinctly(self):
        bind = _bind()
        task_payload = {"status": "in_progress", "source_context": {}}
        materialization = {"task": {"status": "attention", "note": "Drive 403"}, "handoff": {"status": "verified"}}
        state = dashboard_terminal_state(task_payload, _execution(), _task_root_document(bind, materialization))
        self.assertEqual("terminal_committed_materialization_attention", state)


class SessionCenterScopeInventoryTests(unittest.TestCase):
    def test_session_center_never_reads_task_projection_truth(self):
        """Documents the Gap 1 inventory finding: session_center.py's own
        terminal-truth function (_authoritative_state/_cleanup_confirmed)
        takes only an Execution record and judges purely from
        Execution.cleanup_evidence -- never a Task's status,
        active_execution_id, or Handoff presence. That lattice is already
        correctly fail-closed (see manager.execution_lifecycle's
        merge_cleanup_evidence), so no GCS Task Root integration applies
        here without genuine scope creep."""
        import inspect
        from manager import session_center
        source = inspect.getsource(session_center)
        self.assertNotIn('"tasks"', source)
        self.assertNotIn("active_execution_id", source)


if __name__ == "__main__":
    unittest.main()
