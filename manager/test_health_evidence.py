import json
import tempfile
import unittest
from pathlib import Path

from manager.health_evidence import (
    COMPONENTS, HUMAN_REQUIRED_REMEDIATIONS, MAX_EVIDENCE_HISTORY, SAFE_AUTO_REMEDIATIONS,
    classify_remediation, command_watcher_evidence_from_task_health, drive_evidence_from_read_attempt,
    evidence_store_path, quota_evidence_from_summary, read_all, read_component, record,
    session_center_evidence_from_supervisor,
)


class ClassifyRemediationTests(unittest.TestCase):
    def test_every_safe_auto_reason_classifies_as_auto(self):
        for reason in SAFE_AUTO_REMEDIATIONS:
            self.assertEqual("auto", classify_remediation(reason))

    def test_every_explicit_human_required_reason_stays_human_required(self):
        for reason in HUMAN_REQUIRED_REMEDIATIONS:
            self.assertEqual("human_required", classify_remediation(reason))

    def test_unrecognized_reason_fails_closed_to_human_required(self):
        self.assertEqual("human_required", classify_remediation("some_reason_never_seen_before"))

    def test_none_reason_is_human_required(self):
        self.assertEqual("human_required", classify_remediation(None))

    def test_allow_list_and_deny_list_never_overlap(self):
        self.assertEqual(frozenset(), SAFE_AUTO_REMEDIATIONS & HUMAN_REQUIRED_REMEDIATIONS)


class RecordAndReadTests(unittest.TestCase):
    def test_record_then_read_round_trips_all_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            record(path, "dashboard", state="degraded", degraded_reason="dashboard_process_missing",
                   last_remediation="dashboard_process_missing", remediation_result="recovered",
                   unresolved_blocker=None, observed_pid=1234, observed_port=8501)
            latest = read_component(path, "dashboard")["latest"]
            self.assertEqual("dashboard", latest["component"])
            self.assertEqual("degraded", latest["state"])
            self.assertEqual("dashboard_process_missing", latest["degraded_reason"])
            self.assertEqual("recovered", latest["remediation_result"])
            self.assertEqual(1234, latest["observed_pid"])
            self.assertEqual(8501, latest["observed_port"])
            self.assertIsInstance(latest["timestamp"], str)
            self.assertTrue(latest["timestamp"])

    def test_state_must_reflect_an_observed_check_never_defaults_to_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            with self.assertRaises(ValueError):
                record(path, "dashboard", state="running_but_unverified")

    def test_unknown_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            with self.assertRaises(ValueError):
                record(path, "not_a_real_component", state="healthy")

    def test_missing_store_reads_as_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory) / "does" / "not" / "exist.json"
            self.assertEqual({"components": {}}, read_all(path))
            self.assertIsNone(read_component(path, "dashboard"))

    def test_malformed_store_file_fails_closed_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual({"components": {}}, read_all(path))

    def test_history_is_append_only_and_never_rewrites_prior_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            record(path, "dashboard", state="degraded", degraded_reason="dashboard_process_missing")
            first_entry = read_component(path, "dashboard")["history"][0]
            record(path, "dashboard", state="healthy")
            history = read_component(path, "dashboard")["history"]
            self.assertEqual(2, len(history))
            self.assertEqual(first_entry, history[0])  # untouched
            self.assertEqual("healthy", history[1]["state"])

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            for _ in range(MAX_EVIDENCE_HISTORY + 10):
                record(path, "dashboard", state="healthy")
            history = read_component(path, "dashboard")["history"]
            self.assertEqual(MAX_EVIDENCE_HISTORY, len(history))

    def test_components_do_not_interfere_with_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            record(path, "dashboard", state="degraded", degraded_reason="dashboard_process_missing")
            record(path, "drive", state="healthy")
            data = read_all(path)
            self.assertEqual("degraded", data["components"]["dashboard"]["latest"]["state"])
            self.assertEqual("healthy", data["components"]["drive"]["latest"]["state"])

    def test_last_remediation_is_classified_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            entry = record(path, "dashboard", state="degraded", last_remediation="dashboard_process_missing")
            self.assertEqual("auto", entry["remediation_classification"])
            entry2 = record(path, "quota", state="degraded", last_remediation="oauth_refresh_token_invalid")
            self.assertEqual("human_required", entry2["remediation_classification"])

    def test_write_is_atomic_no_partial_json_ever_observable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = evidence_store_path(directory)
            record(path, "dashboard", state="healthy")
            # A successful write leaves no .tmp file behind and the real
            # file always parses.
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())
            with open(path, encoding="utf-8") as handle:
                json.load(handle)  # must not raise


