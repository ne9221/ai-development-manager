import subprocess
import unittest
from datetime import datetime, timezone

from manager.quota_reader import QuotaReaderError
from manager.tasks import TaskError
from manager.status_bar import (
    UNKNOWN, build_snapshot, fetch_execution, fetch_quota_document, fetch_snapshot,
    fetch_task, github_sync_status,
)


NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def execution(status="running", **overrides):
    base = {
        "provider": "codex", "project_id": "p1", "task_id": "t1", "execution_id": "e1",
        "session_id": "codex:s1", "status": status, "access": "production_write",
        "started_at": "2026-08-17T11:00:00.000000Z",
        "heartbeat_at": "2026-08-17T11:50:00.000000Z",
        "progress_updated_at": "2026-08-17T11:45:00.000000Z",
        "cleanup_evidence": {"task_claim_release": "released", "writer_release": "released"},
    }
    base.update(overrides)
    return base


def quota_document(remaining=(80,), last_updated="2026-08-17T11:59:00Z", source_type="official",
                    confidence="official", provider="codex"):
    windows = [{"name": f"w{i}", "remaining_percent": value, "used_percent": 100 - value, "resets_at": None} for i, value in enumerate(remaining)]
    return {
        "generated_at": last_updated,
        "providers": [{
            "provider": provider, "display_name": provider, "status": "ok",
            "collection_mode": "automatic", "source": "official_app_server",
            "source_type": source_type, "confidence": confidence,
            "last_updated": last_updated, "windows": windows,
        }],
    }


class FakeStore:
    """Read-only-by-contract double: any write call is a test failure."""

    def __init__(self, records=None, get_error=None):
        self.records = records or {}
        self.get_error = get_error

    def get(self, area, project_id, name):
        if self.get_error is not None:
            raise self.get_error
        return self.records[(area, project_id, name)]

    def put(self, *args, **kwargs):
        raise AssertionError("status_bar must never write: put() was called")

    def delete(self, *args, **kwargs):
        raise AssertionError("status_bar must never write: delete() was called")


class FakeService:
    def __init__(self, error=None):
        self.error = error

    def files(self):
        if self.error is not None:
            raise self.error
        raise AssertionError("unused in these tests")


def fake_git_runner(upstream="origin/main", counts="0 0", upstream_ok=True, counts_ok=True):
    def runner(command, **_kwargs):
        args = command[3:]
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return subprocess.CompletedProcess(command, 0 if upstream_ok else 1, (upstream + "\n") if upstream_ok else "", "")
        if args[:3] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(command, 0 if counts_ok else 1, (counts + "\n") if counts_ok else "", "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected")
    return runner


class StatusFieldTests(unittest.TestCase):
    def test_no_execution_evidence_is_unknown_never_running(self):
        self.assertEqual(UNKNOWN, build_snapshot(execution=None, now=NOW)["status"])
        self.assertEqual(UNKNOWN, build_snapshot(execution={"access": "production_write"}, now=NOW)["status"])

    def test_non_terminal_status_passes_through(self):
        self.assertEqual("running", build_snapshot(execution=execution("running"), now=NOW)["status"])
        self.assertEqual("reserved", build_snapshot(execution=execution("reserved"), now=NOW)["status"])

    def test_terminal_execution_with_retained_cleanup_is_not_shown_completed(self):
        record = execution("completed", cleanup_evidence={"task_claim_release": "retained", "writer_release": "released"})
        self.assertEqual("finishing", build_snapshot(execution=record, now=NOW)["status"])

    def test_terminal_execution_with_missing_cleanup_evidence_is_not_shown_completed(self):
        record = execution("completed", cleanup_evidence=None)
        self.assertEqual("finishing", build_snapshot(execution=record, now=NOW)["status"])

    def test_terminal_execution_with_confirmed_cleanup_is_completed(self):
        record = execution("completed")
        self.assertEqual("completed", build_snapshot(execution=record, now=NOW)["status"])

    def test_account_alias_is_always_unknown(self):
        self.assertEqual(UNKNOWN, build_snapshot(execution=None, now=NOW)["account_alias"])
        self.assertEqual(UNKNOWN, build_snapshot(execution=execution(), now=NOW)["account_alias"])

    def test_needs_user_action_has_no_authoritative_source_yet(self):
        self.assertIsNone(build_snapshot(execution=execution(), now=NOW)["needs_user_action"])

    def test_blocker_reflects_task_blocked_reason_only_when_blocked(self):
        blocked_task = {"status": "blocked", "blocked_reason": "waiting on human review"}
        self.assertEqual("waiting on human review", build_snapshot(task=blocked_task, now=NOW)["blocker"])
        ready_task = {"status": "ready", "blocked_reason": None}
        self.assertIsNone(build_snapshot(task=ready_task, now=NOW)["blocker"])
        self.assertIsNone(build_snapshot(task=None, now=NOW)["blocker"])

    def test_last_trustworthy_evidence_picks_latest_present_execution_timestamp(self):
        record = execution("running", heartbeat_at="2026-08-17T11:50:00.000000Z", progress_updated_at="2026-08-17T11:45:00.000000Z")
        evidence = build_snapshot(execution=record, now=NOW)["last_trustworthy_evidence"]
        self.assertEqual("execution_heartbeat_at", evidence["source"])
        self.assertEqual("2026-08-17T11:50:00.000000Z", evidence["at"])
        self.assertEqual({"source": None, "at": None}, build_snapshot(execution=None, now=NOW)["last_trustworthy_evidence"])


