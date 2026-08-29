"""No-provider tests for the C-line ten-round acceptance harness."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from manager.dispatch_10round_acceptance import (
    ROUND_COUNT,
    JsonlEvidenceRecorder,
    claim_latency_seconds,
    evaluate_ten_rounds,
    run_unattended_ten_rounds,
)
from manager.dispatch_3of3_acceptance import STATUS_PASS, STATUS_UNKNOWN, FreshnessViolation

PROJECT = "adm-demo"
RUN_STARTED_AT = "2026-08-29T00:00:00Z"
TICK_SECONDS = 300.0


def _ts(seconds: int) -> str:
    value = datetime.fromisoformat(RUN_STARTED_AT.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _good(request_id: str, *, offset: int = 60) -> dict:
    task_id = f"{request_id}-task"
    session_id = f"{request_id}-session"
    return {
        "request_id": request_id,
        "project_id": PROJECT,
        "provider": "codex",
        "account_id": "test-account",
        "timestamps": {
            "request_created_at": _ts(0),
            "ingress_first_observed_at": _ts(offset),
            "task_created_at": _ts(offset + 1),
            "command_created_at": _ts(offset + 2),
            "claimed_at": _ts(offset + 3),
            "reserved_at": _ts(offset + 4),
            "running_at": _ts(offset + 5),
            "terminal_at": _ts(offset + 30),
            "handoff_at": _ts(offset + 31),
        },
        "ids": {"task_id": task_id, "command_id": f"{request_id}-command",
                "execution_id": f"{request_id}-execution", "session_id": session_id,
                "handoff_id": f"{request_id}-handoff"},
        "backend_visibility": {"status": "RUNNING", "observed_at": _ts(offset + 6)},
        "user_visibility": {"status": "RUNNING", "observed_at": _ts(offset + 7)},
        "backend_status": "COMPLETED",
        "dashboard_status": "COMPLETED",
        "linkage": {stage: {"occurred": True, "task_id_matches": True}
                    for stage in ("task", "command", "execution", "session", "handoff")},
        "duplicate_counts": {stage: 1 for stage in ("task", "command", "execution", "session", "handoff")},
        "manual_trigger_evidence": {"found": False},
        "reached_running": True,
        "real_provider_evidence": {"present": True, "pid": 4242, "host": "test-host"},
        "scheduler_provenance": {
            "status": "PASS", "scheduler_invocation_id": "a" * 32,
            "task_name": "ADM watcher", "reason": "scheduled test fixture",
            "os_scheduler_evidence": {
                "status": "PASS", "instance_id": "instance-1",
                "trigger_event_record_id": 10, "action_event_record_id": 12,
                "action_process_id": 41, "trigger_origin": "scheduled_time",
            },
        },
        "execution": {"execution_id": f"{request_id}-execution", "task_id": task_id,
                       "session_id": session_id, "status": "completed"},
        "session": {"session_id": session_id, "provider_session_id": f"provider-{request_id}",
                    "provider": "codex", "task_id": task_id, "execution_id": f"{request_id}-execution"},
        "provider_output": {"observed": True, "matched_expected": True,
                            "observed_at": _ts(offset + 25),
                            "verification_method": "provider_result_summary",
                            "sha256": "a" * 64},
        "terminal": {"state": "COMPLETED", "command_status": "completed", "execution_status": "completed",
                      "cleanup_evidence": {"task_claim_release": "released", "writer_release": "not_required"}},
        "dashboard_truth": {"observed": True, "backend_status": "COMPLETED", "dashboard_status": "COMPLETED",
                            "matches": True, "observed_at": _ts(offset + 32)},
    }


class TenRoundAcceptanceTests(unittest.TestCase):
    def test_ten_rounds_pass_and_capture_claim_latency(self):
        evidences = [_good(f"r{number}", offset=60 + number) for number in range(1, ROUND_COUNT + 1)]
        report = evaluate_ten_rounds(evidences, run_id="run-1", run_started_at=RUN_STARTED_AT,
                                     expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
        self.assertEqual(STATUS_PASS, report.overall)
        self.assertEqual(ROUND_COUNT, report.passed_count)
        self.assertAlmostEqual(3.0, report.results[0].claim_latency)
        self.assertEqual("completed", report.results[0].evidence_record["execution"]["status"])
        self.assertTrue(report.results[0].evidence_record["session"]["matching_execution_session_id"])
        self.assertTrue(report.results[0].evidence_record["provider"]["output"]["matched_expected"])
        self.assertEqual("released", report.results[0].evidence_record["terminal"]["task_claim_release"])
        self.assertTrue(report.results[0].evidence_record["dashboard_truth"]["matches"])

    def test_claim_latency_is_measured_from_ingress_observation(self):
        evidence = _good("r1")
        self.assertAlmostEqual(3.0, claim_latency_seconds(evidence))
        evidence["timestamps"]["request_created_at"] = _ts(-10000)
        self.assertAlmostEqual(3.0, claim_latency_seconds(evidence))

    def test_middle_failure_cannot_be_cherry_picked_and_all_rounds_run(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            recorder = JsonlEvidenceRecorder(Path(directory) / "evidence.jsonl")

            def dispatch(round_number, request_id):
                calls.append(("dispatch", round_number, request_id))
                return {"accepted": True}

            def collect(round_number, request_id, _receipt):
                calls.append(("collect", round_number, request_id))
                evidence = _good(request_id, offset=60 + round_number)
                if round_number == 5:
                    evidence["provider_output"]["matched_expected"] = False
                return evidence

            report = run_unattended_ten_rounds(project_id=PROJECT, dispatch_round=dispatch, collect_round=collect,
                                               tick_seconds=TICK_SECONDS, run_id="run-fail", recorder=recorder,
                                               now=lambda: RUN_STARTED_AT)
            self.assertEqual("FAIL", report.overall)
            self.assertEqual(9, report.passed_count)
            self.assertEqual(ROUND_COUNT * 2, len(calls))
            self.assertEqual(list(range(1, ROUND_COUNT + 1)), [item[1] for item in calls if item[0] == "dispatch"])
            self.assertEqual("FAIL", report.results[4].result)
            lines = (Path(directory) / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(ROUND_COUNT + 2, len(lines))
            serialized = "\n".join(lines)
            self.assertNotIn("raw_output", serialized)
            self.assertNotIn("transcript", serialized)


    def test_provider_raw_output_is_rejected_and_never_recorded(self):
        evidence = _good("r1")
        evidence["provider_output"]["raw_output"] = "SECRET PROVIDER TRANSCRIPT"
        report = evaluate_ten_rounds([evidence] + [_good(f"r{n}") for n in range(2, ROUND_COUNT + 1)],
                                     run_id="run-raw", run_started_at=RUN_STARTED_AT,
                                     expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
        self.assertEqual("FAIL", report.overall)
        self.assertTrue(any(check.name == "PROVIDER_OUTPUT" and check.status == "FAIL"
                            for check in report.results[0].checks))
        self.assertNotIn("SECRET PROVIDER TRANSCRIPT", json.dumps(report.as_dict()))


    def test_reused_request_id_is_rejected_before_any_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = JsonlEvidenceRecorder(Path(directory) / "evidence.jsonl")
            recorder.emit({"event": "round", "request_id": "already-used"})
            calls = []
            with self.assertRaises(FreshnessViolation):
                run_unattended_ten_rounds(
                    project_id=PROJECT,
                    request_ids=["already-used"] + [f"fresh-{n}" for n in range(2, ROUND_COUNT + 1)],
                    dispatch_round=lambda *_: calls.append("dispatch"),
                    collect_round=lambda *_: _good("unused"),
                    tick_seconds=TICK_SECONDS,
                    recorder=recorder,
                )
            self.assertEqual([], calls)


    def test_dispatch_or_collection_exception_fails_only_that_round_and_continues(self):
        dispatched = []

        def dispatch(round_number, request_id):
            dispatched.append(round_number)
            if round_number == 2:
                raise RuntimeError("test-only failure")
            return None

        def collect(round_number, request_id, _receipt):
            return _good(request_id, offset=60 + round_number)

        report = run_unattended_ten_rounds(project_id=PROJECT, dispatch_round=dispatch, collect_round=collect,
                                           tick_seconds=TICK_SECONDS, run_id="run-exception",
                                           now=lambda: RUN_STARTED_AT)
        self.assertEqual("FAIL", report.overall)
        self.assertEqual(9, report.passed_count)
        self.assertEqual(list(range(1, ROUND_COUNT + 1)), dispatched)
        self.assertTrue(any(check.name == "BACKEND_VISIBLE" and check.status == STATUS_UNKNOWN
                            for check in report.results[1].checks))
        self.assertTrue(any(check.name == "HARNESS_CALL" and check.status == "FAIL"
                            for check in report.results[1].checks))


if __name__ == "__main__":
    unittest.main()
