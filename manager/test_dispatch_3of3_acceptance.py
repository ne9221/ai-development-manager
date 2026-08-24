"""Deterministic unit tests for manager/dispatch_3of3_acceptance.py.

All tests operate on constructed fixture dicts -- no real store, no Drive,
no Dashboard, no Scheduled Task, no provider process. This is intentional:
the spec requires evaluate_dispatch()/evaluate_three_consecutive() to be
fully exercised, deterministically, against fixtures for every required
failure scenario.
"""

from __future__ import annotations

import pytest

from manager.dispatch_3of3_acceptance import (
    STATUS_FAIL,
    STATUS_PASS,
    FreshnessViolation,
    RunLedger,
    detect_cross_task_borrowing,
    evaluate_dispatch,
    evaluate_three_consecutive,
)

PROJECT = "adm-demo"
TICK_SECONDS = 300.0  # 5-minute scheduler tick, matches Command Watcher cadence


def ts(minute: float) -> str:
    """Deterministic ISO8601 timestamp `minute` minutes after a fixed epoch."""
    base_minutes = 1000.0
    total_minutes = base_minutes + minute
    hh = int(total_minutes // 60) % 24
    mm = total_minutes - int(total_minutes // 60) * 60
    ss = (mm - int(mm)) * 60
    return f"2026-08-24T{hh:02d}:{int(mm):02d}:{ss:05.2f}Z"


def good_evidence(
    request_id: str,
    *,
    request_created_min: float = 0.0,
    ingress_min: float = 1.0,
    backend_visible_min: float = 2.0,
    user_visible_min: float = 2.5,
    task_id: str = None,
    command_id: str = None,
    execution_id: str = None,
    session_id: str = None,
    handoff_id: str = None,
    backend_status: str = "RUNNING",
    dashboard_status: str = "RUNNING",
    duplicate_counts: dict = None,
    manual_found: bool = False,
    reached_running: bool = True,
    provider_present: bool = True,
    terminal_state: str = None,
    terminal_reason: str = None,
) -> dict:
    task_id = task_id or f"{request_id}-task"
    command_id = command_id or f"{request_id}-cmd"
    execution_id = execution_id or f"{request_id}-exec"
    session_id = session_id or f"{request_id}-sess"
    handoff_id = handoff_id or f"{request_id}-handoff"
    return {
        "request_id": request_id,
        "project_id": PROJECT,
        "provider": "codex",
        "account_id": "acct-1",
        "timestamps": {
            "request_created_at": ts(request_created_min),
            "ingress_first_observed_at": ts(ingress_min),
            "task_created_at": ts(ingress_min + 0.1),
            "command_created_at": ts(ingress_min + 0.2),
            "claimed_at": ts(ingress_min + 0.3),
            "reserved_at": ts(ingress_min + 0.4),
            "running_at": ts(ingress_min + 0.5),
            "terminal_at": ts(ingress_min + 5.0) if terminal_state else None,
            "handoff_at": ts(ingress_min + 5.5) if terminal_state else None,
        },
        "ids": {
            "task_id": task_id,
            "command_id": command_id,
            "execution_id": execution_id,
            "session_id": session_id,
            "handoff_id": handoff_id,
        },
        "backend_visibility": {"status": backend_status, "observed_at": ts(backend_visible_min)},
        "user_visibility": {"status": dashboard_status, "observed_at": ts(user_visible_min)},
        "backend_status": backend_status,
        "dashboard_status": dashboard_status,
        "linkage": {
            "task": {"occurred": True, "task_id_matches": True},
            "command": {"occurred": True, "task_id_matches": True},
            "execution": {"occurred": True, "task_id_matches": True},
            "session": {"occurred": True, "task_id_matches": True},
            "handoff": {"occurred": bool(terminal_state), "task_id_matches": True if terminal_state else None},
        },
        "duplicate_counts": duplicate_counts or {"task": 1, "command": 1, "execution": 1, "session": 1, "handoff": 1 if terminal_state else 0},
        "manual_trigger_evidence": {"found": manual_found, "source": "Start-ScheduledTask (manual)" if manual_found else None},
        "reached_running": reached_running,
        "real_provider_evidence": {"present": provider_present, "pid": 4242, "host": "HOME"} if provider_present else {"present": False},
        "terminal": {"state": terminal_state, "reason_code": terminal_reason} if terminal_state else None,
    }


def eval_one(evidence, **kwargs):
    kwargs.setdefault("expected_project_id", PROJECT)
    kwargs.setdefault("tick_seconds", TICK_SECONDS)
    kwargs.setdefault("max_visibility_ticks", 2)
    return evaluate_dispatch(evidence, **kwargs)


# ---------------------------------------------------------------------------
# 1. 3/3 normal success
# ---------------------------------------------------------------------------

def test_scenario_1_three_of_three_normal_success():
    evidences = {
        "r1": good_evidence("r1"),
        "r2": good_evidence("r2"),
        "r3": good_evidence("r3"),
    }
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
    )
    assert report.overall == STATUS_PASS
    assert report.consecutive_pass_count == 3
    assert report.as_dict()["HANDSOFF_DAILY_USABLE"] == "PASS"
    for r in report.results:
        assert r.result == STATUS_PASS


# ---------------------------------------------------------------------------
# 2. Request 2's visibility exceeds 2 ticks -> overall FAIL
# ---------------------------------------------------------------------------

def test_scenario_2_visibility_exceeds_two_ticks():
    # tick=300s (5 min); 2 ticks = 10 min. Push backend/user visibility to
    # 20 minutes after SLA_START (ingress_first_observed_at).
    late = good_evidence("r2", backend_visible_min=1.0 + 20.0, user_visible_min=1.0 + 20.0)
    evidences = {"r1": good_evidence("r1"), "r2": late, "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    assert report.overall == STATUS_FAIL
    r2 = report.results[1]
    assert r2.result == STATUS_FAIL
    assert any(c.name == "BACKEND_VISIBLE" and c.status == STATUS_FAIL for c in r2.checks)


# ---------------------------------------------------------------------------
# 3. Request 2 has a cross-task execution borrowed from another request
# ---------------------------------------------------------------------------

def test_scenario_3_cross_task_borrowed_execution():
    r2 = good_evidence("r2")
    r2["ids"]["execution_id"] = "r1-exec"  # borrowed from r1
    evidences = {"r1": good_evidence("r1"), "r2": r2, "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    assert report.overall == STATUS_FAIL
    r2_result = report.results[1]
    assert any(c.name == "CROSS_TASK_BORROWING" and c.status == STATUS_FAIL for c in r2_result.checks)
    # r1 is also implicated (its execution_id was borrowed)
    r1_result = report.results[0]
    assert any(c.name == "CROSS_TASK_BORROWING" and c.status == STATUS_FAIL for c in r1_result.checks)


def test_detect_cross_task_borrowing_pure_function():
    evidences = [good_evidence("r1"), good_evidence("r2"), good_evidence("r3")]
    evidences[1]["ids"]["session_id"] = evidences[2]["ids"]["session_id"]
    conflicts = detect_cross_task_borrowing(evidences)
    assert conflicts["r2"]["found"] is True
    assert conflicts["r3"]["found"] is True
    assert conflicts["r1"]["found"] is False


# ---------------------------------------------------------------------------
# 4. Duplicate Task detected for one request -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_4_duplicate_task_fails():
    ev = good_evidence("r1", duplicate_counts={"task": 2, "command": 1, "execution": 1, "session": 1, "handoff": 0})
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "IDEMPOTENCY" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 5. Duplicate Command detected for one request -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_5_duplicate_command_fails():
    ev = good_evidence("r1", duplicate_counts={"task": 1, "command": 2, "execution": 1, "session": 1, "handoff": 0})
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "IDEMPOTENCY" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 6. Borrowed/reused Session detected -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_6_borrowed_session_via_linkage_mismatch():
    ev = good_evidence("r1")
    ev["linkage"]["session"] = {"occurred": True, "task_id_matches": False}
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "SESSION_LINKAGE" and c.status == STATUS_FAIL for c in result.checks)


def test_scenario_6b_borrowed_session_via_batch_conflict():
    r1 = good_evidence("r1")
    r2 = good_evidence("r2")
    r2["ids"]["session_id"] = r1["ids"]["session_id"]
    evidences = {"r1": r1, "r2": r2, "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    assert report.overall == STATUS_FAIL


# ---------------------------------------------------------------------------
# 7. Manual trigger evidence present -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_7_manual_trigger_fails():
    ev = good_evidence("r1", manual_found=True)
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "NO_MANUAL_TRIGGER" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 8. Backend ACCEPTED but Dashboard shows nothing -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_8_backend_accepted_dashboard_silent():
    ev = good_evidence("r1", backend_status="ACCEPTED", dashboard_status="ACCEPTED")
    # Dashboard was actually checked (not "unobserved") and showed nothing.
    ev["user_visibility"] = {"status": None, "observed_at": None}
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "USER_VISIBLE" and c.status == STATUS_FAIL for c in result.checks)


def test_scenario_8b_dashboard_truth_mismatch_stale():
    # Backend=RUNNING but Dashboard still shows ACCEPTED (stale) -> FAIL
    ev = good_evidence("r1", backend_status="RUNNING", dashboard_status="ACCEPTED")
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "DASHBOARD_TRUTH" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 9. Legitimate BLOCKED with exact machine-readable reason -> PASS
# ---------------------------------------------------------------------------

def test_scenario_9_legitimate_blocked_with_reason_passes():
    ev = good_evidence(
        "r1",
        backend_status="BLOCKED",
        dashboard_status="BLOCKED",
        reached_running=False,
        provider_present=False,
        terminal_state="BLOCKED",
        terminal_reason="NO_QUOTA_AVAILABLE",
    )
    # execution/session/handoff never happened for a pre-execution block
    ev["ids"]["execution_id"] = None
    ev["ids"]["session_id"] = None
    ev["ids"]["handoff_id"] = None
    ev["linkage"]["execution"] = {"occurred": False}
    ev["linkage"]["session"] = {"occurred": False}
    ev["linkage"]["handoff"] = {"occurred": False}
    ev["real_provider_evidence"] = None
    result = eval_one(ev)
    assert result.result == STATUS_PASS
    assert any(c.name == "EXECUTION_LINKAGE" and c.status == "N/A" for c in result.checks)
    assert any(c.name == "REAL_PROVIDER" and c.status == "N/A" for c in result.checks)
    assert any(c.name == "TERMINAL_REASON_CODE" and c.status == STATUS_PASS for c in result.checks)


def test_scenario_9b_blocked_without_reason_fails():
    ev = good_evidence("r1", backend_status="BLOCKED", dashboard_status="BLOCKED", terminal_state="BLOCKED", terminal_reason=None)
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "TERMINAL_REASON_CODE" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 10. Historical/pre-existing execution mistakenly picked up as fresh -> FAIL
# ---------------------------------------------------------------------------

def test_scenario_10_stale_historical_record_fails_freshness():
    ev = good_evidence("r1")
    ev["freshness"] = {"is_fresh": False, "detail": "execution_id r1-exec pre-dates request_created_at by 3 days"}
    result = eval_one(ev)
    assert result.result == STATUS_FAIL
    assert any(c.name == "FRESHNESS" and c.status == STATUS_FAIL for c in result.checks)


# ---------------------------------------------------------------------------
# 11. Cannot silently drop a failed sample and cherry-pick 3 passes
# ---------------------------------------------------------------------------

def test_scenario_11_cannot_drop_failing_sample_and_recombine():
    ledger = RunLedger()
    failing_r2 = good_evidence("r2", manual_found=True)  # this one fails
    evidences_run1 = {"r1": good_evidence("r1"), "r2": failing_r2, "r3": good_evidence("r3")}
    report1 = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences_run1,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
    )
    assert report1.overall == STATUS_FAIL

    # Attempt to "retry" by dropping r2 and combining r1, r3 with a fresh r4
    # to get a false 3/3. This must be structurally rejected: r1 and r3 are
    # already consumed by the ledger, so recombining them is impossible.
    evidences_run2 = {"r1": good_evidence("r1"), "r3": good_evidence("r3"), "r4": good_evidence("r4")}
    with pytest.raises(FreshnessViolation):
        evaluate_three_consecutive(
            ["r1", "r3", "r4"], evidences_run2,
            expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
        )

    # The only legitimate way forward is a brand-new consecutive fresh batch.
    evidences_run3 = {"r4": good_evidence("r4"), "r5": good_evidence("r5"), "r6": good_evidence("r6")}
    report3 = evaluate_three_consecutive(
        ["r4", "r5", "r6"], evidences_run3,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
    )
    assert report3.overall == STATUS_PASS


def test_scenario_11b_cannot_filter_declared_list_post_hoc():
    # evaluate_three_consecutive must evaluate exactly what was declared --
    # requesting 3 but supplying a filtered/short list is a hard error, not
    # a quiet re-derivation of "3/3" from fewer inputs.
    evidences = {"r1": good_evidence("r1"), "r3": good_evidence("r3")}
    with pytest.raises(ValueError):
        evaluate_three_consecutive(["r1", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, required_count=3)


# ---------------------------------------------------------------------------
# 12. The three request_ids must be consecutive/fresh, not reused
# ---------------------------------------------------------------------------

def test_scenario_12_duplicate_request_id_within_batch_rejected():
    evidences = {"r1": good_evidence("r1"), "r2": good_evidence("r2")}
    with pytest.raises(FreshnessViolation):
        evaluate_three_consecutive(["r1", "r1", "r2"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)


def test_scenario_12b_reused_request_id_from_prior_run_rejected():
    ledger = RunLedger()
    evidences1 = {"r1": good_evidence("r1"), "r2": good_evidence("r2"), "r3": good_evidence("r3")}
    evaluate_three_consecutive(["r1", "r2", "r3"], evidences1, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger)

    evidences2 = {"r2": good_evidence("r2"), "r4": good_evidence("r4"), "r5": good_evidence("r5")}
    with pytest.raises(FreshnessViolation):
        evaluate_three_consecutive(["r2", "r4", "r5"], evidences2, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger)


def test_scenario_12c_out_of_order_request_id_rejected():
    ledger = RunLedger()
    evidences1 = {"r1": good_evidence("r1"), "r2": good_evidence("r2"), "r5": good_evidence("r5")}
    evaluate_three_consecutive(["r1", "r2", "r5"], evidences1, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger)

    # r3/r4 sort before the already-consumed max "r5" -> rejected even
    # though neither id was individually seen before.
    evidences2 = {"r3": good_evidence("r3"), "r4": good_evidence("r4"), "r6": good_evidence("r6")}
    with pytest.raises(FreshnessViolation):
        evaluate_three_consecutive(["r3", "r4", "r6"], evidences2, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger)


# ---------------------------------------------------------------------------
# Additional coverage: UNKNOWN handling, SLA-start semantics, human/json output
# ---------------------------------------------------------------------------

def test_sla_start_is_ingress_observed_not_request_created():
    # request_created_at is far earlier than ingress_first_observed_at
    # (long scheduler pickup latency); visibility right after ingress
    # observation must still PASS the 2-tick SLA window.
    ev = good_evidence("r1", request_created_min=0.0, ingress_min=120.0, backend_visible_min=121.0, user_visible_min=121.5)
    result = eval_one(ev)
    assert result.sla_start == ev["timestamps"]["ingress_first_observed_at"]
    assert result.scheduler_pickup_latency_seconds == pytest.approx(120.0 * 60)
    assert any(c.name == "BACKEND_VISIBLE" and c.status == STATUS_PASS for c in result.checks)
    assert result.result == STATUS_PASS


def test_missing_evidence_is_unknown_not_pass():
    ev = {"request_id": "r1", "project_id": PROJECT}
    result = eval_one(ev)
    assert result.result == STATUS_FAIL  # UNKNOWN never displays/counts as PASS
    backend_check = [c for c in result.checks if c.name == "BACKEND_VISIBLE"][0]
    assert backend_check.status in ("UNKNOWN",)
    assert backend_check.display_status == STATUS_FAIL


def test_human_summary_contains_required_fields():
    evidences = {"r1": good_evidence("r1"), "r2": good_evidence("r2"), "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    text = report.to_human_summary()
    for required in (
        "REQUEST_1:", "REQUEST_ID:", "SLA_START:", "FIRST_VISIBLE:", "VISIBILITY_TICKS:",
        "BACKEND_VISIBLE:", "USER_VISIBLE:", "TASK_LINKAGE:", "COMMAND_LINKAGE:",
        "EXECUTION_LINKAGE:", "SESSION_LINKAGE:", "HANDOFF_LINKAGE:", "IDEMPOTENCY:",
        "REAL_PROVIDER:", "NO_MANUAL_TRIGGER:", "DASHBOARD_TRUTH:", "RESULT:",
        "CONSECUTIVE_PASS_COUNT:", "HANDSOFF_DAILY_USABLE:",
    ):
        assert required in text, f"missing {required!r} in human summary"


def test_json_report_is_machine_readable():
    evidences = {"r1": good_evidence("r1"), "r2": good_evidence("r2"), "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    data = report.to_json()
    assert data["HANDSOFF_DAILY_USABLE"] == "PASS"
    assert data["CONSECUTIVE_PASS_COUNT"] == 3
    assert len(data["results"]) == 3
    for r in data["results"]:
        assert r["RESULT"] in ("PASS", "FAIL")
        assert isinstance(r["checks"], list)


def test_project_scope_mismatch_fails():
    ev = good_evidence("r1")
    result = eval_one(ev, expected_project_id="some-other-project")
    assert result.result == STATUS_FAIL


def test_consecutive_pass_count_stops_at_first_failure():
    evidences = {
        "r1": good_evidence("r1"),
        "r2": good_evidence("r2", manual_found=True),
        "r3": good_evidence("r3"),
    }
    report = evaluate_three_consecutive(["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    # r1 passes, r2 fails, r3 (evaluated independently) may pass -- but the
    # CONSECUTIVE run is broken at r2, so consecutive_pass_count must be 1,
    # not "2 passes out of 3" cherry-picked.
    assert report.results[0].result == STATUS_PASS
    assert report.results[1].result == STATUS_FAIL
    assert report.consecutive_pass_count == 1
    assert report.overall == STATUS_FAIL