class RunningRequiresActiveEvidenceTests(unittest.TestCase):
    """AG SB-1.1: status='running' alone must never surface as RUNNING.
    Only a fresh heartbeat_at/progress_updated_at counts as active evidence;
    reserved_at/started_at/completed_at, record existence, and PID liveness
    do not. The freshness threshold is passed explicitly and fixed here --
    never the code's only allowed value (see test_threshold_is_injectable)."""

    MAX_AGE = 900  # arbitrary fixed value for these tests, not a hardcoded architectural constant

    def test_running_with_stale_heartbeat_is_unknown(self):
        record = execution("running", heartbeat_at="2026-08-17T11:00:00.000000Z", progress_updated_at=None)
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_no_heartbeat_or_progress_is_unknown(self):
        record = execution("running", heartbeat_at=None, progress_updated_at=None)
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_only_fresh_started_at_is_unknown(self):
        record = execution("running", started_at="2026-08-17T11:59:00.000000Z", heartbeat_at=None, progress_updated_at=None)
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_only_fresh_reserved_at_is_unknown(self):
        record = execution("running", heartbeat_at=None, progress_updated_at=None, reserved_at="2026-08-17T11:59:30.000000Z")
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_fresh_heartbeat_is_running(self):
        record = execution("running", heartbeat_at="2026-08-17T11:59:00.000000Z", progress_updated_at=None)
        self.assertEqual("running", build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_fresh_progress_updated_at_is_running(self):
        record = execution("running", heartbeat_at=None, progress_updated_at="2026-08-17T11:58:00.000000Z")
        self.assertEqual("running", build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_malformed_timestamps_is_unknown(self):
        record = execution("running", heartbeat_at="not-a-timestamp", progress_updated_at="2026/08/17")
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_running_with_future_dated_evidence_is_unknown(self):
        record = execution("running", heartbeat_at="2026-08-17T13:00:00.000000Z", progress_updated_at=None)
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_terminal_with_retained_cleanup_is_still_finishing_not_gated_by_active_evidence(self):
        record = execution("completed", cleanup_evidence={"task_claim_release": "retained", "writer_release": "released"}, heartbeat_at=None, progress_updated_at=None)
        self.assertEqual("finishing", build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=self.MAX_AGE)["status"])

    def test_threshold_is_injectable_not_hardcoded(self):
        record = execution("running", heartbeat_at="2026-08-17T11:55:00.000000Z", progress_updated_at=None)  # 5 minutes old
        self.assertEqual(UNKNOWN, build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=240)["status"])
        self.assertEqual("running", build_snapshot(execution=record, now=NOW, active_evidence_max_age_seconds=360)["status"])


class QuotaProjectionTests(unittest.TestCase):
    def test_no_quota_document_returns_null_remaining(self):
        snapshot = build_snapshot(execution=execution(), quota_document=None, now=NOW)
        self.assertIsNone(snapshot["quota"]["remaining_percent"])
        self.assertEqual("unknown", snapshot["quota"]["freshness"])

    def test_unreliable_source_returns_null_remaining(self):
        document = quota_document(source_type="manual", confidence="manual")
        snapshot = build_snapshot(execution=execution(), quota_document=document, now=NOW)
        self.assertIsNone(snapshot["quota"]["remaining_percent"])

    def test_stale_quota_reports_stale_and_null_remaining_not_fresh(self):
        document = quota_document(last_updated="2026-08-17T09:00:00Z")  # 3h old, default max_age=60m
        snapshot = build_snapshot(execution=execution(), quota_document=document, now=NOW)
        self.assertEqual("stale", snapshot["quota"]["freshness"])
        self.assertIsNone(snapshot["quota"]["remaining_percent"])

    def test_fresh_reliable_quota_reports_conservative_remaining(self):
        document = quota_document(remaining=(80, 55), last_updated="2026-08-17T11:59:00Z")
        snapshot = build_snapshot(execution=execution(), quota_document=document, now=NOW)
        self.assertEqual("fresh", snapshot["quota"]["freshness"])
        self.assertEqual(55, snapshot["quota"]["remaining_percent"])

    def test_no_provider_evidence_returns_null_remaining(self):
        snapshot = build_snapshot(execution=None, quota_document=quota_document(), now=NOW)
        self.assertIsNone(snapshot["quota"]["remaining_percent"])
        self.assertEqual("unknown", snapshot["quota"]["freshness"])


