import unittest
from unittest.mock import patch

from manager.run_live_stability_gate_c import BoundedEvidenceStore, collect_round, provider_output_matches
from manager.tasks import MIME_FOLDER, MIME_JSON, ROOT_FOLDER_ID, ROOT_FOLDERS


class BoundedEvidenceStoreTests(unittest.TestCase):
    def test_list_records_uses_bounded_read_contract(self):
        class Store:
            def list_records_bounded(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                return [{"task_id": "task-1"}]

        store = Store()
        with patch("manager.run_live_stability_gate_c.time.monotonic", return_value=100.0):
            result = BoundedEvidenceStore(store).list_records("tasks", "project-1")
        self.assertEqual([{"task_id": "task-1"}], result)
        self.assertEqual(("tasks", "project-1"), store.args)
        self.assertEqual(130.0, store.kwargs["deadline"])
        self.assertEqual(10, store.kwargs["single_request_worst_case"])

    def test_real_drive_adapter_forwards_deadline_through_folder_lookup(self):
        class MediaRequest:
            def execute(self):
                return b'{"task_id":"task-1"}'

        class Files:
            def get_media(self, fileId):
                self.file_id = fileId
                return MediaRequest()

        class Store:
            files = Files()

            def children(self, parent, name=None, deadline=None):
                self.calls.append((parent, name, deadline))
                if parent == ROOT_FOLDER_ID and name == ROOT_FOLDERS["tasks"]:
                    return [{"id": "tasks-root", "mimeType": MIME_FOLDER}]
                if parent == "tasks-root" and name == "project-1":
                    return [{"id": "project-folder", "mimeType": MIME_FOLDER}]
                if parent == "project-folder":
                    return [{"id": "record", "name": "task-1.json", "mimeType": MIME_JSON}]
                return []

            def __init__(self):
                self.calls = []

        store = Store()
        with patch("manager.run_live_stability_gate_c.time.monotonic", side_effect=[100.0, 100.0, 100.0, 100.0]):
            result = BoundedEvidenceStore(store).list_records("tasks", "project-1")
        self.assertEqual([{"task_id": "task-1"}], result)
        self.assertEqual(3, len(store.calls))
        self.assertTrue(all(call[2] == 130.0 for call in store.calls))

    def test_provider_output_accepts_independently_validated_no_change(self):
        self.assertTrue(provider_output_matches({
            "status": "completed",
            "terminal_reason": "codex verified no code change was needed (validation passed)",
            "repo_write_evidence": {"push_status": "not_applicable", "tests_status": "passed"},
        }))

    def test_collect_round_records_command_terminal_without_execution(self):
        task = {
            "task_id": "task-1", "project_id": "project-1", "status": "blocked",
            "created_at": "2026-08-29T18:00:00Z", "updated_at": "2026-08-29T18:01:00Z",
            "blocked_reason": "prelaunch_contract_or_gate_failure",
            "source_context": {"external_request_id": "request-1"},
        }
        command = {
            "command_id": "command-1", "task_id": "task-1", "project_id": "project-1",
            "status": "failed", "provider": "codex", "execution_id": "execution-1",
            "claimed_at": "2026-08-29T18:00:30Z", "completed_at": "2026-08-29T18:01:00Z",
            "created_at": "2026-08-29T18:00:10Z", "process_provenance": {},
        }

        class Store:
            def get(self, area, project_id, name):
                if area == "tasks" and name == "task-1":
                    return task
                if area == "commands" and name == "command-1":
                    return command
                raise RuntimeError("no execution")

            def list_records_bounded(self, area, project_id, **kwargs):
                return {
                    "tasks": [task], "commands": [command], "executions": [],
                    "sessions": [], "handoffs": [],
                }.get(area, [])

        with patch("manager.run_live_stability_gate_c.live_store", return_value=(Store(), object())), \
             patch("manager.run_live_stability_gate_c.build_service", return_value=object()), \
             patch("manager.run_live_stability_gate_c.DriveRecords", return_value=Store()), \
             patch("manager.run_live_stability_gate_c.dispatch_request_registry", return_value=object()), \
             patch("manager.run_live_stability_gate_c.RUN_STARTED_AT", "2026-08-29T18:00:00Z", create=True):
            evidence = collect_round(1, "request-1", {"task_id": "task-1", "command_id": "command-1"})

        self.assertEqual("failed", evidence["terminal"]["command_status"])
        self.assertIsNone(evidence["terminal"]["execution_status"])
        self.assertEqual("execution-1", evidence["execution"]["execution_id"])
        self.assertFalse(evidence["provider_output"]["observed"])


if __name__ == "__main__":
    unittest.main()
