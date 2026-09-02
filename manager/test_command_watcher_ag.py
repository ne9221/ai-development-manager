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
from manager.test_task_claims import MemoryClaimRegistry
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
    """Both proofs are required: fresh official SSOT quota AND a reachable local language server."""

    def test_gate_passes_when_quota_reliable_and_language_server_reachable(self):
        with patch("manager.command_watcher.provider_quota_reliable", return_value=True) as quota,              patch("manager.ag_language_server.discover_language_server", return_value=object()) as discover:
            result = ag_availability_check(service="svc")
        self.assertTrue(result)
        quota.assert_called_once_with("svc", "antigravity")
        discover.assert_called_once_with()

    def test_gate_fails_closed_when_quota_unreliable_without_touching_transport(self):
        with patch("manager.command_watcher.provider_quota_reliable", return_value=False),              patch("manager.ag_language_server.discover_language_server") as discover:
            result = ag_availability_check(service="svc")
        self.assertFalse(result)
        discover.assert_not_called()

    def test_gate_fails_closed_when_language_server_missing(self):
        from manager.ag_language_server import AgLsError
        with patch("manager.command_watcher.provider_quota_reliable", return_value=True),              patch("manager.ag_language_server.discover_language_server",
                   side_effect=AgLsError("ide_not_running", "no process")):
            result = ag_availability_check(service="svc")
        self.assertFalse(result)

    def test_gate_fails_closed_on_unexpected_exception(self):
        with patch("manager.command_watcher.provider_quota_reliable", side_effect=RuntimeError("unexpected")):
            result = ag_availability_check(service=None)
        self.assertFalse(result)

    def test_gate_reads_antigravity_quota_only_never_another_provider(self):
        """One provider's fresh quota must never satisfy AG's gate."""
        def by_provider(service, provider, account_id=None):
            return provider == "codex"
        with patch("manager.command_watcher.provider_quota_reliable", side_effect=by_provider),              patch("manager.ag_language_server.discover_language_server", return_value=object()):
            self.assertFalse(ag_availability_check(service="svc"))


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
             patch("manager.command_watcher.launch_task", return_value=fake_outcome) as mock_launch:
            result = process_command(
                store, MagicMock(), cmd,
                quota_check=lambda svc: True,
                # process_command's Session Center gate is the injectable
                # `health_check` parameter (default session_center_healthy,
                # bound at definition time) -- patching the module attribute
                # alone never reaches it, which is why this test was failing
                # on main with reason=session_center_unavailable.
                health_check=lambda: True,
                claim_factory=lambda *_args: MemoryClaimRegistry(),
                allowlist=frozenset({("p1", "t1")}),
            )
        # launch_task must actually have been reached and called with provider="antigravity"
        mock_launch.assert_called_once()
        self.assertEqual(mock_launch.call_args.kwargs.get("provider"), "antigravity")
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
