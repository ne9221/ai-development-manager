import unittest

from manager.actions import ActionItem, STATUS_RESOLVED
from manager.dashboard_core import build_operational_events, build_review_evidence_vm, build_sessions_vm


class SessionsReviewsLogsTests(unittest.TestCase):
    def test_sessions_are_execution_backed_and_deterministic(self):
        rows = build_sessions_vm([
            {"execution_id": "old", "status": "completed", "started_at": "2026-08-20T00:00:00Z"},
            {"execution_id": "live", "status": "running", "project_id": "P", "task_id": "T", "provider": "codex", "provider_session_id": "session-1", "last_provider_event_at": "2026-08-21T01:00:00Z"},
            {"execution_id": "none", "status": "failed", "started_at": "2026-08-21T02:00:00Z"},
        ])
        self.assertEqual(rows["current"][0]["execution_id"], "live")
        self.assertEqual(rows["current"][0]["provider_session_id"], "session-1")
        self.assertEqual(rows["historical"][0]["provider_session_id"], "Not recorded")
        self.assertNotEqual(rows["historical"][0]["provider_session_id"], rows["historical"][0]["execution_id"])

    def test_review_evidence_never_infers_verdict(self):
        rows = build_review_evidence_vm([{"project_id": "P", "task_id": "T", "created_at": "2026-08-21T00:00:00Z", "tests": ["unit PASS"], "known_issues": ["gap"]}])
        self.assertEqual(rows[0]["verdict"], "Not recorded")
        self.assertEqual(rows[0]["reviewer"], "Not recorded")
        self.assertEqual(rows[0]["known_issues"], ["gap"])

    def test_operational_events_are_capped_and_not_heartbeat_noise(self):
        action = ActionItem(action_id="A", title="A", project_id="P", status=STATUS_RESOLVED, resolved_at="2026-08-21T04:00:00Z")
        events = build_operational_events(
            [{"project_id": "P", "task_id": "T", "provider": "codex", "status": "failed", "completed_at": "2026-08-21T03:00:00Z"}],
            [{"project_id": "P", "task_id": "T", "provider": "codex", "status": "completed", "started_at": "2026-08-21T01:00:00Z", "heartbeat_at": "2026-08-21T02:00:00Z", "completed_at": "2026-08-21T03:00:00Z", "recovery_reason": "retry"}],
            [action], [{"project_id": "P", "task_id": "T", "created_at": "2026-08-21T00:00:00Z"}], limit=4, project_id="P")
        self.assertEqual(len(events), 4)
        self.assertTrue(any(e["kind"] == "Action" for e in events))
        self.assertLessEqual(sum(e["event"] == "last activity" for e in events), 1)
        self.assertEqual(events, sorted(events, key=lambda e: (e["timestamp"], e["kind"], e["event"]), reverse=True))


if __name__ == "__main__":
    unittest.main()
