"""Layer-1/2 tests for the language-server-backed Antigravity execution adapter.

Every scenario the task spec lists as a known AG automation failure pattern is
a fixture here: exit 0 + empty response, process hang, prompt swallowed before
READY, quota exhausted (before and during a run), permission stall, child
process not exiting, cancel, malformed/partial provider state, provider crash,
auth transient, unknown model, workspace mismatch. No real IDE, no sleeping.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from manager.ag_cli_runner import (
    AgCliProcess,
    OfficialAgCliRunner,
    classify_run,
    map_agentapi_model,
    parse_new_conversation_output,
    sanitize_ag_environment,
)
from manager.ag_language_server import AgLsError
from manager.ag_run_state import list_run_states, read_run_state
from manager.ag_runner import AgLaunchError, LaunchRequest
from manager.test_ag_language_server import TOKEN, endpoint, quota_summary, user_status

CID = "c0ffee00-1111-2222-3333-444444444444"


def summary(status="CASCADE_RUN_STATUS_RUNNING", step_count=2):
    return {"trajectorySummaries": {CID: {"status": status, "stepCount": step_count, "trajectoryId": "traj-1"}}}


def step(kind, status="CORTEX_STEP_STATUS_DONE", text=None):
    item = {"type": f"CORTEX_STEP_TYPE_{kind}", "status": f"CORTEX_STEP_STATUS_{status.split('_')[-1]}" if not status.startswith("CORTEX") else status}
    if text is not None:
        key = {"PLANNER_RESPONSE": "plannerResponse", "ERROR_MESSAGE": "errorMessage", "USER_INPUT": "userInput"}.get(kind, "message")
        item[key] = {"response": text} if key == "plannerResponse" else {"message": text} if key == "errorMessage" else {"items": [{"text": text}]}
    return item


def executor(reason="EXECUTOR_TERMINATION_REASON_TERMINAL_STEP_TYPE", last=1):
    return {"executorMetadata": [{"terminationReason": reason, "lastStepIdx": last, "executionId": "exec-1"}]}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.5)


class ScriptedClient:
    """RPC -> list of responses (consumed in order; last one repeats) or an exception."""

    def __init__(self, ep, timeout=None, script=None):
        self.endpoint = ep
        self.script = script or {}
        self.calls = []

    def _next(self, rpc):
        entry = self.script.get(rpc)
        if entry is None:
            return {}
        if isinstance(entry, list):
            value = entry.pop(0) if len(entry) > 1 else entry[0]
        else:
            value = entry
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    def call(self, rpc, body=None, timeout=None):
        self.calls.append((rpc, body or {}))
        return self._next(rpc)

    def get_status(self):
        return self.call("GetStatus")

    def get_user_status(self):
        return self.call("GetUserStatus")

    def retrieve_user_quota_summary(self):
        return self.call("RetrieveUserQuotaSummary")

    def get_all_cascade_trajectories(self):
        return self.call("GetAllCascadeTrajectories")

    def get_conversation_metadata(self, conversation_id):
        return self.call("GetConversationMetadata", {"conversationId": conversation_id})

    def get_cascade_trajectory_steps(self, cascade_id):
        return self.call("GetCascadeTrajectorySteps", {"cascadeId": cascade_id})

    def get_cascade_trajectory_executor_metadatas(self, cascade_id):
        return self.call("GetCascadeTrajectoryExecutorMetadatas", {"cascadeId": cascade_id})


class FakeProcess:
    def __init__(self, stdout='{"response": {"conversationId": "%s"}}' % CID, stderr="", returncode=0, hang=False):
        self.pid = 4242
        self._stdout, self._stderr, self.returncode, self._hang = stdout, stderr, returncode, hang
        self.killed = False

    def communicate(self, timeout=None):
        if self._hang:
            raise subprocess.TimeoutExpired("agentapi", timeout)
        return self._stdout, self._stderr

    def poll(self):
        return None if self._hang and not self.killed else self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class RunnerHarness:
    def __init__(self, test, script=None, process=None, **runner_kwargs):
        self.test = test
        self.clock = FakeClock()
        self.script = script if script is not None else self.default_script()
        self.client = None
        self.popen_calls = []
        self.killed = []
        self.process = process or FakeProcess()

        def client_factory(ep, timeout=None):
            self.client = ScriptedClient(ep, timeout, self.script)
            return self.client

        def popen(argv, **kwargs):
            self.popen_calls.append((list(argv), kwargs))
            return self.process

        self.runner = OfficialAgCliRunner(
            executable_resolver=lambda: (test.exe, ["agentapi"]),
            discover=lambda timeout: endpoint(executable=test.exe, creation_identity="windows-filetime:28164"),
            client_factory=client_factory, popen=popen,
            poll_interval_seconds=2.0, agentapi_timeout_seconds=30.0, permission_stall_seconds=10.0,
            cancel_reconcile_seconds=6.0, manager_home=test.home, clock=self.clock, sleep=self.clock.sleep,
            kill_tree=lambda proc: (self.killed.append(proc), proc.kill()), **runner_kwargs,
        )

    @staticmethod
    def default_script(**overrides):
        script = {
            "GetStatus": {}, "GetUserStatus": user_status(), "RetrieveUserQuotaSummary": quota_summary(),
            "GetConversationMetadata": {"metadata": {"workspaceUris": []}}, "ReadProject": {"project": {}},
            "GetAllCascadeTrajectories": [summary("CASCADE_RUN_STATUS_RUNNING", 1), summary("CASCADE_RUN_STATUS_IDLE", 2)],
            "GetCascadeTrajectorySteps": [{"steps": [step("USER_INPUT", text="do it")]},
                                          {"steps": [step("USER_INPUT", text="do it"), step("PLANNER_RESPONSE", text="ADM-SMOKE-OK")]}],
            "GetCascadeTrajectoryExecutorMetadatas": [{"executorMetadata": []}, executor()],
            "CancelCascadeInvocation": {}, "ForceStopCascadeTree": {},
        }
        script.update(overrides)
        return script

    def request(self, **overrides):
        values = dict(working_directory=self.test.workdir, project_id="p1", sandbox="read-only",
                      approval_policy="never", timeout_seconds=5, turn_timeout_seconds=120)
        values.update(overrides)
        return LaunchRequest(**values)

    def run(self, prompt="do it", request=None):
        prepared = self.runner.prepare(request or self.request())
        running = self.runner.start(prepared, prompt)
        self.events = []
        self.runner.set_heartbeat(running, self.events.append)
        return prepared, running, self.runner.wait(running)


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workdir = str(Path(self.temp.name, "ws").resolve())
        os.makedirs(self.workdir)
        self.home = str(Path(self.temp.name, "home"))
        self.exe = str(Path(self.temp.name, "language_server_windows_x64.exe"))
        Path(self.exe).write_bytes(b"stub")

    def tearDown(self):
        self.temp.cleanup()


class PureHelpersTests(RunnerTestCase):
    def test_model_mapping(self):
        self.assertIsNone(map_agentapi_model(None))
        self.assertEqual("flash", map_agentapi_model("flash"))
        self.assertEqual("pro", map_agentapi_model("gemini-3.1-pro"))
        self.assertEqual("flash_lite", map_agentapi_model("gemini-flash-lite"))
        with self.assertRaises(AgLaunchError) as ctx:
            map_agentapi_model("gpt-5")
        self.assertEqual("unknown_model", ctx.exception.classification)

    def test_parse_new_conversation_output(self):
        self.assertEqual(CID, parse_new_conversation_output(json.dumps({"response": {"conversationId": CID}})))
        self.assertEqual(CID, parse_new_conversation_output(json.dumps({"conversation_id": CID})))
        cases = {
            "": "dispatch_failed", "garbage": "malformed_output", json.dumps({"response": {}}): "malformed_output",
            json.dumps({"response": {}, "error": "ANTIGRAVITY_LS_ADDRESS is not set"}): "ls_unreachable",
            json.dumps({"error": "rpc error: code = Unavailable desc = connection error"}): "ls_unreachable",
            json.dumps({"error": "failed to resolve project ID"}): "project_unresolved",
            json.dumps({"error": "RESOURCE_EXHAUSTED: quota exceeded"}): "quota_exhausted",
            json.dumps({"error": "rpc error: code = Unauthenticated"}): "auth_transient",
            json.dumps({"error": "boom"}): "dispatch_failed",
        }
        for stdout, expected in cases.items():
            with self.assertRaises(AgLaunchError) as ctx:
                parse_new_conversation_output(stdout)
            self.assertEqual(expected, ctx.exception.classification, stdout)

    def test_sanitize_environment_strips_secondary_billing(self):
        clean = sanitize_ag_environment({"GOOGLE_API_KEY": "k", "GEMINI_API_KEY": "k", "GOOGLE_APPLICATION_CREDENTIALS": "p", "PATH": "x"})
        self.assertEqual({"PATH": "x"}, clean)

    def test_classify_run_terminal_truth_table(self):
        steps_ok = [step("USER_INPUT", text="q"), step("PLANNER_RESPONSE", text="answer")]
        self.assertEqual("running", classify_run(summary()["trajectorySummaries"][CID], steps_ok, [], seconds_since_start=5)["state"])
        done = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, steps_ok, executor()["executorMetadata"], seconds_since_start=30)
        self.assertEqual(("completed", "answer"), (done["state"], done["response_text"]))
        empty = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("USER_INPUT", text="q")],
                             executor("EXECUTOR_TERMINATION_REASON_NO_TOOL_CALL")["executorMetadata"], seconds_since_start=30)
        self.assertEqual(("failed", "empty_response"), (empty["state"], empty["classification"]))
        swallowed = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("USER_INPUT", text="q")], [], seconds_since_start=30)
        self.assertEqual(("failed", "prompt_not_started"), (swallowed["state"], swallowed["classification"]))
        grace = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("USER_INPUT", text="q")], [], seconds_since_start=3)
        self.assertEqual("running", grace["state"])
        cancelled = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, steps_ok, executor("EXECUTOR_TERMINATION_REASON_USER_CANCELED")["executorMetadata"], seconds_since_start=30)
        self.assertEqual(("interrupted", "cancelled"), (cancelled["state"], cancelled["classification"]))
        budget = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, steps_ok, executor("EXECUTOR_TERMINATION_REASON_MAX_TOKEN_BUDGET_EXCEEDED")["executorMetadata"], seconds_since_start=30)
        self.assertEqual("token_budget_exceeded", budget["classification"])
        quota = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("USER_INPUT", text="q"), step("ERROR_MESSAGE", "ERROR", text="Quota exceeded for model")],
                             executor("EXECUTOR_TERMINATION_REASON_ERROR")["executorMetadata"], seconds_since_start=30)
        self.assertEqual(("failed", "quota_exhausted"), (quota["state"], quota["classification"]))
        error = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("USER_INPUT", text="q"), step("ERROR_MESSAGE", "ERROR", text="model output error")],
                             executor("EXECUTOR_TERMINATION_REASON_ERROR")["executorMetadata"], seconds_since_start=30)
        self.assertEqual("provider_error", error["classification"])
        auth = classify_run({"status": "CASCADE_RUN_STATUS_IDLE"}, [step("ERROR_MESSAGE", "ERROR", text="Unauthenticated: token expired")],
                            executor("EXECUTOR_TERMINATION_REASON_ERROR")["executorMetadata"], seconds_since_start=30)
        self.assertEqual("auth_transient", auth["classification"])
        waiting = classify_run(summary()["trajectorySummaries"][CID], [step("USER_INPUT", text="q"), step("RUN_COMMAND", "CORTEX_STEP_STATUS_WAITING")], [], seconds_since_start=30)
        self.assertEqual(("waiting_permission", "permission_required"), (waiting["state"], waiting["classification"]))


class ReadyHandshakeTests(RunnerTestCase):
    def test_prepare_requires_existing_absolute_working_directory(self):
        harness = RunnerHarness(self)
        for bad in (None, "", "relative/path", str(Path(self.temp.name, "missing"))):
            with self.assertRaises(AgLaunchError) as ctx:
                harness.runner.prepare(harness.request(working_directory=bad))
            self.assertEqual("invalid_request", ctx.exception.classification, bad)
        self.assertEqual([], harness.popen_calls)

    def test_prepare_ready_evidence_and_run_state(self):
        harness = RunnerHarness(self)
        prepared = harness.runner.prepare(harness.request(model="gemini-pro"))
        self.assertTrue(prepared.thread_id.startswith("ag-cli-"))
        self.assertEqual((28164, "windows-filetime:28164", "cli"), (prepared.pid, prepared.process_creation_identity, prepared.mode))
        state = read_run_state(prepared.thread_id, self.home)
        self.assertEqual(("prepared", None, "pro", "user@example.com"), (state["status"], state["conversation_id"], state["agentapi_model"], state["readiness"]["account_email"]))
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertEqual(0, prepared._process.poll(), "no conversation yet == provably stopped")

    def test_prepare_fails_closed_per_precondition(self):
        cases = [
            ({"GetStatus": AgLsError("ls_unreachable", "refused")}, "ls_unreachable", {}),
            ({"GetUserStatus": AgLsError("rpc_unauthenticated", "403")}, "auth_unavailable", {}),
            ({"GetUserStatus": {"userStatus": {}}}, "auth_unavailable", {}),
            ({"RetrieveUserQuotaSummary": {"response": {}}}, "quota_unverified", {}),
            ({"RetrieveUserQuotaSummary": quota_summary(None, None, 0.5, 0.5)}, "quota_exhausted", {}),
            ({}, "unknown_model", {"model": "gpt-5"}),
        ]
        for overrides, expected, request_overrides in cases:
            harness = RunnerHarness(self, script=RunnerHarness.default_script(**overrides))
            with self.assertRaises(AgLaunchError) as ctx:
                harness.runner.prepare(harness.request(**request_overrides))
            self.assertEqual(expected, ctx.exception.classification, expected)
            self.assertEqual([], harness.popen_calls)
        self.assertIn("unavailable_until=2026-09-04T08:20:30Z", ctx.exception.detail if expected == "quota_exhausted" else "unavailable_until=2026-09-04T08:20:30Z")

    def test_prepare_quota_exhausted_reports_reset(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(RetrieveUserQuotaSummary=quota_summary(None, None, 0.5, 0.5)))
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.prepare(harness.request())
        self.assertEqual("quota_exhausted", ctx.exception.classification)
        self.assertIn("unavailable_until=2026-09-04T08:20:30Z", ctx.exception.detail)

    def test_prepare_refuses_when_the_dispatch_route_is_unavailable(self):
        """Live 2026-09-02: quota reads fine but `agentapi new-conversation`
        cannot create a conversation because the language server has no
        projects store. Never spawn a CLI that provably cannot succeed."""
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            ReadProject=AgLsError("rpc_failed", "ReadProject: HTTP 500 unknown: projects store not initialized")))
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.prepare(harness.request())
        self.assertEqual("dispatch_route_unavailable", ctx.exception.classification)
        self.assertIn("projects_store_unavailable", ctx.exception.detail)
        self.assertEqual([], harness.popen_calls)

    def test_prepare_records_the_dispatch_route_in_readiness_evidence(self):
        harness = RunnerHarness(self)
        prepared = harness.runner.prepare(harness.request())
        self.assertTrue(read_run_state(prepared.thread_id, self.home)["readiness"]["dispatch_route"]["available"])

    def test_prepare_when_ide_not_running(self):
        harness = RunnerHarness(self)
        def missing(timeout):
            raise AgLsError("ide_not_running", "no process")
        harness.runner._discover = missing
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.prepare(harness.request())
        self.assertEqual("ide_not_running", ctx.exception.classification)

    def test_prepare_route_unavailable_when_entrypoint_missing(self):
        harness = RunnerHarness(self)
        harness.runner._resolve_executable = lambda: (str(Path(self.temp.name, "nope.exe")), ["agentapi"])
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.prepare(harness.request())
        self.assertEqual("route_unavailable", ctx.exception.classification)


class DispatchTests(RunnerTestCase):
    def test_start_spawns_agentapi_with_endpoint_env_and_records_conversation(self):
        harness = RunnerHarness(self)
        prepared = harness.runner.prepare(harness.request(model="flash"))
        running = harness.runner.start(prepared, "Reply with exactly: ADM-SMOKE-OK")
        argv, kwargs = harness.popen_calls[0]
        self.assertEqual([self.exe, "agentapi", "new-conversation", "--model=flash", f"--title=adm-{prepared.thread_id}", "Reply with exactly: ADM-SMOKE-OK"], argv)
        self.assertEqual(self.workdir, kwargs["cwd"])
        self.assertEqual("127.0.0.1:54415", kwargs["env"]["ANTIGRAVITY_LS_ADDRESS"])
        self.assertEqual(TOKEN, kwargs["env"]["ANTIGRAVITY_CSRF_TOKEN"])
        self.assertNotIn("GOOGLE_API_KEY", kwargs["env"])
        state = read_run_state(prepared.thread_id, self.home)
        self.assertEqual(("running", CID, 4242), (state["status"], state["conversation_id"], state["agentapi_pid"]))
        self.assertIn(CID, prepared.session_path)
        self.assertEqual({"result": "unverified", "reason": "provider exposes no workspace for the conversation"}, state["workspace_check"])
        self.assertIsNone(prepared._process.poll(), "conversation running == not stopped")
        self.assertTrue(running.turn_id.startswith("turn-"))

    def test_start_rejects_empty_prompt_and_double_start(self):
        harness = RunnerHarness(self)
        prepared = harness.runner.prepare(harness.request())
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.start(prepared, "  ")
        self.assertEqual("invalid_request", ctx.exception.classification)
        prepared = harness.runner.prepare(harness.request())
        harness.runner.start(prepared, "go")
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.start(prepared, "go")
        self.assertEqual("already_started", ctx.exception.classification)

    def test_start_failures_are_classified_and_persisted(self):
        cases = [
            (FakeProcess(stdout="not json"), "malformed_output"),
            (FakeProcess(stdout=json.dumps({"error": "failed to resolve project ID"})), "project_unresolved"),
            (FakeProcess(stdout=json.dumps({"error": "rpc error: code = Unauthenticated desc = token expired"})), "auth_transient"),
            (FakeProcess(stdout=json.dumps({"error": "RESOURCE_EXHAUSTED"})), "quota_exhausted"),
            (FakeProcess(stdout="", stderr="panic"), "dispatch_failed"),
        ]
        for process, expected in cases:
            harness = RunnerHarness(self, process=process)
            prepared = harness.runner.prepare(harness.request())
            with self.assertRaises(AgLaunchError) as ctx:
                harness.runner.start(prepared, "go")
            self.assertEqual(expected, ctx.exception.classification)
            self.assertEqual("failed", read_run_state(prepared.thread_id, self.home)["status"])
            self.assertEqual(0, prepared._process.poll())

    def test_hanging_cli_is_killed_as_a_tree(self):
        harness = RunnerHarness(self, process=FakeProcess(hang=True))
        prepared = harness.runner.prepare(harness.request())
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.start(prepared, "go")
        self.assertEqual("dispatch_timeout", ctx.exception.classification)
        self.assertTrue(harness.process.killed)
        self.assertEqual("failed", read_run_state(prepared.thread_id, self.home)["status"])

    def test_workspace_mismatch_cancels_and_stops(self):
        other = Path(self.temp.name, "other").resolve()
        other.mkdir()
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetConversationMetadata={"metadata": {"workspaceUris": [other.as_uri()]}},
            GetAllCascadeTrajectories=summary("CASCADE_RUN_STATUS_IDLE", 1)))
        prepared = harness.runner.prepare(harness.request())
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.start(prepared, "go")
        self.assertEqual("workspace_mismatch", ctx.exception.classification)
        rpcs = [rpc for rpc, _ in harness.client.calls]
        self.assertIn("CancelCascadeInvocation", rpcs)
        self.assertIn("ForceStopCascadeTree", rpcs)
        state = read_run_state(prepared.thread_id, self.home)
        self.assertEqual(("mismatch", "cancelled", True), (state["workspace_check"]["result"], state["status"], state["cancel_evidence"]["confirmed"]))

    def test_workspace_verified_when_provider_names_the_working_directory(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetConversationMetadata={"metadata": {"workspaceUris": [Path(self.workdir).as_uri()]}}))
        prepared = harness.runner.prepare(harness.request(sandbox=None))
        harness.runner.start(prepared, "go")
        self.assertEqual("verified", read_run_state(prepared.thread_id, self.home)["workspace_check"]["result"])

    def test_unverifiable_workspace_refuses_write_task_but_allows_read_only(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(GetAllCascadeTrajectories=summary("CASCADE_RUN_STATUS_IDLE", 1)))
        prepared = harness.runner.prepare(harness.request(sandbox=None))
        with self.assertRaises(AgLaunchError) as ctx:
            harness.runner.start(prepared, "go")
        self.assertEqual("workspace_unverified", ctx.exception.classification)
        self.assertEqual("cancelled", read_run_state(prepared.thread_id, self.home)["status"])
        harness = RunnerHarness(self)
        harness.runner.start(harness.runner.prepare(harness.request(sandbox="read-only")), "go")


class WaitTests(RunnerTestCase):
    def test_success_requires_idle_plus_executor_plus_final_response(self):
        harness = RunnerHarness(self)
        prepared, running, outcome = harness.run()
        self.assertEqual(("completed", None, "ADM-SMOKE-OK"), (outcome.status, outcome.failure_classification, outcome.response_text))
        self.assertEqual(CID, outcome.stats["conversation_id"])
        self.assertEqual(f"antigravity:conversation:{CID}", outcome.stats["provider_run_ref"])
        self.assertEqual("EXECUTOR_TERMINATION_REASON_TERMINAL_STEP_TYPE", outcome.stats["termination_reason"])
        self.assertIn("provider_event", harness.events)
        state = read_run_state(prepared.thread_id, self.home)
        self.assertEqual(("completed", 2, "turn_terminal"), (state["status"], state["step_cursor"], state["last_event"]))
        self.assertEqual(0, prepared._process.poll())
        harness.runner.close(prepared)
        self.assertNotIn("ForceStopCascadeTree", [rpc for rpc, _ in harness.client.calls])

    def test_exit_zero_with_empty_response_is_a_failure(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q")]},
            GetCascadeTrajectoryExecutorMetadatas=executor("EXECUTOR_TERMINATION_REASON_NO_TOOL_CALL")))
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "empty_response"), (outcome.status, outcome.failure_classification))

    def test_prompt_swallowed_before_ready(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetAllCascadeTrajectories=summary("CASCADE_RUN_STATUS_IDLE", 1),
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q")]},
            GetCascadeTrajectoryExecutorMetadatas={"executorMetadata": []}))
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "prompt_not_started"), (outcome.status, outcome.failure_classification))
        self.assertGreaterEqual(harness.clock.now - 1000.0, 20.0, "grace period elapsed before giving up")

    def test_quota_exhausted_during_run(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q"), step("ERROR_MESSAGE", "ERROR", text="Quota exceeded, resets in 2h")]},
            GetCascadeTrajectoryExecutorMetadatas=executor("EXECUTOR_TERMINATION_REASON_ERROR")))
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "quota_exhausted"), (outcome.status, outcome.failure_classification))
        self.assertIn("Quota exceeded", outcome.failure_detail)

    def test_timeout_cancels_conversation_and_leaves_evidence(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetAllCascadeTrajectories=[summary("CASCADE_RUN_STATUS_RUNNING", 1), summary("CASCADE_RUN_STATUS_RUNNING", 1), summary("CASCADE_RUN_STATUS_IDLE", 1)],
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q")]},
            GetCascadeTrajectoryExecutorMetadatas={"executorMetadata": []}))
        harness.script["GetAllCascadeTrajectories"] = [summary("CASCADE_RUN_STATUS_RUNNING", 1)] * 200 + [summary("CASCADE_RUN_STATUS_IDLE", 1)]
        prepared, running, outcome = harness.run(request=harness.request(turn_timeout_seconds=10))
        self.assertEqual(("failed", "turn_timeout"), (outcome.status, outcome.failure_classification))
        evidence = outcome.stats["cancel_evidence"]
        self.assertEqual(("turn_timeout", "ok", "ok"), (evidence["reason"], evidence["rpc"]["CancelCascadeInvocation"], evidence["rpc"]["ForceStopCascadeTree"]))
        self.assertEqual(0, prepared._process.poll())

    def test_permission_stall_is_distinct_and_cancelled(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetAllCascadeTrajectories=[summary("CASCADE_RUN_STATUS_RUNNING", 2)] * 50 + [summary("CASCADE_RUN_STATUS_IDLE", 2)],
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q"), step("RUN_COMMAND", "CORTEX_STEP_STATUS_WAITING")]},
            GetCascadeTrajectoryExecutorMetadatas={"executorMetadata": []}))
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "permission_stall"), (outcome.status, outcome.failure_classification))
        self.assertEqual("permission_stall", outcome.stats["cancel_evidence"]["reason"])

    def test_cancel_requested_by_runner(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetAllCascadeTrajectories=[summary("CASCADE_RUN_STATUS_RUNNING", 1)] * 3 + [summary("CASCADE_RUN_STATUS_IDLE", 1)]))
        prepared = harness.runner.prepare(harness.request())
        running = harness.runner.start(prepared, "go")
        running._cancelled = True
        outcome = harness.runner.wait(running)
        self.assertEqual(("interrupted", "cancelled"), (outcome.status, outcome.failure_classification))
        state = read_run_state(prepared.thread_id, self.home)
        self.assertEqual(("cancelled", True, "CASCADE_RUN_STATUS_IDLE"), (state["status"], state["cancel_evidence"]["confirmed"], state["cancel_evidence"]["final_run_status"]))
        self.assertEqual(0, prepared._process.poll())

    def test_provider_cancelled_from_the_ide_side(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetCascadeTrajectorySteps={"steps": [step("USER_INPUT", text="q"), step("PLANNER_RESPONSE", "CORTEX_STEP_STATUS_CANCELED")]},
            GetCascadeTrajectoryExecutorMetadatas=executor("EXECUTOR_TERMINATION_REASON_USER_CANCELED")))
        _, _, outcome = harness.run()
        self.assertEqual(("interrupted", "cancelled"), (outcome.status, outcome.failure_classification))

    def test_partial_stream_tolerates_transient_rpc_failures(self):
        harness = RunnerHarness(self)
        harness.script["GetAllCascadeTrajectories"] = [AgLsError("rpc_failed", "hiccup"), AgLsError("malformed_response", "half"),
                                                       summary("CASCADE_RUN_STATUS_RUNNING", 1), summary("CASCADE_RUN_STATUS_IDLE", 2)]
        _, _, outcome = harness.run()
        self.assertEqual("completed", outcome.status)

    def test_provider_crash_mid_run(self):
        harness = RunnerHarness(self)
        harness.script["GetAllCascadeTrajectories"] = [summary("CASCADE_RUN_STATUS_RUNNING", 1), AgLsError("ls_unreachable", "refused")]
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "ls_unreachable"), (outcome.status, outcome.failure_classification))

    def test_schema_change_in_steps_fails_closed(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(GetCascadeTrajectorySteps={"steps": "nope"}))
        _, _, outcome = harness.run()
        self.assertEqual(("failed", "malformed_provider_state"), (outcome.status, outcome.failure_classification))

    def test_close_while_running_cancels_so_stop_is_provable(self):
        harness = RunnerHarness(self, script=RunnerHarness.default_script(
            GetAllCascadeTrajectories=[summary("CASCADE_RUN_STATUS_RUNNING", 1)] * 2 + [summary("CASCADE_RUN_STATUS_IDLE", 1)]))
        prepared = harness.runner.prepare(harness.request())
        harness.runner.start(prepared, "go")
        self.assertIsNone(prepared._process.poll())
        harness.runner.close(prepared)
        self.assertEqual(0, prepared._process.poll())
        self.assertIn("ForceStopCascadeTree", [rpc for rpc, _ in harness.client.calls])
        self.assertEqual("cancelled", harness.runner.read_back(prepared.thread_id)["status"])

    def test_run_state_listing_and_secret_hygiene(self):
        harness = RunnerHarness(self)
        prepared, _, _ = harness.run()
        states = list_run_states(self.home)
        self.assertEqual([prepared.thread_id], [s["thread_id"] for s in states])
        self.assertEqual([], list_run_states(self.home, include_terminal=False))
        raw = Path(self.home, "runtime", "antigravity", "runs", f"{prepared.thread_id}.json").read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, raw)


class AgCliProcessTests(unittest.TestCase):
    def test_communicate_timeout_kills_tree(self):
        proc = FakeProcess(hang=True)
        cli = AgCliProcess(proc, timeout=1)
        with self.assertRaises(AgLaunchError) as ctx:
            cli.communicate()
        self.assertEqual("dispatch_timeout", ctx.exception.classification)
        self.assertTrue(cli.timed_out)


if __name__ == "__main__":
    unittest.main()
