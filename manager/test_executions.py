import io
import unittest
from copy import deepcopy
from unittest.mock import patch

from manager import executions as executions_module
from manager.estimator import estimate
from manager.executions import cancel_reserved_execution, finish_execution, link_execution_session, list_executions, main as executions_main, prepare_task_retry, quota_delta, quota_snapshot, read_execution, read_session_for_link, reserve_execution, start_execution, task_snapshot
from manager.task_claims import claim_task_execution
from manager.test_task_claims import MemoryClaimRegistry
from manager.sessions import manager_session_key
from manager.tasks import DriveRecords, TaskError, create_task, update_task, validate
from manager.test_tasks import FakeDriveService


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name): return deepcopy(self.records[(area, project, name)])
    def list_records(self, area, project): return [deepcopy(v) for (a, p, _), v in self.records.items() if a == area and p == project]


def quota(windows, generated="2026-08-09T00:00:00Z", provider="codex"):
    return {"generated_at": generated, "providers": [{"provider": provider, "source_type": "official", "confidence": "official", "last_updated": generated, "windows": windows}]}


def window(name="primary", used=10, reset="2026-08-10T00:00:00Z"):
    return {"name": name, "used_percent": used, "remaining_percent": 100-used, "resets_at": reset}


def snapshot(windows, captured_at="2026-08-09T00:00:00Z"):
    return {"status": "known", "captured_at": captured_at, "source_type": "official", "confidence": "official", "windows": windows}


def task():
    return {
        "task_id": "t1", "project_id": "p1", "title": "Test", "task_type": "implementation",
        "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True,
        "needs_research": False, "needs_browser": False, "parallelizable": False,
        "read_only": False, "scope": ["manager/executions.py"], "constraints": ["Slice 2 only"],
        "acceptance_criteria": ["reserved"], "working_directory": "C:/repo", "branch": "feature",
        "allowed_paths": ["manager/executions.py"], "execution_policies": ["fail closed"],
        "quota_evidence": {"stale-task-evidence": True},
    }


def decision(provider="codex", marker="fresh"):
    return {"selected_provider": provider, "decision": marker}


def execution(minutes, delta=2, status="completed"):
    return {"provider": "codex", "mode": "code", "effort": "medium", "status": status, "elapsed_minutes": minutes, "task_snapshot": {"task_type": "implementation", "complexity": "medium", "needs_repo_edit": True}, "quota_delta": {"status": "known", "windows": [{"name": "primary", "status": "known", "used_percent_delta": delta}]}}


def session(provider="codex", provider_session_id="s1", project_id="p1"):
    return {"session_id": manager_session_key(provider, provider_session_id), "provider": provider, "provider_session_id": provider_session_id, "project_id": project_id}


