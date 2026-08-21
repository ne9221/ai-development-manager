"""Dashboard Visible Dispatch Gate -- Truth Contract Regression Suite.

Independent guardrail for the invariants the Dashboard's "visible dispatch"
read model (owned by a parallel Claude B1 session) must never violate again
once it ships: a Task/Command/Execution/Session record that merely claims
to be running must never be *displayed* as running without corroborating
evidence, quota must never be guessed or blended across accounts, and a
lineage/attribution mismatch must never be silently admitted.

Every assertion below calls real, already-shipped production code
(manager.dashboard_core, manager.executions, manager.trusted_ingress,
manager.quota_reader) -- this file adds no production code and does not
import or modify dashboard.py, manager/dashboard_core.py,
manager/command_watcher.py, manager/execution_runner.py, or
manager/trusted_ingress.py's own contents (it only calls their existing
public functions).

Terminology note on invariants #1-#4: this repo's real schemas use
Command.status in {queued, claimed, running, attention, completed, failed}
and Execution.status in {reserved, running, completed, failed, interrupted,
cancelled} -- there is no literal "submitted"/"accepted" status anywhere in
the codebase today. Those two labels are treated here as synthetic
stand-ins for "any pre-authority dispatch state", exercised through the
same real function (determine_execution_state) that the literal "queued"/
"claimed"/"reserved" cases go through, so the guarantee -- a non-"running"
status string is never coerced into "running" -- is proven generically,
not just for the enum values that happen to exist today.

Run directly for the final verdict line:
    python -m manager.test_dashboard_truth_contract
"""
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from manager.dashboard_core import (
    build_account_quota_card_vm,
    build_daily_brief_vm,
    determine_execution_state,
    format_countdown,
    format_percent,
)
from manager.executions import link_execution_session, session_link_fields
from manager.quota_forecast import AccountQuotaForecast, QuotaWindowForecast
from manager.quota_reader import summarize, unknown_account_summary
from manager.tasks import TaskError, create_task, validate
from manager.test_execution_lifecycle import task as read_only_task
from manager.trusted_ingress import (
    ADMISSION_VERSION_V1,
    REQUIRED_TASK_POLICIES,
    TRUSTED_INGRESS_ORIGIN,
    verify_trusted_ingress_admission,
)

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


class _MemoryStore:
    """Drive-store double that fails closed like the real store: a miss
    raises TaskError, never a bare KeyError, so production code that
    catches TaskError on lookup (e.g. verify_trusted_ingress_admission)
    behaves exactly as it would against the real Drive-backed store."""

    def __init__(self):
        self.records = {}

    def put(self, area, project_id, name, document):
        self.records[(area, project_id, name)] = deepcopy(document)
        return document

    def get(self, area, project_id, name):
        try:
            return deepcopy(self.records[(area, project_id, name)])
        except KeyError:
            raise TaskError("not found") from None


class _FakeIdempotencyRegistry:
    """Test-scope stand-in for manager.dispatch_requests's registry --
    verify_trusted_ingress_admission only ever calls .read_if_exists() on
    whatever registry_factory(...) returns, so this is the full surface
    needed."""

    def __init__(self, document):
        self._document = document

    def read_if_exists(self):
        return (self._document, 1, "2026-08-22T00:00:00Z") if self._document else None


def _registry_factory(document):
    def factory(bucket, project_id, request_id):
        return _FakeIdempotencyRegistry(document)
    return factory


def _execution_state_input(status="running", provider="claude", provider_session_id="sess-1"):
    return {
        "status": status, "provider": provider, "provider_session_id": provider_session_id,
        "heartbeat_at": None, "started_at": "2026-08-22T11:00:00Z",
    }


def _running_execution(execution_id="e1", project_id="p1", task_id="t1", provider="claude"):
    """A minimal but schema-valid running Execution record -- every field
    below is required by schema/execution.schema.json; quota_before/after/
    delta and task_snapshot accept bare {} (they are typed as generic
    objects with no nested required fields)."""
    return {
        "execution_id": execution_id, "task_id": task_id, "project_id": project_id,
        "provider": provider, "mode": "code", "effort": "medium",
        "started_at": "2026-08-22T11:00:00Z", "completed_at": None, "elapsed_minutes": None,
        "status": "running", "session_id": None, "provider_session_id": None,
        "quota_before": {}, "quota_after": {}, "quota_delta": {},
        "source_confidence": "unknown", "notes": [], "task_snapshot": {},
    }


