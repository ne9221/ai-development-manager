"""Layer-1/2 tests for the Antigravity language-server discovery + RPC core (no real IDE needed)."""

import json
import unittest

from manager import ag_live_fence
from manager.ag_language_server import (
    CASCADE_SOURCE_AGENT_API,
    ROLE_CASCADE_HOST,
    ROLE_WORKSPACE_LSP,
    TRANSPORT_AGENTAPI,
    TRANSPORT_IDE_BRIDGE,
    AgLanguageServerClient,
    AgLsError,
    LanguageServerEndpoint,
    availability_snapshot,
    discover_language_server,
    probe_dispatch_route,
    executable_is_trusted,
    parse_command_line,
    parse_listening_ports,
    parse_process_listing,
    redact,
    resolve_model_placeholder,
)

TOKEN = "11111111-2222-3333-4444-555555555555"
EXE = r"C:\Users\EE\AppData\Local\Programs\Antigravity IDE\resources\app\extensions\antigravity\bin\language_server_windows_x64.exe"
CMDLINE = f'"{EXE}" --csrf_token {TOKEN} --extension_server_port 54411 --extension_server_csrf_token other --app_data_dir antigravity-ide --subclient_type ide'
# The per-workspace LSP server the IDE also starts (live argv shape, 2026-09-05): it answers the
# cascade RPCs with an empty map and must never be picked as the dispatch target.
LSP_CMDLINE = (f'"{EXE}" --enable_lsp --csrf_token {TOKEN} --extension_server_port 56641 --extension_server_csrf_token other '
               f'--workspace_id 0765dc392543520fe50f641702eb0c58652f14df3ac9a9f2da84c7c2c5e31401 --subclient_type ide '
               f'--app_data_dir antigravity-ide --parent_pipe_path \\\\.\\pipe\\server_3e7997d61807730f')
NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:54414        0.0.0.0:0              LISTENING       28164
  TCP    127.0.0.1:54415        0.0.0.0:0              LISTENING       28164
  TCP    127.0.0.1:54415        127.0.0.1:51873        ESTABLISHED     28164
  TCP    127.0.0.1:8501         0.0.0.0:0              LISTENING       999
"""


def process(pid=28164, cmdline=CMDLINE, exe=EXE, created="2026-09-02T12:11:30Z"):
    return {"pid": pid, "parent_pid": 9556, "name": "language_server_windows_x64.exe",
            "command_line": cmdline, "executable_path": exe, "creation_date": created}


def endpoint(**overrides):
    values = dict(pid=28164, http_port=54415, https_port=54414, app_data_dir="antigravity-ide", executable=EXE,
                  observed_at="2026-09-02T12:20:00Z", ls_version="1.107.0", csrf_token=TOKEN)
    values.update(overrides)
    return LanguageServerEndpoint(**values)


class FakeOpener:
    """Maps rpc name -> (status, json body); records every request's headers."""

    def __init__(self, responses, http_ports=(54415,)):
        self.responses = responses
        self.http_ports = set(http_ports)
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        port = int(url.split("127.0.0.1:")[1].split("/")[0])
        rpc = url.rsplit("/", 1)[1]
        self.calls.append((port, rpc, json.loads(data or b"{}"), dict(headers)))
        if port not in self.http_ports:
            raise OSError("wrong version number")
        status, body = self.responses.get(rpc, (200, {}))
        return status, json.dumps(body).encode("utf-8")


def user_status(email="user@example.com"):
    return {"userStatus": {"email": email, "name": "User", "planStatus": {
        "planInfo": {"planName": "Pro", "teamsTier": "TEAMS_TIER_PRO"},
        "availablePromptCredits": 500, "availableFlowCredits": 100},
        "cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "Gemini 3.7 Flash (Medium)", "modelId": "gemini-3-7-flash-medium", "isRecommended": True,
             "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M7"},
             "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-09-02T17:11:44Z"}},
            {"label": "Claude Opus 4.6 (Thinking)", "modelId": "claude-opus-4-6-thinking",
             "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M9"},
             "quotaInfo": {"resetTime": "2026-09-02T17:11:44Z"}},
        ]}}}


