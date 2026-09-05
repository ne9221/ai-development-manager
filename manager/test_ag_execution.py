"""Tests for Antigravity execution lifecycle integration."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from manager.ag_runner import AgRunner, LaunchOutcome, LaunchRequest, PreparedLaunch, RunningLaunch
from manager.execution_runner import launch_task, run_execution
from manager.tasks import TaskError
from manager.test_execution_lifecycle import HEAD, build_store, quota_document
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry


class MockLiveProcess:
    def poll(self): return 0
    def wait(self, timeout=None): return 0


class MockAgBridge:
    def __init__(self):
        self.is_alive_val = True
        self.events = []

    def is_alive(self):
        return self.is_alive_val

    def is_transport_available(self):
        return True

    def prepare(self, request):
        self.events.append("prepare")
        prep = PreparedLaunch(
            thread_id="ag-live-abc12345",
            session_path=None,
            pid=8888,
            process_creation_identity="pid-8888",
            prepared_at="2026-08-17T08:00:00Z",
            mode="live_ide",
            _target=self,
            _request=request,
        )
        prep._process = MockLiveProcess()
        return prep

    def start(self, prepared, prompt):
        self.events.append("start")
        return RunningLaunch(
            prepared=prepared,
            turn_id="turn-xyz",
            started_at="2026-08-17T08:00:01Z",
        )

    def set_heartbeat(self, running, callback):
        running._heartbeat = callback

    def wait(self, running):
        self.events.append("wait")
        return LaunchOutcome(
            status="completed",
            thread_id="ag-live-abc12345",
            turn_id="turn-xyz",
            completed_at="2026-08-17T08:00:10Z",
            response_text="Analysis complete",
            stats={"tokens": 100},
        )

    def close(self, prepared):
        self.events.append("close")


class TestAgExecutionLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.working_directory = str(Path(self.temp.name).resolve())
        self.store = build_store(read_only=True, working_directory=self.working_directory, provider="antigravity")
        # Add latest mock to MemoryStore
        self.store.latest = lambda *args: (_ for _ in ()).throw(TaskError("found 0"))
        self.project_id = "p1"
        self.task_id = "t1"

    def tearDown(self):
        self.temp.cleanup()

    def test_ag_run_execution_read_only(self):
        mock_bridge = MockAgBridge()
        launcher = AgRunner(ide_bridge=mock_bridge)
        service = MagicMock()
        claim_registry = MemoryClaimRegistry()
        request = LaunchRequest(self.working_directory, sandbox="read-only", approval_policy="never")

        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            result = run_execution(
                self.store,
                service,
                None,  # read-only: no writer registry
                claim_registry,
                launcher,
                self.project_id,
                self.task_id,
                "exec-a",
                "Analyze repo",
                request,
                access="read_only",
                provider="antigravity",
            )

        self.assertEqual(result["terminal"]["execution"]["status"], "completed")
        self.assertEqual(result["terminal"]["execution"]["provider"], "antigravity")
        self.assertEqual(result["terminal"]["execution"]["access"], "read_only")
        self.assertIsNone(result["terminal"]["execution"]["lease_evidence"])

        # Verify Session link
        session_id = result["terminal"]["execution"]["session_id"]
        self.assertEqual(session_id, "antigravity:ag-live-abc12345")
        saved_session = self.store.get("sessions", self.project_id, session_id)
        self.assertEqual(saved_session["status"], "completed")
        self.assertEqual(saved_session["provider_session_id"], "ag-live-abc12345")
        self.assertEqual(saved_session["provider"], "antigravity")

        # Verify launcher lifecycle sequence
        self.assertEqual(mock_bridge.events, ["prepare", "start", "wait", "close"])



class MockCliProcess:
    """Minimal mock for AgCliProcess in CLI-mode launch_task tests."""
    def __init__(self):
        self.events = []
        self._process = type("P", (), {
            "poll": lambda self: 0,
            "wait": lambda self, timeout=None: 0,
            "pid": 9999,
        })()
        self.pid = 9999

    def poll(self): return 0
    def wait(self, timeout=None): return 0


class MockCliRunner:
    """Mock OfficialAgCliRunner compatible with AgRunner facade for CLI path."""

    def __init__(self):
        self.events = []
        self.request = None

    def prepare(self, request):
        self.events.append("prepare")
        self.request = request
        prep = PreparedLaunch(
            thread_id="ag-cli-abc99999",
            session_path=None,
            pid=9999,
            process_creation_identity="cli-user",
            prepared_at="2026-08-20T00:00:00Z",
            mode="cli",
            _target=None,
            _request=request,
        )
        prep._process = type("P", (), {
            "poll": lambda self: 0,
            "wait": lambda self, timeout=None: 0,
            "pid": 9999,
        })()
        return prep

    def start(self, prepared, prompt):
        self.events.append("start")
        return RunningLaunch(
            prepared=prepared,
            turn_id="turn-cli-001",
            started_at="2026-08-20T00:00:01Z",
        )

    def set_heartbeat(self, running, callback):
        running._heartbeat = callback

    def wait(self, running):
        self.events.append("wait")
        return LaunchOutcome(
            status="completed",
            thread_id="ag-cli-abc99999",
            turn_id="turn-cli-001",
            completed_at="2026-08-20T00:00:10Z",
            response_text="Preflight complete",
            stats={},
        )

    def close(self, prepared):
        self.events.append("close")


class TestAgLaunchTaskPath(unittest.TestCase):
    """Tests covering launch_task(provider='antigravity') — the full dispatch path."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.working_directory = str(Path(self.temp.name).resolve())
        self.store = build_store(
            read_only=True,
            working_directory=self.working_directory,
            provider="antigravity",
        )
        self.store.latest = lambda *args: (_ for _ in ()).throw(TaskError("found 0"))

    def tearDown(self):
        self.temp.cleanup()

    def _make_runner(self, cli_runner=None):
        cli_runner = cli_runner or MockCliRunner()
        # An explicit dead IDE bridge: with the language-server bridge real,
        # a bare AgRunner(cli_runner=...) would auto-discover the user's live
        # IDE and dispatch a REAL model turn from a unit test (it did, three
        # times, on 2026-09-05 -- see conftest.py's live-Antigravity fence).
        dead_bridge = MockAgBridge()
        dead_bridge.is_alive_val = False
        launcher = AgRunner(ide_bridge=dead_bridge, cli_runner=cli_runner)
        launcher.last_fallback_reason = None
        return launcher, cli_runner

    def test_launch_task_antigravity_session_id_format(self):
        launcher, cli_runner = self._make_runner()
        service = MagicMock()
        claim_registry = MemoryClaimRegistry()

        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.dispatcher.dispatch") as mock_dispatch, \
             patch("manager.execution_runner.dispatch") as mock_er_dispatch:

            dispatched = {
                "recommended_provider": "antigravity",
                "provider": "antigravity",
                "account_id": None,
                "model": None,
                "fallback_model": None,
                "mode": "interactive",
                "effort": "medium",
                "selection_reason": ["test"],
                "quota_evidence": {"antigravity": {}},
                "estimated_minutes": 20,
                "split_recommended": False,
                "phase_count": 1,
                "alternatives": [],
                "quota_summary": "unknown",
                "warnings": [],
                "generated_prompt": "Analyze the repo structure.",
            }
            mock_dispatch.return_value = dispatched
            mock_er_dispatch.return_value = dispatched

            result = launch_task(
                self.store,
                service,
                None,  # read-only: no writer registry
                claim_registry,
                launcher,
                "p1",
                "t1",
                execution_id="exec-ag-001",
                provider="antigravity",
            )

        self.assertEqual(result["dispatch"]["provider"], "antigravity")
        session_id = result["terminal"]["execution"]["session_id"]
        self.assertTrue(
            session_id.startswith("antigravity:ag-cli-"),
            f"Expected 'antigravity:ag-cli-...' prefix, got {session_id!r}",
        )
        self.assertEqual(result["terminal"]["execution"]["status"], "completed")
        self.assertEqual(result["terminal"]["execution"]["provider"], "antigravity")
        self.assertEqual(cli_runner.request.project_id, "p1")
        self.assertEqual(cli_runner.request.working_directory, self.working_directory)
        self.assertEqual(cli_runner.request.sandbox, "read-only")
        self.assertEqual(cli_runner.request.approval_policy, "never")

    def test_launch_task_antigravity_read_only_preserved(self):
        """read_only task produces read_only execution with no lease_evidence."""
        launcher, _ = self._make_runner()
        claim_registry = MemoryClaimRegistry()

        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner.dispatch") as mock_dispatch:

            mock_dispatch.return_value = {
                "recommended_provider": "antigravity",
                "provider": "antigravity",
                "account_id": None,
                "model": None,
                "fallback_model": None,
                "mode": "interactive",
                "effort": "medium",
                "selection_reason": ["test"],
                "quota_evidence": {"antigravity": {}},
                "estimated_minutes": 20,
                "split_recommended": False,
                "phase_count": 1,
                "alternatives": [],
                "quota_summary": "unknown",
                "warnings": [],
                "generated_prompt": "Preflight read-only analysis.",
            }
            result = launch_task(
                self.store,
                MagicMock(),
                None,
                claim_registry,
                launcher,
                "p1",
                "t1",
                execution_id="exec-ag-002",
                provider="antigravity",
            )

        execution = result["terminal"]["execution"]
        self.assertEqual(execution["access"], "read_only")
        self.assertIsNone(execution["lease_evidence"])

    def test_launch_task_antigravity_failure_classification_propagates(self):
        """A provider failure classification reaches the terminal execution record."""
        cli_runner = MockCliRunner()
        cli_runner.wait = lambda running: LaunchOutcome(
            status="failed",
            thread_id="ag-cli-abc99999",
            turn_id="turn-cli-001",
            completed_at="2026-08-20T00:00:05Z",
            failure_classification="turn_timeout",
            failure_detail="Antigravity turn exceeded timeout",
        )
        launcher, _ = self._make_runner(cli_runner)
        claim_registry = MemoryClaimRegistry()

        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner.dispatch") as mock_dispatch:

            mock_dispatch.return_value = {
                "recommended_provider": "antigravity",
                "provider": "antigravity",
                "account_id": None,
                "model": None,
                "fallback_model": None,
                "mode": "interactive",
                "effort": "medium",
                "selection_reason": ["test"],
                "quota_evidence": {"antigravity": {}},
                "estimated_minutes": 20,
                "split_recommended": False,
                "phase_count": 1,
                "alternatives": [],
                "quota_summary": "unknown",
                "warnings": [],
                "generated_prompt": "Analyze with timeout.",
            }
            result = launch_task(
                self.store,
                MagicMock(),
                None,
                claim_registry,
                launcher,
                "p1",
                "t1",
                execution_id="exec-ag-003",
                provider="antigravity",
            )

        execution = result["terminal"]["execution"]
        self.assertEqual(execution["status"], "failed")
        self.assertEqual(execution["provider"], "antigravity")


if __name__ == "__main__":
    unittest.main()
