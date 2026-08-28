"""Focused tests for the direct-dispatch-ingress request_id idempotency
primitive (manager/dispatch_requests.py), mirroring manager/test_task_claims.py's
proven approach against an in-memory double of the GCS transport."""

import threading
import unittest
from unittest.mock import MagicMock

from manager.dispatch_requests import (
    DEFAULT_RECENT_REQUEST_LIST_LIMIT, DispatchRequestClaimConflict, annotate_partial_identity_from_filename,
    claim_dispatch_request, dispatch_request_object_name, list_recent_dispatch_request_ids,
    list_recent_dispatch_rejected_request_ids, mark_dispatch_request_status, read_dispatch_request_status,
    read_dispatch_rejection_status_by_request, read_dispatch_rejection_status_by_request_id_only,
    record_dispatch_rejection_by_request, record_dispatch_rejection_by_request_id_only,
    release_dispatch_request_claim, resolve_dispatch_status_for_request,
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

    # --- Blocker 2 (SLA_START_POINT durability) required tests ---

    def test_ingress_first_observed_at_set_once_and_durable_across_retries(self):
        registry = MemoryClaimRegistry()
        first = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        self.assertEqual("2026-08-24T00:00:00Z", first["ingress_first_observed_at"])
        retry = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:05:00Z")
        # The retry's own (later) call-time value must never overwrite the
        # winner's durable first-observation evidence.
        self.assertEqual("2026-08-24T00:00:00Z", retry["ingress_first_observed_at"])
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("2026-08-24T00:00:00Z", status["ingress_first_observed_at"])

    def test_request_created_at_persisted_when_provided_and_none_when_omitted(self):
        registry = MemoryClaimRegistry()
        claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z",
                               request_created_at="2026-08-23T23:57:00Z")
        status = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("2026-08-23T23:57:00Z", status["request_created_at"])

        registry2 = MemoryClaimRegistry()
        claim_dispatch_request(registry2, "p1", "req-2", "dispatch-req-2", "dispatch-req-2", "2026-08-24T00:00:00Z")
        status2 = read_dispatch_request_status(registry2, "p1", "req-2")
        self.assertIsNone(status2["request_created_at"])

    def test_status_updated_at_advances_on_transition_but_created_at_never_moves(self):
        registry = MemoryClaimRegistry()
        claim = claim_dispatch_request(registry, "p1", "req-1", "dispatch-req-1", "dispatch-req-1", "2026-08-24T00:00:00Z")
        initial = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("2026-08-24T00:00:00Z", initial["status_updated_at"])
        mark_dispatch_request_status(registry, "p1", "req-1", claim["generation"], "dispatched", updated_at="2026-08-24T00:00:45Z")
        after = read_dispatch_request_status(registry, "p1", "req-1")
        self.assertEqual("2026-08-24T00:00:45Z", after["status_updated_at"])
        # created_at/ingress_first_observed_at must remain untouched by a
        # status transition.
        self.assertEqual("2026-08-24T00:00:00Z", after["created_at"])
        self.assertEqual("2026-08-24T00:00:00Z", after["ingress_first_observed_at"])

    def test_legacy_record_without_sla_fields_defaults_first_observed_at_to_created_at(self):
        """A record written before this change has no ingress_first_
        observed_at/request_created_at/status_updated_at fields at all --
        created_at is the mechanically correct default for
        ingress_first_observed_at (every existing record's created_at
        already IS that same moment), while request_created_at stays
        honestly None (never guessed)."""
        registry = MemoryClaimRegistry()
        registry.document = {"schema_version": "0.1.0", "project_id": "p1", "request_id": "req-legacy",
                              "task_id": "dispatch-req-legacy", "command_id": "dispatch-req-legacy",
                              "created_at": "2026-08-16T00:00:00Z"}
        registry.generation = 1
        status = read_dispatch_request_status(registry, "p1", "req-legacy")
        self.assertEqual("2026-08-16T00:00:00Z", status["ingress_first_observed_at"])
        self.assertEqual("2026-08-16T00:00:00Z", status["status_updated_at"])
        self.assertIsNone(status["request_created_at"])


def _fake_session(status_code=200, items=None, raise_exc=None, next_page_token=None):
    session = MagicMock()
    if raise_exc is not None:
        session.get.side_effect = raise_exc
    else:
        response = MagicMock()
        response.status_code = status_code
        payload = {"items": items or []}
        if next_page_token is not None:
            payload["nextPageToken"] = next_page_token
        response.json.return_value = payload
        session.get.return_value = response
    return session