def quota_summary(gemini_weekly=0.675, gemini_5h=1, tp_weekly=0.65, tp_5h=1):
    def bucket(bucket_id, window, fraction, reset):
        item = {"bucketId": bucket_id, "displayName": bucket_id, "window": window, "resetTime": reset}
        if fraction is not None:
            item["remainingFraction"] = fraction
        return item
    return {"response": {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            bucket("gemini-weekly", "weekly", gemini_weekly, "2026-09-04T08:20:30Z"),
            bucket("gemini-5h", "5h", gemini_5h, "2026-09-02T17:11:42Z")]},
        {"displayName": "Claude and GPT models", "buckets": [
            bucket("3p-weekly", "weekly", tp_weekly, "2026-09-06T11:52:32Z"),
            bucket("3p-5h", "5h", tp_5h, "2026-09-02T17:11:42Z")]},
    ]}}


class ParsingTests(unittest.TestCase):
    def test_parse_process_listing_accepts_object_list_and_empty(self):
        self.assertEqual([], parse_process_listing(""))
        self.assertEqual([], parse_process_listing("null"))
        one = parse_process_listing(json.dumps({"ProcessId": 5, "ParentProcessId": "7", "Name": "x", "CommandLine": None, "ExecutablePath": None}))
        self.assertEqual([{"pid": 5, "parent_pid": 7, "name": "x", "command_line": "", "executable_path": "", "creation_date": None}], one)
        many = parse_process_listing(json.dumps([{"ProcessId": 1}, {"ProcessId": "bad"}, "junk"]))
        self.assertEqual([1], [item["pid"] for item in many])

    def test_parse_process_listing_rejects_garbage(self):
        with self.assertRaises(AgLsError) as ctx:
            parse_process_listing("{not json")
        self.assertEqual("process_enumeration_failed", ctx.exception.classification)

    def test_parse_command_line_extracts_token_and_app_data_dir(self):
        parsed = parse_command_line(CMDLINE)
        self.assertEqual(TOKEN, parsed["csrf_token"])
        self.assertEqual("antigravity-ide", parsed["app_data_dir"])
        self.assertFalse(parsed["persistent_mode"])
        minimal = parse_command_line("ls.exe --persistent_mode")
        self.assertEqual((None, "antigravity-ide", True), (minimal["csrf_token"], minimal["app_data_dir"], minimal["persistent_mode"]))
        self.assertEqual("custom", parse_command_line("x --csrf_token=t --app_data_dir=custom")["app_data_dir"])

    def test_parse_command_line_tells_the_cascade_host_from_the_workspace_lsp_server(self):
        host = parse_command_line(CMDLINE)
        self.assertEqual((ROLE_CASCADE_HOST, None, False), (host["role"], host["workspace_id"], host["enable_lsp"]))
        lsp = parse_command_line(LSP_CMDLINE)
        self.assertEqual(ROLE_WORKSPACE_LSP, lsp["role"])
        self.assertEqual("0765dc392543520fe50f641702eb0c58652f14df3ac9a9f2da84c7c2c5e31401", lsp["workspace_id"])
        self.assertTrue(lsp["enable_lsp"])
        self.assertEqual(TOKEN, lsp["csrf_token"])
        # Either marker alone is enough to demote a server to the LSP role.
        self.assertEqual(ROLE_WORKSPACE_LSP, parse_command_line("x --csrf_token t --enable_lsp")["role"])
        self.assertEqual(ROLE_WORKSPACE_LSP, parse_command_line("x --csrf_token t --workspace_id abc")["role"])

    def test_executable_trust_requires_antigravity_install_location(self):
        self.assertTrue(executable_is_trusted(EXE))
        self.assertTrue(executable_is_trusted("/home/u/.gemini/antigravity-ide/bin/language_server"))
        self.assertFalse(executable_is_trusted(r"C:\evil\language_server_windows_x64.exe"))
        self.assertFalse(executable_is_trusted(r"C:\Users\EE\AppData\Local\Programs\Antigravity IDE\resources\app\extensions\antigravity\bin\other.exe"))
        self.assertFalse(executable_is_trusted(""))

    def test_parse_listening_ports_filters_by_pid_and_state(self):
        self.assertEqual([54414, 54415], parse_listening_ports(NETSTAT, 28164))
        self.assertEqual([8501], parse_listening_ports(NETSTAT, 999))
        self.assertEqual([], parse_listening_ports(NETSTAT, 42))
        self.assertEqual([], parse_listening_ports("", 28164))


