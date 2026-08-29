import unittest
from unittest.mock import patch

from manager.run_live_stability_gate_c import BoundedEvidenceStore, provider_output_matches
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


if __name__ == "__main__":
    unittest.main()
