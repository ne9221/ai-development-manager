from copy import deepcopy

from manager.handsoff_reliability_acceptance import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_BLOCKED,
    STATUS_UNKNOWN,
    collect_evidence,
    evaluate_dispatch,
    run_acceptance,
)
from manager.test_tasks import MemoryStore

PROJECT = "proj-adm"
TICK_SECONDS = 60.0
MAX_TICKS = 2.0


def _good_evidence(request_id="req-1", offset_seconds=0):
    def ts(n):
        # n ticks after a fixed epoch, offset per-dispatch so timelines don't collide
        base = 1_700_000_000 + offset_seconds
        from datetime import datetime, timezone
        return datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "request_id": request_id,
        "project_id": PROJECT,
        "timestamps": {
            "REQUEST_CREATED_AT": ts(0),
            "TASK_CREATED_AT": ts(2),
            "COMMAND_CREATED_AT": ts(3),
            "CLAIMED_AT": ts(30),  # within 2 ticks (120s)
            "RUNNING_AT": ts(40),
            "TERMINAL_AT": ts(600),
            "HANDOFF_AT": ts(610),
        },
        "linkage": {
            "task_found": True,
            "command_task_id_matches": True,
            "execution_task_id_matches": True,
            "session_task_id_matches": True,
            "handoff_task_id_matches": True,
        },
        "provider_evidence": {"present": True, "pid": 4242, "host": "HOME"},
        "manual_trigger_evidence": {"found": False, "source": None},
        "duplicate_count": 0,
        "dashboard_visibility": {"visible": True, "matches_store": True},
        "blocked": None,
        "provider_started": True,
    }


def _blocked_evidence(request_id="req-blocked"):
    ev = _good_evidence(request_id)
    ev["blocked"] = {"is_blocked": True, "blocked_reason": "no_quota_available_all_providers", "reason_truthful": True}
    ev["provider_started"] = None
    ev["provider_evidence"] = None
    return ev


