"""Deterministic unit tests for manager/dispatch_3of3_acceptance.py.

All tests operate on constructed fixture dicts -- no real store, no Drive,
no Dashboard, no Scheduled Task, no provider process. This is intentional:
the spec requires evaluate_dispatch()/evaluate_three_consecutive() to be
fully exercised, deterministically, against fixtures for every required
failure scenario. The freshness tests additionally exercise the real
collect_evidence() store-walk (against an in-memory FakeStore, still no
network/Drive/Scheduled Task) to prove the fix holds on the actual
evidence-collection path, not just on hand-built fixtures.
"""

from __future__ import annotations

import pytest

from manager.dispatch_3of3_acceptance import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    FreshnessViolation,
    RunLedger,
    _backend_visibility_from_records,
    _durable_visibility_from_registry,
    _list_area_records,
    collect_evidence,
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


# The cutoff every "real" test in this file declares for its acceptance run.
# All good_evidence() fixtures default ingress_min=1.0, i.e. one minute after
# this cutoff, so they PASS freshness unless a test deliberately backdates
# ingress_first_observed_at to before RUN_STARTED_AT.
RUN_STARTED_AT = ts(0.0)

# A genuinely historical timestamp (5 days before the 2026-08-24 base date
# used by ts()/RUN_STARTED_AT) -- stands in for a real once-seen historical
# request (e.g. a prior "1916"-style id) that must never count toward a
# fresh 3/3 run no matter how self-consistent its own timestamps are.
HISTORICAL_TS = "2026-08-19T00:01:00Z"


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
        "scheduler_provenance": scheduler_provenance(),
        "terminal": {"state": terminal_state, "reason_code": terminal_reason} if terminal_state else None,
    }


def scheduler_provenance(status=STATUS_PASS, *, action_pid=41):
    return {
        "status": status, "scheduler_invocation_id": "a" * 32, "task_name": "ADM watcher",
        "os_scheduler_evidence": {"status": status, "instance_id": "instance-1",
            "task_name": "ADM watcher",
            "trigger_event_record_id": 10, "action_event_record_id": 12,
            "action_process_id": action_pid, "trigger_origin": "scheduled_time",
            "reason": "event_129_pid_and_instance_link"},
        "reason": "event_129_pid_and_instance_link",
    }


def eval_one(evidence, **kwargs):
    kwargs.setdefault("expected_project_id", PROJECT)
    kwargs.setdefault("tick_seconds", TICK_SECONDS)
    kwargs.setdefault("max_visibility_ticks", 2)
    kwargs.setdefault("acceptance_run_started_at", RUN_STARTED_AT)
    return evaluate_dispatch(evidence, **kwargs)


class FakeStore:
    """Minimal stand-in for the real store: `.records` is {(area, project,
    name): doc}, exactly what collect_evidence() walks. No I/O, no Drive."""

    def __init__(self, records: dict, dispatch_request_registry=None):
        self.records = records
        self.dispatch_request_registry = dispatch_request_registry


class FakeRegistry:
    def __init__(self, document):
        self.document = document

    def read_if_exists(self):
        return (self.document, 1, None) if self.document else None


