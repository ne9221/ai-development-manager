import json
import io
import unittest
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from manager.quota_reader import QuotaReaderError
from manager import runtime_bridge as runtime_bridge_module
from manager.runtime_bridge import human_summary, main, read_runtime_status, resolve_project, runtime_bridge, runtime_status_contract
from manager.tasks import TaskError, create_handoff, create_project, create_task


NOW = datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name):
        if (area, project, name) not in self.records: raise TaskError("not found")
        return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task_id):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task_id]
        if not items: raise TaskError("no handoff")
        return max(items, key=lambda item: item["created_at"])
    def list_projects(self): return [deepcopy(value) for (area, _, _), value in self.records.items() if area == "projects"]


def project(active=None):
    return {"project_id": "adm", "name": "AI Development Manager", "aliases": ["ADM", "开发管理器", "development manager"], "repo": "https://github.com/example/adm", "default_branch": "main", "working_directory": "C:/adm", "baseline_commit": "abc", "runtime_ssot": "Drive", "project_rules": ["Project facts win within project scope"], "active_tasks": active or [], "current_phase": "9", "important_constraints": ["Do not touch other repos"], "execution_policies": ["ponytail"]}


def task(task_id="t1", **changes):
    value = {"task_id": task_id, "project_id": "adm", "title": "Continue bridge", "status": "ready", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "needs_repo_edit": True, "scope": ["manager/runtime_bridge.py"], "constraints": ["Task scope is narrow"], "acceptance_criteria": ["Tests pass"], "depends_on": [], "source_context": {}, "current_progress": "Bridge skeleton done", "next_action": "Add contract tests"}
    value.update(changes); return value


def quota(codex=80, claude=None, updated=NOW):
    providers = []
    for name, remaining in (("codex", codex), ("claude", claude), ("antigravity", None), ("gemini_app", None)):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100-remaining, "resets_at": None}]
        source = {"codex": "codex_app_server", "claude": "claude_code_statusline_rate_limits"}.get(name, "manual_report")
        providers.append({"provider": name, "display_name": name, "collection_mode": "automatic" if name in ("codex", "claude") else "manual", "source": source, "source_type": "official" if name in ("codex", "claude") else "manual", "confidence": "official" if windows else "unknown", "last_updated": updated.isoformat(), "status": "ok" if windows else "unknown", "windows": windows})
    return {"schema_version": "0.1.0", "generated_at": updated.isoformat(), "providers": providers}


class RuntimeBridgeTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def call(self, request, q=None): return runtime_bridge(self.store, object(), request, q or quota(), [])

    def test_alias_and_explicit_project_resolution(self):
        self.assertEqual("adm", resolve_project(self.store, "adm")["project_id"])
        self.assertEqual("adm", resolve_project(self.store, "开发管理器")["project_id"])
        self.assertEqual("adm", resolve_project(self.store, None, "请让 ADM 处理") ["project_id"])

    def test_new_task_current_quota_provider_alternatives_and_json(self):
        result = self.call({"project_id": "ADM", "user_request": "Implement runtime bridge", "task_type": "implementation", "complexity": "medium"}, quota(80, 60))
        self.assertEqual("new_task", result["request_type"]); self.assertEqual("codex", result["recommended_provider"])
        self.assertTrue(result["alternatives"]); self.assertEqual("fresh", result["quota_freshness"]); self.assertIn("80% remaining", result["quota_summary"])
        self.assertIsInstance(json.loads(json.dumps(result)), dict); self.assertIn("推荐：codex", human_summary(result))

    def test_read_only_new_task_does_not_write_runtime_records(self):
        before = deepcopy(self.store.records)
        result = runtime_bridge(self.store, object(), {"project_id": "adm", "user_request": "Cloud read-only dispatch"}, quota(), [], read_only=True)
        self.assertEqual("new_task", result["request_type"]); self.assertEqual(before, self.store.records)
        self.assertEqual("cloud-read-only-dispatch", result["active_task"]["task_id"])

    def test_continuation_task_handoff_and_no_handoff(self):
        create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        no_handoff = self.call({"project_id": "adm", "task_id": "t1", "user_request": "继续"})
        self.assertEqual("continuation", no_handoff["request_type"]); self.assertIsNone(no_handoff["latest_handoff_summary"]); self.assertEqual("Add contract tests", no_handoff["next_action"])
        create_handoff(self.store, {"handoff_id": "h1", "task_id": "t1", "project_id": "adm", "from_provider": "codex", "to_provider": "claude", "from_session": "a", "reason": "switch", "completed_work": [], "current_state": "Parser done", "files_changed": [], "commits": [], "tests": [], "known_issues": [], "do_not_touch": ["quota collectors"], "next_action": "Add tests", "acceptance_criteria": [], "minimal_context": "Continue from bridge skeleton. token=sensitive"})
        continued = self.call({"project_id": "adm", "user_request": "Continue bridge"})
        self.assertEqual("h1", continued["latest_handoff_summary"]["handoff_id"]); self.assertIn("Continue from bridge skeleton", continued["generated_prompt"])
        self.assertNotIn("sensitive", json.dumps(continued))

    def test_status_split_stale_unknown_and_safety(self):
        """DASHBOARD_TRUTH_CONNECTED gate 1: quota state must never block
        Task admission. Stale quota (every provider's last_updated 2 hours
        old) used to make dispatch() raise "no eligible provider" before any
        Task was created; now the Task is durably admitted as waiting_quota
        instead, and the secret in the task's own constraints never leaks
        into the (now absent) generated prompt or any other returned field."""
        create_task(self.store, task(expected_minutes=45, constraints=["token=sensitive", "Task scope is narrow"]), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        result = self.call({"project_id": "adm", "task_id": "t1", "user_request": "进度状态"}, quota(updated=NOW-timedelta(hours=2)))
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])
        self.assertIsNone(result["generated_prompt"])
        blob = json.dumps(result)
        self.assertNotIn("token=sensitive", blob)

    def test_shared_project_task_rule_priority_in_prompt(self):
        create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = ["t1"]
        prompt = self.call({"project_id": "adm", "task_id": "t1", "user_request": "继续"})["generated_prompt"]
        self.assertLess(prompt.index("Project business / acceptance:"), prompt.index("AI Development Manager scope / protection:")); self.assertLess(prompt.index("AI Development Manager scope / protection:"), prompt.index("Ponytail minimal-change preference"))
        self.assertIn("20 minutes", prompt); self.assertIn("Task scope is narrow", prompt); self.assertIn("smallest safe change", prompt)
        self.assertNotIn("## Persistence", prompt)

    def test_ponytail_research_and_unavailable_fallback(self):
        coding = self.call({"project_id": "adm", "user_request": "Fix regression safely", "task_type": "regression", "complexity": "medium", "ponytail_available": False})["generated_prompt"]
        self.assertIn("Ponytail skill is unavailable", coding); self.assertIn("smallest safe change", coding)
        self.store.records[("projects", "adm", "adm")]["active_tasks"] = []
        research = self.call({"project_id": "adm", "user_request": "Research architecture options", "task_type": "research", "complexity": "medium"})["generated_prompt"]
        self.assertNotIn("Ponytail minimal-change preference", research)

    def test_preferred_and_excluded(self):
        with self.assertRaisesRegex(TaskError, "preferred Claude provider has no reliable quota"):
            self.call({"project_id": "adm", "user_request": "Research design", "task_type": "research", "complexity": "medium", "preferred_provider": "claude"}, quota(50, None))
        excluded = self.call({"project_id": "adm", "user_request": "Implement second bridge", "task_type": "implementation", "complexity": "medium", "excluded_provider": "codex"}, quota(90, 80))
        self.assertNotEqual("codex", excluded["recommended_provider"])

    def test_multi_task_reuses_scheduler(self):
        ready = create_task(self.store, task(), assign=False); self.store.records[("projects", "adm", "adm")]["active_tasks"] = [ready["task_id"]]
        called = []
        def fake_schedule(store, service, project_id, tasks, raw, history, dispatch_func=None):
            called.append((project_id, len(tasks), bool(dispatch_func)))
            dispatcher_result = {"recommended_provider": "codex", "mode": "code", "effort": "medium", "estimated_minutes": 10, "split_recommended": False, "alternatives": ["claude"], "quota_summary": "primary: 80% remaining; official; fresh", "warnings": [], "generated_prompt": "scheduler reused prompt"}
            return {"execution_batches": [{"batch": 1, "tasks": [{"task_id": "t1", "recommended_provider": "codex", "dispatcher_result": dispatcher_result}]}], "deferred_tasks": [], "warnings": []}
        result = runtime_bridge(self.store, object(), {"project_id": "adm", "user_request": "安排全部任务", "multi_task": True}, quota(), [], schedule_func=fake_schedule)
        self.assertEqual("scheduling", result["request_type"]); self.assertEqual([("adm", 1, True)], called); self.assertEqual("scheduler reused prompt", result["generated_prompt"])

    def test_malformed_input(self):
        with self.assertRaises(TaskError): runtime_bridge(self.store, object(), {}, quota(), [])
        with self.assertRaises(TaskError): runtime_bridge(self.store, object(), {"project_id": "missing", "user_request": "work"}, quota(), [])


