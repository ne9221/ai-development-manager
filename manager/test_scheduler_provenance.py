import json
import os
import tempfile
import unittest
from unittest.mock import patch

from manager import scheduler_provenance as provenance


class SchedulerProvenanceTests(unittest.TestCase):
    def test_verified_wrapper_context_persists_start_and_end(self):
        with tempfile.TemporaryDirectory() as home, \
             patch.object(provenance.os, "getppid", return_value=41), \
             patch.object(provenance, "process_creation_identity", side_effect=lambda pid: f"identity-{pid}"):
            context = provenance.start(home, "command_watcher", {
                provenance.ENV_ID: "12345678-1234-1234-1234-123456789abc",
                provenance.ENV_TASK: "AI Development Manager - Command Watcher",
                provenance.ENV_WRAPPER_PID: "41",
                provenance.ENV_TRIGGER: "scheduled",
            })
            provenance.finish(home, context, "completed")
            path = os.path.join(home, "runtime", "scheduler-invocations", context["scheduler_invocation_id"] + ".json")
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.read())
        self.assertEqual("unknown", record["trigger_origin"])
        self.assertEqual(41, record["wrapper_pid"])
        self.assertTrue(record["python_pid"] > 0)
        self.assertEqual("completed", record["status"])
        self.assertIsNotNone(record["ended_at"])
        self.assertNotIn(provenance.ENV_TRIGGER, record)

    def test_direct_or_spoofed_context_is_not_watcher_context(self):
        with patch.object(provenance.os, "getppid", return_value=99):
            self.assertIsNone(provenance.context_from_environment({
                provenance.ENV_ID: "12345678-1234-1234-1234-123456789abc",
                provenance.ENV_TASK: "watcher", provenance.ENV_WRAPPER_PID: "41",
            }))
        self.assertEqual({"caller_origin": "direct_or_unknown", "scheduler_invocation_id": None},
                         provenance.command_origin())

    def test_missing_and_mismatched_provider_linkage_fail_closed(self):
        command = {"process_provenance": {"caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32}}
        self.assertEqual("UNKNOWN", provenance.evidence_status(command, {"provider_evidence": {}}))
        self.assertEqual("FAIL", provenance.evidence_status(command, {"provider_evidence": {"scheduler_invocation_id": "b" * 32}}))
        evidence = {"scheduler_invocation_id": "a" * 32, "launcher_pid": 1,
                    "launcher_creation_identity": "launcher", "provider_pid": 2,
                    "provider_creation_identity": "provider", "provider_parent_identity": "launcher"}
        self.assertEqual("PASS", provenance.evidence_status(command, {"provider_evidence": evidence}))

    def test_legacy_records_are_unknown_not_pass(self):
        self.assertEqual("UNKNOWN", provenance.evidence_status({}, {"provider_evidence": {"pid": 123}}))


if __name__ == "__main__":
    unittest.main()
