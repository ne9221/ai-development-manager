"""Focused tests for the direct-dispatch-ingress request_id idempotency
primitive (manager/dispatch_requests.py), mirroring manager/test_task_claims.py's
proven approach against an in-memory double of the GCS transport."""

import threading
import unittest
from unittest.mock import MagicMock

from manager.dispatch_requests import (
    DEFAULT_RECENT_REQUEST_LIST_LIMIT, DispatchRequestClaimConflict, claim_dispatch_request,
    dispatch_request_object_name, list_recent_dispatch_request_ids, mark_dispatch_request_status,
    read_dispatch_request_status, release_dispatch_request_claim, resolve_dispatch_status_for_request,
)
from manager.tasks import DriveRecords, TaskError, create_project
from manager.test_task_claims import AmbiguousThenUnreadableRegistry, MemoryClaimRegistry
from manager.test_tasks import FakeDriveService


class DispatchRequestClaimTests(unittest.TestCase):
    def test_first_submission_is_claimed_and_creates_identity(self):
        registry = MemoryClaimRegistry()
        result = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertTrue(result["claimed"])
        self.assertEqual("dispatch-req-1", result["task_id"])
        self.assertEqual("dispatch-req-1", result["command_id"])
        self.assertEqual(1, result["generation"])
        self.assertTrue(result["created_by_this_call"])

    def test_same_request_id_retry_is_idempotent_no_second_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        second = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:05Z")
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["command_id"], second["command_id"])
        self.assertEqual(first["generation"], second["generation"])
        # The retry's own (later) created_at must never overwrite the winner's.
        self.assertEqual("2026-08-17T00:00:00Z", second["created_at"])

    def test_two_concurrent_identical_requests_have_exactly_one_claimant(self):
        for _ in range(20):
            registry = MemoryClaimRegistry()
            barrier = threading.Barrier(2)
            results = []

            def run():
                barrier.wait(timeout=2)
                results.append(claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z"))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=3)
            self.assertEqual(2, len(results))
            claimed = [r for r in results if r["claimed"]]
            replayed = [r for r in results if not r["claimed"]]
            self.assertEqual(1, len(claimed))
            self.assertEqual(1, len(replayed))
            self.assertEqual(claimed[0]["task_id"], replayed[0]["task_id"])
            self.assertEqual(claimed[0]["command_id"], replayed[0]["command_id"])

    def test_ambiguous_create_self_recognizes_own_success(self):
        registry = MemoryClaimRegistry()
        registry.ambiguous_queue.append(ConnectionError("simulated client timeout after server-side success"))
        result = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertTrue(result["claimed"])
        self.assertEqual(1, result["generation"])
        self.assertFalse(result["created_by_this_call"])

    def test_generation_matched_release_makes_pre_artifact_claim_retryable(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        released = release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", claim["generation"])
        self.assertTrue(released["released"])
        retry = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:01:00Z")
        self.assertTrue(retry["claimed"])
        self.assertTrue(retry["created_by_this_call"])

    def test_stale_release_never_deletes_newer_claim(self):
        registry = MemoryClaimRegistry()
        first = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", first["generation"])
        newer = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:01:00Z")
        with self.assertRaises(DispatchRequestClaimConflict):
            release_dispatch_request_claim(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", first["generation"])
        self.assertEqual(newer["generation"], registry.generation)
        self.assertIsNotNone(registry.document)

    def test_ambiguous_create_and_reread_failure_fails_closed(self):
        registry = AmbiguousThenUnreadableRegistry()
        with self.assertRaises(TaskError):
            claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")
        self.assertIsNone(registry.document)

    def test_backend_permission_denied_fails_closed(self):
        registry = MemoryClaimRegistry()
        registry.unavailable = True
        with self.assertRaises(TaskError):
            claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-17T00:00:00Z")

    def test_object_name_is_scoped_and_collision_free(self):
        self.assertEqual("dispatch-requests/p1/req-1.json", dispatch_request_object_name("p1", "req-1"))
        with self.assertRaises(TaskError):
            dispatch_request_object_name("../p1", "req-1")

    def test_claim_is_accepted_status_immediately_on_creation(self):
        """P0 dispatch-two-tick-final: status defaults to "accepted" in the
        SAME create-if-absent write the claim itself lands in -- durable,
        queryable request-received truth before any slow provider/quota/
        history resolution ever runs."""
        registry = MemoryClaimRegistry()
        claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("accepted", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_missing_claim_status_is_none_not_a_fabricated_status(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(read_dispatch_request_status(registry, "p1", "req-never-seen"))

    def test_mark_status_transitions_and_is_queryable(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        new_generation = mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched")
        self.assertIsNotNone(new_generation)
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("dispatched", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_mark_status_failed_carries_a_reason(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "failed", failure_reason="no eligible provider")
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("failed", status["status"])
        self.assertEqual("no eligible provider", status["failure_reason"])

    def test_mark_status_stale_generation_is_a_lost_race_not_an_exception(self):
        """Best-effort, observability-only contract: a stale generation must
        return None (lost race), never raise -- the caller's own real
        success/failure must never be masked by this side channel failing."""
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched")
        result = mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "failed", failure_reason="stale")
        self.assertIsNone(result)
        # The winning "dispatched" transition must survive untouched.
        self.assertEqual("dispatched", read_dispatch_request_status(registry, "p1", "req-1")["status"])

    def test_mark_status_backend_unavailable_returns_none_not_exception(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        registry.unavailable = True
        self.assertIsNone(mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched"))

    def test_legacy_record_without_status_field_defaults_to_accepted(self):
        """A record written before this change (no status/failure_reason
        fields at all) must still validate and read as "accepted" rather
        than being treated as malformed -- full backward compatibility with
        every dispatch-request claim already live in GCS."""
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1", "request_id": "req-legacy",
                              "task_id": "dispatch-req-legacy", "command_id": "dispatch-req-legacy",
                              "created_at": "2026-08-16T00:00:00Z"}
        registry.generation = 1
        status = read_dispatch_request_status(registry, "p1", "req-legacy")
        self.assertEqual("accepted", status["status"])
        self.assertIsNone(status["failure_reason"])

    def test_read_status_of_malformed_record_fails_closed(self):
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1"}  # missing required fields
        registry.generation = 1
        with self.assertRaises(TaskError):
            read_dispatch_request_status(registry, "p1", "req-malformed")

    def test_invalid_status_value_rejected(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        with self.assertRaises(TaskError):
            mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "bogus-status")


def _fake_session(status_code=200, items=None, raise_exc=None):
    session = MagicMock()
    if raise_exc is not None:
        session.get.side_effect = raise_exc
    else:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = {"items": items or []}
        session.get.return_value = response
    return session


class ListRecentDispatchRequestIdsTests(unittest.TestCase):
    """Bounded discovery of candidate request_ids for the Dashboard's
    pre-Task ingress-truth view -- must never become an unbounded/
    full-history scan (see project memory: prior incidents from unbounded
    list_executions()/full Drive history scans caused multi-minute
    Dashboard load latency)."""

    def test_returns_request_ids_stripped_of_prefix_and_suffix(self):
        session = _fake_session(items=[
            {"name": "dispatch-requests/p1/req-a.json"},
            {"name": "dispatch-requests/p1/req-b.json"},
        ])
        ids = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual(["req-a", "req-b"], ids)

    def test_bounded_by_max_results_query_param(self):
        session = _fake_session(items=[])
        list_recent_dispatch_request_ids("bucket-1", "p1", session=session, max_results=7)
        _, kwargs = session.get.call_args
        self.assertEqual(7, kwargs["params"]["maxResults"])

    def test_default_max_results_is_a_small_fixed_cap(self):
        # The default itself must be a small fixed number, never "unbounded"
        # (e.g. None) -- this is the actual guardrail against an
        # ever-growing dispatch-requests/ prefix turning one Dashboard
        # refresh into a full-history scan.
        self.assertIsInstance(DEFAULT_RECENT_REQUEST_LIST_LIMIT, int)
        self.assertLessEqual(DEFAULT_RECENT_REQUEST_LIST_LIMIT, 100)
        session = _fake_session(items=[])
        list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        _, kwargs = session.get.call_args
        self.assertEqual(DEFAULT_RECENT_REQUEST_LIST_LIMIT, kwargs["params"]["maxResults"])

    def test_result_is_never_longer_than_max_results_even_if_backend_over_returns(self):
        # A defensive local slice -- even if a (misbehaving or future)
        # backend response somehow returns more items than maxResults asked
        # for, this function must never hand back more than it promised.
        items = [{"name": f"dispatch-requests/p1/req-{i}.json"} for i in range(50)]
        session = _fake_session(items=items)
        ids = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, max_results=5)
        self.assertLessEqual(len(ids), 5)

    def test_missing_bucket_or_project_id_returns_empty_without_a_call(self):
        session = _fake_session(items=[{"name": "dispatch-requests/p1/req-a.json"}])
        self.assertEqual([], list_recent_dispatch_request_ids(None, "p1", session=session))
        self.assertEqual([], list_recent_dispatch_request_ids("bucket-1", None, session=session))
        session.get.assert_not_called()

    def test_non_200_response_fails_soft_to_empty_list(self):
        session = _fake_session(status_code=500, items=[{"name": "dispatch-requests/p1/req-a.json"}])
        self.assertEqual([], list_recent_dispatch_request_ids("bucket-1", "p1", session=session))

    def test_transport_exception_fails_soft_to_empty_list(self):
        session = _fake_session(raise_exc=Exception("simulated network failure"))
        self.assertEqual([], list_recent_dispatch_request_ids("bucket-1", "p1", session=session))

    def test_names_outside_the_project_prefix_or_wrong_suffix_are_ignored(self):
        session = _fake_session(items=[
            {"name": "dispatch-requests/other-project/req-x.json"},
            {"name": "dispatch-requests/p1/req-a.json"},
            {"name": "dispatch-requests/p1/not-json.txt"},
        ])
        ids = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual(["req-a"], ids)


class ResolveDispatchStatusReadFailureTests(unittest.TestCase):
    """P0 fix: resolve_dispatch_status_for_request() must distinguish a
    genuine backend/read failure of the claim record from "no claim record
    exists at all" -- see the function's own docstring. Before this fix,
    both cases silently collapsed to `dispatch_request_status: None`,
    making a real read failure indistinguishable from "this request was
    never received" to any caller (e.g. the Dashboard), which could then
    either show nothing at all, or (worse, if also passing
    has_dispatch_request=True) report a false ACCEPTED. This must never
    change what a SUCCESSFUL read reports for ACCEPTED/DISPATCHED/FAILED --
    only a failed read's shape changes (additively)."""

    def _project_store(self):
        store = DriveRecords(FakeDriveService())
        create_project(store, {"project_id": "p1", "name": "P1", "repo": "r", "default_branch": "main",
                               "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
                               "current_phase": "Phase 1", "important_constraints": []})
        return store

    def test_genuinely_never_received_request_reports_no_read_failure(self):
        store = self._project_store()
        registry = MemoryClaimRegistry()
        resolved = resolve_dispatch_status_for_request(store, registry, "p1", "req-never-seen")
        self.assertIsNone(resolved["task"])
        self.assertIsNone(resolved["dispatch_request_status"])
        self.assertFalse(resolved["dispatch_request_read_failed"])

    def test_genuine_read_failure_is_flagged_distinctly_from_no_record(self):
        store = self._project_store()
        registry = MemoryClaimRegistry()
        claim_dispatch_request(registry, "p1", "req-x", "dispatch-req-x", "dispatch-req-x", "2026-08-24T00:00:00Z")
        # The claim record genuinely exists (a request WAS received) but its
        # read is now failing -- e.g. a transient GCS/network outage.
        registry.read_unavailable = True
        resolved = resolve_dispatch_status_for_request(store, registry, "p1", "req-x")
        self.assertIsNone(resolved["task"])
        self.assertIsNone(resolved["dispatch_request_status"])
        self.assertTrue(resolved["dispatch_request_read_failed"])

    def test_successful_read_of_an_accepted_request_is_unaffected_by_the_fix(self):
        store = self._project_store()
        registry = MemoryClaimRegistry()
        claim_dispatch_request(registry, "p1", "req-y", "dispatch-req-y", "dispatch-req-y", "2026-08-24T00:00:00Z")
        resolved = resolve_dispatch_status_for_request(store, registry, "p1", "req-y")
        self.assertIsNone(resolved["task"])
        self.assertEqual("accepted", resolved["dispatch_request_status"]["status"])
        self.assertFalse(resolved["dispatch_request_read_failed"])

    def test_task_exists_branch_also_carries_the_new_flag_as_false(self):
        store = self._project_store()
        from manager.tasks import create_task
        create_task(store, {
            "task_id": "dispatch-req-z", "project_id": "p1", "title": "Ingress task",
            "task_type": "general", "expected_minutes": 20, "scope": [], "constraints": [],
            "acceptance_criteria": [], "source_context": {},
        }, assign=False)
        registry = MemoryClaimRegistry()
        resolved = resolve_dispatch_status_for_request(store, registry, "p1", "req-z")
        self.assertIsNotNone(resolved["task"])
        self.assertFalse(resolved["dispatch_request_read_failed"])


if __name__ == "__main__":
    unittest.main()