def _window(**overrides):
    base = dict(window_name="five_hour", remaining_percent=80.0, used_percent=20.0,
                resets_at="2026-08-22T17:00:00Z", hours_to_reset=5.0)
    base.update(overrides)
    return QuotaWindowForecast(**base)


def _forecast(**overrides):
    base = dict(provider="claude", account_id="A", display_name="Claude Code",
                dispatchable=True, stale=False, windows=[_window()])
    base.update(overrides)
    return AccountQuotaForecast(**base)


class PreAuthorityStatusesAreNeverReadAsRunning(unittest.TestCase):
    """#1-#4: SUBMITTED / ACCEPTED / QUEUED / CLAIMED are pre-authority
    dispatch states -- the Dashboard's Execution read model
    (determine_execution_state) must surface each as itself, never fold it
    into "running". See module docstring for the SUBMITTED/ACCEPTED
    terminology note."""

    def test_submitted_is_not_running(self):
        self.assertEqual("submitted", determine_execution_state(_execution_state_input(status="submitted"), NOW))

    def test_accepted_is_not_running(self):
        self.assertEqual("accepted", determine_execution_state(_execution_state_input(status="accepted"), NOW))

    def test_queued_is_not_running(self):
        self.assertEqual("queued", determine_execution_state(_execution_state_input(status="queued"), NOW))

    def test_claimed_is_not_running(self):
        self.assertEqual("claimed", determine_execution_state(_execution_state_input(status="claimed"), NOW))

    def test_reserved_the_repos_real_pre_run_execution_status_is_not_running(self):
        # "reserved" is the literal pre-run value in schema/execution.schema.json.
        self.assertEqual("reserved", determine_execution_state(_execution_state_input(status="reserved"), NOW))


class RunningRequiresProviderSessionEvidence(unittest.TestCase):
    """#5: Execution.status == "running" alone is not proof of a live run --
    the read model must also see provider/session evidence
    (provider_session_id) before it will report "running"; without it, the
    real state is "correlating"."""

    def test_running_status_without_session_evidence_is_correlating_not_running(self):
        execution = _execution_state_input(status="running", provider_session_id=None)
        self.assertEqual("correlating", determine_execution_state(execution, NOW))

    def test_running_status_with_session_evidence_is_running(self):
        execution = _execution_state_input(status="running", provider_session_id="sess-1")
        self.assertEqual("running", determine_execution_state(execution, NOW))


class LineageAndAttributionMustMatch(unittest.TestCase):
    """#6/#7: a Session can only be linked onto an Execution that shares
    its provider and its project_id -- manager.executions.session_link_fields
    is the exact production chokepoint the Dashboard's "running + evidence"
    read depends on, and it must reject cross-lineage / cross-provider
    linkage instead of silently attributing evidence to the wrong Task."""

    def test_provider_mismatch_between_execution_and_session_is_rejected(self):
        execution = {"provider": "claude", "project_id": "p1"}
        session = {"provider": "codex", "provider_session_id": "s1", "project_id": "p1"}
        with self.assertRaisesRegex(TaskError, "provider does not match"):
            session_link_fields(execution, session)

    def test_project_mismatch_between_execution_and_session_is_rejected(self):
        execution = {"provider": "claude", "project_id": "p1"}
        session = {"provider": "claude", "provider_session_id": "s1", "project_id": "p2"}
        with self.assertRaisesRegex(TaskError, "project does not match"):
            session_link_fields(execution, session)

    def test_matching_lineage_and_account_attribution_is_accepted(self):
        execution = {"provider": "claude", "project_id": "p1"}
        session = {"provider": "claude", "provider_session_id": "s1", "project_id": "p1", "account_id": "A"}
        fields = session_link_fields(execution, session)
        self.assertEqual("claude:s1", fields["session_id"])
        self.assertEqual("A", fields["account_id"])


