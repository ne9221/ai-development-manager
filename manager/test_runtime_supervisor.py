import json
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from manager import health_evidence, scheduler_provenance
from manager.dashboard_core import ServiceHealthViewModel
from manager.runtime_supervisor import (
    HEARTBEAT_MAX_AGE_SECONDS, RECOVERY_COOLDOWN_SECONDS, SWEEP_MIN_INTERVAL_SECONDS, TASK_NAMES,
    _component_state, _cooldown_ok, _record_recovery_attempt, _should_run_sweep, _task_status_is_running,
    check_and_recover, check_heartbeat_component, check_quota, query_scheduled_task, recover_scheduled_task,
    try_check_and_recover,
)


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def fake_run(returncode=0, stdout="", stderr=""):
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class QueryScheduledTaskTests(unittest.TestCase):
    def test_returns_stdout_on_success(self):
        runner = Mock(return_value=fake_run(0, "Status: Ready"))
        self.assertEqual("Status: Ready", query_scheduled_task("Some Task", runner=runner))

    def test_returns_none_on_nonzero_exit(self):
        runner = Mock(return_value=fake_run(1))
        self.assertIsNone(query_scheduled_task("Some Task", runner=runner))

    def test_returns_none_when_runner_raises(self):
        runner = Mock(side_effect=OSError("schtasks not found"))
        self.assertIsNone(query_scheduled_task("Some Task", runner=runner))


class RecoverScheduledTaskTests(unittest.TestCase):
    def test_only_ever_calls_run_never_enable(self):
        runner = Mock(return_value=fake_run(0))
        self.assertEqual("attempted", recover_scheduled_task("Some Task", runner=runner))
        self.assertEqual(1, runner.call_count)
        args = runner.call_args.args[0]
        self.assertIn("/Run", args)
        self.assertNotIn("/Change", args)
        self.assertNotIn("/ENABLE", args)

    def test_run_failure_reports_outcome(self):
        runner = Mock(return_value=fake_run(5))
        self.assertEqual("run_failed:5", recover_scheduled_task("Some Task", runner=runner))

    def test_runner_exception_reports_outcome(self):
        runner = Mock(side_effect=OSError("boom"))
        self.assertEqual("error:OSError", recover_scheduled_task("Some Task", runner=runner))


class ComponentStateTests(unittest.TestCase):
    def test_disabled_task_is_degraded_with_no_remediation(self):
        task_output = "Status: Ready\nScheduled Task State: Disabled"
        state, reason, remediation, health = _component_state("command_watcher", NOW, task_output, {})
        self.assertEqual("degraded", state)
        self.assertEqual("scheduled_task_disabled", reason)
        self.assertIsNone(remediation)  # never auto-recovered -- see module docstring
        self.assertEqual("Offline", health.status_label)

    def test_enabled_with_fresh_heartbeat_is_healthy(self):
        heartbeats = {"command_watcher": {"updated_at": (NOW - timedelta(seconds=30)).isoformat()}}
        state, reason, remediation, _ = _component_state("command_watcher", NOW, "Status: Ready", heartbeats)
        self.assertEqual("healthy", state)
        self.assertIsNone(reason)
        self.assertIsNone(remediation)

    def test_enabled_with_stale_heartbeat_is_degraded_and_recoverable(self):
        stale_at = NOW - timedelta(seconds=HEARTBEAT_MAX_AGE_SECONDS + 60)
        heartbeats = {"command_watcher": {"updated_at": stale_at.isoformat()}}
        state, reason, remediation, _ = _component_state("command_watcher", NOW, "Status: Ready", heartbeats)
        self.assertEqual("degraded", state)
        self.assertEqual("heartbeat_stale", reason)
        self.assertEqual("scheduled_task_heartbeat_stale_restart", remediation)

    def test_enabled_with_no_heartbeat_ever_is_unknown_not_healthy_or_degraded(self):
        state, reason, remediation, _ = _component_state("command_watcher", NOW, "Status: Ready", {})
        self.assertEqual("unknown", state)
        self.assertIsNone(remediation)

    def test_task_query_failed_and_no_heartbeat_is_unknown(self):
        state, reason, remediation, health = _component_state("command_watcher", NOW, None, {})
        self.assertEqual("unknown", state)
        self.assertFalse(health.found)

    def test_stale_heartbeat_with_status_running_is_wedged_not_recoverable(self):
        """A `/Run` on a task Task Scheduler already believes is Running
        would be a silent no-op (MultipleInstances=IgnoreNew) -- this must
        be reported as a distinct, no-safe-action failure shape, never as
        an ordinary recoverable heartbeat_stale."""
        stale_at = NOW - timedelta(seconds=HEARTBEAT_MAX_AGE_SECONDS + 60)
        heartbeats = {"command_watcher": {"updated_at": stale_at.isoformat()}}
        task_output = "Status: Running\nScheduled Task State: Enabled"
        state, reason, remediation, _ = _component_state("command_watcher", NOW, task_output, heartbeats)
        self.assertEqual("degraded", state)
        self.assertEqual("heartbeat_stale_process_possibly_wedged", reason)
        self.assertIsNone(remediation)

    def test_task_query_failed_but_heartbeat_fresh_is_still_healthy(self):
        heartbeats = {"command_watcher": {"updated_at": (NOW - timedelta(seconds=10)).isoformat()}}
        state, _, _, _ = _component_state("command_watcher", NOW, None, heartbeats)
        self.assertEqual("healthy", state)