def _item(request_id, updated, project_id="p1"):
    return {"name": f"dispatch-requests/{project_id}/{request_id}.json", "updated": updated}


class ListRecentDispatchRequestIdsTests(unittest.TestCase):
    """Bounded, recency-CORRECT discovery of candidate request_ids for the
    Dashboard's pre-Task ingress-truth view -- must never become an
    unbounded/full-history scan (see project memory: prior incidents from
    unbounded list_executions()/full Drive history scans caused
    multi-minute Dashboard load latency), and must never silently return
    the alphabetically-first page instead of the truly most recent
    request_ids (GCS Objects.list has no server-side recency ordering --
    see this module's own docstring for the incident this class guards
    against: a real most-recent pending request sorting past a naive
    lexicographic single-page cap and vanishing with no signal)."""

    def test_returns_request_ids_stripped_of_prefix_and_suffix(self):
        session = _fake_session(items=[
            _item("req-a", "2026-08-24T00:00:01Z"),
            _item("req-b", "2026-08-24T00:00:02Z"),
        ])
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual({"req-b", "req-a"}, set(result["request_ids"]))
        self.assertFalse(result["truncated"])

    def test_bounded_by_page_size_query_param(self):
        session = _fake_session(items=[])
        list_recent_dispatch_request_ids("bucket-1", "p1", session=session, page_size=7)
        _, kwargs = session.get.call_args
        self.assertEqual(7, kwargs["params"]["maxResults"])

    def test_default_max_results_is_a_small_fixed_cap(self):
        # The default itself must be a small fixed number, never "unbounded"
        # (e.g. None) -- this is the actual guardrail against an
        # ever-growing dispatch-requests/ prefix turning one Dashboard
        # refresh into a full-history scan.
        self.assertIsInstance(DEFAULT_RECENT_REQUEST_LIST_LIMIT, int)
        self.assertLessEqual(DEFAULT_RECENT_REQUEST_LIST_LIMIT, 100)
        items = [_item(f"req-{i}", f"2026-08-24T00:{i:02d}:00Z") for i in range(DEFAULT_RECENT_REQUEST_LIST_LIMIT + 5)]
        session = _fake_session(items=items)
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertLessEqual(len(result["request_ids"]), DEFAULT_RECENT_REQUEST_LIST_LIMIT)

    def test_result_is_never_longer_than_max_results_even_if_backend_over_returns(self):
        # A defensive local slice -- even if a (misbehaving or future)
        # backend response somehow returns more items than the caller asked
        # to keep, this function must never hand back more than it promised.
        items = [_item(f"req-{i}", f"2026-08-24T00:{i:02d}:00Z") for i in range(50)]
        session = _fake_session(items=items)
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, max_results=5)
        self.assertLessEqual(len(result["request_ids"]), 5)

    def test_missing_bucket_or_project_id_returns_empty_without_a_call(self):
        session = _fake_session(items=[_item("req-a", "2026-08-24T00:00:00Z")])
        self.assertEqual({"request_ids": [], "truncated": False}, list_recent_dispatch_request_ids(None, "p1", session=session))
        self.assertEqual({"request_ids": [], "truncated": False}, list_recent_dispatch_request_ids("bucket-1", None, session=session))
        session.get.assert_not_called()

    def test_non_200_response_fails_soft_and_reports_truncated(self):
        # Blocker 1, requirement 3: a pagination/backend failure must be
        # reported as truncated (unproven), never as a confirmed empty list.
        session = _fake_session(status_code=500, items=[_item("req-a", "2026-08-24T00:00:00Z")])
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual([], result["request_ids"])
        self.assertTrue(result["truncated"])

    def test_transport_exception_fails_soft_and_reports_truncated(self):
        session = _fake_session(raise_exc=Exception("simulated network failure"))
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual([], result["request_ids"])
        self.assertTrue(result["truncated"])

    def test_names_outside_the_project_prefix_or_wrong_suffix_are_ignored(self):
        session = _fake_session(items=[
            _item("req-x", "2026-08-24T00:00:00Z", project_id="other-project"),
            _item("req-a", "2026-08-24T00:00:00Z"),
            {"name": "dispatch-requests/p1/not-json.txt", "updated": "2026-08-24T00:00:00Z"},
        ])
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual(["req-a"], result["request_ids"])
        self.assertFalse(result["truncated"])

    # --- Blocker 1 required tests (dispatch-two-tick-final-20260824 R6) ---

    def test_true_most_recent_request_found_despite_reversed_lexical_order(self):
        # 30 requests where filename lexical order is the OPPOSITE of write
        # recency: req-000 (lexically first) is oldest, req-029 (lexically
        # last) is newest -- but the single response page returns them in
        # GCS's real lexicographic order (req-000, req-001, ...), so a naive
        # "take the first max_results" implementation would keep req-000..
        # req-019 and completely miss the true most-recent, still-pending
        # req-029 (lexical position 29, i.e. past a cap of 20).
        items = [_item(f"req-{i:03d}", f"2026-08-24T00:{i:02d}:00Z") for i in range(30)]
        session = _fake_session(items=items)
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, max_results=20)
        self.assertIn("req-029", result["request_ids"], "the true most-recent request must not be dropped")
        self.assertNotIn("req-000", result["request_ids"], "the oldest request must be pushed out by recency, not kept by lexical luck")
        self.assertFalse(result["truncated"])

    def test_exceeding_cap_with_unprovable_completeness_never_reports_confirmed_none(self):
        # More objects exist than one page (page_size) budget can read, and
        # GCS reports more pages remain (nextPageToken) -- completeness is
        # not provable, so truncated=True must be set even if some
        # candidates were still found this call.
        page1 = [_item(f"req-{i:03d}", f"2026-08-24T00:{i:02d}:00Z") for i in range(5)]
        session = _fake_session(items=page1, next_page_token="more")
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, page_size=5, page_budget=1)
        self.assertTrue(result["truncated"])

    def test_pagination_failure_mid_scan_reports_truncated_not_empty_confirmed(self):
        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "items": [_item("req-a", "2026-08-24T00:00:00Z")], "nextPageToken": "page-2",
        }
        session = MagicMock()
        session.get.side_effect = [first_response, Exception("simulated mid-scan failure")]
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, page_size=1, page_budget=5)
        # The candidate found on the successful first page is still
        # returned (best-effort), but the scan is honestly unproven.
        self.assertEqual(["req-a"], result["request_ids"])
        self.assertTrue(result["truncated"])

    def test_item_missing_updated_metadata_is_excluded_and_marks_truncated(self):
        session = _fake_session(items=[
            {"name": "dispatch-requests/p1/req-no-metadata.json"},  # no "updated" field
            _item("req-a", "2026-08-24T00:00:00Z"),
        ])
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session)
        self.assertEqual(["req-a"], result["request_ids"])
        self.assertTrue(result["truncated"])

    def test_boundedness_never_scans_past_the_page_budget(self):
        # Simulate an effectively unbounded backlog (every page reports a
        # nextPageToken) and prove the scan still stops after exactly
        # page_budget calls -- never an unbounded/full-history walk.
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "items": [_item("req-x", "2026-08-24T00:00:00Z")], "nextPageToken": "keep-going",
        }
        session = MagicMock()
        session.get.return_value = response
        result = list_recent_dispatch_request_ids("bucket-1", "p1", session=session, page_size=10, page_budget=4)
        self.assertEqual(4, session.get.call_count)
        self.assertTrue(result["truncated"])


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