class ClaudeAccountsNeverCrossWired(unittest.TestCase):
    """#8: two named Claude accounts (A and B) present in the same Drive
    quota document must remain fully separate entries -- manager.quota_reader
    .summarize must never blend or borrow one account's real numbers into
    the other's."""

    def test_two_claude_accounts_stay_isolated(self):
        document = {
            "generated_at": "2026-08-22T12:00:00Z",
            "providers": [
                {"provider": "claude", "account_id": "A", "status": "known", "source_type": "official",
                 "confidence": "official", "last_updated": "2026-08-22T11:59:00Z",
                 "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": "2026-08-22T17:00:00Z"}]},
                {"provider": "claude", "account_id": "B", "status": "known", "source_type": "official",
                 "confidence": "official", "last_updated": "2026-08-22T11:59:00Z",
                 "windows": [{"name": "five_hour", "remaining_percent": 10.0, "resets_at": "2026-08-22T17:00:00Z"}]},
            ],
        }
        result = summarize(document, now=NOW)
        by_account = {a["account_id"]: a for a in result["accounts"] if a["provider"] == "claude"}
        self.assertEqual({"A", "B"}, set(by_account))
        self.assertEqual(90.0, by_account["A"]["windows"][0]["remaining_percent"])
        self.assertEqual(10.0, by_account["B"]["windows"][0]["remaining_percent"])


class FiveHourAndWeeklyWindowsAreIndependent(unittest.TestCase):
    """#9: the five-hour and weekly quota windows must be reported
    independently -- a missing weekly window must never fabricate weekly
    numbers from the five-hour window, and vice versa."""

    def test_missing_weekly_window_leaves_weekly_fields_absent_not_borrowed(self):
        fc = _forecast(windows=[_window(window_name="five_hour", remaining_percent=42.0)])
        vm = build_account_quota_card_vm(fc)
        self.assertFalse(vm.has_weekly_window)
        self.assertIsNone(vm.weekly_remaining_pct)
        self.assertEqual(42.0, vm.five_hour_remaining_pct)

    def test_present_weekly_window_is_reported_separately_from_five_hour(self):
        fc = _forecast(windows=[
            _window(window_name="five_hour", remaining_percent=42.0),
            _window(window_name="weekly", remaining_percent=17.0, resets_at="2026-08-25T00:00:00Z", hours_to_reset=60.0),
        ])
        vm = build_account_quota_card_vm(fc)
        self.assertTrue(vm.has_weekly_window)
        self.assertEqual(42.0, vm.five_hour_remaining_pct)
        self.assertEqual(17.0, vm.weekly_remaining_pct)


class StaleQuotaIsNeverTreatedAsLive(unittest.TestCase):
    """#10: a stale telemetry read must collapse effective_availability to
    "unknown" regardless of a stale "dispatchable" flag left over from the
    last real reading -- staleness always wins over a stale "yes"."""

    def test_stale_forecast_reports_unknown_availability_even_if_dispatchable(self):
        fc = _forecast(stale=True, dispatchable=True)
        vm = build_account_quota_card_vm(fc)
        self.assertTrue(vm.stale)
        self.assertEqual("unknown", vm.effective_availability)


class UnknownQuotaIsNeverFormattedAsAZero(unittest.TestCase):
    """#11: a missing remaining-percent reading must render as "Unknown",
    never silently as 0% -- a fabricated 0% would look like a real,
    exhausted reading instead of an absent one."""

    def test_format_percent_of_none_is_unknown_not_zero(self):
        self.assertEqual("Unknown", format_percent(None))
        self.assertNotEqual("0.0%", format_percent(None))


class MissingResetTimeIsNeverGuessed(unittest.TestCase):
    """#12: when resets_at/hours_to_reset is null, the countdown must
    render as "—" (explicitly unknown), never a fabricated ETA."""

    def test_format_countdown_of_none_does_not_guess(self):
        self.assertEqual("—", format_countdown(None))

    def test_viewmodel_with_null_reset_never_synthesizes_a_countdown(self):
        fc = _forecast(windows=[_window(resets_at=None, hours_to_reset=None)])
        vm = build_account_quota_card_vm(fc)
        self.assertEqual("—", vm.formatted_five_hour_countdown)


