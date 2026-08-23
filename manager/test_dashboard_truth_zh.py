"""HOME Dashboard zh-TW Truth Layer -- deterministic view-model tests.

One TestCase per required truth-contract scenario. All fixtures are plain
dicts (schema-shaped, no I/O, no network) fed straight into the pure
builders in manager.dashboard_truth_zh. Run directly for the verdict line:

    python -m manager.test_dashboard_truth_zh
"""
import sys
import unittest
from datetime import datetime, timezone

from manager.dashboard_core import UNKNOWN_LABEL, build_provenance_vm
from manager.dashboard_truth_zh import (
    NOT_CREATED_ZH,
    UNKNOWN_ZH,
    AccountQuotaCardViewModel,
    build_chain_truth_zh,
    build_execution_truth_zh,
    build_handoff_truth_zh,
    build_provenance_truth_zh,
    build_quota_truth_zh,
    build_routing_truth_zh,
    build_session_truth_zh,
    build_task_truth_zh,
    dispatch_availability_zh,
    latest_handoff,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def _quota_vm(**overrides) -> AccountQuotaCardViewModel:
    base = dict(
        provider="claude", account_id="a1", display_name="Claude A", card_title="Claude A",
        status="ok", freshness="fresh", stale=False, confidence="high", source="official",
        source_type="official", has_reliable_quota=True, source_reliable=True, source_verified=True,
        dispatchable=True, last_updated="2026-08-23T11:55:00Z",
        five_hour_remaining_pct=42.0, five_hour_used_pct=58.0, five_hour_resets_at="2026-08-23T16:00:00Z",
    )
    base.update(overrides)
    return AccountQuotaCardViewModel(**base)


class TestFreshUsableProvider(unittest.TestCase):
    """Fresh telemetry with remaining quota > 0 must render usable (可用)."""

    def test_fresh_usable(self):
        vm = build_quota_truth_zh([_quota_vm()], "claude", "a1")
        self.assertEqual(vm.freshness_zh, "最新")
        self.assertEqual(vm.usable, "可用")
        self.assertTrue(vm.found)


class TestStaleProvider(unittest.TestCase):
    """Stale telemetry can never render as usable, no matter what the last
    known percentage was (invariant #1: stale quota cannot render fresh)."""

    def test_stale_never_usable(self):
        vm = build_quota_truth_zh(
            [_quota_vm(freshness="STALE", stale=True, five_hour_remaining_pct=90.0)], "claude", "a1"
        )
        self.assertEqual(vm.freshness_zh, "過時")
        self.assertEqual(vm.usable, UNKNOWN_ZH)


class TestZeroQuota(unittest.TestCase):
    """0% remaining must render explicitly unusable, not merely 'low' (invariant #3)."""

    def test_zero_quota_unusable(self):
        vm = build_quota_truth_zh([_quota_vm(five_hour_remaining_pct=0.0)], "claude", "a1")
        self.assertEqual(vm.usable, "不可用（額度為 0）")


class TestUnknownQuota(unittest.TestCase):
    """No captured record for this exact (provider, account_id) -> every
    field UNKNOWN, never borrowed from another account (invariant #8)."""

    def test_no_matching_account(self):
        vm = build_quota_truth_zh([_quota_vm(account_id="a1")], "claude", "a2")
        self.assertFalse(vm.found)
        self.assertEqual(vm.freshness_zh, UNKNOWN_ZH)
        self.assertEqual(vm.usable, UNKNOWN_ZH)
        self.assertEqual(vm.five_hour_remaining, UNKNOWN_LABEL)


class TestRequestedNotEqualSelected(unittest.TestCase):
    """requested provider != selected provider must remain distinguishable,
    never silently collapsed into 'matches' (invariant #5)."""

    def test_mismatch_visible(self):
        command = {
            "requested_provider": "claude", "requested_account_id": "a1",
            "provider": "codex", "account_id": None,
            "selection_reason": ["claude quota exhausted", "codex fallback selected"],
        }
        vm = build_routing_truth_zh(command)
        self.assertEqual(vm.requested_provider, "claude")
        self.assertEqual(vm.actual_provider, "codex")
        self.assertFalse(vm.provider_matches_request)
        self.assertIn("codex fallback selected", vm.selection_reason)

    def test_automatic_selection_is_not_a_mismatch(self):
        command = {"requested_provider": None, "provider": "codex", "account_id": None}
        vm = build_routing_truth_zh(command)
        self.assertIsNone(vm.provider_matches_request)
        self.assertEqual(vm.requested_provider, "自動選擇")


class TestNoExecutionYet(unittest.TestCase):
    """No Execution ID -> NOT_CREATED (尚未建立), not an error (invariant #7)."""

    def test_no_execution(self):
        vm = build_execution_truth_zh(None, NOW)
        self.assertEqual(vm.execution_id, NOT_CREATED_ZH)
        self.assertEqual(vm.status_zh, NOT_CREATED_ZH)

    def test_chain_no_execution_referenced(self):
        chain = build_chain_truth_zh(False, None, False, None, "ready")
        self.assertEqual(chain.execution_link_zh, NOT_CREATED_ZH)


class TestBrokenExecutionLink(unittest.TestCase):
    """Execution ID exists but the record could not be fetched must
    visibly indicate broken linkage, not 'no execution' (invariant #6)."""

    def test_execution_unreadable(self):
        chain = build_chain_truth_zh(True, None, False, None, "in_progress")
        self.assertIn("execution_unreadable", chain.execution_link_zh)
        self.assertEqual(chain.chain_state_zh, "鏈結中斷")


class TestBrokenSessionLink(unittest.TestCase):
    """Same guarantee for a referenced-but-unreadable Session record."""

    def test_session_unreadable(self):
        chain = build_chain_truth_zh(True, {"execution_id": "e1"}, True, None, "in_progress")
        self.assertIn("session_unreadable", chain.session_link_zh)
        self.assertEqual(chain.chain_state_zh, "鏈結中斷")


class TestCompletedChain(unittest.TestCase):
    """A fully readable chain on a completed task reads as 已完成, not 鏈結中斷."""

    def test_completed(self):
        chain = build_chain_truth_zh(True, {"execution_id": "e1"}, True, {"session_id": "s1"}, "completed")
        self.assertEqual(chain.execution_link_zh, "已讀取")
        self.assertEqual(chain.session_link_zh, "已讀取")
        self.assertEqual(chain.chain_state_zh, "已完成")


class TestBlockedChain(unittest.TestCase):
    """A blocked task's chain state reads 已阻擋 even when linkage itself is
    fine -- blocked-ness is a task-level fact, not a linkage failure."""

    def test_blocked(self):
        chain = build_chain_truth_zh(True, {"execution_id": "e1"}, False, None, "blocked")
        self.assertEqual(chain.chain_state_zh, "已阻擋")

    def test_task_truth_blocked_reason_visible(self):
        task = {"status": "blocked", "blocked_reason": "waiting on quota reset", "task_id": "t1", "project_id": "p1"}
        vm = build_task_truth_zh(task)
        self.assertEqual(vm.status_zh, "已阻擋")
        self.assertEqual(vm.blocked_reason, "waiting on quota reset")

    def test_task_truth_not_blocked_has_no_reason(self):
        task = {"status": "in_progress", "task_id": "t1", "project_id": "p1"}
        vm = build_task_truth_zh(task)
        self.assertEqual(vm.blocked_reason, "—")

    def test_task_truth_no_command_yet(self):
        task = {"status": "queued", "task_id": "t1", "project_id": "p1"}
        vm = build_task_truth_zh(task, command=None)
        self.assertEqual(vm.command_id, NOT_CREATED_ZH)


class TestShaMismatch(unittest.TestCase):
    """Any SHA divergence across dashboard/running/tested/activated must
    render 不一致 (not aligned), never a soft/partial pass (invariant #10)."""

    def test_mismatch(self):
        vm = build_provenance_vm(
            dashboard_repository_path="C:/repo", dashboard_branch="main", dashboard_sha="aaa",
            watcher_repository_path="C:/repo", watcher_branch="main",
            watcher_running_sha="aaa", watcher_tested_sha="bbb", watcher_activated_sha="aaa",
            now=NOW,
        )
        truth = build_provenance_truth_zh(vm)
        self.assertEqual(truth.all_match_zh, "不一致")


class TestAllShasAligned(unittest.TestCase):
    """All four SHAs known and identical -> the only case allowed to say 一致."""

    def test_aligned(self):
        vm = build_provenance_vm(
            dashboard_repository_path="C:/repo", dashboard_branch="main", dashboard_sha="aaa",
            watcher_repository_path="C:/repo", watcher_branch="main",
            watcher_running_sha="aaa", watcher_tested_sha="aaa", watcher_activated_sha="aaa",
            now=NOW,
        )
        truth = build_provenance_truth_zh(vm)
        self.assertEqual(truth.all_match_zh, "一致")


class TestNoEligibleProvider(unittest.TestCase):
    """No dispatchable AI account -> automatic dispatch unavailable must be
    stated plainly, never a blank/omitted recommendation (invariant #9)."""

    def test_none_eligible(self):
        msg = dispatch_availability_zh(None, "No AI Available")
        self.assertIn("不可用", msg)

    def test_eligible_shows_name(self):
        msg = dispatch_availability_zh("claude", "Claude A")
        self.assertIn("Claude A", msg)


class TestSessionAndHandoffTruth(unittest.TestCase):
    """Session/handoff detail truth: never-created is distinguished from a
    populated record, and 'latest' handoff selection never guesses order."""

    def test_no_session_yet(self):
        vm = build_session_truth_zh(None)
        self.assertEqual(vm.session_id, NOT_CREATED_ZH)

    def test_populated_session(self):
        vm = build_session_truth_zh({
            "session_id": "s1", "status": "active", "provider": "claude",
            "started_at": "2026-08-23T10:00:00Z", "updated_at": "2026-08-23T11:00:00Z", "summary": "doing work",
        })
        self.assertEqual(vm.status_zh, "進行中")
        self.assertEqual(vm.summary, "doing work")

    def test_no_handoff_yet(self):
        vm = build_handoff_truth_zh(None)
        self.assertEqual(vm.handoff_id, NOT_CREATED_ZH)

    def test_latest_handoff_picks_newest(self):
        handoffs = [
            {"handoff_id": "h1", "created_at": "2026-08-20T00:00:00Z"},
            {"handoff_id": "h2", "created_at": "2026-08-23T00:00:00Z"},
        ]
        self.assertEqual(latest_handoff(handoffs)["handoff_id"], "h2")

    def test_latest_handoff_empty_list_is_none(self):
        self.assertIsNone(latest_handoff([]))


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