class TaskStatusIsRunningTests(unittest.TestCase):
    def test_status_running_is_true(self):
        self.assertTrue(_task_status_is_running("Status: Running\nScheduled Task State: Enabled"))

    def test_status_ready_is_false(self):
        self.assertFalse(_task_status_is_running("Status: Ready\nScheduled Task State: Enabled"))

    def test_missing_status_line_is_false(self):
        self.assertFalse(_task_status_is_running("Scheduled Task State: Enabled"))

    def test_none_output_is_false(self):
        self.assertFalse(_task_status_is_running(None))

    def test_only_first_status_line_counts(self):
        # Real schtasks /FO LIST /V output can repeat "Status:" style
        # labels for unrelated fields in some locales/versions -- only the
        # FIRST "Status:" line (Task Scheduler's own top-level status) is
        # authoritative, matching parse_scheduled_task_health's own
        # first-match convention for Status:/Scheduled Task State:.
        self.assertTrue(_task_status_is_running("Status: Running\nSomething Status: Ready"))


class CooldownTests(unittest.TestCase):
    def test_no_prior_attempt_is_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(_cooldown_ok(directory, "command_watcher", NOW))

    def test_recent_attempt_blocks_until_cooldown_elapses(self):
        with tempfile.TemporaryDirectory() as directory:
            _record_recovery_attempt(directory, "command_watcher", NOW)
            self.assertFalse(_cooldown_ok(directory, "command_watcher", NOW + timedelta(seconds=1)))
            just_after = NOW + timedelta(seconds=RECOVERY_COOLDOWN_SECONDS + 1)
            self.assertTrue(_cooldown_ok(directory, "command_watcher", just_after))

    def test_cooldown_is_independent_per_component(self):
        with tempfile.TemporaryDirectory() as directory:
            _record_recovery_attempt(directory, "command_watcher", NOW)
            self.assertTrue(_cooldown_ok(directory, "drive_dispatch_ingress", NOW))