class SuiteFenceTests(unittest.TestCase):
    """manager/ag_live_fence.py fences the live IDE off for every unmarked test, under every runner.
    Tripwire: if this fails while an Antigravity IDE is running, ordinary regression could dispatch
    real model turns again. The probe is deliberate, hence ``expecting_refusal()``."""

    def test_ordinary_tests_never_see_a_live_language_server(self):
        with ag_live_fence.expecting_refusal():
            with self.assertRaises(AgLsError) as ctx:
                discover_language_server()
            snap = availability_snapshot(now="2026-09-05T00:00:00Z")
        self.assertEqual("ide_not_running", ctx.exception.classification)
        self.assertIn("fenced off", ctx.exception.detail)
        self.assertEqual(("unavailable", "ide_not_running", False), (snap["status"], snap["reason"], snap["can_accept_new_task"]))


class DiscoveryTests(unittest.TestCase):
    def discover(self, processes, opener=None, ports=None):
        opener = opener or FakeOpener({})
        return discover_language_server(process_lister=lambda: processes,
                                        port_lister=lambda pid: ports if ports is not None else [54414, 54415],
                                        opener=opener, identity=lambda pid: f"windows-filetime:{pid}", ls_version="1.107.0")

    def test_no_process_is_ide_not_running(self):
        with self.assertRaises(AgLsError) as ctx:
            self.discover([])
        self.assertEqual("ide_not_running", ctx.exception.classification)

    def test_untrusted_executable_is_ignored(self):
        with self.assertRaises(AgLsError) as ctx:
            self.discover([process(exe=r"C:\evil\language_server_windows_x64.exe", cmdline=CMDLINE.replace(EXE, r"C:\evil\language_server_windows_x64.exe"))])
        self.assertEqual("ide_not_running", ctx.exception.classification)

    def test_other_app_data_dir_is_ignored(self):
        with self.assertRaises(AgLsError) as ctx:
            self.discover([process(cmdline=CMDLINE.replace("antigravity-ide", "antigravity"))])
        self.assertEqual("ide_not_running", ctx.exception.classification)

    def test_missing_csrf_is_csrf_unavailable(self):
        with self.assertRaises(AgLsError) as ctx:
            self.discover([process(cmdline=f'"{EXE}" --app_data_dir antigravity-ide')])
        self.assertEqual("csrf_unavailable", ctx.exception.classification)

    def test_no_answering_port_is_ls_unreachable(self):
        with self.assertRaises(AgLsError) as ctx:
            self.discover([process()], opener=FakeOpener({}, http_ports=()))
        self.assertEqual("ls_unreachable", ctx.exception.classification)
        with self.assertRaises(AgLsError) as ctx:
            self.discover([process()], ports=[])
        self.assertEqual("ls_unreachable", ctx.exception.classification)

    def test_http_port_is_the_one_answering_get_status(self):
        opener = FakeOpener({}, http_ports=(54415,))
        found = self.discover([process()], opener=opener)
        self.assertEqual((28164, 54415, 54414), (found.pid, found.http_port, found.https_port))
        self.assertEqual("windows-filetime:28164", found.creation_identity)
        self.assertEqual("1.107.0", found.ls_version)
        self.assertEqual("http://127.0.0.1:54415", found.base_url)
        self.assertEqual({"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:54415", "ANTIGRAVITY_CSRF_TOKEN": TOKEN,
                          "ANTIGRAVITY_LS_VERSION": "1.107.0"}, found.agentapi_env())
        probe_headers = [call[3] for call in opener.calls if call[1] == "GetStatus"]
        self.assertTrue(probe_headers and all(h["x-codeium-csrf-token"] == TOKEN for h in probe_headers))

    def test_newest_server_wins_when_two_run(self):
        older = process(pid=100, created="2026-09-01T00:00:00Z")
        newer = process(pid=200, created="2026-09-02T00:00:00Z")
        found = self.discover([older, newer])
        self.assertEqual(200, found.pid)

    def test_cascade_host_beats_a_newer_workspace_lsp_server(self):
        """Live 2026-09-05: pid 17044 (app-level, older) hosts every cascade; pid 28604
        (--enable_lsp --workspace_id, newer) answers the cascade RPCs with nothing."""
        host = process(pid=17044, created="2026-09-04T01:15:57Z")
        lsp = process(pid=28604, cmdline=LSP_CMDLINE, created="2026-09-04T01:16:09Z")
        found = self.discover([lsp, host])
        self.assertEqual((17044, ROLE_CASCADE_HOST, None), (found.pid, found.role, found.workspace_id))
        self.assertEqual(ROLE_CASCADE_HOST, found.evidence()["role"])
        only_lsp = self.discover([lsp])
        self.assertEqual((28604, ROLE_WORKSPACE_LSP), (only_lsp.pid, only_lsp.role))
        self.assertEqual("0765dc392543520fe50f641702eb0c58652f14df3ac9a9f2da84c7c2c5e31401", only_lsp.evidence()["workspace_id"])

    def test_endpoint_never_leaks_token(self):
        found = self.discover([process()])
        self.assertNotIn(TOKEN, repr(found))
        self.assertNotIn(TOKEN, json.dumps(found.evidence()))
        self.assertEqual("language_server_windows_x64.exe", found.evidence()["executable"])
        self.assertNotIn(TOKEN, str(AgLsError("x", f"argv --csrf_token {TOKEN} leaked")))
        self.assertEqual({"csrf_token": "[REDACTED]", "ok": ["--csrf_token [REDACTED]"]},
                         redact({"csrf_token": TOKEN, "ok": [f"--csrf_token {TOKEN}"]}))