class GithubSyncTests(unittest.TestCase):
    def test_ahead(self):
        result = github_sync_status("C:/repo", runner=fake_git_runner(counts="0 3"))
        self.assertEqual({"state": "ahead", "ahead": 3, "behind": 0, "reason": None}, result)

    def test_behind(self):
        result = github_sync_status("C:/repo", runner=fake_git_runner(counts="2 0"))
        self.assertEqual({"state": "behind", "ahead": 0, "behind": 2, "reason": None}, result)

    def test_diverged(self):
        result = github_sync_status("C:/repo", runner=fake_git_runner(counts="1 1"))
        self.assertEqual("diverged", result["state"])

    def test_synced(self):
        result = github_sync_status("C:/repo", runner=fake_git_runner(counts="0 0"))
        self.assertEqual("synced", result["state"])

    def test_no_repo_dir_is_unknown(self):
        result = github_sync_status(None, runner=fake_git_runner())
        self.assertEqual("unknown", result["state"])
        self.assertEqual("no_repo_dir", result["reason"])

    def test_no_upstream_is_unknown_not_a_crash(self):
        result = github_sync_status("C:/repo", runner=fake_git_runner(upstream_ok=False))
        self.assertEqual("unknown", result["state"])
        self.assertEqual("no_upstream_or_not_a_git_repo", result["reason"])

    def test_git_unavailable_is_unknown_not_a_crash(self):
        def raising_runner(*_args, **_kwargs):
            raise FileNotFoundError("git not found")
        result = github_sync_status("C:/repo", runner=raising_runner)
        self.assertEqual("unknown", result["state"])


class DriveGcsUnavailableTests(unittest.TestCase):
    def test_fetch_execution_and_task_never_raise_on_backend_failure(self):
        store = FakeStore(get_error=TaskError("Drive unreachable"))
        execution_record, error = fetch_execution(store, "p1", "e1")
        self.assertIsNone(execution_record)
        self.assertIsInstance(error, TaskError)
        task_record, error = fetch_task(store, "p1", "t1")
        self.assertIsNone(task_record)
        self.assertIsInstance(error, TaskError)

    def test_fetch_quota_document_never_raises_on_backend_failure(self):
        document, error = fetch_quota_document(FakeService(error=ConnectionError("no network")))
        self.assertIsNone(document)
        self.assertIsInstance(error, QuotaReaderError)

    def test_fetch_snapshot_degrades_to_unreachable_without_raising(self):
        store = FakeStore(get_error=TaskError("Drive unreachable"))
        service = FakeService(error=ConnectionError("no network"))
        snapshot = fetch_snapshot(store, service, project_id="p1", execution_id="e1", task_id="t1")
        self.assertEqual(UNKNOWN, snapshot["status"])
        self.assertEqual("unreachable", snapshot["drive_sync"]["state"])
        self.assertIsNone(snapshot["quota"]["remaining_percent"])


class ReadOnlyContractTests(unittest.TestCase):
    def test_fetch_snapshot_never_calls_a_write_method_on_success(self):
        store = FakeStore(records={
            ("executions", "p1", "e1"): execution(),
            ("tasks", "p1", "t1"): {"status": "ready", "blocked_reason": None},
        })
        snapshot = fetch_snapshot(store, FakeService(error=RuntimeError("no quota configured")), "p1", "e1", "t1", now=NOW)
        self.assertEqual("running", snapshot["status"])  # would raise via FakeStore.put/delete if ever called

    def test_fetch_snapshot_never_calls_a_write_method_on_failure_paths(self):
        store = FakeStore(get_error=TaskError("Drive unreachable"))
        fetch_snapshot(store, FakeService(error=ConnectionError("no network")), "p1", "e1", "t1")


if __name__ == "__main__":
    unittest.main()
