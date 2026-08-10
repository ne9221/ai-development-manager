import unittest
from copy import deepcopy

from manager.context_pack import context_pack
from manager.overview import add_item, empty_overview, initialize_overview, read_overview, summary, update_item
from manager.tasks import ROOT_FOLDERS, TaskError, validate


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project_id, name, document):
        self.records[(area, project_id, name)] = deepcopy(document); return deepcopy(document)
    def get(self, area, project_id, name): return deepcopy(self.records[(area, project_id, name)])


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.put("overviews", "example", "overview", empty_overview("example"))

    def test_schema_and_area_support(self):
        self.assertEqual("OVERVIEWS", ROOT_FOLDERS["overviews"])
        validate("overview", empty_overview("example"))
        with self.assertRaises(TaskError): validate("overview", {"project_id": "example"})

    def test_stable_id_duplicate_protection_and_update(self):
        add_item(self.store, "example", "P01", "First", priority="high")
        with self.assertRaises(TaskError): add_item(self.store, "example", "P01", "Duplicate")
        updated = update_item(self.store, "example", "P01", status="awaiting_validation", current_progress="Done", next_action="Validate")
        item = updated["items"][0]
        self.assertEqual(("P01", "awaiting_validation", "Done"), (item["item_id"], item["status"], item["current_progress"]))

    def test_summary_groups_all_statuses(self):
        add_item(self.store, "example", "P01", "First")
        result = summary(read_overview(self.store, "example"))
        self.assertEqual("P01", result["by_status"]["pending"][0]["item_id"])
        self.assertEqual([], result["by_status"]["merged"])

    def test_initial_seed_is_minimal_and_idempotent(self):
        first = initialize_overview(self.store, "ai-development-manager")
        second = initialize_overview(self.store, "ai-development-manager")
        self.assertEqual(["P03", "P04", "P05"], [item["item_id"] for item in first["items"]])
        self.assertEqual(first, second)


if __name__ == "__main__": unittest.main()