class DispatchRejectionByRequestTests(unittest.TestCase):
    """P0 regression: adm-worktree-materialization-repair-20260828-0445 was
    durably rejected (extra schema fields) but was invisible to any caller
    holding only its request_id, because the ONLY durable evidence lived
    under a Drive-file-id-keyed rejection record no request_id-based lookup
    could ever find -- resolve_dispatch_status_for_request(project_id,
    request_id) silently reported None, indistinguishable from "never
    received at all". These tests cover the (project_id, request_id)-keyed
    mirror that closes that gap."""

    def _project_store(self):
        store = DriveRecords(FakeDriveService())
        create_project(store, {"project_id": "p1", "name": "P1", "repo": "r", "default_branch": "main",
                               "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
                               "current_phase": "Phase 1", "important_constraints": []})
        return store

    def test_record_and_read_round_trip(self):
        registry = MemoryClaimRegistry()
        record_dispatch_rejection_by_request(
            registry, "p1", "req-rejected", "drive-file-123", "ingress_rejected",
            "invalid dispatch_request: Additional properties are not allowed ('task_type' was unexpected)",
            "2026-08-27T20:42:06Z")
        status = read_dispatch_rejection_status_by_request(registry, "p1", "req-rejected")
        self.assertEqual("rejected", status["status"])
        self.assertEqual("ingress_rejected", status["reason_code"])
        self.assertIn("Additional properties", status["message"])
        self.assertEqual("drive-file-123", status["file_id"])

    def test_read_returns_none_when_never_rejected(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(read_dispatch_rejection_status_by_request(registry, "p1", "req-never"))

    def test_resolve_dispatch_status_finds_rejection_by_request_id_alone(self):
        """The exact incident: no claim record was ever created (rejected
        before claim), but the caller still holds only the request_id --
        resolve_dispatch_status_for_request() must now surface REJECTED
        truth via the by-request mirror when `bucket`/a rejection registry
        factory is supplied, instead of silently returning None."""
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        rejection_registry = MemoryClaimRegistry()
        record_dispatch_rejection_by_request(
            rejection_registry, "p1", "adm-worktree-materialization-repair-20260828-0445", "drive-file-999",
            "ingress_rejected", "invalid dispatch_request: Additional properties are not allowed", "2026-08-27T20:42:06Z")
        resolved = resolve_dispatch_status_for_request(
            store, claim_registry, "p1", "adm-worktree-materialization-repair-20260828-0445",
            bucket="fake-bucket", rejection_registry_factory=lambda bucket, project_id, request_id: rejection_registry)
        self.assertIsNone(resolved["task"])
        self.assertFalse(resolved["dispatch_request_read_failed"])
        self.assertIsNotNone(resolved["dispatch_request_status"])
        self.assertEqual("rejected", resolved["dispatch_request_status"]["status"])
        self.assertIn("Additional properties", resolved["dispatch_request_status"]["message"])

    def test_resolve_dispatch_status_without_bucket_preserves_prior_behavior(self):
        """Omitting `bucket` (every pre-existing caller) must behave exactly
        as before this fix -- no rejection-by-request lookup is attempted at
        all, even if one exists."""
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        resolved = resolve_dispatch_status_for_request(store, claim_registry, "p1", "req-never-seen")
        self.assertIsNone(resolved["dispatch_request_status"])
        self.assertFalse(resolved["dispatch_request_read_failed"])

    def test_claimed_request_never_consults_rejection_mirror(self):
        """A request that WAS successfully claimed must resolve from the
        claim record alone -- the rejection-by-request mirror is never even
        consulted when claim status is already known."""
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        claim_dispatch_request(claim_registry, "p1", "req-ok", "dispatch-req-ok", "dispatch-req-ok", "2026-08-24T00:00:00Z")
        rejection_registry = MagicMock()
        resolved = resolve_dispatch_status_for_request(
            store, claim_registry, "p1", "req-ok", bucket="fake-bucket",
            rejection_registry_factory=lambda bucket, project_id, request_id: rejection_registry)
        self.assertEqual("accepted", resolved["dispatch_request_status"]["status"])
        rejection_registry.read_if_exists.assert_not_called()


class AnnotatePartialIdentityFromFilenameTests(unittest.TestCase):
    """P0 fix: a failure BEFORE any document body ever parses (unreadable
    bytes, not valid UTF-8, not valid JSON -- the real live incident: a
    UTF-8 BOM-prefixed ChatGPT upload) previously left annotate_partial_
    identity() with nothing to read request_id/project_id from at all, so
    the rejection was only ever discoverable via this Drive file's own id
    -- indistinguishable from "never received" to any caller (the normal
    case) holding only request_id. Drive's OWN filename==request_id.json
    contract (already enforced once a document DOES parse) is the only
    identity signal available this early."""

    def test_recovers_request_id_from_conforming_filename(self):
        exc = TaskError("Drive request is not valid UTF-8 JSON")
        annotate_partial_identity_from_filename(exc, {"name": "adm-p0-immediate-dispatch-wakeup-20260829.json"})
        self.assertEqual("adm-p0-immediate-dispatch-wakeup-20260829", exc.partial_request_id_unscoped)

    def test_non_json_filename_recovers_nothing(self):
        exc = TaskError("x")
        annotate_partial_identity_from_filename(exc, {"name": "not-a-json-file.txt"})
        self.assertIsNone(exc.partial_request_id_unscoped)

    def test_missing_or_malformed_metadata_recovers_nothing(self):
        for metadata in (None, {}, {"name": None}, {"name": ""}, {"name": ".json"}):
            with self.subTest(metadata=metadata):
                exc = TaskError("x")
                annotate_partial_identity_from_filename(exc, metadata)
                self.assertIsNone(exc.partial_request_id_unscoped)

    def test_returns_the_same_exception_for_call_site_chaining(self):
        exc = TaskError("x")
        self.assertIs(exc, annotate_partial_identity_from_filename(exc, {"name": "r.json"}))


class DispatchRejectionByRequestIdOnlyTests(unittest.TestCase):
    """The live incident this closes: 3 real ChatGPT-submitted Drive
    requests carried a UTF-8 BOM, decoded successfully (harmlessly, as a
    literal U+FEFF) but failed json.loads() before request_id/project_id
    were ever parseable -- so even though the BOM decode bug is now fixed
    separately, ANY future truly-malformed-JSON submission would still be
    invisible to a request_id-only query without this mirror."""

    def _project_store(self):
        store = DriveRecords(FakeDriveService())
        create_project(store, {"project_id": "p1", "name": "P1", "repo": "r", "default_branch": "main",
                               "runtime_ssot": "Drive", "project_rules": [], "active_tasks": [],
                               "current_phase": "Phase 1", "important_constraints": []})
        return store

    def test_record_and_read_round_trip(self):
        registry = MemoryClaimRegistry()
        record_dispatch_rejection_by_request_id_only(
            registry, "adm-p0-immediate-dispatch-wakeup-20260829", "drive-file-abc", "ingress_rejected",
            "Drive request is not valid UTF-8 JSON", "2026-08-28T20:43:50Z")
        status = read_dispatch_rejection_status_by_request_id_only(registry, "adm-p0-immediate-dispatch-wakeup-20260829")
        self.assertEqual("rejected", status["status"])
        self.assertEqual("ingress_rejected", status["reason_code"])
        self.assertIn("not valid UTF-8", status["message"])
        self.assertEqual("drive-file-abc", status["file_id"])

    def test_read_returns_none_when_never_rejected(self):
        registry = MemoryClaimRegistry()
        self.assertIsNone(read_dispatch_rejection_status_by_request_id_only(registry, "req-never"))

    def test_resolve_dispatch_status_finds_rejection_via_id_only_mirror_as_last_resort(self):
        """No claim record and no project-scoped rejection record exist at
        all (project_id was never recoverable) -- resolve_dispatch_status_
        for_request() must still surface REJECTED truth via this final
        fallback rather than silently returning None, matching the
        project-scoped mirror's own contract one level down."""
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        empty_by_request_registry = MemoryClaimRegistry()
        id_only_registry = MemoryClaimRegistry()
        record_dispatch_rejection_by_request_id_only(
            id_only_registry, "adm-p0-immediate-dispatch-wakeup-20260829", "drive-file-abc", "ingress_rejected",
            "Drive request is not valid UTF-8 JSON", "2026-08-28T20:43:50Z")
        resolved = resolve_dispatch_status_for_request(
            store, claim_registry, "p1", "adm-p0-immediate-dispatch-wakeup-20260829", bucket="fake-bucket",
            rejection_registry_factory=lambda bucket, project_id, request_id: empty_by_request_registry,
            rejection_id_only_registry_factory=lambda bucket, request_id: id_only_registry)
        self.assertIsNone(resolved["task"])
        self.assertFalse(resolved["dispatch_request_read_failed"])
        self.assertIsNotNone(resolved["dispatch_request_status"])
        self.assertEqual("rejected", resolved["dispatch_request_status"]["status"])

    def test_project_scoped_rejection_takes_priority_over_id_only_mirror(self):
        """When BOTH mirrors have a record (rare -- would require the same
        request_id to fail once pre-parse and once post-parse), the richer,
        project-scoped one must win; the id-only mirror is a last resort,
        never consulted otherwise."""
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        by_request_registry = MemoryClaimRegistry()
        id_only_registry = MagicMock()
        record_dispatch_rejection_by_request(
            by_request_registry, "p1", "req-both", "drive-file-1", "ingress_rejected", "schema-level rejection",
            "2026-08-28T00:00:00Z")
        resolved = resolve_dispatch_status_for_request(
            store, claim_registry, "p1", "req-both", bucket="fake-bucket",
            rejection_registry_factory=lambda bucket, project_id, request_id: by_request_registry,
            rejection_id_only_registry_factory=lambda bucket, request_id: id_only_registry)
        self.assertEqual("schema-level rejection", resolved["dispatch_request_status"]["message"])
        id_only_registry.read_if_exists.assert_not_called()

    def test_resolve_dispatch_status_without_bucket_never_consults_id_only_mirror(self):
        store = self._project_store()
        claim_registry = MemoryClaimRegistry()
        resolved = resolve_dispatch_status_for_request(store, claim_registry, "p1", "req-never-seen")
        self.assertIsNone(resolved["dispatch_request_status"])
        self.assertFalse(resolved["dispatch_request_read_failed"])


if __name__ == "__main__":
    unittest.main()