class RuntimeStatusContractTests(unittest.TestCase):
    def contract(self, **changes):
        document = quota(codex=80, claude=60, updated=NOW)
        for provider in document["providers"]:
            provider.update(changes.get(provider["provider"], {}))
        return runtime_status_contract(document, now=NOW)

    def test_codex_and_claude_known(self):
        result = self.contract()
        self.assertEqual("known", result["providers"]["codex"]["status"])
        self.assertEqual(80, result["providers"]["codex"]["windows"][0]["remaining_percent"])
        self.assertEqual("known", result["providers"]["claude"]["status"])
        self.assertEqual(60, result["providers"]["claude"]["windows"][0]["remaining_percent"])

    def test_unknown_and_stale_are_not_zero(self):
        unknown = self.contract(claude={"status": "unknown", "windows": []})
        self.assertEqual("unknown", unknown["providers"]["claude"]["status"])
        self.assertEqual([], unknown["providers"]["claude"]["windows"])
        stale = runtime_status_contract(quota(updated=NOW - timedelta(hours=2)), now=NOW)
        self.assertEqual("stale", stale["providers"]["codex"]["status"])
        self.assertEqual("stale", stale["providers"]["codex"]["freshness"])

    def test_unofficial_sources_never_become_known(self):
        for source_type, confidence, source in (
            ("local_estimate", "local_estimate", "claude_code_jsonl"),
            ("manual", "manual", "manual_report"),
            ("official", "official", "synthetic_source"),
        ):
            result = self.contract(claude={"source_type": source_type, "confidence": confidence, "source": source})
            self.assertEqual("unknown", result["providers"]["claude"]["status"])
            self.assertEqual([], result["providers"]["claude"]["windows"])
            self.assertEqual("unknown", result["providers"]["claude"]["source"])

    def test_future_timestamp_tolerance_and_malformed_timestamp(self):
        small_skew = runtime_status_contract(quota(codex=80, claude=60, updated=NOW + timedelta(minutes=1)), now=NOW)
        self.assertEqual("known", small_skew["providers"]["codex"]["status"])
        far_future = runtime_status_contract(quota(codex=80, claude=60, updated=NOW + timedelta(days=36500)), now=NOW)
        self.assertEqual("unavailable", far_future["providers"]["codex"]["status"])
        malformed = quota(codex=80, claude=60)
        malformed["providers"][0]["last_updated"] = "not-a-time"
        result = runtime_status_contract(malformed, now=NOW)
        self.assertEqual("unavailable", result["providers"]["codex"]["status"])
        self.assertEqual("known", result["providers"]["claude"]["status"])

    def test_future_clock_skew_exact_boundary_is_pinned(self):
        exactly_five = runtime_status_contract(quota(codex=80, claude=60, updated=NOW + timedelta(minutes=5)), now=NOW)
        self.assertEqual("known", exactly_five["providers"]["codex"]["status"])
        one_second_over = runtime_status_contract(quota(codex=80, claude=60, updated=NOW + timedelta(minutes=5, seconds=1)), now=NOW)
        self.assertEqual("unavailable", one_second_over["providers"]["codex"]["status"])
        self.assertEqual([], one_second_over["providers"]["codex"]["windows"])

    def test_stale_threshold_exact_boundary_is_pinned(self):
        exactly_max_age = runtime_status_contract(quota(codex=80, claude=60, updated=NOW - timedelta(minutes=60)), max_age_minutes=60, now=NOW)
        self.assertEqual("known", exactly_max_age["providers"]["codex"]["status"])
        slightly_over = runtime_status_contract(quota(codex=80, claude=60, updated=NOW - timedelta(minutes=60, seconds=1)), max_age_minutes=60, now=NOW)
        self.assertEqual("stale", slightly_over["providers"]["codex"]["status"])
        self.assertNotEqual([], slightly_over["providers"]["codex"]["windows"])

    def test_nan_and_infinite_percentages_never_become_known(self):
        for bad_value in (float("nan"), float("inf"), float("-inf")):
            document = quota(codex=80, claude=60)
            document["providers"][0]["windows"][0]["remaining_percent"] = bad_value
            result = runtime_status_contract(document, now=NOW)
            self.assertEqual("unavailable", result["providers"]["codex"]["status"])
            self.assertEqual([], result["providers"]["codex"]["windows"])
            self.assertEqual("known", result["providers"]["claude"]["status"])

    def test_newline_and_control_character_free_text_is_whitelisted(self):
        for attack in ("codex_app_server\nBearer secret", "primary\x00leak", "line1\r\nline2\tsecret"):
            document = quota(codex=80, claude=60)
            document["providers"][0]["source"] = attack
            document["providers"][0]["windows"][0]["name"] = attack
            serialized = json.dumps(runtime_status_contract(document, now=NOW))
            self.assertNotIn(attack, serialized)

    def test_missing_provider_is_unavailable(self):
        document = quota()
        document["providers"] = [item for item in document["providers"] if item["provider"] != "claude"]
        result = runtime_status_contract(document, now=NOW)
        self.assertEqual("unavailable", result["providers"]["claude"]["status"])
        self.assertIsNone(result["providers"]["claude"]["last_updated"])

    def test_contract_keys_are_stable_and_bounded(self):
        result = self.contract()
        self.assertEqual({"contract_version", "schema_version", "generated_at", "providers"}, set(result))
        self.assertEqual({"codex", "claude"}, set(result["providers"]))
        for provider in result["providers"].values():
            self.assertEqual({"status", "windows", "source", "last_updated", "freshness"}, set(provider))
        self.assertLessEqual(len(result["providers"]["codex"]["windows"]), 8)

    def test_window_bounds_safe_names_and_duplicate_first_wins(self):
        document = quota(codex=80, claude=60)
        document["providers"][0]["windows"] = [
            {"name": "primary", "used_percent": index, "remaining_percent": 100-index, "resets_at": None}
            for index in range(2)
        ] + [{"name": f"Bearer secret {index}", "used_percent": 20, "remaining_percent": 80} for index in range(10000)]
        result = runtime_status_contract(document, now=NOW)["providers"]["codex"]
        self.assertEqual(8, len(result["windows"]))
        self.assertEqual(0, result["windows"][0]["used_percent"])
        self.assertNotIn("Bearer", json.dumps(result))

    def test_duplicate_provider_isolated(self):
        document = quota(codex=80, claude=60)
        document["providers"].append(deepcopy(document["providers"][0]))
        result = runtime_status_contract(document, now=NOW)
        self.assertEqual("unavailable", result["providers"]["codex"]["status"])
        self.assertEqual("known", result["providers"]["claude"]["status"])

    def test_no_metadata_secrets_or_raw_payload_leak(self):
        document = quota(codex=80, claude=60)
        document["providers"][0]["metadata"] = {"raw_payload": {"access_token": "raw-secret"}}
        document["providers"][0]["source"] = "codex_app_server token=raw-secret"
        serialized = json.dumps(runtime_status_contract(document, now=NOW))
        self.assertNotIn("metadata", serialized)
        self.assertNotIn("raw_payload", serialized)
        self.assertNotIn("raw-secret", serialized)

    def test_free_text_source_and_window_names_are_whitelisted(self):
        attacks = [
            "Bearer sk-live-secret", "sk-proj-live-secret", "user@example.com",
            "C:/Users/Alice/private", "/home/alice/private", "account customer-12345",
            "x" * 10000,
        ]
        for attack in attacks:
            document = quota(codex=80, claude=60)
            document["providers"][0]["source"] = attack
            document["providers"][0]["windows"][0]["name"] = attack
            serialized = json.dumps(runtime_status_contract(document, now=NOW))
            self.assertNotIn(attack, serialized)
        document = quota(codex=80, claude=60)
        document["providers"][0]["windows"][0]["name"] = "Bearer sk-live-secret"
        codex = runtime_status_contract(document, now=NOW)["providers"]["codex"]
        self.assertEqual("window_1", codex["windows"][0]["name"])

    def test_malformed_python_provider_is_isolated(self):
        invalid_windows = [
            {"name": {"email": "leak@example.com"}, "remaining_percent": 80},
            {"name": "primary", "remaining_percent": -1},
            {"name": "primary", "remaining_percent": 101},
            {"name": "primary", "remaining_percent": "80"},
            None, [], "bad",
        ]
        for invalid in invalid_windows:
            document = quota(codex=80, claude=60)
            document["providers"][0]["windows"] = [invalid]
            result = runtime_status_contract(document, now=NOW)
            self.assertEqual("unavailable", result["providers"]["codex"]["status"])
            self.assertEqual("known", result["providers"]["claude"]["status"])

    def test_public_loader_sanitizes_drive_failures(self):
        for reader in (
            lambda **_: (_ for _ in ()).throw(RuntimeError("Bearer backend-secret")),
            lambda **_: None,
            lambda **_: {"schema_version": "0.1.0", "generated_at": "bad", "providers": []},
        ):
            result = read_runtime_status(reader=reader, now=NOW)
            self.assertEqual("unavailable", result["providers"]["codex"]["status"])

        class BadService:
            def files(self): raise RuntimeError("token=raw-secret")
        result = read_runtime_status(service=BadService(), now=NOW)
        self.assertEqual("unavailable", result["providers"]["codex"]["status"])

        class FailedRequest:
            def execute(self): raise RuntimeError("Bearer execute-secret")
        class FailedFiles:
            def list(self, **_): return FailedRequest()
        class FailedExecuteService:
            def files(self): return FailedFiles()
        result = read_runtime_status(service=FailedExecuteService(), now=NOW)
        self.assertEqual("unavailable", result["providers"]["claude"]["status"])

        class ResultRequest:
            def __init__(self, value=None, error=None): self.value, self.error = value, error
            def execute(self):
                if self.error: raise self.error
                return self.value
        class FailedGetFiles:
            def list(self, **_): return ResultRequest({"files": [{"id": "status"}]})
            def get_media(self, **_): return ResultRequest(error=RuntimeError("sk-get-secret"))
        class FailedGetService:
            def files(self): return FailedGetFiles()
        result = read_runtime_status(service=FailedGetService(), now=NOW)
        self.assertEqual("unavailable", result["providers"]["codex"]["status"])

        class MalformedFiles(FailedGetFiles):
            def get_media(self, **_): return ResultRequest(b"not json")
        class MalformedService:
            def files(self): return MalformedFiles()
        result = read_runtime_status(service=MalformedService(), now=NOW)
        self.assertEqual("unavailable", result["providers"]["codex"]["status"])

    def test_final_contract_validator_regression_fails_closed(self):
        """A maintenance regression in the internal window-name allowlist must not
        let the public loader raise; it must fail closed to unavailable, log only
        the sanitized exception type, and keep the CLI's stdout pure JSON."""
        original_names = runtime_bridge_module.RUNTIME_STATUS_WINDOW_NAMES["codex"]
        runtime_bridge_module.RUNTIME_STATUS_WINDOW_NAMES["codex"] = {"not a valid window name!"}
        try:
            document = quota(codex=80, claude=60)
            document["providers"][0]["windows"][0]["name"] = "not a valid window name!"
            with self.assertLogs("runtime_bridge", level="WARNING") as captured:
                result = read_runtime_status(reader=lambda **_: document, now=NOW)
            self.assertEqual("unavailable", result["providers"]["codex"]["status"])
            self.assertEqual("unavailable", result["providers"]["claude"]["status"])
            self.assertEqual([], result["providers"]["codex"]["windows"])
            joined_log = "\n".join(captured.output)
            self.assertIn("RuntimeError", joined_log)
            self.assertNotIn("not a valid window name", joined_log)

            output, errors = io.StringIO(), io.StringIO()
            with patch("manager.runtime_bridge.read_drive_status", side_effect=lambda **_: document), \
                 redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(0, main(["status", "--json"]))
            parsed = json.loads(output.getvalue())
            self.assertEqual("unavailable", parsed["providers"]["codex"]["status"])
            self.assertNotIn("not a valid window name", output.getvalue())
            self.assertNotIn("not a valid window name", errors.getvalue())
        finally:
            runtime_bridge_module.RUNTIME_STATUS_WINDOW_NAMES["codex"] = original_names

    def test_missing_and_malformed_drive_status_emit_unavailable_json(self):
        for message in ("found 0", "malformed token=raw-secret payload"):
            output = io.StringIO()
            with patch("manager.runtime_bridge.build_service", return_value=object()), \
                 patch("manager.runtime_bridge.read_drive_status", side_effect=QuotaReaderError(message)), \
                 redirect_stdout(output):
                self.assertEqual(0, main(["status", "--json"]))
            result = json.loads(output.getvalue())
            self.assertEqual("unavailable", result["providers"]["codex"]["status"])
            self.assertEqual("unavailable", result["providers"]["claude"]["status"])
            self.assertNotIn("raw-secret", output.getvalue())

    def test_cli_stdout_is_pure_json_and_stderr_has_no_backend_detail(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("manager.runtime_bridge.read_drive_status", side_effect=RuntimeError("Bearer backend-secret")), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(0, main(["status", "--json"]))
        json.loads(output.getvalue())
        self.assertEqual("", errors.getvalue())
        self.assertNotIn("backend-secret", output.getvalue())


if __name__ == "__main__": unittest.main()