def build_fake_store(
    request_id: str, ingress_first_observed_at: str, *,
    request_created_at: str = None, claimed_at: str = None, task_status: str = "running",
    include_command: bool = True,
) -> FakeStore:
    """A single well-formed task->command->execution->session->handoff chain,
    for exercising the REAL collect_evidence() walk (not a hand-built
    evaluate_dispatch() fixture). Only `ingress_first_observed_at` varies
    across the freshness tests that use this -- everything else about the
    request is otherwise legitimate, so FRESHNESS is provably the only
    dimension that can fail/pass.

    `claimed_at` defaults to `ingress_first_observed_at` (fast pickup) but
    can be set independently -- that is the real, durable evidence
    BACKEND_VISIBLE is measured against (see
    manager.dispatch_3of3_acceptance._backend_visibility_from_records), so
    tests that need a genuinely late/backlogged backend observation set it
    explicitly rather than relying on SLA_START.

    The dispatch-request registry record deliberately carries NO
    `backend_visible_status`/`first_backend_visible_at`/`user_visible_status`/
    `first_user_visible_at` fields: `read_dispatch_request_status()` never
    projects them through even when a real writer sets them (see
    `manager.dispatch_requests.read_dispatch_request_status`), so they are
    not real production evidence today -- this fixture reflects that.
    """
    task_id = f"{request_id}-task"
    command_id = f"{request_id}-cmd"
    execution_id = f"{request_id}-exec"
    session_id = f"{request_id}-sess"
    handoff_id = f"{request_id}-handoff"
    request_created_at = request_created_at or ingress_first_observed_at
    claimed_at = claimed_at if claimed_at is not None else ingress_first_observed_at
    records = {
        ("tasks", PROJECT, task_id): {
            "task_id": task_id,
            "status": task_status,
            "created_at": ingress_first_observed_at,
            "updated_at": ingress_first_observed_at,
            "source_context": {
                "request_id": request_id,
                "request_created_at": request_created_at,
                "ingress_first_observed_at": ingress_first_observed_at,
            },
        },
        ("sessions", PROJECT, session_id): {
            "task_id": task_id,
            "session_id": session_id,
        },
        ("handoffs", PROJECT, handoff_id): {
            "task_id": task_id,
            "handoff_id": handoff_id,
            "created_at": ingress_first_observed_at,
        },
    }
    if include_command:
        records[("commands", PROJECT, command_id)] = {
            "task_id": task_id,
            "command_id": command_id,
            "created_at": ingress_first_observed_at,
            "claimed_at": claimed_at,
            "execution_id": execution_id,
            "provider": "codex",
            "selection_reason": "quota-eligible-auto-dispatch",
            "process_provenance": {"scheduler_invocation_id": "a" * 32, "wrapper_pid": 41, "wrapper_parent_pid": 41,
                "wrapper_creation_identity": "wrapper", "os_scheduler_evidence": scheduler_provenance()["os_scheduler_evidence"]},
        }
        records[("executions", PROJECT, execution_id)] = {
            "task_id": task_id,
            "execution_id": execution_id,
            "reserved_at": ingress_first_observed_at,
            "started_at": ingress_first_observed_at,
            "completed_at": ingress_first_observed_at,
            "session_id": session_id,
            "status": "succeeded",
            "provider_evidence": {"pid": 4242, "host": "HOME", "scheduler_invocation_id": "a" * 32,
                "launcher_pid": 11, "launcher_creation_identity": "launcher", "provider_pid": 4242,
                "provider_creation_identity": "provider", "provider_parent_identity": "launcher"},
            "quota_evidence": {"account_id": "acct-1"},
        }
    return FakeStore(records, FakeRegistry({
        "schema_version": "0.1.0", "project_id": PROJECT, "request_id": request_id,
        "task_id": task_id, "command_id": command_id, "created_at": ingress_first_observed_at,
        "ingress_first_observed_at": ingress_first_observed_at,
        "request_created_at": request_created_at, "status": "dispatched", "failure_reason": None,
        "status_updated_at": ts(5.0),
    }))


class FakeDriveRecords:
    """Shape-accurate stand-in for the real `manager.tasks.DriveRecords`:
    exposes `list_records(area, project_id)` and `get(area, project_id, name)`
    (the real production contract), and deliberately has NO `.records`
    mapping attribute at all -- proving collect_evidence() resolves the
    full Task->Command->Execution->Session->Handoff chain through
    `list_records()` alone, with no external reshaping adapter required.
    """

    def __init__(self, records: dict, dispatch_request_registry=None):
        self._records = records
        self.dispatch_request_registry = dispatch_request_registry

    def list_records(self, area, project_id):
        return [doc for (rec_area, proj, _name), doc in self._records.items() if rec_area == area and proj == project_id]

    def get(self, area, project_id, name):
        return self._records[(area, project_id, name)]


def build_fake_drive_records(*args, **kwargs) -> FakeDriveRecords:
    """Same fixture data as build_fake_store(), exposed through the real
    DriveRecords-shaped surface (list_records()/get(), no .records)."""
    store = build_fake_store(*args, **kwargs)
    return FakeDriveRecords(store.records, store.dispatch_request_registry)


def _running_dashboard_probe(observed_at: str):
    return lambda project_id, task_id: {"status": "RUNNING", "observed_at": observed_at}


