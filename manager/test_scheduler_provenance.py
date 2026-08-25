import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from manager import scheduler_provenance as provenance


class SchedulerProvenanceTests(unittest.TestCase):
    TASK = "AI Development Manager - Command Watcher"

    def raw_event(self, event_id, record_id, instance, pid=None, message="", when=None, task_name=None):
        when = when or datetime.now(timezone.utc)
        fields = f'<Data Name="TaskName">{task_name or self.TASK}</Data>'
        if instance is not None:
            fields += f'<Data Name="InstanceId">{instance}</Data>'
        if pid is not None:
            fields += f'<Data Name="ProcessID">{pid}</Data>'
        return {"record_id": record_id, "event_id": event_id, "time_created": when.isoformat(), "message": message,
                "xml": f'<Event><System><EventID>{event_id}</EventID><EventRecordID>{record_id}</EventRecordID><TimeCreated SystemTime="{when.isoformat()}" /></System><EventData>{fields}</EventData></Event>'}

    def os_context(self):
        return {"task_name": self.TASK, "wrapper_pid": 222, "wrapper_parent_pid": 111,
                "wrapper_creation_identity": "wrapper-222"}

    @staticmethod
    def windows_identity(created):
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        return f"windows-filetime:{int((created - epoch).total_seconds() * 10_000_000)}"

    @staticmethod
    def os_pass():
        return {"status": "PASS", "trigger_origin": "scheduled_time", "instance_id": "i-1",
                "trigger_event_record_id": 10, "action_event_record_id": 12, "action_process_id": 111}

    def correlated(self, events, identity="wrapper-222"):
        context = {**self.os_context(), "wrapper_creation_identity": identity}
        with patch.object(provenance.os, "name", "nt"), \
             patch.object(provenance, "process_identity_state", return_value="live"):
            return provenance.correlate_os_evidence(context, datetime.now(timezone.utc).isoformat(),
                                                     reader=lambda *_: events)

    def test_unique_natural_os_instance_requires_event_129_pid_link(self):
        evidence = self.correlated([
            self.raw_event(107, 10, "i-1", message="due to a time trigger condition", task_name="\\" + self.TASK),
            self.raw_event(129, 12, "i-1", 111, task_name="\\" + self.TASK),
        ])
        self.assertEqual("PASS", evidence["status"])
        self.assertEqual("scheduled_time", evidence["trigger_origin"])
        self.assertEqual(111, evidence["action_process_id"])

    def test_pid_mismatch_is_fail_and_missing_or_duplicate_is_unknown(self):
        mismatch = self.correlated([self.raw_event(129, 12, "i-1", 99)])
        self.assertEqual("FAIL", mismatch["status"])
        missing = self.correlated([])
        self.assertEqual("UNKNOWN", missing["status"])
        duplicate = self.correlated([
            self.raw_event(107, 10, "i-1", message="scheduled time"), self.raw_event(129, 12, "i-1", 111),
            self.raw_event(107, 20, "i-2", message="scheduled time"), self.raw_event(129, 22, "i-2", 111),
        ])
        self.assertEqual("UNKNOWN", duplicate["status"])

    def test_task_name_and_pid_contracts_are_exact(self):
        scheduled = self.raw_event(107, 10, "i-1", message="scheduled time")
        action = self.raw_event(129, 12, "i-1", 111)
        self.assertEqual("UNKNOWN", self.correlated([
            self.raw_event(107, 10, "i-1", message="scheduled time", task_name="Other Task"), action,
        ])["status"])
        missing_parent = {key: value for key, value in self.os_context().items() if key != "wrapper_parent_pid"}
        with patch.object(provenance.os, "name", "nt"), patch.object(provenance, "process_identity_state", return_value="live"):
            self.assertEqual("UNKNOWN", provenance.correlate_os_evidence(missing_parent, datetime.now(timezone.utc).isoformat(), reader=lambda *_: [scheduled, action])["status"])
        missing_wrapper = {key: value for key, value in self.os_context().items() if key != "wrapper_pid"}
        with patch.object(provenance.os, "name", "nt"), patch.object(provenance, "process_identity_state", return_value="live"):
            self.assertEqual("UNKNOWN", provenance.correlate_os_evidence(missing_wrapper, datetime.now(timezone.utc).isoformat(), reader=lambda *_: [scheduled, action])["status"])

    def test_adjacent_tick_action_does_not_contaminate_unique_pid_match(self):
        evidence = self.correlated([
            self.raw_event(107, 10, "current", message="scheduled time"), self.raw_event(129, 12, "current", 111),
            self.raw_event(107, 20, "adjacent", message="scheduled time"), self.raw_event(129, 22, "adjacent", 999),
        ])
        self.assertEqual(("PASS", "current"), (evidence["status"], evidence["instance_id"]))

    def test_event129_parent_before_wrapper_has_bounded_causality(self):
        created = datetime.now(timezone.utc)
        identity = self.windows_identity(created)
        for delta, expected in ((-.119, "PASS"), (0, "PASS"), (.5, "PASS"),
                                (2, "FAIL"), (-6, "FAIL")):
            with self.subTest(delta=delta):
                evidence = self.correlated([
                    self.raw_event(107, 10, "i-1", message="scheduled time", when=created),
                    self.raw_event(129, 12, "i-1", 111,
                                   when=created + timedelta(seconds=delta)),
                ], identity)
                self.assertEqual(expected, evidence["status"])

    def test_event129_without_instance_uses_only_its_unique_prior_event107(self):
        created = datetime.now(timezone.utc)
        evidence = self.correlated([
            self.raw_event(107, 10, "i-1", message="scheduled time", when=created - timedelta(milliseconds=125)),
            self.raw_event(129, 12, None, 111, when=created - timedelta(milliseconds=119)),
            self.raw_event(107, 20, "adjacent", message="scheduled time", when=created - timedelta(seconds=2)),
        ], self.windows_identity(created))
        self.assertEqual(("PASS", "i-1"), (evidence["status"], evidence["instance_id"]))

    def test_missing_chain_parts_malformed_and_instance_mismatch_are_unknown(self):
        for events in (
            [self.raw_event(100, 11, "i-1"), self.raw_event(129, 12, "i-1", 111)],
            [self.raw_event(107, 10, "i-1", message="manual run"), self.raw_event(129, 12, "i-1", 111)],
            [self.raw_event(107, 10, "i-1", message="scheduled time")],
            [self.raw_event(107, 10, "i-1", message="scheduled time"), self.raw_event(129, 12, "i-2", 111)],
            [{"xml": "not xml"}],
        ):
            self.assertEqual("UNKNOWN", self.correlated(events)["status"])

    def test_ignorenew_is_recorded_without_binding_and_old_events_are_ignored(self):
        evidence = self.correlated([self.raw_event(322, 99, "running-instance")])
        self.assertEqual("UNKNOWN", evidence["status"])
        self.assertEqual("running-instance", evidence["ignore_new_events"][0]["instance_id"])
        old = datetime.now(timezone.utc) - timedelta(seconds=provenance.EVENT_WINDOW_SECONDS + 1)
        self.assertEqual("UNKNOWN", self.correlated([
            self.raw_event(107, 10, "i-1", when=old), self.raw_event(100, 11, "i-1", when=old),
            self.raw_event(129, 12, "i-1", 111, when=old),
        ])["status"])

    def test_identity_mismatch_and_log_failure_fail_closed(self):
        with patch.object(provenance.os, "name", "nt"), \
             patch.object(provenance, "process_identity_state", return_value="replaced"):
            evidence = provenance.correlate_os_evidence(self.os_context(), datetime.now(timezone.utc).isoformat(), reader=lambda *_: [])
        self.assertEqual("FAIL", evidence["status"])
        with patch.object(provenance.os, "name", "nt"):
            status, events, _ = provenance.read_os_events(datetime.now(timezone.utc).isoformat(), reader=lambda *_: (_ for _ in ()).throw(OSError()))
        self.assertEqual(("UNKNOWN", []), (status, events))

    def test_manual_text_cannot_pass_as_a_scheduled_trigger(self):
        evidence = self.correlated([
            self.raw_event(107, 10, "i-1", message="manual run"),
            self.raw_event(129, 12, "i-1", 111),
        ])
        self.assertEqual("UNKNOWN", evidence["status"])

    def test_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as home, patch.object(provenance, "MAX_RECORDS", 2), patch.object(provenance, "RETAIN_DAYS", 1):
            directory = os.path.join(home, "runtime", "scheduler-invocations"); os.makedirs(directory)
            for index, age in enumerate((3, 2, 0)):
                with open(os.path.join(directory, f"{index}.json"), "w", encoding="utf-8") as handle:
                    json.dump({"started_at": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()}, handle)
            provenance._cleanup(provenance.Path(directory))
            self.assertEqual(1, len(os.listdir(directory)))

    def test_retention_never_deletes_running_invocation(self):
        with tempfile.TemporaryDirectory() as home, patch.object(provenance, "RETAIN_DAYS", 1):
            directory = provenance.Path(home); path = directory / "running.json"
            path.write_text(json.dumps({"started_at": "2000-01-01T00:00:00Z", "status": "running"}), encoding="utf-8")
            provenance._cleanup(directory)
            self.assertTrue(path.exists())

    def test_evidence_status_requires_complete_scheduled_os_proof(self):
        command = {"process_provenance": {"caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32,
                   "wrapper_pid": 222, "wrapper_parent_pid": 111, "wrapper_creation_identity": "wrapper-222",
                   "os_scheduler_evidence": self.os_pass()}}
        provider = {"provider_evidence": {"scheduler_invocation_id": "a" * 32, "launcher_pid": 1,
                    "launcher_creation_identity": "launcher", "provider_pid": 2,
                    "provider_creation_identity": "provider", "provider_parent_identity": "launcher"}}
        self.assertEqual("PASS", provenance.evidence_status(command, provider))
        command["process_provenance"]["os_scheduler_evidence"] = {"status": "UNKNOWN"}
        self.assertEqual("UNKNOWN", provenance.evidence_status(command, provider))
        command["process_provenance"]["os_scheduler_evidence"] = {**self.os_pass(), "action_process_id": 99}
        self.assertEqual("FAIL", provenance.evidence_status(command, provider))

    def test_legacy_context_preserves_id_without_new_fields_or_pass(self):
        context = {"scheduler_invocation_id": "a" * 32}
        origin = provenance.command_origin(context)
        self.assertEqual({"caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32}, origin)
        provider = {"provider_evidence": {"scheduler_invocation_id": "a" * 32, "launcher_pid": 1,
                    "launcher_creation_identity": "launcher", "provider_pid": 2,
                    "provider_creation_identity": "provider", "provider_parent_identity": "launcher"}}
        self.assertEqual("UNKNOWN", provenance.evidence_status({"process_provenance": origin}, provider))

    def test_installed_watcher_and_supervisor_use_ignorenew(self):
        manager = provenance.Path(__file__).parent
        for filename in ("install_command_watcher.ps1", "install_session_center_supervisor.ps1"):
            self.assertIn("-MultipleInstances IgnoreNew", (manager / filename).read_text(encoding="utf-8"))

    def test_start_threads_os_evidence_to_command_provenance(self):
        with tempfile.TemporaryDirectory() as home, \
             patch.object(provenance.os, "getppid", return_value=41), \
             patch.object(provenance, "process_creation_identity", side_effect=lambda pid: f"identity-{pid}"), \
             patch.object(provenance, "correlate_os_evidence", return_value=self.os_pass()):
            context = provenance.start(home, "command_watcher", {
                provenance.ENV_ID: "12345678-1234-1234-1234-123456789abc", provenance.ENV_TASK: self.TASK,
                provenance.ENV_WRAPPER_PID: "41", provenance.ENV_WRAPPER_PARENT_PID: "111",
            })
        origin = provenance.command_origin(context)
        self.assertEqual(41, origin["wrapper_pid"])
        self.assertEqual("PASS", origin["os_scheduler_evidence"]["status"])

    def test_verified_wrapper_context_persists_start_and_end(self):
        with tempfile.TemporaryDirectory() as home, \
             patch.object(provenance.os, "getppid", return_value=41), \
             patch.object(provenance, "process_creation_identity", side_effect=lambda pid: f"identity-{pid}"):
            context = provenance.start(home, "command_watcher", {
                provenance.ENV_ID: "12345678-1234-1234-1234-123456789abc",
                provenance.ENV_TASK: "AI Development Manager - Command Watcher",
                provenance.ENV_WRAPPER_PID: "41", provenance.ENV_WRAPPER_PARENT_PID: "111",
                provenance.ENV_TRIGGER: "scheduled",
            })
            provenance.finish(home, context, "completed")
            path = os.path.join(home, "runtime", "scheduler-invocations", context["scheduler_invocation_id"] + ".json")
            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.read())
        self.assertEqual("unknown", record["trigger_origin"])
        self.assertEqual(41, record["wrapper_pid"])
        self.assertEqual(111, record["wrapper_parent_pid"])
        self.assertTrue(record["python_pid"] > 0)
        self.assertEqual("completed", record["status"])
        self.assertIsNotNone(record["ended_at"])
        self.assertNotIn(provenance.ENV_TRIGGER, record)

    def test_direct_or_spoofed_context_is_not_watcher_context(self):
        with patch.object(provenance.os, "getppid", return_value=99):
            self.assertIsNone(provenance.context_from_environment({
                provenance.ENV_ID: "12345678-1234-1234-1234-123456789abc",
                provenance.ENV_TASK: "watcher", provenance.ENV_WRAPPER_PID: "41", provenance.ENV_WRAPPER_PARENT_PID: "111",
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
        self.assertEqual("UNKNOWN", provenance.evidence_status(command, {"provider_evidence": evidence}))

    def test_legacy_records_are_unknown_not_pass(self):
        self.assertEqual("UNKNOWN", provenance.evidence_status({}, {"provider_evidence": {"pid": 123}}))


if __name__ == "__main__":
    unittest.main()
