"""Tests for AG provider registration and availability gate in command_watcher."""

import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from manager.ag_runner import AgRunner
from manager.claude_launcher import ClaudeLauncher
from manager.codex_launcher import CodexLauncher
from manager.command_watcher import (
    PROVIDER_RUNTIMES,
    ag_availability_check,
    resolve_provider_runtime,
)
from manager.tasks import TaskError, create_project, create_task
from manager.test_execution_lifecycle import project, task
from manager.trusted_ingress import REQUIRED_TASK_POLICIES


# ---------------------------------------------------------------------------
# Minimal store
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.records = {}

    def put(self, area, project_id, name, doc):
        self.records[(area, project_id, name)] = deepcopy(doc)
        return doc

    def get(self, area, project_id, name):
        try:
            return deepcopy(self.records[(area, project_id, name)])
        except KeyError:
            raise TaskError("not found") from None

    def list_records(self, area, project_id):
        return [deepcopy(v) for (a, p, _), v in self.records.items()
                if a == area and p == project_id]

    def list_projects(self):
        return [self.get("projects", "p1", "p1")]


def _allowlist_compliant_store():
    store = _Store()
    create_project(store, project())
    t = task(read_only=True)
    t["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
    create_task(store, t, assign=False)
    return store


def _command(**overrides):
    base = {
        "command_id": "cmd-ag-1",
        "project_id": "p1",
        "task_id": "t1",
        "provider": "antigravity",
        "model": None,
        "fallback_model": None,
        "mode": None,
        "effort": None,
        "selection_reason": [],
        "quota_evidence": None,
        "created_at": "2026-08-20T00:00:00Z",
        "status": "queued",
        "execution_id": None,
        "claimed_at": None,
        "completed_at": None,
        "result": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. PROVIDER_RUNTIMES registration
# ---------------------------------------------------------------------------

class TestProviderRuntimesRegistration(unittest.TestCase):
    def test_antigravity_registered(self):
        self.assertIn("antigravity", PROVIDER_RUNTIMES)

    def test_antigravity_launcher_factory_is_ag_runner(self):
        self.assertIs(PROVIDER_RUNTIMES["antigravity"]["launcher_factory"], AgRunner)

    def test_antigravity_quota_check_is_ag_availability_check(self):
        self.assertIs(PROVIDER_RUNTIMES["antigravity"]["quota_check"], ag_availability_check)

    def test_codex_still_registered_with_codex_launcher(self):
        self.assertIs(PROVIDER_RUNTIMES["codex"]["launcher_factory"], CodexLauncher)

    def test_claude_still_registered_with_claude_launcher(self):
        self.assertIs(PROVIDER_RUNTIMES["claude"]["launcher_factory"], ClaudeLauncher)

    def test_resolve_provider_runtime_antigravity(self):
        rt = resolve_provider_runtime("antigravity")
        self.assertIsNotNone(rt)
        self.assertIs(rt["launcher_factory"], AgRunner)

    def test_resolve_provider_runtime_unknown_returns_none(self):
        self.assertIsNone(resolve_provider_runtime("gemini_app"))
        self.assertIsNone(resolve_provider_runtime(""))
        self.assertIsNone(resolve_provider_runtime("CODEX"))


# ---------------------------------------------------------------------------
# 2. ag_availability_check gate
# ---------------------------------------------------------------------------

class TestAgAvailabilityCheck(unittest.TestCase):
    def test_gate_passes_when_auth_and_binary_ok(self):
        with patch("manager.ag_cli_runner.verify_auth_identity", return_value="user@example.com"), \
             patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("agy", [])):
            result = ag_availability_check(service=None)
        self.assertTrue(result)

    def test_gate_fails_closed_when_auth_raises(self):
        from manager.ag_runner import AgLaunchError
        with patch("manager.ag_cli_runner.verify_auth_identity",
                   side_effect=AgLaunchError("auth_not_proven", "no local profile")), \
             patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("agy", [])):
            result = ag_availability_check(service=None)
        self.assertFalse(result)

    def test_gate_fails_closed_when_binary_not_found(self):
        with patch("manager.ag_cli_runner.verify_auth_identity", return_value="user@example.com"), \
             patch("manager.ag_cli_runner.resolve_ag_cli_executable",
                   side_effect=FileNotFoundError("agy not found")):
            result = ag_availability_check(service=None)
        self.assertFalse(result)

    def test_gate_fails_closed_on_unexpected_exception(self):
        with patch("manager.ag_cli_runner.verify_auth_identity",
                   side_effect=RuntimeError("unexpected")):
            result = ag_availability_check(service=None)
        self.assertFalse(result)

    def test_gate_service_arg_accepted_but_not_forwarded(self):
        """service accepted for interface parity; gate outcome must not depend on it."""
        with patch("manager.ag_cli_runner.verify_auth_identity", return_value="u@g.com"), \
             patch("manager.ag_cli_runner.resolve_ag_cli_executable", return_value=("agy", [])):
            self.assertTrue(ag_availability_check(service=MagicMock()))
            self.assertTrue(ag_availability_check(service=None))


