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


if __name__ == "__main__":
    unittest.main()
