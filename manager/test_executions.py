import unittest
from copy import deepcopy
from unittest.mock import patch

from manager.estimator import estimate
from manager.executions import finish_execution, link_execution_session, list_executions, quota_delta, quota_snapshot, read_execution, read_session_for_link, start_execution
from manager.sessions import manager_session_key
from manager.tasks import DriveRecords, TaskError, create_task, validate
from manager.test_tasks import FakeDriveService


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name): return deepcopy(self.records[(area, project, name)])


def quota(windows, generated="2026-08-09T00:00:00Z", provider="codex"):
    return {"generated_at": generated, "providers": [{"provider": provider, "source_type": "official", "confidence": "official", "last_updated": generated, "windows": windows}]}


def window(name="primary", used=10, reset="2026-08-10T00:00:00Z"):
    return {"name": name, "used_percent": used, "remaining_percent": 100-used, "resets_at": reset}


def snapshot(windows, captured_at="2026-08-09T00:00:00Z"):
    return {"status": "known", "captured_at": captured_at, "source_type": "official", "confidence": "official", "windows": windows}


def task():
    return {"task_id": "t1", "project_id": "p1", "title": "Test", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True, "scope": [], "constraints": [], "acceptance_criteria": []}


def execution(minutes, delta=2, status="completed"):
    return {"provider": "codex", "mode": "code", "effort": "medium", "status": status, "elapsed_minutes": minutes, "task_snapshot": {"task_type": "implementation", "complexity": "medium", "needs_repo_edit": True}, "quota_delta": {"status": "known", "windows": [{"name": "primary", "status": "known", "used_percent_delta": delta}]}}