class SweepDebounceTests(unittest.TestCase):
    def test_first_call_runs_subsequent_immediate_call_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(_should_run_sweep(directory, NOW))
            self.assertFalse(_should_run_sweep(directory, NOW + timedelta(seconds=1)))

    def test_call_after_interval_runs_again(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(_should_run_sweep(directory, NOW))
            later = NOW + timedelta(seconds=SWEEP_MIN_INTERVAL_SECONDS + 1)
            self.assertTrue(_should_run_sweep(directory, later))


class CheckQuotaTests(unittest.TestCase):
    def test_drive_read_failure_reports_unreachable(self):
        def factory():
            raise RuntimeError("no credentials")
        result = check_quota(service_factory=factory)
        self.assertFalse(result["drive_reachable"])
        self.assertIsNone(result["providers"])

    def test_reachable_returns_provider_summaries(self):
        # Patch manager.quota_reader.read_drive_status directly rather than
        # faking the full Drive service -> schema-validated document chain
        # (check_quota() calls it with validate_document's real default of
        # True, matching production -- a hand-rolled fake document would
        # need to satisfy the full status.schema.json to reach this path).
        document = {"generated_at": NOW.isoformat(), "providers": [
            {"provider": "codex", "display_name": "Codex", "status": "ok", "source_type": "official",
             "confidence": "official", "source": "codex_cli_auth_json", "last_updated": NOW.isoformat(),
             "windows": [{"remaining_percent": 80.0}]},
        ]}
        with unittest.mock.patch("manager.quota_reader.read_drive_status", return_value=document):
            result = check_quota(service_factory=lambda: object())
        self.assertTrue(result["drive_reachable"])
        self.assertIsInstance(result["providers"], list)


class CheckAndRecoverIntegrationTests(unittest.TestCase):
    """End-to-end sweep with every external boundary (schtasks, Drive)
    faked, exercising the real health_evidence store + heartbeat files on
    a real temp directory -- the only things NOT faked."""

    def _write_heartbeat(self, manager_home, component, when):
        scheduler_provenance.write_heartbeat(manager_home, component, "completed")
        # Overwrite with a specific timestamp for deterministic staleness.
        path = Path(manager_home) / "runtime" / "component-heartbeat.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data[component]["updated_at"] = when.isoformat()
        path.write_text(json.dumps(data), encoding="utf-8")

    def _unreachable_quota_factory(self):
        def factory():
            raise RuntimeError("no credentials in test")
        return factory

    def test_healthy_components_produce_healthy_evidence_no_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            for component in TASK_NAMES:
                if component == "quota_refresh":
                    continue
                self._write_heartbeat(directory, component, NOW - timedelta(seconds=10))
            runner = Mock(return_value=fake_run(0, "Status: Ready"))
            results = check_and_recover(directory, now=NOW, runner=runner,
                                        service_factory=self._unreachable_quota_factory())
            for component in ("command_watcher", "drive_dispatch_ingress", "github_dispatch_ingress",
                              "session_center_supervisor"):
                self.assertEqual("healthy", results[component]["state"], component)
                self.assertIsNone(results[component]["remediation_result"])
            # /Query (health check) is expected every sweep; /Run (recovery)
            # must never fire for an already-healthy component.
            run_calls = [call for call in runner.call_args_list if "/Run" in call.args[0]]
            self.assertEqual([], run_calls)

    def test_disabled_task_never_triggers_a_run_call(self):
        with tempfile.TemporaryDirectory() as directory:
            def runner(cmd, **_kwargs):
                if cmd[1] == "/Query":
                    return fake_run(0, "Status: Ready\nScheduled Task State: Disabled")
                self.fail(f"schtasks /Run must never be called for a Disabled task: {cmd}")
            results = check_and_recover(directory, now=NOW, runner=runner,
                                        service_factory=self._unreachable_quota_factory())
            self.assertEqual("degraded", results["command_watcher"]["state"])
            self.assertIsNone(results["command_watcher"]["remediation_result"])
            entry = health_evidence.read_component(health_evidence.evidence_store_path(directory), "command_watcher")
            self.assertEqual("human_required", entry["latest"]["remediation_classification"])

    def test_stale_heartbeat_triggers_bounded_recovery_once_then_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            stale_at = NOW - timedelta(seconds=HEARTBEAT_MAX_AGE_SECONDS + 60)
            self._write_heartbeat(directory, "command_watcher", stale_at)
            run_calls = []

            def runner(cmd, **_kwargs):
                if cmd[1] == "/Query":
                    return fake_run(0, "Status: Ready")
                run_calls.append(cmd)
                return fake_run(0)

            first = check_and_recover(directory, now=NOW, runner=runner,
                                      service_factory=self._unreachable_quota_factory())
            self.assertEqual("attempted", first["command_watcher"]["remediation_result"])
            self.assertEqual(1, len(run_calls))

            second = check_and_recover(directory, now=NOW + timedelta(seconds=5), runner=runner,
                                       service_factory=self._unreachable_quota_factory())
            self.assertEqual("skipped_cooldown", second["command_watcher"]["remediation_result"])
            self.assertEqual(1, len(run_calls))  # no second /Run within cooldown

    def test_dry_run_never_calls_the_runner_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            stale_at = NOW - timedelta(seconds=HEARTBEAT_MAX_AGE_SECONDS + 60)
            self._write_heartbeat(directory, "command_watcher", stale_at)

            def runner(cmd, **_kwargs):
                if cmd[1] == "/Query":
                    return fake_run(0, "Status: Ready")
                self.fail("dry_run must never call schtasks /Run")

            result = check_and_recover(directory, now=NOW, runner=runner, dry_run=True,
                                       service_factory=self._unreachable_quota_factory())
            self.assertIsNone(result["command_watcher"]["remediation_result"])

    def test_try_check_and_recover_never_raises_even_on_internal_error(self):
        with tempfile.TemporaryDirectory() as directory:
            def exploding_runner(*_args, **_kwargs):
                raise RuntimeError("simulated failure")
            def exploding_quota_factory():
                raise RuntimeError("simulated quota failure")
            # Should not raise despite every internal check failing -- an
            # injected runner/service_factory can raise anything, and this
            # module's whole contract is "never raise into the caller".
            result = try_check_and_recover(directory, now=NOW, runner=exploding_runner,
                                           service_factory=exploding_quota_factory)
            self.assertIsInstance(result, dict)  # still completes: every failure is caught internally

    def test_try_check_and_recover_is_debounced_across_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Mock(return_value=fake_run(0, "Status: Ready"))
            first = try_check_and_recover(directory, now=NOW, runner=runner,
                                          service_factory=self._unreachable_quota_factory())
            second = try_check_and_recover(directory, now=NOW + timedelta(seconds=1), runner=runner,
                                           service_factory=self._unreachable_quota_factory())
            self.assertIsInstance(first, dict)
            self.assertIsNone(second)  # debounced -- no-op this soon after


if __name__ == "__main__":
    unittest.main()
