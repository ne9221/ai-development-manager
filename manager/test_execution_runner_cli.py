import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from manager.codex_launcher import CodexLaunchError
from manager.execution_runner import launch_task, main
from manager.task_claims import TaskClaimConflict
from manager.tasks import TaskError
from manager.test_execution_lifecycle import build_store, quota_document
from manager.test_execution_runner import Launcher
from manager.test_task_claims import MemoryClaimRegistry
from manager.test_worktree_locks import MemoryRegistry


class RunnerEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = build_store(working_directory=str(Path(self.temp.name).resolve()))
        self.store.latest = self._missing_handoff
        self.store.project_folder = lambda *_args, **_kwargs: "executions"
        self.store.list_records = lambda *_args, **_kwargs: []

    def tearDown(self): self.temp.cleanup()

    @staticmethod
    def _missing_handoff(*_args, **_kwargs): raise TaskError("not found")

    def launch(self, launcher=None, claim=None, writer=None):
        with patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()):
            return launch_task(self.store, object(), writer or MemoryRegistry(), claim or MemoryClaimRegistry(),
                               launcher or Launcher(), "p1", "t1", "e2e-a", quota_document=quota_document(), executions=[])

    def test_mocked_e2e_dispatch_reserve_run_and_terminal_output(self):
        result = self.launch()
        self.assertEqual("e2e-a", result["execution_id"])
        self.assertEqual("completed", result["terminal"]["execution"]["status"])
        self.assertEqual("codex", result["dispatch"]["recommended_provider"])
        self.assertEqual("codex:thread-1", result["session"]["session_id"])

    def test_claim_conflict_stops_before_provider_prepare(self):
        claim = MemoryClaimRegistry()
        claim.document = {"schema_version": "0.1.0", "project_id": "p1", "task_id": "t1", "execution_id": "other", "provider": "codex", "claimed_at": "2026-08-13T00:00:00Z"}
        claim.generation = 1; launcher = Launcher()
        with self.assertRaises(TaskClaimConflict): self.launch(launcher=launcher, claim=claim)
        self.assertEqual([], launcher.events)

    def test_launch_and_protocol_failures_terminalize_interrupted(self):
        for failure in ("prepare", "wait"):
            with self.subTest(failure=failure):
                self.store = build_store(working_directory=str(Path(self.temp.name).resolve()))
                self.store.latest = self._missing_handoff
                with self.assertRaises(CodexLaunchError): self.launch(launcher=Launcher(failure=failure))
                self.assertEqual("interrupted", self.store.get("executions", "p1", "e2e-a")["status"])

    def test_main_emits_machine_readable_success_and_error(self):
        output = io.StringIO()
        with patch("manager.execution_runner.build_service", return_value=object()), \
             patch("manager.execution_runner.DriveRecords", return_value=self.store), \
             patch("manager.execution_runner.task_claim_registry", return_value=MemoryClaimRegistry()), \
             patch("manager.execution_runner.GCSLockRegistry.from_environment", return_value=MemoryRegistry()), \
             patch("manager.execution_runner.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner.CodexLauncher", return_value=Launcher()), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), redirect_stdout(output):
            self.assertEqual(0, main(["p1", "t1", "--execution-id", "cli-a"]))
        result = json.loads(output.getvalue()); self.assertEqual("completed", result["status"])

        output = io.StringIO()
        with patch("manager.execution_runner.build_service", side_effect=OSError("token=raw-secret")), redirect_stdout(output):
            self.assertEqual(1, main(["p1", "t1"]))
        error = json.loads(output.getvalue()); self.assertEqual("error", error["status"]); self.assertNotIn("raw-secret", output.getvalue())

    def test_main_reports_terminalized_provider_failure_without_raw_detail(self):
        output = io.StringIO()
        with patch("manager.execution_runner.build_service", return_value=object()), \
             patch("manager.execution_runner.DriveRecords", return_value=self.store), \
             patch("manager.execution_runner.task_claim_registry", return_value=MemoryClaimRegistry()), \
             patch("manager.execution_runner.GCSLockRegistry.from_environment", return_value=MemoryRegistry()), \
             patch("manager.execution_runner.read_drive_status", return_value=quota_document()), \
             patch("manager.execution_runner.CodexLauncher", return_value=Launcher(failure="wait")), \
             patch("manager.execution_lifecycle.validate_local_preflight"), \
             patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()), \
             patch("manager.executions.read_drive_status", return_value=quota_document()), redirect_stdout(output):
            self.assertEqual(1, main(["p1", "t1", "--execution-id", "cli-fail"]))
        result = json.loads(output.getvalue())
        self.assertEqual("interrupted", result["status"])
        self.assertEqual("timeout", result["error"]["kind"])
        self.assertNotIn("raw secret", output.getvalue())


if __name__ == "__main__": unittest.main()