class ClientTests(unittest.TestCase):
    def test_call_sends_csrf_header_and_json_body(self):
        opener = FakeOpener({"GetConversationMetadata": (200, {"metadata": {"rootConversationId": "c1"}})})
        client = AgLanguageServerClient(endpoint(), opener=opener)
        self.assertEqual({"metadata": {"rootConversationId": "c1"}}, client.get_conversation_metadata("c1"))
        port, rpc, body, headers = opener.calls[-1]
        self.assertEqual((54415, "GetConversationMetadata", {"conversationId": "c1"}), (port, rpc, body))
        self.assertEqual(TOKEN, headers["x-codeium-csrf-token"])
        self.assertEqual("application/json", headers["content-type"])

    def test_error_classification(self):
        cases = {
            "A": ((403, {"message": "Invalid CSRF token"}), "rpc_unauthenticated"),
            "B": ((400, {"code": "invalid_argument", "message": "conversation_id is required"}), "rpc_invalid_argument"),
            "C": ((500, {"code": "unknown", "message": "trajectory not found: x"}), "rpc_not_found"),
            "D": ((400, {"code": "failed_precondition", "message": "auth client is not initialized"}), "rpc_failed_precondition"),
            "E": ((500, {"code": "unknown", "message": "boom"}), "rpc_failed"),
            "F": ((401, {"code": "unauthenticated", "message": "no"}), "rpc_unauthenticated"),
        }
        for rpc, (response, expected) in cases.items():
            client = AgLanguageServerClient(endpoint(), opener=FakeOpener({rpc: response}))
            with self.assertRaises(AgLsError) as ctx:
                client.call(rpc)
            self.assertEqual(expected, ctx.exception.classification, rpc)

    def test_transport_and_malformed_failures(self):
        def broken(url, data, headers, timeout):
            raise ConnectionRefusedError("refused")
        with self.assertRaises(AgLsError) as ctx:
            AgLanguageServerClient(endpoint(), opener=broken).get_status()
        self.assertEqual("ls_unreachable", ctx.exception.classification)

        def html(url, data, headers, timeout):
            return 200, b"<html>not json</html>"
        with self.assertRaises(AgLsError) as ctx:
            AgLanguageServerClient(endpoint(), opener=html).get_status()
        self.assertEqual("malformed_response", ctx.exception.classification)

        def listing(url, data, headers, timeout):
            return 200, b"[1, 2]"
        with self.assertRaises(AgLsError) as ctx:
            AgLanguageServerClient(endpoint(), opener=listing).get_status()
        self.assertEqual("malformed_response", ctx.exception.classification)


