import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from manager import scheduler_provenance as provenance


class SchedulerProvenanceTests(unittest.TestCase):
    TASK = "AI Development Manager - Command Watcher"

    def raw_event(self, event_id, record_id, instance, pid=None, message="", when=None):
        when = when or datetime.now(timezone.utc)
        fields = f'<Data Name="TaskName">{self.TASK}</Data><Data Name="InstanceId">{instance}</Data>'
        if pid is not None:
            fields += f'<Data Name="ProcessId">{pid}</Data>'
        return {"record_id": record_id, "event_id": event_id, "time_created": when.isoformat(), "message": message,
                "xml": f'<Event><System><EventID>{event_id}</EventID><EventRecordID>{record_id}</EventRecordID><TimeCreated SystemTime="{when.isoformat()}" /></System><EventData>{fields}</EventData></Event>'}

    def os_context(self):
        return {"task_name": self.TASK, "wrapper_pid": 41, "wrapper_creation_identity": "wrapper-41"}

    def correlated(self, events):
        with patch.object(provenance.os, "name", "nt"), \
             patch.object(provenance, "process_identity_state", return_value="live"):
            return provenance.correlate_os_evidence(self.os_context(), datetime.now(timezone.utc).isoformat(),
                                                     reader=lambda *_: events)

    def test_unique_natural_os_instance_requires_event_129_pid_link(self):
        evidence = self.correlated([
            self.raw_event(107, 10, "i-1", message="due to a time trigger condition"),
            self.raw_event(100, 11, "i-1"), self.raw_event(129, 12, "i-1", 41),
        ])
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual("scheduled_time", evidence["trigger_origin"])
        self.assertEqual(41, evidence["action_process_id"])

    def test_pid_mismatch_is_fail_and_missing_or_duplicate_is_unknown(self):
        mismatch = self.correlated([self.raw_event(129, 12, "i-1", 99)])
        self.assertEqual("FAIL", mismatch["status"])
        missing = self.correlated([])
        self.assertEqual("UNKNOWN", missing["status"])
        duplicate = self.correlated([
            self.raw_event(107, 10, "i-1"), self.raw_event(100, 11, "i-1"), self.raw_event(129, 12, "i-1", 41),
            self.raw_event(107, 20, "i-2"), self.raw_event(100, 21, "i-2"), self.raw_event(129, 22, "i-2", 41),
        ])
        self.assertEqual("UNKNOWN", duplicate["status"])

    def test_ignorenew_is_recorded_without_binding_and_old_events_are_ignored(self):
        evidence = self.correlated([self.raw_event(322, 99, "running-instance")])
        self.assertEqual("UNKNOWN", evidence["status"])
        self.assertEqual("running-instance", evidence["ignore_new_events"][0]["instance_id"])
        old = datetime.now(timezone.utc) - timedelta(seconds=provenance.EVENT_WINDOW_SECONDS + 1)
        self.assertEqual("UNKNOWN", self.correlated([
            self.raw_event(107, 10, "i-1", when=old), self.raw_event(100, 11, "i-1", when=old),
            self.raw_event(129, 12, "i-1", 41, when=old),
        ])["status"])

    def test_identity_mismatch_and_log_failure_fail_closed(self):
        with patch.object(provenance.os, "name", "nt"), \
             patch.object(provenance, "process_identity_state", return_value="replaced"):
            evidence = provenance.correlate_os_evidence(self.os_context(), datetime.now(timezone.utc).isoformat(), reader=lambda *_: [])
        self.assertEqual("FAIL", evidence["status"])
        with patch.object(provenance.os, "name", "nt"):
            status, events, _ = provenance.read_os_events(datetime.now(timezone.utc).isoformat(), reader=lambda *_: (_ for _ in ()).throw(OSError()))
        self.assertEqual(("UNKNOWN", []), (status, events))

    def test_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as home, patch.object(provenance, "MAX_RECORDS", 2), patch.object(provenance, "RETAIN_DAYS", 1):
            directory = os.path.join(home, "runtime", "scheduler-invocations"); os.makedirs(directory)
            for index, age in enumerate((3, 2, 0)):
                with open(os.path.join(directory, f"{index}.json"), "w", encoding="utf-8") as handle:
                    json.dump({"started_at": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()}, handle)
            provenance._cleanup(provenance.Path(directory))
            self.assertEqual(1, len(os.listdir(directory)))

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