class AdapterTests(unittest.TestCase):
    """These normalize *already observed* evidence -- they never probe or
    act themselves, matching the module's read-only adapter contract."""

    def test_session_center_adapter_reports_healthy_when_no_degraded_reason(self):
        raw = {"last_health_check": "2026-08-21T00:00:00+00:00", "degraded_reason": None,
               "last_remediation": None, "recovery_result": None, "unresolved_blocker": None}
        result = session_center_evidence_from_supervisor(raw)
        self.assertEqual("healthy", result["state"])
        self.assertEqual("session_center", result["component"])

    def test_session_center_adapter_reports_degraded_with_reason(self):
        raw = {"last_health_check": "t", "degraded_reason": "correlation_failed",
               "last_remediation": {"event": "correlation_failed_detected"}, "recovery_result": "reobserving",
               "unresolved_blocker": "execution has not yet correlated"}
        result = session_center_evidence_from_supervisor(raw)
        self.assertEqual("degraded", result["state"])
        self.assertEqual("correlation_failed_detected", result["last_remediation"])
        self.assertEqual("reobserving", result["remediation_result"])

    def test_session_center_adapter_never_probed_is_unknown_not_healthy(self):
        result = session_center_evidence_from_supervisor(None)
        self.assertEqual("unknown", result["state"])

    def test_command_watcher_adapter_maps_online_offline_unknown(self):
        class FakeHealth:
            def __init__(self, status_label, detail=""):
                self.status_label = status_label
                self.detail = detail
        self.assertEqual("healthy", command_watcher_evidence_from_task_health(FakeHealth("Online"))["state"])
        self.assertEqual("degraded", command_watcher_evidence_from_task_health(FakeHealth("Offline", "Disabled"))["state"])
        self.assertEqual("unknown", command_watcher_evidence_from_task_health(FakeHealth("Unknown"))["state"])

    def test_drive_adapter_reports_degraded_on_unreachable(self):
        result = drive_evidence_from_read_attempt(False, "Drive status read failed: timeout")
        self.assertEqual("degraded", result["state"])
        self.assertIn("timeout", result["degraded_reason"])

    def test_quota_adapter_classifies_stale_as_auto_recoverable(self):
        summary = {"providers": [{"provider": "codex", "stale": True, "has_reliable_quota": False}]}
        result = quota_evidence_from_summary(summary)[0]
        self.assertEqual("degraded", result["state"])
        self.assertEqual("stale_telemetry_recollect", result["last_remediation"])
        self.assertEqual("auto", classify_remediation(result["last_remediation"]))

    def test_quota_adapter_never_claims_healthy_from_unreliable_non_stale_source(self):
        # Not stale, but source isn't official/confident -- must not be
        # silently treated as auto-fixable or healthy.
        summary = {"providers": [{"provider": "claude", "stale": False, "has_reliable_quota": False}]}
        result = quota_evidence_from_summary(summary)[0]
        self.assertEqual("degraded", result["state"])
        self.assertIsNone(result["last_remediation"])

    def test_quota_adapter_reports_healthy_for_reliable_quota(self):
        summary = {"providers": [{"provider": "codex", "stale": False, "has_reliable_quota": True}]}
        result = quota_evidence_from_summary(summary)[0]
        self.assertEqual("healthy", result["state"])


if __name__ == "__main__":
    unittest.main()