def test_backend_visible_preserves_real_early_evidence_through_terminal_snapshot():
    """Command.claimed_at is real, immutable Phase-1 evidence: a Task that
    later reaches a terminal snapshot does not erase or replace it (a real
    early observation is not "faked" just because it is later preserved --
    adversarial requirement 9's companion case)."""
    ingress = ts(1.0)
    store = build_fake_store("terminal", ingress, claimed_at=ts(1.2))
    task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
    task["status"] = "completed"
    evidence = collect_evidence(store, PROJECT, "terminal", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["backend_visibility"] == {"status": "ACCEPTED", "observed_at": ts(1.2),
                                              "request_id": "terminal", "task_id": "terminal-task"}
    # No dashboard_probe was supplied at all -- USER_VISIBLE must fail
    # closed, never inherit a status from the backend or anywhere else.
    assert evidence["user_visibility"] is None
    result = eval_one(evidence)
    assert _check(result, "BACKEND_VISIBLE").status == STATUS_PASS
    assert _check(result, "USER_VISIBLE").status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL  # overall FAIL: no Dashboard evidence exists


def test_backend_visible_cannot_fake_early_when_real_claim_was_late():
    """Adversarial requirement 9: a genuinely backlogged Command Watcher
    (late claimed_at) must FAIL the visibility SLA even once the Task has
    since completed -- the terminal snapshot must never manufacture an
    earlier-looking timestamp than what was really observed."""
    ingress = ts(1.0)
    late_claim = ts(1.0 + 20.0)  # 20 min after ingress; tick=5min, 2 ticks=10min window
    store = build_fake_store("late-claim", ingress, claimed_at=late_claim)
    task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
    task["status"] = "completed"
    evidence = collect_evidence(store, PROJECT, "late-claim", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["backend_visibility"]["observed_at"] == late_claim
    result = eval_one(evidence)
    assert _check(result, "BACKEND_VISIBLE").status == STATUS_FAIL
    assert result.result == STATUS_FAIL


def test_backend_and_user_visible_unknown_when_no_task_or_evidence_exists_at_all():
    """No Task materialized yet and no dispatch-request registry either --
    there is genuinely no evidence, so both checks degrade to UNKNOWN, never
    a PASS."""
    empty = FakeStore({}, None)
    evidence = collect_evidence(empty, PROJECT, "missing", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["backend_visibility"] is None
    assert evidence["user_visibility"] is None
    result = eval_one(evidence)
    assert _check(result, "BACKEND_VISIBLE").status == STATUS_UNKNOWN
    assert _check(result, "USER_VISIBLE").status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_user_visible_wrong_task_identity_from_probe_fails():
    """Adversarial requirement 11: a Dashboard probe that itself reports a
    DIFFERENT task_id than the one this evidence was resolved against is a
    real identity mismatch -- must FAIL, never be silently accepted."""
    ingress = ts(1.0)
    store = build_fake_store("wrong", ingress)
    mismatched_probe = lambda project_id, task_id: {
        "status": "RUNNING", "observed_at": ts(1.5), "task_id": "someone-elses-task",
    }
    evidence = collect_evidence(store, PROJECT, "wrong", dashboard_probe=mismatched_probe,
                                acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "USER_VISIBLE").status == STATUS_FAIL


def test_ingress_record_only_cannot_pass_backend_or_user_visibility():
    """Adversarial requirements 1 & 2: a dispatch-request record that only
    proves ingress happened (no Task ever materialized) must never PASS
    either visibility check."""
    ingress = ts(1.0)
    record = build_fake_store("rejected", ingress).dispatch_request_registry.document
    record.update({"status": "failed", "failure_reason": "rejected before task"})
    evidence = collect_evidence(FakeStore({}, FakeRegistry(record)), PROJECT, "rejected", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["timestamps"]["ingress_first_observed_at"] == ingress
    assert evidence["backend_visibility"] is None
    assert evidence["user_visibility"] is None
    result = eval_one(evidence)
    assert _check(result, "BACKEND_VISIBLE").status == STATUS_UNKNOWN
    assert _check(result, "USER_VISIBLE").status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_user_visible_dashboard_probe_is_authoritative_and_observed_at_propagates():
    """Adversarial requirement 8: USER_VISIBLE comes only from the real
    dashboard_probe result, and its observed_at must reach the final
    evidence exactly as the probe reported it."""
    ingress = ts(1.0)
    store = build_fake_store("canonical", ingress)
    probe_observed_at = ts(1.7)
    evidence = collect_evidence(store, PROJECT, "canonical", dashboard_probe=_running_dashboard_probe(probe_observed_at),
                                acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["user_visibility"]["observed_at"] == probe_observed_at
    assert _check(eval_one(evidence), "USER_VISIBLE").status == STATUS_PASS


def test_user_visible_dashboard_not_found_fails_closed():
    """Adversarial requirement 7: a probe that finds nothing (None or an
    empty result) must degrade USER_VISIBLE to UNKNOWN, never PASS."""
    ingress = ts(1.0)
    for not_found_probe in (lambda *_: None, lambda *_: {}):
        store = build_fake_store("not-found", ingress)
        evidence = collect_evidence(store, PROJECT, "not-found", dashboard_probe=not_found_probe,
                                    acceptance_run_started_at=RUN_STARTED_AT)
        assert evidence["user_visibility"] is None
        assert _check(eval_one(evidence), "USER_VISIBLE").status == STATUS_UNKNOWN


def test_backend_visible_two_tick_window_pass_and_fail():
    """Adversarial requirements 3 & 4: BACKEND_VISIBLE derived from the real
    Command.claimed_at must PASS within the 2-tick window and FAIL past it."""
    ingress = ts(1.0)
    within = build_fake_store("within", ingress, claimed_at=ts(1.0 + 9.0))  # 9 min < 10 min (2 ticks)
    evidence = collect_evidence(within, PROJECT, "within", acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "BACKEND_VISIBLE").status == STATUS_PASS

    beyond = build_fake_store("beyond", ingress, claimed_at=ts(1.0 + 11.0))  # 11 min > 10 min (2 ticks)
    evidence = collect_evidence(beyond, PROJECT, "beyond", acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "BACKEND_VISIBLE").status == STATUS_FAIL


def test_user_visible_two_tick_window_pass_and_fail():
    """Adversarial requirements 5 & 6: USER_VISIBLE derived from the real
    dashboard_probe observed_at must PASS within the 2-tick window and FAIL
    past it."""
    ingress = ts(1.0)
    within = build_fake_store("within-u", ingress)
    evidence = collect_evidence(within, PROJECT, "within-u",
                                dashboard_probe=_running_dashboard_probe(ts(1.0 + 9.0)),
                                acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "USER_VISIBLE").status == STATUS_PASS

    beyond = build_fake_store("beyond-u", ingress)
    evidence = collect_evidence(beyond, PROJECT, "beyond-u",
                                dashboard_probe=_running_dashboard_probe(ts(1.0 + 11.0)),
                                acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "USER_VISIBLE").status == STATUS_FAIL


def test_caller_supplied_registry_visibility_cannot_override_canonical_observation():
    """Adversarial requirement 10: even if a dispatch-request registry
    record somehow claimed an early/fake accepted status, once a real Task
    exists the canonical Task/Command truth is the ONLY source -- the
    registry claim can never override or shortcut it."""
    ingress = ts(1.0)
    late_claim = ts(1.0 + 20.0)
    store = build_fake_store("override-attempt", ingress, claimed_at=late_claim)
    # A caller/registry claiming an implausibly early accepted status/time
    # must have zero effect: read_dispatch_request_status() does not even
    # project these fields through, and _backend_visibility_from_records()
    # never consults the registry once a Task exists.
    store.dispatch_request_registry.document["backend_visible_status"] = "accepted"
    store.dispatch_request_registry.document["first_backend_visible_at"] = ingress
    evidence = collect_evidence(store, PROJECT, "override-attempt", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["backend_visibility"]["observed_at"] == late_claim
    assert _check(eval_one(evidence), "BACKEND_VISIBLE").status == STATUS_FAIL


def test_durable_visibility_from_registry_never_fabricates():
    """Direct unit coverage of _durable_visibility_from_registry(): absent,
    partial, or empty explicit fields all degrade to None (UNKNOWN) -- never
    the old "accepted @ ingress_first_observed_at" synthesis."""
    assert _durable_visibility_from_registry(None, "backend", "r1") is None
    assert _durable_visibility_from_registry({"ingress_first_observed_at": ts(1.0)}, "backend", "r1") is None
    assert _durable_visibility_from_registry({"backend_visible_status": "accepted"}, "backend", "r1") is None
    assert _durable_visibility_from_registry({"first_backend_visible_at": ts(1.0)}, "backend", "r1") is None
    real = _durable_visibility_from_registry(
        {"backend_visible_status": "accepted", "first_backend_visible_at": ts(1.0), "task_id": "r1-task"},
        "backend", "r1",
    )
    assert real == {"status": "ACCEPTED", "observed_at": ts(1.0), "request_id": "r1", "task_id": "r1-task"}


def test_backend_visibility_from_records_never_fabricates_from_missing_fields():
    """Direct unit coverage of _backend_visibility_from_records(): a
    malformed task with no status/timestamp at all degrades to None."""
    assert _backend_visibility_from_records({}, None, "r1", "r1-task") is None
    assert _backend_visibility_from_records({"status": "blocked"}, None, "r1", "r1-task") is None


def test_list_area_records_prefers_records_mapping_then_falls_back_to_list_records():
    fake_drive = FakeDriveRecords({("tasks", PROJECT, "t1"): {"task_id": "t1"}})
    assert _list_area_records(fake_drive, "tasks", PROJECT) == [{"task_id": "t1"}]
    assert _list_area_records(fake_drive, "commands", PROJECT) == []

    fake_store = FakeStore({("tasks", PROJECT, "t1"): {"task_id": "t1"}})
    assert _list_area_records(fake_store, "tasks", PROJECT) == [{"task_id": "t1"}]

    class NoInterface:
        pass

    assert _list_area_records(NoInterface(), "tasks", PROJECT) == []


def test_real_driverecords_shape_resolves_complete_lifecycle_with_no_adapter():
    """Adversarial requirements 14 & 15: a store shaped exactly like the
    real production `manager.tasks.DriveRecords` -- list_records()/get(),
    NO `.records` mapping at all -- must resolve the full
    Task->Command->Execution->Session->Handoff chain directly through
    collect_evidence(), with no external reshaping adapter."""
    ingress = ts(1.0)
    store = build_fake_drive_records("drive-real", ingress)
    assert not hasattr(store, "records")
    evidence = collect_evidence(
        store, PROJECT, "drive-real",
        dashboard_probe=_running_dashboard_probe(ingress),
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    assert evidence["ids"]["task_id"] == "drive-real-task"
    assert evidence["ids"]["command_id"] == "drive-real-cmd"
    assert evidence["ids"]["execution_id"] == "drive-real-exec"
    assert evidence["ids"]["session_id"] == "drive-real-sess"
    assert evidence["ids"]["handoff_id"] == "drive-real-handoff"
    assert evidence["duplicate_counts"] == {"task": 1, "command": 1, "execution": 1, "session": 1, "handoff": 1}
    result = eval_one(evidence)
    for name in ("TASK_LINKAGE", "COMMAND_LINKAGE", "EXECUTION_LINKAGE", "SESSION_LINKAGE",
                 "HANDOFF_LINKAGE", "BACKEND_VISIBLE", "USER_VISIBLE"):
        assert _check(result, name).status == STATUS_PASS
    assert result.result == STATUS_PASS


def test_real_driverecords_shape_duplicate_detection_and_wrong_request_no_borrow():
    ingress = ts(1.0)
    duplicate_store = build_fake_drive_records("dup", ingress)
    other_task = dict(duplicate_store.get("tasks", PROJECT, "dup-task"))
    other_task["task_id"] = "dup-task-2"
    duplicate_store._records[("tasks", PROJECT, "dup-task-2")] = other_task
    evidence = collect_evidence(duplicate_store, PROJECT, "dup", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["duplicate_counts"]["task"] == 2
    assert eval_one(evidence).result == STATUS_FAIL

    store = build_fake_drive_records("target", ingress)
    task = store.get("tasks", PROJECT, "target-task")
    task["source_context"] = {"external_request_id": "other"}
    evidence = collect_evidence(store, PROJECT, "target", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["linkage"]["task"] == {"occurred": False}
    assert eval_one(evidence).result == STATUS_FAIL


def test_real_driverecords_shape_missing_record_fails_closed():
    empty = FakeDriveRecords({})
    evidence = collect_evidence(empty, PROJECT, "nothing", acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["backend_visibility"] is None
    assert evidence["user_visibility"] is None
    assert eval_one(evidence).result == STATUS_FAIL


def test_external_request_id_real_shape_resolves_complete_terminal_lifecycle():
    ingress = ts(1.0)
    store = build_fake_store("external", ingress)
    task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
    task["source_context"] = {"external_request_id": "external"}
    # Deliberately NOT a terminal status: RECOGNIZED_VISIBLE_STATUSES has no
    # "COMPLETED" entry (pre-existing, unrelated to this fix) and
    # collect_evidence() calls dashboard_probe exactly once for both
    # DASHBOARD_TRUTH (compared against the Task's current status) and
    # USER_VISIBLE -- this test's real purpose is proving the full
    # Task->Command->Execution->Session->Handoff chain resolves correctly
    # under external_request_id identity, not terminal-state handling
    # (covered separately above).
    evidence = collect_evidence(
        store, PROJECT, "external",
        dashboard_probe=lambda *_: {"status": "RUNNING", "observed_at": ingress},
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    result = eval_one(evidence)
    for name in ("TASK_LINKAGE", "COMMAND_LINKAGE", "EXECUTION_LINKAGE", "SESSION_LINKAGE",
                 "HANDOFF_LINKAGE", "REAL_PROVIDER", "SCHEDULER_PROVENANCE", "NO_MANUAL_TRIGGER"):
        assert _check(result, name).status == STATUS_PASS
    assert _check(result, "BACKEND_VISIBLE").status == STATUS_PASS
    assert _check(result, "USER_VISIBLE").status == STATUS_PASS
    assert result.result == STATUS_PASS


def test_external_request_id_precedence_accepts_agreeing_legacy_identity():
    ingress = ts(1.0)
    store = build_fake_store("same", ingress)
    task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
    task["source_context"] = {"external_request_id": "same", "request_id": "same"}
    evidence = collect_evidence(
        store, PROJECT, "same", dashboard_probe=_running_dashboard_probe(ingress),
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    assert _check(eval_one(evidence), "TASK_LINKAGE").status == STATUS_PASS


def test_legacy_request_id_remains_an_explicit_fallback():
    store = build_fake_store("legacy", ts(1.0))
    evidence = collect_evidence(store, PROJECT, "legacy", acceptance_run_started_at=RUN_STARTED_AT)
    assert _check(eval_one(evidence), "TASK_LINKAGE").status == STATUS_PASS


def test_disagreeing_source_identities_fail_closed_even_when_legacy_matches():
    for external_request_id, legacy_request_id in (("other", "ambiguous"), ("ambiguous", "other")):
        store = build_fake_store("ambiguous", ts(1.0))
        task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
        task["source_context"] = {
            "external_request_id": external_request_id,
            "request_id": legacy_request_id,
        }
        evidence = collect_evidence(store, PROJECT, "ambiguous", acceptance_run_started_at=RUN_STARTED_AT)
        assert evidence["linkage"]["task"]["identity_ambiguous"] is True
        assert _check(eval_one(evidence), "TASK_LINKAGE").status == STATUS_FAIL


def test_wrong_external_request_id_and_missing_source_identity_cannot_borrow_task():
    for source_context in ({"external_request_id": "other"}, {}):
        store = build_fake_store("target", ts(1.0))
        task = next(doc for (area, _, _), doc in store.records.items() if area == "tasks")
        task["source_context"] = source_context
        evidence = collect_evidence(store, PROJECT, "target", acceptance_run_started_at=RUN_STARTED_AT)
        assert evidence["linkage"]["task"] == {"occurred": False}
        assert eval_one(evidence).result == STATUS_FAIL


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
        acceptance_run_started_at=RUN_STARTED_AT,
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
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
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
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
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
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
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
# 11. Cannot silently drop a failed sample and cherry-pick 3 passes
# ---------------------------------------------------------------------------

def test_scenario_11_cannot_drop_failing_sample_and_recombine():
    ledger = RunLedger()
    failing_r2 = good_evidence("r2", manual_found=True)  # this one fails
    evidences_run1 = {"r1": good_evidence("r1"), "r2": failing_r2, "r3": good_evidence("r3")}
    report1 = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences_run1,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
        acceptance_run_started_at=RUN_STARTED_AT,
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
            acceptance_run_started_at=RUN_STARTED_AT,
        )

    # The only legitimate way forward is a brand-new consecutive fresh batch.
    evidences_run3 = {"r4": good_evidence("r4"), "r5": good_evidence("r5"), "r6": good_evidence("r6")}
    report3 = evaluate_three_consecutive(
        ["r4", "r5", "r6"], evidences_run3,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
        acceptance_run_started_at=RUN_STARTED_AT,
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
# Freshness contract: FRESHNESS is computed from
# (ingress_first_observed_at, acceptance_run_started_at), never from
# caller-supplied evidence["freshness"] and never from request_id lexical
# order (that remains RunLedger's job, and only as a same-process secondary
# guard against reuse/recombination within one run's lifetime).
# ---------------------------------------------------------------------------

def test_freshness_1_real_collect_evidence_historical_request_fails():
    # Real collect_evidence() store-walk, not a hand-built fixture: a
    # request whose own ingress_first_observed_at predates this run's
    # declared cutoff must fail FRESHNESS even though every other stage of
    # its evidence is otherwise perfectly self-consistent.
    store = build_fake_store("historical-1916", HISTORICAL_TS)
    evidence = collect_evidence(
        store, PROJECT, "historical-1916",
        dashboard_probe=_running_dashboard_probe(HISTORICAL_TS),
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    # collect_evidence() itself must emit the freshness evidence record --
    # not leave it to be inferred later -- with all four required fields.
    assert evidence["freshness"]["status"] == STATUS_FAIL
    assert evidence["freshness"]["ingress_first_observed_at"] == HISTORICAL_TS
    assert evidence["freshness"]["acceptance_run_started_at"] == RUN_STARTED_AT
    assert evidence["freshness"]["reason"]

    result = evaluate_dispatch(evidence, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, acceptance_run_started_at=RUN_STARTED_AT)
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_FAIL
    assert result.result == STATUS_FAIL


def test_freshness_2_real_collect_evidence_fresh_request_passes():
    fresh_ts = ts(1.0)  # one minute after RUN_STARTED_AT = ts(0.0)
    store = build_fake_store("r-fresh", fresh_ts)
    evidence = collect_evidence(
        store, PROJECT, "r-fresh",
        dashboard_probe=_running_dashboard_probe(fresh_ts),
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    assert evidence["freshness"]["status"] == STATUS_PASS
    assert evidence["freshness"]["ingress_first_observed_at"] == fresh_ts
    assert evidence["freshness"]["acceptance_run_started_at"] == RUN_STARTED_AT
    assert evidence["freshness"]["reason"]

    result = evaluate_dispatch(evidence, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, acceptance_run_started_at=RUN_STARTED_AT)
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_PASS
    assert result.result == STATUS_PASS


def test_freshness_12_collect_evidence_without_cutoff_emits_unknown_freshness():
    # collect_evidence() called without acceptance_run_started_at (e.g. an
    # older caller that hasn't been updated to pass one) must not omit the
    # freshness record either -- it degrades to an explicit UNKNOWN, and
    # evaluate_dispatch() independently still fails closed regardless of
    # what collect_evidence() attached.
    fresh_ts = ts(1.0)
    store = build_fake_store("r-fresh", fresh_ts)
    evidence = collect_evidence(store, PROJECT, "r-fresh", dashboard_probe=_running_dashboard_probe(fresh_ts))
    assert evidence["freshness"]["status"] == STATUS_UNKNOWN
    assert evidence["freshness"]["acceptance_run_started_at"] is None

    result = evaluate_dispatch(evidence, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_freshness_3_missing_ingress_first_observed_at_is_unknown():
    ev = good_evidence("r1")
    ev["timestamps"]["ingress_first_observed_at"] = None
    result = eval_one(ev, acceptance_run_started_at=RUN_STARTED_AT)
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_freshness_4_malformed_ingress_timestamp_is_unknown():
    ev = good_evidence("r1")
    ev["timestamps"]["ingress_first_observed_at"] = "not-a-timestamp"
    result = eval_one(ev, acceptance_run_started_at=RUN_STARTED_AT)
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_freshness_5_no_cutoff_supplied_is_unknown_never_silently_omitted():
    # This is the exact bug this fix closes: previously, when the caller
    # (or a collector that never learned about the new contract) supplied
    # no freshness signal at all, the FRESHNESS check simply did not appear
    # in the results -- not even as UNKNOWN -- letting an otherwise-good
    # evidence dict PASS overall. Now it must always be present and must
    # always fail closed.
    ev = good_evidence("r1")
    result = evaluate_dispatch(ev, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS)  # no acceptance_run_started_at
    names = [c.name for c in result.checks]
    assert "FRESHNESS" in names
    freshness = [c for c in result.checks if c.name == "FRESHNESS"][0]
    assert freshness.status == STATUS_UNKNOWN
    assert result.result == STATUS_FAIL


def test_freshness_6_historical_1916_style_record_cannot_count_in_batch():
    r1 = good_evidence("r1")
    stale = good_evidence("r-1916")
    stale["timestamps"]["ingress_first_observed_at"] = HISTORICAL_TS
    r3 = good_evidence("r3")
    evidences = {"r1": r1, "r-1916": stale, "r3": r3}
    report = evaluate_three_consecutive(
        ["r1", "r-1916", "r3"], evidences,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    stale_result = report.results[1]
    assert any(c.name == "FRESHNESS" and c.status == STATUS_FAIL for c in stale_result.checks)
    assert stale_result.result == STATUS_FAIL
    assert report.overall == STATUS_FAIL
    assert report.consecutive_pass_count == 1  # r1 passes, run breaks at r-1916


def test_freshness_7_failed_middle_sample_cannot_be_replaced_by_fresh_fourth():
    ledger = RunLedger()
    r1 = good_evidence("r1")
    stale_r2 = good_evidence("r2")
    stale_r2["timestamps"]["ingress_first_observed_at"] = HISTORICAL_TS  # fails on FRESHNESS specifically
    r3 = good_evidence("r3")
    report1 = evaluate_three_consecutive(
        ["r1", "r2", "r3"], {"r1": r1, "r2": stale_r2, "r3": r3},
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    assert report1.overall == STATUS_FAIL
    assert any(c.name == "FRESHNESS" and c.status == STATUS_FAIL for c in report1.results[1].checks)

    # Drop the freshness-failing r2 and try to combine r1+r3 with a new r4.
    with pytest.raises(FreshnessViolation):
        evaluate_three_consecutive(
            ["r1", "r3", "r4"], {"r1": good_evidence("r1"), "r3": good_evidence("r3"), "r4": good_evidence("r4")},
            expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
            acceptance_run_started_at=RUN_STARTED_AT,
        )


def test_freshness_8_brand_new_runledger_still_rejects_historical_request():
    # A fresh process / fresh RunLedger instance has zero reuse history, so
    # the ledger alone would have no basis to reject a never-before-seen id.
    # FRESHNESS must still reject it on timestamp grounds -- proving
    # RunLedger is a secondary guard, not the freshness source of truth.
    ledger = RunLedger()
    stale = good_evidence("historical-1916")
    stale["timestamps"]["ingress_first_observed_at"] = HISTORICAL_TS
    evidences = {"r1": good_evidence("r1"), "historical-1916": stale, "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(
        ["historical-1916", "r1", "r3"], evidences,
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, ledger=ledger,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    # No FreshnessViolation was raised (the ledger had never seen this id --
    # this is not a ledger rejection); the rejection comes from FRESHNESS.
    assert report.overall == STATUS_FAIL
    assert any(c.name == "FRESHNESS" and c.status == STATUS_FAIL for c in report.results[0].checks)


def test_freshness_9_exact_cutoff_boundary_is_deterministic():
    cutoff = "2026-08-24T16:40:00.00Z"
    at_cutoff = good_evidence("r1")
    at_cutoff["timestamps"]["ingress_first_observed_at"] = cutoff
    result_at = eval_one(at_cutoff, acceptance_run_started_at=cutoff)
    assert any(c.name == "FRESHNESS" and c.status == STATUS_PASS for c in result_at.checks)

    one_second_before = good_evidence("r1")
    one_second_before["timestamps"]["ingress_first_observed_at"] = "2026-08-24T16:39:59.00Z"
    result_before = eval_one(one_second_before, acceptance_run_started_at=cutoff)
    assert any(c.name == "FRESHNESS" and c.status == STATUS_FAIL for c in result_before.checks)


def test_freshness_10_timezone_aware_timestamps_are_compared_by_instant():
    # "2026-08-25T00:40:00+08:00" is the exact same instant as
    # "2026-08-24T16:40:00Z" -- a naive string/lexical comparison of these
    # two representations would disagree; instant-based comparison must not.
    cutoff = "2026-08-24T16:40:00.00Z"
    same_instant_different_offset = good_evidence("r1")
    same_instant_different_offset["timestamps"]["ingress_first_observed_at"] = "2026-08-25T00:40:00+08:00"
    result = eval_one(same_instant_different_offset, acceptance_run_started_at=cutoff)
    assert any(c.name == "FRESHNESS" and c.status == STATUS_PASS for c in result.checks)

    # A naive (no offset/tzinfo) timestamp must not be silently interpreted
    # as local machine time -- that would make the result depend on which
    # host runs the evaluation. It degrades to UNKNOWN instead.
    naive = good_evidence("r1")
    naive["timestamps"]["ingress_first_observed_at"] = "2026-08-24T17:00:00"
    naive_result = eval_one(naive, acceptance_run_started_at=cutoff)
    assert any(c.name == "FRESHNESS" and c.status == STATUS_UNKNOWN for c in naive_result.checks)
    assert naive_result.result == STATUS_FAIL


# ---------------------------------------------------------------------------
# Additional coverage: UNKNOWN handling, SLA-start semantics, human/json output
# ---------------------------------------------------------------------------

def test_sla_start_is_ingress_observed_not_request_created():
    # request_created_at is far earlier than ingress_first_observed_at
    # (long scheduler pickup latency); visibility right after ingress
    # observation must still PASS the 2-tick SLA window.
    ev = good_evidence("r1", request_created_min=0.0, ingress_min=120.0, backend_visible_min=121.0, user_visible_min=121.5)
    result = eval_one(ev, acceptance_run_started_at=ts(0.0))
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
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    text = report.to_human_summary()
    for required in (
        "REQUEST_1:", "REQUEST_ID:", "SLA_START:", "FIRST_VISIBLE:", "VISIBILITY_TICKS:",
        "BACKEND_VISIBLE:", "USER_VISIBLE:", "FRESHNESS:", "TASK_LINKAGE:", "COMMAND_LINKAGE:",
        "EXECUTION_LINKAGE:", "SESSION_LINKAGE:", "HANDOFF_LINKAGE:", "IDEMPOTENCY:",
        "REAL_PROVIDER:", "NO_MANUAL_TRIGGER:", "DASHBOARD_TRUTH:", "RESULT:",
        "CONSECUTIVE_PASS_COUNT:", "HANDSOFF_DAILY_USABLE:",
    ):
        assert required in text, f"missing {required!r} in human summary"


def test_json_report_is_machine_readable():
    evidences = {"r1": good_evidence("r1"), "r2": good_evidence("r2"), "r3": good_evidence("r3")}
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
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
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], evidences, expected_project_id=PROJECT, tick_seconds=TICK_SECONDS,
        acceptance_run_started_at=RUN_STARTED_AT,
    )
    # r1 passes, r2 fails, r3 (evaluated independently) may pass -- but the
    # CONSECUTIVE run is broken at r2, so consecutive_pass_count must be 1,
    # not "2 passes out of 3" cherry-picked.
    assert report.results[0].result == STATUS_PASS
    assert report.results[1].result == STATUS_FAIL
    assert report.consecutive_pass_count == 1
    assert report.overall == STATUS_FAIL


def _check(result, name):
    return next(check for check in result.checks if check.name == name)


def test_scheduler_provenance_is_required_and_complete_proof_passes():
    result = eval_one(good_evidence("r1"))
    assert _check(result, "SCHEDULER_PROVENANCE").status == STATUS_PASS
    assert result.result == STATUS_PASS


def test_python_only_missing_and_legacy_provenance_are_unknown_not_pass():
    for value in ({"status": "UNKNOWN"}, None, {}):
        evidence = good_evidence("r1")
        if value is None:
            evidence.pop("scheduler_provenance")
        else:
            evidence["scheduler_provenance"] = value
        result = eval_one(evidence)
        assert _check(result, "SCHEDULER_PROVENANCE").status == STATUS_UNKNOWN
        assert result.result == STATUS_FAIL


def test_explicit_provenance_failure_and_middle_failure_cannot_pass_batch():
    failing = good_evidence("r2")
    failing["scheduler_provenance"] = scheduler_provenance(STATUS_FAIL)
    result = eval_one(failing)
    assert _check(result, "SCHEDULER_PROVENANCE").status == STATUS_FAIL
    report = evaluate_three_consecutive(
        ["r1", "r2", "r3"], {"r1": good_evidence("r1"), "r2": failing, "r3": good_evidence("r3")},
        expected_project_id=PROJECT, tick_seconds=TICK_SECONDS, acceptance_run_started_at=RUN_STARTED_AT,
    )
    assert report.overall == STATUS_FAIL
    assert report.consecutive_pass_count == 1


def test_collect_evidence_emits_canonical_scheduler_provenance_and_rejects_cross_task_provider():
    fresh = ts(1.0)
    store = build_fake_store("r1", fresh)
    evidence = collect_evidence(store, PROJECT, "r1", dashboard_probe=_running_dashboard_probe(fresh),
                                acceptance_run_started_at=RUN_STARTED_AT)
    assert evidence["scheduler_provenance"]["status"] == STATUS_PASS
    assert evidence["scheduler_provenance"]["os_scheduler_evidence"]["instance_id"] == "instance-1"
    execution = next(doc for (area, _, _), doc in store.records.items() if area == "executions")
    execution["provider_evidence"]["scheduler_invocation_id"] = "b" * 32
    mismatched = collect_evidence(store, PROJECT, "r1", dashboard_probe=_running_dashboard_probe(fresh),
                                  acceptance_run_started_at=RUN_STARTED_AT)
    assert mismatched["scheduler_provenance"]["status"] == STATUS_FAIL