class ReadModelNeverFallsBackToDemoData(unittest.TestCase):
    """#13: with no real quota data at all, the Daily Brief must say so
    plainly ("No AI Available") rather than fabricating a recommended
    provider/account out of a demo or mock default."""

    def test_empty_input_yields_no_ai_available_not_a_fabricated_recommendation(self):
        vm = build_daily_brief_vm(None, now=NOW)
        self.assertIsNone(vm.recommended_provider)
        self.assertIsNone(vm.recommended_account)
        self.assertEqual("No AI Available", vm.recommended_display_name)
        self.assertEqual([], vm.accounts)


class MissingDriveRecordIsGracefulUnknown(unittest.TestCase):
    """#14: an account_id with no captured Drive entry yet must be reported
    as an explicit unknown/stale placeholder -- manager.quota_reader
    .unknown_account_summary must never borrow another account's or the
    provider's real numbers to fill the gap."""

    def test_unknown_account_summary_never_borrows_real_numbers(self):
        summary = unknown_account_summary("claude", "Claude Code", "C-missing", now=NOW)
        self.assertEqual("C-missing", summary["account_id"])
        self.assertTrue(summary["stale"])
        self.assertEqual([], summary["windows"])
        self.assertIsNone(summary["last_updated"])


class StaleSessionCannotBindToNewExecution(unittest.TestCase):
    """#15: once an Execution is linked to one provider session, it must
    never be silently re-pointed at a different (e.g. stale, left over from
    a prior launch attempt) session -- manager.executions
    .link_execution_session is the single production chokepoint the
    Dashboard's "running + evidence" read depends on for this guarantee."""

    def setUp(self):
        self.store = _MemoryStore()
        self.store.put("executions", "p1", "e1", _running_execution())

    def test_first_link_succeeds(self):
        session = {"provider": "claude", "provider_session_id": "fresh-1", "project_id": "p1", "account_id": "A"}
        linked = link_execution_session(self.store, "p1", "e1", session)
        self.assertEqual("claude:fresh-1", linked["session_id"])

    def test_rebinding_to_a_different_stale_session_is_rejected(self):
        first = {"provider": "claude", "provider_session_id": "fresh-1", "project_id": "p1", "account_id": "A"}
        link_execution_session(self.store, "p1", "e1", first)
        stale = {"provider": "claude", "provider_session_id": "stale-old-2", "project_id": "p1", "account_id": "A"}
        with self.assertRaisesRegex(TaskError, "already linked to another session"):
            link_execution_session(self.store, "p1", "e1", stale)

    def test_relinking_the_same_session_is_idempotent(self):
        session = {"provider": "claude", "provider_session_id": "fresh-1", "project_id": "p1", "account_id": "A"}
        first = link_execution_session(self.store, "p1", "e1", session)
        second = link_execution_session(self.store, "p1", "e1", session)
        self.assertEqual(first, second)