def put_legacy_running(store, service, project_id, task_id, execution_id, provider, mode=None, effort=None, started_at=None, notes=None, session=None):
    """Create a legacy running fixture without exposing a production bypass."""
    task_document = store.get("tasks", project_id, task_id)
    before = quota_snapshot(executions_module.read_drive_status(service=service), provider)
    record = {
        "execution_id": execution_id, "task_id": task_id, "project_id": project_id,
        "provider": provider, "mode": mode or task_document.get("mode"), "effort": effort or task_document.get("effort"),
        "started_at": started_at, "completed_at": None, "elapsed_minutes": None, "status": "running",
        "finished_at": None, "session_id": None, "provider_session_id": None,
        "quota_before": before, "quota_after": None, "quota_delta": None,
        "source_confidence": before.get("confidence", "unknown"), "notes": notes or [],
        "task_snapshot": task_snapshot(task_document),
    }
    if session:
        record.update(executions_module.session_link_fields(record, session))
    validate("execution", record)
    store.put("executions", project_id, execution_id, record)
    update_task(store, project_id, task_id, status="in_progress", assigned_provider=provider)
    return record


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(); create_task(self.store, task(), assign=False)

    def test_reserve_is_idempotent_persists_evidence_and_does_not_start_task(self):
        task_before = self.store.get("tasks", "p1", "t1")
        with patch("manager.executions.read_drive_status") as reader:
            first = reserve_execution(self.store, "p1", "t1", "reserved-1", "codex", decision(), "code", "high", "2026-08-09T00:00:00Z", ["planned"])
            second = reserve_execution(self.store, "p1", "t1", "reserved-1", "codex", decision(), "code", "high", notes=["planned"])
        self.assertEqual(first, second); reader.assert_not_called()
        self.assertEqual("reserved", first["status"]); self.assertEqual("2026-08-09T00:00:00Z", first["reserved_at"])
        self.assertIsNone(first["started_at"]); self.assertEqual(("codex", "code", "high"), (first["provider"], first["mode"], first["effort"]))
        self.assertEqual(task_snapshot(task_before), first["task_snapshot"])
        self.assertEqual(
            {"title", "task_type", "complexity", "expected_minutes", "needs_repo_edit", "needs_research", "needs_browser", "parallelizable", "read_only", "scope", "constraints", "acceptance_criteria", "working_directory", "branch", "allowed_paths", "execution_policies"},
            set(first["task_snapshot"]),
        )
        self.assertTrue({"status", "current_progress", "next_action", "updated_at"}.isdisjoint(first["task_snapshot"]))
        self.assertEqual(decision(), first["quota_evidence"])
        self.assertEqual(task_before, self.store.get("tasks", "p1", "t1"))
        for field in ("started_at", "completed_at", "finished_at", "elapsed_minutes", "quota_before", "quota_after", "quota_delta", "session_id", "provider_session_id", "source_confidence"):
            self.assertIsNone(first[field])
        invalid = deepcopy(first); invalid.pop("reserved_at")
        with self.assertRaisesRegex(TaskError, "reserved_at"):
            validate("execution", invalid)

    def test_cancel_never_started_reservation_is_strict_and_idempotent(self):
        reserved = reserve_execution(self.store, "p1", "t1", "cancel-me", "codex", decision(), reserved_at="2026-08-09T00:00:00Z")
        cancelled = cancel_reserved_execution(self.store, MemoryClaimRegistry(), "p1", "cancel-me", "retry requested", "2026-08-09T00:01:00Z")
        self.assertEqual("cancelled", cancelled["status"]); self.assertEqual(0, cancelled["elapsed_minutes"])
        self.assertIsNone(cancelled["completed_at"]); self.assertEqual("2026-08-09T00:01:00Z", cancelled["finished_at"])
        for field in ("started_at", "session_id", "provider_session_id", "access", "lease_evidence", "cleanup_evidence"):
            self.assertIsNone(cancelled[field])
        self.assertEqual(cancelled, cancel_reserved_execution(self.store, MemoryClaimRegistry(), "p1", "cancel-me", "retry requested"))
        invalid = deepcopy(cancelled); invalid["session_id"] = "codex:s1"
        with self.assertRaises(TaskError): validate("execution", invalid)
        with self.assertRaisesRegex(TaskError, "different reason"):
            cancel_reserved_execution(self.store, MemoryClaimRegistry(), "p1", "cancel-me", "other")

        claimed = reserve_execution(self.store, "p1", "t1", "claimed", "codex", decision())
        registry = MemoryClaimRegistry(); claim_task_execution(registry, "p1", "t1", "claimed", "codex", "2026-08-09T00:00:00Z")
        with self.assertRaisesRegex(TaskError, "task claim exists"):
            cancel_reserved_execution(self.store, registry, "p1", "claimed", "no")

    def test_cancel_rejects_running_and_terminal_states(self):
        for status in ("running", "completed", "failed", "interrupted"):
            reserved = reserve_execution(self.store, "p1", "t1", f"no-{status}", "codex", decision())
            record = {**reserved, "status": status, "started_at": "2026-08-09T00:00:00Z", "quota_before": {}, "source_confidence": "official", "access": "read_only", "lease_evidence": None}
            if status != "running": record.update(completed_at="2026-08-09T00:01:00Z", finished_at="2026-08-09T00:01:00Z", elapsed_minutes=1, quota_after={}, quota_delta={})
            self.store.put("executions", "p1", f"no-{status}", record)
            with self.subTest(status=status), self.assertRaisesRegex(TaskError, "only a reserved"):
                cancel_reserved_execution(self.store, MemoryClaimRegistry(), "p1", f"no-{status}", "no")

    def test_retry_requires_terminal_cleanup_and_clears_stale_task_fields(self):
        prior = reserve_execution(self.store, "p1", "t1", "prior", "codex", decision())
        prior.update(status="interrupted", started_at="2026-08-09T00:00:00Z", completed_at="2026-08-09T00:01:00Z",
                     finished_at="2026-08-09T00:01:00Z", elapsed_minutes=1, quota_before={}, quota_after={}, quota_delta={},
                     source_confidence="official", access="read_only", lease_evidence=None,
                     cleanup_evidence={"persistence":"complete","task_claim_release":"released","writer_release":"not_required"})
        self.store.put("executions", "p1", "prior", prior)
        task_record = self.store.get("tasks", "p1", "t1"); task_record.update(status="blocked", blocked_reason="old", source_context={"active_execution_id":"prior","keep":True})
        self.store.put("tasks", "p1", "t1", task_record)
        ready = prepare_task_retry(self.store, MemoryClaimRegistry(), "p1", "t1", "prior")
        self.assertEqual("ready", ready["status"]); self.assertIsNone(ready["blocked_reason"])
        self.assertNotIn("active_execution_id", ready["source_context"]); self.assertTrue(ready["source_context"]["keep"])
        self.assertEqual(ready, prepare_task_retry(self.store, MemoryClaimRegistry(), "p1", "t1", "prior"))

    def test_retry_rejects_incomplete_cleanup_claim_writer_and_active_execution(self):
        base = reserve_execution(self.store, "p1", "t1", "prior", "codex", decision())
        base.update(status="failed", started_at="2026-08-09T00:00:00Z", completed_at="2026-08-09T00:01:00Z", finished_at="2026-08-09T00:01:00Z",
                    elapsed_minutes=1, quota_before={}, quota_after={}, quota_delta={}, source_confidence="official", access="read_only", lease_evidence=None,
                    cleanup_evidence={"persistence":"complete","task_claim_release":"released","writer_release":"not_required"})
        self.store.put("executions", "p1", "prior", base)
        task_record=self.store.get("tasks","p1","t1"); task_record.update(status="blocked",blocked_reason="x",source_context={"active_execution_id":"prior"}); self.store.put("tasks","p1","t1",task_record)
        lease = {"authority":"acquired","lock_id":"repo-"+"0"*64,"generation":1,"repository":"github:ne9221/ai-development-manager","branch":"refs/heads/main","scope":["manager/executions.py"],"baseline_head":"0"*40}
        cases = [({"cleanup_evidence":{"persistence":"partial","task_claim_release":"released","writer_release":"not_required"}}, MemoryClaimRegistry(), "complete persistence"),
                 ({"cleanup_evidence":{"persistence":"complete","task_claim_release":"retained","writer_release":"not_required"}}, MemoryClaimRegistry(), "released task claim"),
                 ({"access":"production_write","lease_evidence":lease,"cleanup_evidence":{"persistence":"complete","task_claim_release":"released","writer_release":"retained"}}, MemoryClaimRegistry(), "writer authority")]
        for changes, registry, message in cases:
            record=deepcopy(base); record.update(changes); self.store.put("executions","p1","prior",record)
            with self.subTest(message=message), self.assertRaisesRegex(TaskError,message): prepare_task_retry(self.store,registry,"p1","t1","prior")
        self.store.put("executions","p1","prior",base); pending=reserve_execution(self.store,"p1","t1","pending","codex",decision())
        with self.assertRaisesRegex(TaskError,"running or reserved"): prepare_task_retry(self.store,MemoryClaimRegistry(),"p1","t1","prior")
        pending.update(status="running",started_at="2026-08-09T00:02:00Z",quota_before={},source_confidence="official",access="read_only",lease_evidence=None)
        self.store.put("executions","p1","pending",pending)
        with self.assertRaisesRegex(TaskError,"running or reserved"): prepare_task_retry(self.store,MemoryClaimRegistry(),"p1","t1","prior")

        self.store.records.pop(("executions","p1","pending")); registry=MemoryClaimRegistry()
        claim_task_execution(registry,"p1","t1","other","codex","2026-08-09T00:03:00Z")
        with self.assertRaisesRegex(TaskError,"no active task claim"): prepare_task_retry(self.store,registry,"p1","t1","prior")

    def test_reserve_rejects_payload_and_snapshot_conflicts_without_overwrite(self):
        original = reserve_execution(self.store, "p1", "t1", "reserved-conflict", "codex", decision(), "code", "medium", "2026-08-09T00:00:00Z")
        conflicts = [
            ("claude", decision(), "code", "medium"),
            ("codex", decision(), "analysis", "medium"),
            ("codex", decision(), "code", "high"),
            ("codex", decision(marker="changed"), "code", "medium"),
        ]
        for provider, evidence, mode, effort in conflicts:
            with self.subTest(provider=provider, evidence=evidence, mode=mode, effort=effort):
                with self.assertRaisesRegex(TaskError, "different reservation"):
                    reserve_execution(self.store, "p1", "t1", "reserved-conflict", provider, evidence, mode, effort)
                self.assertEqual(original, self.store.get("executions", "p1", "reserved-conflict"))
        changed = self.store.get("tasks", "p1", "t1"); changed["scope"] = ["different.py"]
        self.store.put("tasks", "p1", "t1", changed)
        with self.assertRaisesRegex(TaskError, "different reservation"):
            reserve_execution(self.store, "p1", "t1", "reserved-conflict", "codex", decision(), "code", "medium")
        self.assertEqual(original, self.store.get("executions", "p1", "reserved-conflict"))
        changed["scope"] = task()["scope"]; changed["current_progress"] = "workflow-only change"
        self.store.put("tasks", "p1", "t1", changed)
        self.assertEqual(original, reserve_execution(self.store, "p1", "t1", "reserved-conflict", "codex", decision(), "code", "medium"))

    def test_reserve_rejects_running_terminal_and_malformed_existing_records(self):
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([]), quota([])]):
            running = put_legacy_running(self.store, object(), "p1", "t1", "already-running", "codex", started_at="2026-08-09T00:00:00Z")
            put_legacy_running(self.store, object(), "p1", "t1", "already-terminal", "codex", started_at="2026-08-09T00:00:00Z")
            terminal = finish_execution(self.store, object(), "p1", "already-terminal", completed_at="2026-08-09T00:01:00Z")
        for execution_id, existing in (("already-running", running), ("already-terminal", terminal)):
            with self.assertRaisesRegex(TaskError, "different reservation"):
                reserve_execution(self.store, "p1", "t1", execution_id, "codex", decision())
            self.assertEqual(existing, self.store.get("executions", "p1", execution_id))
        malformed = {"execution_id": "malformed", "status": "reserved"}
        self.store.put("executions", "p1", "malformed", malformed)
        with self.assertRaisesRegex(TaskError, "invalid execution"):
            reserve_execution(self.store, "p1", "t1", "malformed", "codex", decision())
        self.assertEqual(malformed, self.store.get("executions", "p1", "malformed"))

    def test_reserve_rejects_invalid_quota_evidence(self):
        for evidence in (None, {}, [], "fresh"):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(TaskError, "non-empty object"):
                reserve_execution(self.store, "p1", "t1", "invalid-evidence", "codex", evidence)

    def test_reserved_schema_enforces_unstarted_invariants_and_legacy_records(self):
        reserved = reserve_execution(self.store, "p1", "t1", "schema-reserved", "codex", decision(), reserved_at="2026-08-09T00:00:00Z")
        invalid_values = {
            "started_at": "2026-08-09T00:00:00Z", "completed_at": "2026-08-09T00:00:00Z",
            "finished_at": "2026-08-09T00:00:00Z", "elapsed_minutes": 0,
            "quota_before": {}, "quota_after": {}, "quota_delta": {}, "session_id": "codex:s1",
            "provider_session_id": "s1", "source_confidence": "official", "quota_evidence": {},
        }
        for field, value in invalid_values.items():
            invalid = deepcopy(reserved); invalid[field] = value
            with self.subTest(field=field), self.assertRaises(TaskError):
                validate("execution", invalid)
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
            legacy = put_legacy_running(self.store, object(), "p1", "t1", "legacy-running", "codex", started_at="2026-08-09T00:00:00Z")
            validate("execution", legacy)
            validate("execution", finish_execution(self.store, object(), "p1", "legacy-running", completed_at="2026-08-09T00:01:00Z"))

    def test_new_lifecycle_running_schema_requires_access_and_matching_lease_contract(self):
        reserved = reserve_execution(self.store, "p1", "t1", "schema-running", "codex", decision(), reserved_at="2026-08-09T00:00:00Z")
        running = {
            **reserved, "status": "running", "started_at": "2026-08-09T00:01:00Z",
            "quota_before": {}, "source_confidence": "official",
            "access": "production_write",
            "lease_evidence": {
                "authority": "acquired", "lock_id": "repo-" + "0" * 64, "generation": 1,
                "repository": "github:ne9221/ai-development-manager", "branch": "refs/heads/main",
                "scope": ["manager/executions.py"], "baseline_head": "0" * 40,
            },
        }
        validate("execution", running)
        for changes in ({"access": None}, {"lease_evidence": None}):
            invalid = {**running, **changes}
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                validate("execution", invalid)
        read_only = {**running, "access": "read_only", "lease_evidence": None}
        validate("execution", read_only)

    def test_legacy_start_cannot_promote_or_overwrite_reservation(self):
        with patch("manager.executions.read_drive_status") as reader:
            reserved = reserve_execution(self.store, "p1", "t1", "reserved-gate", "codex", decision(), reserved_at="2026-08-09T00:00:00Z")
            with self.assertRaisesRegex(TaskError, "legacy start is retired"):
                start_execution(self.store, object(), "p1", "t1", "reserved-gate", "codex")
        reader.assert_not_called()
        self.assertEqual(reserved, self.store.get("executions", "p1", "reserved-gate"))
        self.assertEqual("ready", self.store.get("tasks", "p1", "t1")["status"])

    def test_start_finish_elapsed_and_dynamic_windows(self):
        before = quota([window(), window("secondary", 30)])
        after = quota([window(used=13), window("secondary", 34)], "2026-08-09T00:10:00Z")
        with patch("manager.executions.read_drive_status", side_effect=[before, after]):
            put_legacy_running(self.store, object(), "p1", "t1", "e1", "codex", "code", "medium", "2026-08-09T00:00:00Z", session=session())
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
                put_legacy_running(store, object(), "p1", "t1", execution_id, "codex", started_at="2026-08-09T00:00:00Z")
        records = list_executions(store, "p1")
        self.assertEqual(execution_ids, [record["execution_id"] for record in records])
        for execution_id in execution_ids:
            self.assertEqual(execution_id, read_execution(store, "p1", execution_id)["execution_id"])

    def test_drive_reserved_read_list_and_duplicate_physical_records_fail_closed(self):
        service = FakeDriveService(); store = DriveRecords(service)
        create_task(store, task(), assign=False)
        reserved = reserve_execution(store, "p1", "t1", "drive-reserved", "codex", decision(), "code", "high", "2026-08-09T00:00:00Z")
        self.assertEqual(reserved, read_execution(store, "p1", "drive-reserved"))
        self.assertEqual([reserved], list_executions(store, "p1"))
        parent = store.project_folder("executions", "p1", create=False)
        filename = store.record_filename("drive-reserved")
        original_id = store.children(parent, filename)[0]["id"]
        duplicate = deepcopy(service.transport.items[original_id])
        duplicate["meta"]["id"] = "duplicate-execution-record"
        service.transport.items["duplicate-execution-record"] = duplicate
        with self.assertRaisesRegex(TaskError, "found 2"):
            reserve_execution(store, "p1", "t1", "drive-reserved", "codex", decision(), "code", "high")
        self.assertEqual(2, len(store.children(parent, filename)))

    def test_finish_rejects_reserved_and_estimator_ignores_it(self):
        reserved = reserve_execution(self.store, "p1", "t1", "not-running", "codex", decision(), "code", "medium", "2026-08-09T00:00:00Z")
        with self.assertRaisesRegex(TaskError, "not running"):
            finish_execution(self.store, object(), "p1", "not-running")
        query = {"task_type": "implementation", "provider": "codex", "mode": "code", "effort": "medium", "complexity": "medium", "needs_repo_edit": True, "expected_minutes": 25}
        result = estimate(query, [reserved, execution(18)])
        self.assertEqual(1, result["sample_count"])
        self.assertEqual(18, result["estimated_minutes"])

    def test_reservation_write_failure_leaves_task_unchanged(self):
        task_before = self.store.get("tasks", "p1", "t1")
        real_put = self.store.put
        def fail_execution_write(area, project, name, document):
            if area == "executions":
                raise TaskError("Drive write failed")
            return real_put(area, project, name, document)
        with patch.object(self.store, "put", side_effect=fail_execution_write):
            with self.assertRaisesRegex(TaskError, "write failed"):
                reserve_execution(self.store, "p1", "t1", "write-failure", "codex", decision())
        self.assertEqual(task_before, self.store.get("tasks", "p1", "t1"))

    def test_reserve_cli_success_idempotent_and_conflict_exit_behavior(self):
        args = ["executions.py", "reserve", "p1", "t1", "cli-reserved", "--provider", "codex", "--quota-evidence-json", '{"decision":"fresh"}', "--mode", "code", "--effort", "high"]
        with patch("manager.executions.build_service", return_value=object()), patch("manager.executions.DriveRecords", return_value=self.store), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with patch("sys.argv", args):
                self.assertEqual(0, executions_main())
            first = self.store.get("executions", "p1", "cli-reserved")
            with patch("sys.argv", args):
                self.assertEqual(0, executions_main())
            self.assertEqual(first, self.store.get("executions", "p1", "cli-reserved"))
            conflict = args[:]; conflict[conflict.index("codex")] = "claude"
            with patch("sys.argv", conflict):
                self.assertEqual(1, executions_main())
        self.assertIn("different reservation", stderr.getvalue())
        self.assertEqual(first, self.store.get("executions", "p1", "cli-reserved"))

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
            put_legacy_running(self.store, object(), "p1", "t1", "claude-usage", "claude", started_at="2026-08-09T00:00:00Z", session=session("claude", "s1"))
            result = finish_execution(self.store, object(), "p1", "claude-usage", completed_at="2026-08-09T00:10:00Z")
        self.assertEqual("known", result["quota_delta"]["attribution_status"])
        self.assertEqual(4, result["quota_delta"]["windows"][0]["used_percent_delta"])

    def test_failed_and_interrupted_are_preserved(self):
        for execution_id, terminal in (("failed", "failed"), ("interrupted", "interrupted")):
            with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
                put_legacy_running(self.store, object(), "p1", "t1", execution_id, "codex", started_at="2026-08-09T00:00:00Z")
                result = finish_execution(self.store, object(), "p1", execution_id, terminal, "2026-08-09T00:01:00Z")
            self.assertEqual(terminal, result["status"])
            self.assertEqual(1, result["elapsed_minutes"])

    def test_start_links_codex_and_claude_canonical_sessions(self):
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
            codex = put_legacy_running(self.store, object(), "p1", "t1", "codex-run", "codex", started_at="2026-08-09T00:00:00Z", session=session("codex", "abc123"))
            claude = put_legacy_running(self.store, object(), "p1", "t1", "claude-run", "claude", started_at="2026-08-09T00:00:00Z", session=session("claude", "abc123"))
        self.assertEqual("codex:abc123", codex["session_id"])
        self.assertEqual("claude:abc123", claude["session_id"])
        self.assertNotEqual(codex["session_id"], claude["session_id"])
        self.assertEqual("abc123", claude["provider_session_id"])

    def test_link_rejects_provider_and_project_mismatch(self):
        with patch("manager.executions.read_drive_status", return_value=quota([])):
            put_legacy_running(self.store, object(), "p1", "t1", "e-link", "codex", started_at="2026-08-09T00:00:00Z")
        with self.assertRaises(TaskError): link_execution_session(self.store, "p1", "e-link", session("claude", "s1"))
        with self.assertRaises(TaskError): link_execution_session(self.store, "p1", "e-link", session("codex", "s1", "other"))

    def test_link_is_idempotent_allows_shared_session_and_legacy_reads(self):
        with patch("manager.executions.read_drive_status", side_effect=[quota([]), quota([])]):
            put_legacy_running(self.store, object(), "p1", "t1", "e1", "codex", started_at="2026-08-09T00:00:00Z")
            second = put_legacy_running(self.store, object(), "p1", "t1", "e2", "codex", started_at="2026-08-09T00:01:00Z", session=session())
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