class DispatchRouteProbeTests(unittest.TestCase):
    """Readable quota and dispatchability are separate facts (live 2026-09-02): the agentapi route."""

    def probe(self, response):
        client = AgLanguageServerClient(endpoint(), opener=FakeOpener({"ReadProject": response}))
        return probe_dispatch_route(client, transport=TRANSPORT_AGENTAPI)

    def test_initialized_projects_store_is_available(self):
        self.assertEqual({"available": True, "transport": "agentapi", "reason": None, "detail": None},
                         self.probe((200, {"project": {}})))

    def test_uninitialized_projects_store_blocks_dispatch(self):
        result = self.probe((500, {"code": "unknown", "message": "projects store not initialized (error ID: abc)"}))
        self.assertEqual((False, "projects_store_unavailable"), (result["available"], result["reason"]))
        self.assertIn("projects store not initialized", result["detail"])

    def test_agentapi_project_env_error_also_blocks_dispatch(self):
        result = self.probe((500, {"code": "unknown", "message": "projectsStore is nil, but projectEnvConfig was provided"}))
        self.assertEqual((False, "projects_store_unavailable"), (result["available"], result["reason"]))

    def test_argument_rejection_means_the_store_exists(self):
        result = self.probe((400, {"code": "invalid_argument", "message": "project_id is required"}))
        self.assertTrue(result["available"])

    def test_transport_failure_blocks_dispatch_with_its_own_reason(self):
        result = self.probe((403, {"message": "Invalid CSRF token"}))
        self.assertEqual((False, "rpc_unauthenticated"), (result["available"], result["reason"]))


class IdeBridgeRouteProbeTests(unittest.TestCase):
    """The IDE-bridge route (default): the cascade subsystem must answer; no projects store involved."""

    def probe(self, responses):
        client = AgLanguageServerClient(endpoint(), opener=FakeOpener(responses))
        return probe_dispatch_route(client)

    def test_default_transport_is_the_ide_bridge_and_never_touches_read_project(self):
        opener = FakeOpener({"GetAllCascadeTrajectories": (200, {"trajectorySummaries": {}})})
        result = probe_dispatch_route(AgLanguageServerClient(endpoint(), opener=opener))
        self.assertEqual({"available": True, "transport": "ide_bridge", "reason": None, "detail": None}, result)
        self.assertEqual(["GetAllCascadeTrajectories"], [call[1] for call in opener.calls])

    def test_empty_trajectory_map_is_still_available(self):
        # A freshly started IDE (or the LSP-role server) answers {} -- the RPC works.
        self.assertTrue(self.probe({"GetAllCascadeTrajectories": (200, {})})["available"])

    def test_cascade_rpc_failure_blocks_with_its_own_reason(self):
        result = self.probe({"GetAllCascadeTrajectories": (403, {"message": "Invalid CSRF token"})})
        self.assertEqual((False, "ide_bridge", "rpc_unauthenticated"), (result["available"], result["transport"], result["reason"]))

    def test_unknown_transport_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            probe_dispatch_route(AgLanguageServerClient(endpoint(), opener=FakeOpener({})), transport="pty")


class ModelPlaceholderTests(unittest.TestCase):
    """The model sent to the server is its own placeholder enum from the live catalog -- never guessed."""

    def test_requested_model_matches_id_label_or_placeholder(self):
        for wanted in ("gemini-3-7-flash-medium", "Gemini 3.7 Flash (Medium)", "model_placeholder_m7"):
            self.assertEqual("MODEL_PLACEHOLDER_M7", resolve_model_placeholder(user_status(), wanted)["placeholder"], wanted)

    def test_default_is_the_cheapest_recommended_gemini_flash_with_quota(self):
        chosen = resolve_model_placeholder(user_status(), None)
        self.assertEqual(("gemini-3-7-flash-medium", "MODEL_PLACEHOLDER_M7", 1.0), (chosen["model_id"], chosen["placeholder"], chosen["remaining_fraction"]))

    def test_unknown_model_fails_closed(self):
        with self.assertRaises(AgLsError) as ctx:
            resolve_model_placeholder(user_status(), "gpt-9-ultra")
        self.assertEqual("unknown_model", ctx.exception.classification)

    def test_exhausted_model_is_refused_with_its_reset(self):
        # protobuf JSON omits the zero: resetTime present, remainingFraction absent == 0.
        with self.assertRaises(AgLsError) as ctx:
            resolve_model_placeholder(user_status(), "claude-opus-4-6-thinking")
        self.assertEqual("model_quota_exhausted", ctx.exception.classification)
        self.assertIn("2026-09-02T17:11:44Z", ctx.exception.detail)

    def test_catalog_without_placeholders_is_unavailable_not_guessed(self):
        status = user_status()
        for config in status["userStatus"]["cascadeModelConfigData"]["clientModelConfigs"]:
            config.pop("modelOrAlias")
        with self.assertRaises(AgLsError) as ctx:
            resolve_model_placeholder(status, None)
        self.assertEqual("model_catalog_unavailable", ctx.exception.classification)


