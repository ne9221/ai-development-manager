import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager.hydra_adapter import HydraLauncher, HydraRuntime, HydraUnavailable
from manager.codex_launcher import LaunchRequest


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / ".mcp.json"
        self.lock = Path(self.temp.name) / "daemon.lock"
        self.lock.write_text(json.dumps({"pid": 123, "startedAt": "2026-08-19T00:00:00Z"}))

    def tearDown(self):
        self.temp.cleanup()

    def write_endpoint(self, port):
        self.config.write_text(json.dumps({"mcpServers": {"hydra": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp"
        }}}))

    def test_discovers_current_loopback_port_without_a_default(self):
        self.write_endpoint(49123)
        runtime = HydraRuntime(self.config, self.lock)
        self.assertEqual("http://127.0.0.1:49123/mcp", runtime.discover_endpoint())

    def test_rejects_non_loopback_or_malformed_endpoint(self):
        for url in ("http://example.com:1234/mcp", "http://127.0.0.1/mcp", "http://127.0.0.1:1/not-mcp"):
            self.config.write_text(json.dumps({"mcpServers": {"hydra": {"url": url}}}))
            with self.subTest(url=url), self.assertRaises(HydraUnavailable):
                HydraRuntime(self.config, self.lock).discover_endpoint()

    def test_stale_cached_port_is_rejected_then_current_file_is_rediscovered(self):
        self.write_endpoint(41001)
        runtime = HydraRuntime(self.config, self.lock)
        calls = []

        def request(endpoint, method, params=None):
            calls.append(endpoint)
            if endpoint.endswith(":41001/mcp"):
                self.write_endpoint(41002)
                raise HydraUnavailable("dead endpoint")
            if method == "tools/list":
                return {"tools": [{"name": name} for name in runtime.REQUIRED_TOOLS]}
            return []

        runtime._rpc = request
        with patch("manager.hydra_adapter.process_creation_identity", return_value="daemon-a"):
            self.assertTrue(runtime.health()["healthy"])
        self.assertEqual(["http://127.0.0.1:41001/mcp", "http://127.0.0.1:41002/mcp"], calls)

    def test_manager_api_wraps_only_proven_tools(self):
        self.write_endpoint(41003)
        runtime = HydraRuntime(self.config, self.lock)
        calls = []
        runtime._call = lambda name, arguments=None: calls.append((name, arguments or {})) or {"ok": True}
        self.assertEqual({"ok": True}, runtime.list_agents())
        runtime.create_agent(name="n", project_dir="C:\\repo", provider="codex", model="m")
        runtime.send_prompt("a", "p")
        runtime.get_output("a", 20)
        runtime.kill_agent("a")
        runtime.restart_agent("a")
        self.assertEqual(
            ["hydra_list_agents", "hydra_create_agent", "hydra_send_prompt", "hydra_get_output",
             "hydra_kill_agent", "hydra_restart_agent"],
            [name for name, _ in calls],
        )


class FakeRuntime:
    def __init__(self):
        self.agent = None
        self.killed = False

    def health(self):
        return {"healthy": True, "endpoint": "http://127.0.0.1:45678/mcp", "pid": 321, "creation_identity": "daemon-a"}

    def create_agent(self, **kwargs):
        self.agent = {"id": "agent-1", "sessionId": "session-1", "status": "idle", **kwargs}
        return self.agent

    def send_prompt(self, agent_id, prompt):
        self.agent["status"] = "running"
        self.prompt = prompt
        return {"ok": True}

    def list_agents(self):
        if self.agent and self.agent["status"] == "running":
            self.agent["status"] = "idle"
            return [{**self.agent, "status": "running"}]
        return [] if self.killed else [self.agent]

    def get_output(self, agent_id, lines=100):
        return "harmless result" if hasattr(self, "prompt") else ""

    def kill_agent(self, agent_id):
        self.killed = True
        return {"ok": True}


class LauncherTests(unittest.TestCase):
    def test_real_evidence_shapes_existing_launcher_lifecycle(self):
        runtime = FakeRuntime()
        launcher = HydraLauncher("codex", runtime=runtime, poll_interval=0)
        request = LaunchRequest(str(Path.cwd()), model="gpt-test", turn_timeout_seconds=2)
        prepared = launcher.prepare(request)
        self.assertEqual("agent-1", prepared.hydra_agent_id)
        self.assertEqual("session-1", prepared.provider_session_id)
        running = launcher.start(prepared, "return harmless result")
        self.assertEqual("completed", launcher.wait(running).status)
        launcher.close(running)
        self.assertTrue(launcher.provider_stopped(prepared))

    def test_daemon_loss_never_reports_a_live_turn_as_completed(self):
        runtime = FakeRuntime()
        launcher = HydraLauncher("codex", runtime=runtime, poll_interval=0)
        prepared = launcher.prepare(LaunchRequest(str(Path.cwd()), model="gpt-test", turn_timeout_seconds=2))
        running = launcher.start(prepared, "harmless")
        runtime.list_agents = lambda: (_ for _ in ()).throw(HydraUnavailable("daemon crashed"))
        with self.assertRaises(HydraUnavailable):
            launcher.wait(running)

    def test_live_pty_completes_after_changed_output_becomes_stable(self):
        runtime = FakeRuntime()
        launcher = HydraLauncher("claude", runtime=runtime, poll_interval=0, settle_seconds=0)
        prepared = launcher.prepare(LaunchRequest(str(Path.cwd()), model="sonnet", turn_timeout_seconds=2))
        running = launcher.start(prepared, "harmless")
        runtime.list_agents = lambda: [{**runtime.agent, "status": "running"}]
        self.assertEqual("completed", launcher.wait(running).status)

    def test_default_claude_account_is_attributed_but_nondefault_config_is_rejected(self):
        launcher = HydraLauncher("claude", runtime=FakeRuntime(), poll_interval=0)
        request = LaunchRequest(str(Path.cwd()), model="sonnet", turn_timeout_seconds=2)
        self.assertEqual("account-a", launcher.prepare(request, account_id="account-a").account_id)
        with self.assertRaises(HydraUnavailable):
            launcher.prepare(request, account_id="account-b", config_dir=r"C:\accounts\b")


if __name__ == "__main__":
    unittest.main()