def test_single_good_dispatch_passes():
    result = evaluate_dispatch(_good_evidence(), expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_PASS
    assert result.provider_verdict == STATUS_PASS


def test_three_fresh_dispatches_pass_3_of_3():
    evidences = [_good_evidence(f"req-{i}", offset_seconds=i * 10_000) for i in range(3)]
    report = run_acceptance(evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert report.overall == STATUS_PASS
    assert report.as_dict()["HANDSOFF_DAILY_USABLE"] == "PASS"


def test_second_dispatch_exceeds_sla_fails_overall():
    evidences = [_good_evidence(f"req-{i}", offset_seconds=i * 10_000) for i in range(3)]
    evidences[1]["timestamps"]["CLAIMED_AT"] = evidences[1]["timestamps"]["REQUEST_CREATED_AT"]
    from datetime import datetime, timezone, timedelta
    base = datetime.fromisoformat(evidences[1]["timestamps"]["REQUEST_CREATED_AT"].replace("Z", "+00:00"))
    late = base + timedelta(seconds=500)  # way beyond 2 ticks (120s)
    evidences[1]["timestamps"]["CLAIMED_AT"] = late.isoformat().replace("+00:00", "Z")

    report = run_acceptance(evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert report.overall == STATUS_FAIL
    assert report.results[1].verdict == STATUS_FAIL
    assert "req-1" in report.reason


def test_missing_task_fails():
    ev = _good_evidence()
    ev["linkage"] = {"task_found": False}
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_FAIL
    names = [c.name for c in result.checks if c.status == STATUS_FAIL]
    assert "linkage.task_found" in names


def test_borrowed_execution_fails():
    ev = _good_evidence()
    ev["linkage"]["execution_task_id_matches"] = False
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_FAIL
    names = [c.name for c in result.checks if c.status == STATUS_FAIL]
    assert "linkage.execution_task_id_matches" in names


def test_manual_trigger_evidence_fails():
    ev = _good_evidence()
    ev["manual_trigger_evidence"] = {"found": True, "source": "manual Start-ScheduledTask invocation observed in event log"}
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_FAIL
    names = [c.name for c in result.checks if c.status == STATUS_FAIL]
    assert "no_manual_trigger" in names


def test_duplicate_execution_fails():
    ev = _good_evidence()
    ev["duplicate_count"] = 1
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_FAIL


def test_blocked_with_exact_reason_within_sla_visibility_passes_but_provider_blocked():
    ev = _blocked_evidence()
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.visibility_verdict == STATUS_PASS
    assert result.provider_verdict == STATUS_BLOCKED
    # overall .verdict is driven by visibility (provider-start is separate & resource-dependent)
    assert result.verdict == STATUS_PASS


def test_blocked_with_untruthful_reason_fails_visibility():
    ev = _blocked_evidence()
    ev["blocked"]["reason_truthful"] = False
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.visibility_verdict == STATUS_FAIL


def test_silent_request_is_not_pass():
    ev = _good_evidence()
    ev["timestamps"]["CLAIMED_AT"] = None
    ev["blocked"] = None
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict != STATUS_PASS


def test_unknown_evidence_is_never_reported_as_pass():
    # A request we genuinely cannot observe (e.g. dashboard probe never wired
    # up, duplicate count never checked) must not silently count as PASS.
    ev = {"request_id": "req-mystery", "project_id": PROJECT, "timestamps": {}}
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_UNKNOWN
    assert result.verdict != STATUS_PASS

    report = run_acceptance([ev, _good_evidence("req-2"), _good_evidence("req-3", offset_seconds=99999)],
                             expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert report.overall != STATUS_PASS


def test_fewer_than_required_dispatches_fails():
    report = run_acceptance([_good_evidence("only-one")], expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert report.overall == STATUS_FAIL
    assert "need 3" in report.reason


def test_wrong_project_scope_fails():
    ev = _good_evidence()
    ev["project_id"] = "some-other-project"
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    assert result.verdict == STATUS_FAIL


# --- collect_evidence against a real (fake) store --------------------------

def _seed_lifecycle(store, request_id="req-store-1", task_id="task-1", blocked=False):
    store.put("tasks", PROJECT, task_id, {
        "task_id": task_id, "project_id": PROJECT, "status": "blocked" if blocked else "done",
        "created_at": "2026-08-24T00:00:02Z",
        "source_context": {"request_id": request_id, "request_created_at": "2026-08-24T00:00:00Z"},
        "blocked_reason": "no_quota_available_all_providers" if blocked else None,
    })
    if blocked:
        return
    store.put("commands", PROJECT, "cmd-1", {
        "command_id": "cmd-1", "project_id": PROJECT, "task_id": task_id,
        "created_at": "2026-08-24T00:00:03Z", "claimed_at": "2026-08-24T00:00:30Z",
        "execution_id": "exec-1",
    })
    store.put("executions", PROJECT, "exec-1", {
        "execution_id": "exec-1", "project_id": PROJECT, "task_id": task_id,
        "started_at": "2026-08-24T00:00:40Z", "completed_at": "2026-08-24T00:10:00Z",
        "session_id": "sess-1", "provider_evidence": {"pid": 999, "host": "HOME"},
    })
    store.put("sessions", PROJECT, "sess-1", {
        "session_id": "sess-1", "project_id": PROJECT, "task_id": task_id,
    })
    store.put("handoffs", PROJECT, "handoff-1", {
        "handoff_id": "handoff-1", "project_id": PROJECT, "task_id": task_id,
        "created_at": "2026-08-24T00:10:10Z",
    })


def test_collect_evidence_walks_full_lifecycle():
    store = MemoryStore()
    _seed_lifecycle(store)
    evidence = collect_evidence(store, PROJECT, "req-store-1")
    assert evidence["linkage"]["task_found"] is True
    assert evidence["linkage"]["execution_task_id_matches"] is True
    assert evidence["timestamps"]["RUNNING_AT"] == "2026-08-24T00:00:40Z"
    assert evidence["duplicate_count"] == 0

    result = evaluate_dispatch(evidence, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, max_visibility_ticks=MAX_TICKS)
    # dashboard_visibility was never probed -> UNKNOWN, so this must not be PASS
    assert result.verdict == STATUS_UNKNOWN


def test_collect_evidence_missing_task_reports_not_found():
    store = MemoryStore()
    evidence = collect_evidence(store, PROJECT, "req-does-not-exist")
    assert evidence["linkage"]["task_found"] is False


def test_collect_evidence_detects_duplicate_commands():
    store = MemoryStore()
    _seed_lifecycle(store)
    store.put("commands", PROJECT, "cmd-2-dup", {
        "command_id": "cmd-2-dup", "project_id": PROJECT, "task_id": "task-1",
        "created_at": "2026-08-24T00:00:05Z", "claimed_at": None, "execution_id": None,
    })
    evidence = collect_evidence(store, PROJECT, "req-store-1")
    assert evidence["duplicate_count"] == 1