class CascadeRpcShapeTests(unittest.TestCase):
    """Request shapes verified live 2026-09-05 (IDE 1.107.0): a change here changes what the server sees."""

    def test_add_tracked_workspace_start_cascade_and_send_message_bodies(self):
        opener = FakeOpener({"StartCascade": (200, {"cascadeId": "cas-1"})})
        client = AgLanguageServerClient(endpoint(), opener=opener)
        client.add_tracked_workspace(r"C:\work\repo")
        self.assertEqual("cas-1", client.start_cascade(["file:///C:/work/repo"])["cascadeId"])
        client.send_user_cascade_message("cas-1", "do it", model_placeholder="MODEL_PLACEHOLDER_M7", ide_version="1.107.0")
        bodies = {call[1]: call[2] for call in opener.calls}
        self.assertEqual({"workspace": r"C:\work\repo"}, bodies["AddTrackedWorkspace"])
        self.assertEqual({"source": CASCADE_SOURCE_AGENT_API, "workspaceUris": ["file:///C:/work/repo"]}, bodies["StartCascade"])
        sent = bodies["SendUserCascadeMessage"]
        self.assertEqual("cas-1", sent["cascadeId"])
        self.assertEqual([{"text": "do it"}], sent["items"])
        self.assertEqual({"model": "MODEL_PLACEHOLDER_M7"}, sent["cascadeConfig"]["plannerConfig"]["requestedModel"])
        self.assertEqual({"plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT", "agenticMode": True},
                         sent["cascadeConfig"]["plannerConfig"]["conversational"])
        self.assertEqual({"ideName": "antigravity", "ideVersion": "1.107.0", "extensionName": "antigravity", "locale": "en"}, sent["metadata"])
        # No tool-policy widening is ever requested: the server applies its own defaults.
        self.assertNotIn("toolConfig", sent["cascadeConfig"]["plannerConfig"])
        self.assertTrue(all(call[3]["x-codeium-csrf-token"] == TOKEN for call in opener.calls))