def session(provider="codex", provider_session_id="s1", project_id="p1"):
    return {"session_id": manager_session_key(provider, provider_session_id), "provider": provider, "provider_session_id": provider_session_id, "project_id": project_id}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(); create_task(self.store, task(), assign=False)

    def test_start_finish_elapsed_and_dynamic_windows(self):
        before = quota([window(), window("secondary", 30)])
        after = quota([window(used=13), window("secondary", 34)], "2026-08-09T00:10:00Z")
        with patch("manager.executions.read_drive_status", side_effect=[before, after]):
            start_execution(self.store, object(), "p1", "t1", "e1", "codex", "code", "medium", "2026-08-09T00:00:00Z", session=session())
            run = finish_execution(self.store, object(), "p1", "e1", "completed", "2026-08-09T00:10:00Z", "done")
        self.assertEqual(10, run["elapsed_minutes"])
        self.assertEqual([3, 4], [item["used_percent_delta"] for item in run["quota_delta"]["windows"]])
        self.assertEqual("known", run["quota_delta"]["attribution_status"])
        self.assertEqual("codex:s1", run["session_id"])
        self.assertEqual(run, read_execution(self.store, "p1", "e1"))
        self.assertEqual("completed", self.store.get("tasks", "p1", "t1")["status"])

    def test_drive_execution_ids_round_trip_through_create_list_and_read(self):
        store = DriveRecords(FakeDriveService())
        create_task(store, task(), assign=False)
        execution_ids = ["exec-123", "exec:123", "exec 123", "執行:123", "exec/123", "exec\\123"]
        with patch("manager.executions.read_drive_status", return_value=quota([])):
            for execution_id in execution_ids:
                start_execution(store, object(), "p1", "t1", execution_id, "codex", started_at="2026-08-09T00:00:00Z")
        records = list_executions(store, "p1")
        self.assertEqual(execution_ids, [record["execution_id"] for record in records])
        for execution_id in execution_ids:
            self.assertEqual(execution_id, read_execution(store, "p1", execution_id)["execution_id"])

    def test_single_missing_unknown_and_reset(self):
        known = quota_delta(quota_snapshot(quota([window()]), "codex"), quota_snapshot(quota([window(used=12)]), "codex"), "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("known", known["status"])
        missing = quota_delta(snapshot([window()]), None, "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("unknown_due_to_missing_after", missing["windows"][0]["reason"])
        self.assertIsNone(missing["windows"][0]["used_percent_delta"])
        missing_before = quota_delta(None, snapshot([window(used=12)], "2026-08-09T00:10:00Z"), "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("unknown_due_to_missing_before", missing_before["windows"][0]["reason"])
        unknown = quota_delta(None, None, "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("unknown", unknown["status"])
        reset = quota_delta(snapshot([window(used=90, reset="2026-08-09T00:05:00Z")]), snapshot([window(used=2, reset="2026-08-10T00:05:00Z")], "2026-08-09T00:10:00Z"), "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("unknown_due_to_reset", reset["windows"][0]["reason"])

    def test_attribution_rejects_stale_and_changed_windows_without_zero(self):
        stale = quota_delta(snapshot([window()]), snapshot([window(used=12)]), "2026-08-09T00:00:00Z", "2026-08-09T03:00:00Z")
        self.assertEqual("unknown_due_to_stale_snapshot", stale["windows"][0]["reason"])
        self.assertIsNone(stale["windows"][0]["used_percent_delta"])
        changed = quota_delta(snapshot([window("5h")]), snapshot([window("7d", used=12)], "2026-08-09T00:10:00Z"), "2026-08-09T00:00:00Z", "2026-08-09T00:10:00Z")
        self.assertEqual("unknown", changed["attribution_status"])
        self.assertTrue(all(item["used_percent_delta"] is None for item in changed["windows"]))

    def test_claude_execution_uses_same_conservative_attribution_contract(self):
        before = quota([window("5h")], provider="claude")
        after = quota([window("5h", used=14)], "2026-08-09T00:10:00Z", provider="claude")
        with patch("manager.executions.read_drive_status", side_effect=[before, after]):
            start_execution(self.store, object(), "p1", "t1", "claude-usage", "claude", started_at="2026-08-09T00:00:00Z", session=session("claude", "s1"))
            result = finish_execution(self.store, object(), "p1", "claude-usage", completed_at="2026-08-09T00:10:00Z")
        self.assertEqual("known", result["quota_delta"]["attribution_status"])
        self.assertEqual(4, result["quota_delta"]["windows"][0]["used_percent_delta"])

    def test_failed_and_interrupted_are_preserved(self):
        for execution_id, terminal in (("failed", "failed"), ("interrupted", "interrupted")):
            with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
                start_execution(self.store, object(), "p1", "t1", execution_id, "codex", started_at="2026-08-09T00:00:00Z")
                result = finish_execution(self.store, object(), "p1", execution_id, terminal, "2026-08-09T00:01:00Z")
            self.assertEqual(terminal, result["status"])
            self.assertEqual(1, result["elapsed_minutes"])

    def test_start_links_codex_and_claude_canonical_sessions(self):
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
            codex = start_execution(self.store, object(), "p1", "t1", "codex-run", "codex", started_at="2026-08-09T00:00:00Z", session=session("codex", "abc123"))
            claude = start_execution(self.store, object(), "p1", "t1", "claude-run", "claude", started_at="2026-08-09T00:00:00Z", session=session("claude", "abc123"))
        self.assertEqual("codex:abc123", codex["session_id"])
        self.assertEqual("claude:abc123", claude["session_id"])
        self.assertNotEqual(codex["session_id"], claude["session_id"])
        self.assertEqual("abc123", claude["provider_session_id"])

    def test_link_rejects_provider_and_project_mismatch(self):
        with patch("manager.executions.read_drive_status", return_value=quota([])):
            start_execution(self.store, object(), "p1", "t1", "e-link", "codex", started_at="2026-08-09T00:00:00Z")
        with self.assertRaises(TaskError): link_execution_session(self.store, "p1", "e-link", session("claude", "s1"))
        with self.assertRaises(TaskError): link_execution_session(self.store, "p1", "e-link", session("codex", "s1", "other"))

    def test_link_is_idempotent_allows_shared_session_and_legacy_reads(self):
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
            start_execution(self.store, object(), "p1", "t1", "e1", "codex", started_at="2026-08-09T00:00:00Z")
            second = start_execution(self.store, object(), "p1", "t1", "e2", "codex", started_at="2026-08-09T00:01:00Z", session=session())
        first = link_execution_session(self.store, "p1", "e1", session())
        self.assertEqual(first, link_execution_session(self.store, "p1", "e1", session()))
        self.assertEqual(first["session_id"], second["session_id"])
        legacy = deepcopy(first); legacy.pop("session_id"); legacy.pop("provider_session_id"); legacy.pop("finished_at")
        self.store.put("executions", "p1", "legacy", legacy)
        self.assertEqual("e1", read_execution(self.store, "p1", "legacy")["execution_id"])

    def test_link_lookup_reads_legacy_codex_registry_key(self):
        legacy = {"session_id": "abc123", "provider": "codex", "project_id": "p1"}
        self.store.put("sessions", "p1", "abc123", legacy)
        self.assertEqual(legacy, read_session_for_link(self.store, "p1", "codex:abc123"))

    def test_malformed_execution_rejected(self):
        with self.assertRaises(TaskError): validate("execution", {"execution_id": "bad"})

    def test_estimator_zero_one_multiple_and_split(self):
        query = {"task_type": "implementation", "provider": "codex", "mode": "code", "effort": "medium", "complexity": "medium", "needs_repo_edit": True, "expected_minutes": 25}
        empty = estimate(query, [])
        self.assertEqual("none", empty["confidence"]); self.assertTrue(empty["split_recommended"])
        one = estimate(query, [execution(18, 2)])
        self.assertEqual("low", one["confidence"]); self.assertEqual(2, one["estimated_quota_delta"]["windows"][0]["used_percent_delta"])
        many = estimate(query, [execution(18, 1), execution(22, 2), execution(30, 3)])
        self.assertEqual(22, many["estimated_minutes"]); self.assertEqual("medium", many["confidence"]); self.assertTrue(many["split_recommended"]); self.assertEqual(2, many["suggested_phases"])


if __name__ == "__main__": unittest.main()
