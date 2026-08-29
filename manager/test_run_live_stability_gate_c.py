import unittest
from unittest.mock import patch

from manager.run_live_stability_gate_c import BoundedEvidenceStore


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


if __name__ == "__main__":
    unittest.main()