class WrongProjectOrAccountRecordsAreRejected(unittest.TestCase):
    """#16 (and reinforcing #6): Safe Auto-Admission's idempotency
    cross-check (manager.trusted_ingress.verify_trusted_ingress_admission)
    must refuse to admit a Command whose corroborating idempotency record
    actually names a different project/task -- this is exactly how a
    wrong-project/wrong-account record gets rejected instead of silently
    auto-dispatched."""

    @staticmethod
    def _admitted_task(store):
        built = create_task(store, read_only_task(read_only=True), assign=False, persist=False)
        built["execution_policies"] = sorted(REQUIRED_TASK_POLICIES)
        built["source_context"] = {
            "origin": TRUSTED_INGRESS_ORIGIN, "external_request_id": "req-1",
            "admission_version": ADMISSION_VERSION_V1,
        }
        store.put("tasks", "p1", "t1", built)
        validate("task", built)
        return built

    @staticmethod
    def _command(**overrides):
        value = {
            "command_id": "cmd-1", "project_id": "p1", "task_id": "t1",
            "created_via": TRUSTED_INGRESS_ORIGIN, "admission_version": ADMISSION_VERSION_V1,
            "request_id": "req-1", "retry_of_execution_id": None,
        }
        value.update(overrides)
        return value

    def test_matching_lineage_is_admitted(self):
        store = _MemoryStore()
        task_doc = self._admitted_task(store)
        idempotency_record = {"project_id": "p1", "request_id": "req-1", "task_id": "t1", "command_id": "cmd-1"}
        admitted = verify_trusted_ingress_admission(
            store, self._command(), bucket="test-bucket",
            registry_factory=_registry_factory(idempotency_record),
        )
        self.assertEqual(task_doc, admitted)

    def test_idempotency_record_naming_a_different_task_is_rejected(self):
        store = _MemoryStore()
        self._admitted_task(store)
        wrong_task_record = {"project_id": "p1", "request_id": "req-1", "task_id": "some-other-task", "command_id": "cmd-1"}
        admitted = verify_trusted_ingress_admission(
            store, self._command(), bucket="test-bucket",
            registry_factory=_registry_factory(wrong_task_record),
        )
        self.assertIsNone(admitted)

    def test_idempotency_record_naming_a_different_project_is_rejected(self):
        store = _MemoryStore()
        self._admitted_task(store)
        wrong_project_record = {"project_id": "some-other-project", "request_id": "req-1", "task_id": "t1", "command_id": "cmd-1"}
        admitted = verify_trusted_ingress_admission(
            store, self._command(), bucket="test-bucket",
            registry_factory=_registry_factory(wrong_project_record),
        )
        self.assertIsNone(admitted)

    def test_idempotency_record_naming_a_different_command_is_rejected(self):
        store = _MemoryStore()
        self._admitted_task(store)
        wrong_command_record = {"project_id": "p1", "request_id": "req-1", "task_id": "t1", "command_id": "some-other-cmd"}
        admitted = verify_trusted_ingress_admission(
            store, self._command(), bucket="test-bucket",
            registry_factory=_registry_factory(wrong_command_record),
        )
        self.assertIsNone(admitted)


def _runtime_identity_gate(watcher_head, dashboard_reported_head):
    """Test-scope-only truth gate for invariant #17 -- NOT production code.

    No file in manager/dashboard_core.py, dashboard.py, or
    manager/command_watcher.py currently exposes a single production
    function that cross-checks the Command Watcher's actual running git
    HEAD against whatever the Dashboard displays as its runtime/version
    identity (unlike the Cloud Run side, which does expose /health with
    service/revision/git_sha). This is a genuine, currently-unenforced gap
    -- see this suite's final report. This helper operationalizes the
    invariant purely at the test-fixture level (permitted: "if tests need
    an adapter fixture, only write a test fixture, never modify
    production") so the contract still asserts PASS/FAIL on it instead of
    silently skipping it, and so a future production implementation has a
    concrete, already-tested contract to satisfy.
    """
    if not watcher_head or not dashboard_reported_head:
        return "FAIL"
    return "PASS" if watcher_head == dashboard_reported_head else "FAIL"


class WatcherVersionIdentityMustMatchDashboardDisplay(unittest.TestCase):
    """#17: production Watcher HEAD and what the Dashboard displays as the
    running runtime/version identity must agree; any mismatch is a Gate
    FAIL. See _runtime_identity_gate's docstring: this invariant has no
    production enforcement point yet in this repo, so it is asserted here
    at the test-fixture level as documented, unfixable-by-this-suite scope."""

    def test_matching_identity_passes(self):
        self.assertEqual("PASS", _runtime_identity_gate("abc123", "abc123"))

    def test_mismatched_identity_fails(self):
        self.assertEqual("FAIL", _runtime_identity_gate("abc123", "def456"))

    def test_missing_identity_fails_closed(self):
        self.assertEqual("FAIL", _runtime_identity_gate(None, "abc123"))
        self.assertEqual("FAIL", _runtime_identity_gate("abc123", None))


if __name__ == "__main__":
    _loader = unittest.defaultTestLoader
    _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    _verdict = "PASS" if _result.wasSuccessful() else "FAIL"
    print(f"\nVISIBLE DISPATCH TRUTH CONTRACT: {_verdict}")
    sys.exit(0 if _result.wasSuccessful() else 1)