class AvailabilitySnapshotTests(unittest.TestCase):
    def snapshot(self, responses=None, discover=None, transport=None):
        base = {"GetUserStatus": (200, user_status()), "RetrieveUserQuotaSummary": (200, quota_summary()),
                "ReadProject": (200, {"project": {}}), "GetAllCascadeTrajectories": (200, {"trajectorySummaries": {}})}
        if responses is not None:
            base.update(responses)
        opener = FakeOpener(base)
        kwargs = {"transport": transport} if transport else {}
        return availability_snapshot(discover=discover or (lambda timeout: endpoint()),
                                     client_factory=lambda ep, timeout: AgLanguageServerClient(ep, opener=opener),
                                     now="2026-09-02T12:30:00Z", **kwargs)

    def test_blocked_dispatch_route_degrades_without_touching_quota_truth(self):
        snap = self.snapshot({"ReadProject": (500, {"code": "unknown", "message": "projects store not initialized"})},
                             transport=TRANSPORT_AGENTAPI)
        self.assertEqual(("degraded", "projects_store_unavailable", False, "agentapi"),
                         (snap["status"], snap["reason"], snap["can_accept_new_task"], snap["transport"]))
        self.assertEqual((0.65, "official", "fresh"), (snap["remaining"], snap["confidence"], snap["freshness"]))
        self.assertFalse(snap["dispatch_route"]["available"])

    def test_default_snapshot_uses_the_ide_bridge_route(self):
        snap = self.snapshot({"ReadProject": (500, {"code": "unknown", "message": "projects store not initialized"})})
        self.assertEqual(("available", None, True, "ide_bridge"),
                         (snap["status"], snap["reason"], snap["can_accept_new_task"], snap["transport"]))
        self.assertEqual({"available": True, "transport": "ide_bridge", "reason": None, "detail": None}, snap["dispatch_route"])

    def test_ide_bridge_route_failure_degrades_but_keeps_quota(self):
        snap = self.snapshot({"GetAllCascadeTrajectories": (500, {"code": "unknown", "message": "cascade subsystem down"})})
        self.assertEqual(("degraded", "rpc_failed", False), (snap["status"], snap["reason"], snap["can_accept_new_task"]))
        self.assertEqual(0.65, snap["remaining"])

    def test_available_with_account_models_and_buckets(self):
        snap = self.snapshot()
        self.assertEqual("available", snap["status"])
        self.assertTrue(snap["dispatch_route"]["available"])
        self.assertTrue(snap["can_accept_new_task"])
        self.assertEqual(("antigravity_language_server", "fresh", "official", None),
                         (snap["source"], snap["freshness"], snap["confidence"], snap["reason"]))
        self.assertEqual("user@example.com", snap["account"]["email"])
        self.assertEqual("Pro", snap["account"]["plan_name"])
        self.assertEqual(0.65, snap["remaining"])
        self.assertEqual(65.0, snap["remaining_percent"])
        self.assertEqual("2026-09-02T17:11:42Z", snap["reset_at"])
        self.assertEqual(["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"], [b["bucket_id"] for b in snap["buckets"]])
        self.assertEqual([1.0, 0.0], [m["remaining_fraction"] for m in snap["models"]])
        self.assertEqual(28164, snap["language_server"]["pid"])
        self.assertNotIn(TOKEN, json.dumps(snap))

    def test_no_language_server_is_unavailable_with_reason(self):
        def missing(timeout):
            raise AgLsError("ide_not_running", "no process")
        snap = self.snapshot(discover=missing)
        self.assertEqual(("unavailable", "ide_not_running", None, None, False),
                         (snap["status"], snap["reason"], snap["remaining"], snap["account"], snap["can_accept_new_task"]))

    def test_rpc_failure_is_unverified_not_guessed(self):
        snap = self.snapshot({"GetUserStatus": (200, user_status()), "RetrieveUserQuotaSummary": (500, {"code": "unknown", "message": "boom"})})
        self.assertEqual(("unverified", "rpc_failed", None, "unknown"), (snap["status"], snap["reason"], snap["remaining"], snap["confidence"]))
        self.assertEqual(28164, snap["language_server"]["pid"])

    def test_schema_change_is_unverified(self):
        snap = self.snapshot({"GetUserStatus": (200, user_status()), "RetrieveUserQuotaSummary": (200, {"response": {"quota": []}})})
        self.assertEqual(("unverified", "quota_schema_changed"), (snap["status"], snap["reason"]))

    def test_missing_identity_is_unverified(self):
        snap = self.snapshot({"GetUserStatus": (200, {"userStatus": {}}), "RetrieveUserQuotaSummary": (200, quota_summary())})
        self.assertEqual(("unverified", "account_identity_unavailable"), (snap["status"], snap["reason"]))

    def test_partial_exhaustion_is_degraded(self):
        snap = self.snapshot({"GetUserStatus": (200, user_status()),
                              "RetrieveUserQuotaSummary": (200, quota_summary(tp_weekly=None))})
        self.assertEqual(("degraded", "quota_exhausted_partial", True), (snap["status"], snap["reason"], snap["can_accept_new_task"]))
        self.assertEqual("2026-09-06T11:52:32Z", snap["unavailable_until"])
        self.assertEqual(0.0, snap["remaining"])

    def test_full_exhaustion_is_unavailable_until_latest_reset(self):
        snap = self.snapshot({"GetUserStatus": (200, user_status()),
                              "RetrieveUserQuotaSummary": (200, quota_summary(None, None, None, None))})
        self.assertEqual(("unavailable", "quota_exhausted", False), (snap["status"], snap["reason"], snap["can_accept_new_task"]))
        self.assertEqual("2026-09-06T11:52:32Z", snap["unavailable_until"])


if __name__ == "__main__":
    unittest.main()
