import unittest

from manager.tasks import TaskError, validate
from manager.test_command_watcher import command


ORIGIN = {
    "caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32,
    "wrapper_pid": 41, "wrapper_creation_identity": "windows-filetime:123456789",
    "os_scheduler_evidence": {
        "status": "PASS", "reason": "event_129_pid_and_instance_link",
        "task_name": "AI Development Manager - Command Watcher", "instance_id": "instance-1",
        "trigger_event_record_id": 10, "trigger_event_id": 107, "trigger_time": "2026-08-25T00:00:00Z",
        "action_event_record_id": 12, "action_process_id": 41, "action_executable": "powershell.exe",
        "trigger_origin": "scheduled_time", "ignore_new_events": [],
    },
}


class CommandProvenanceSchemaTests(unittest.TestCase):
    def test_realistic_command_producer_shape_validates(self):
        validate("command", command(process_provenance=ORIGIN))

    def test_legacy_command_shapes_remain_valid(self):
        validate("command", command())
        validate("command", command(process_provenance={"caller_origin": "watcher_poll", "scheduler_invocation_id": "a" * 32}))

    def test_malformed_command_evidence_fails_closed(self):
        bad = {**ORIGIN, "os_scheduler_evidence": {**ORIGIN["os_scheduler_evidence"], "trigger_origin": "manual"}}
        with self.assertRaises(TaskError):
            validate("command", command(process_provenance=bad))

    def test_realistic_execution_producer_shape_validates(self):
        validate("execution", {
            "execution_id": "e1", "task_id": "t1", "project_id": "p1", "provider": "codex",
            "mode": "code", "effort": "medium", "started_at": "2026-08-25T00:00:00Z",
            "completed_at": None, "elapsed_minutes": None, "status": "running", "quota_before": {},
            "quota_after": None, "quota_delta": None, "source_confidence": "fresh", "notes": [],
            "task_snapshot": {}, "provider_evidence": {
                "host": "HOME", "pid": 4242, "creation_identity": "provider",
                "started_at": "2026-08-25T00:00:00Z", "launcher_pid": 41,
                "launcher_creation_identity": "launcher", "provider_pid": 4242,
                "provider_creation_identity": "provider", "provider_parent_identity": "launcher",
                "scheduler_invocation_id": "a" * 32,
            },
        })


if __name__ == "__main__":
    unittest.main()