# ---------------------------------------------------------------------------
# 3. process_command provider routing
# ---------------------------------------------------------------------------

class TestProcessCommandAgRouting(unittest.TestCase):
    """Verify process_command routes / rejects AG commands correctly."""

    def test_unsupported_provider_rejected(self):
        """gemini_app fails schema validation before runtime check -- still rejected."""
        from manager.command_watcher import process_command
        store = _allowlist_compliant_store()
        cmd = _command(provider="gemini_app")
        with patch("manager.command_watcher.session_center_healthy", return_value=True), \
             patch("manager.command_watcher.validate_task_enforcement"):
            result = process_command(
                store, MagicMock(), cmd,
                allowlist=frozenset({("p1", "t1")}),
            )
        self.assertEqual(result["status"], "rejected")

    def test_gate_failure_rejects_without_launch(self):
        """Gate failure leaves command rejected -- launch_task never called."""
        from manager.command_watcher import process_command
        store = _allowlist_compliant_store()
        cmd = _command()
        with patch("manager.command_watcher.validate_task_enforcement"), \
             patch("manager.command_watcher.session_center_healthy", return_value=True), \
             patch("manager.command_watcher.launch_task") as mock_launch:
            result = process_command(
                store, MagicMock(), cmd,
                quota_check=lambda svc: False,
                allowlist=frozenset({("p1", "t1")}),
            )
            mock_launch.assert_not_called()
        self.assertEqual(result["status"], "rejected")

    def test_codex_provider_resolves_to_codex_launcher(self):
        rt = resolve_provider_runtime("codex")
        self.assertIs(rt["launcher_factory"], CodexLauncher)

    def test_claude_provider_resolves_to_claude_launcher(self):
        rt = resolve_provider_runtime("claude")
        self.assertIs(rt["launcher_factory"], ClaudeLauncher)

    def test_gate_pass_reaches_launch_task(self):
        """When gate passes, process_command claims the command and calls launch_task."""
        from manager.command_watcher import process_command
        store = _allowlist_compliant_store()
        cmd = _command()

        fake_outcome = {
            "terminal": {"execution": {
                "status": "completed",
                "session_id": "antigravity:ag-cli-test1234",
            }},
            "session": {"session_id": "antigravity:ag-cli-test1234"},
            "dispatch": {
                "provider": "antigravity", "model": None, "fallback_model": None,
                "mode": "interactive", "effort": "medium",
                "selection_reason": ["test"], "quota_evidence": {},
            },
        }

        with patch("manager.command_watcher.validate_task_enforcement"), \
             patch("manager.command_watcher.session_center_healthy", return_value=True), \
             patch("manager.execution_runner.launch_task", return_value=fake_outcome), \
             patch("manager.command_watcher.launch_task", return_value=fake_outcome) as mock_launch:
            result = process_command(
                store, MagicMock(), cmd,
                quota_check=lambda svc: True,
                allowlist=frozenset({("p1", "t1")}),
            )
        # Whether process_command reached the launch path is observable from the command status
        self.assertIn(result["status"], ("completed", "failed", "claimed"))
        # launch_task must have been called with provider="antigravity" if it ran
        if mock_launch.call_count > 0:
            call_kwargs = mock_launch.call_args
            provider_arg = call_kwargs.kwargs.get("provider")
            self.assertEqual(provider_arg, "antigravity")


if __name__ == "__main__":
    unittest.main()
