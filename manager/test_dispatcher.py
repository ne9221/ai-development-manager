import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import mock

from manager.dispatcher import dispatch, request_ok
from manager.rules_manifest import mandatory_rules
from manager.sessions import parse_identity_header
from manager.tasks import TaskError, create_handoff, create_project, create_task


class MemoryStore:
    def __init__(self): self.records = {}
    def put(self, area, project, name, document): self.records[(area, project, name)] = deepcopy(document); return document
    def get(self, area, project, name):
        if (area, project, name) not in self.records: raise TaskError("not found")
        return deepcopy(self.records[(area, project, name)])
    def latest(self, area, project, task_id):
        items = [value for (a, p, _), value in self.records.items() if a == area and p == project and value.get("task_id") == task_id]
        if not items: raise TaskError("no handoff")
        return max(items, key=lambda item: item["created_at"])


def project(active=None):
    return {"project_id": "p1", "name": "Project One", "repo": "https://github.com/example/project", "default_branch": "main", "working_directory": "C:/work/project", "baseline_commit": "abc123", "runtime_ssot": "Drive", "project_rules": [], "active_tasks": active or [], "current_phase": "Phase 7", "important_constraints": ["Do not touch other repos"]}


def quota(codex=80, claude=None, updated=None):
    updated = updated or datetime.now(timezone.utc).isoformat()
    providers = []
    for name, remaining in (("codex", codex), ("claude", claude), ("antigravity", None), ("gemini_app", None)):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100-remaining, "resets_at": None}]
        providers.append({"provider": name, "display_name": name, "collection_mode": "automatic" if name in ("codex", "claude") else "manual", "source": "test", "source_type": "official" if name in ("codex", "claude") else "manual", "confidence": "official" if windows else "unknown", "last_updated": updated, "status": "ok" if windows else "unknown", "windows": windows})
    return {"schema_version": "0.1.0", "generated_at": updated, "providers": providers}


def request(**changes):
    value = {"project_id": "p1", "title": "Fix regression", "task_type": "implementation", "complexity": "medium", "expected_minutes": 20, "scope": ["Fix parser", "Add regression test"], "constraints": [], "acceptance_criteria": ["Tests pass"], "needs_repo_edit": True}
    value.update(changes); return value


def history(minutes=14):
    return [{"provider": "codex", "mode": "code", "effort": "medium", "status": "completed", "elapsed_minutes": minutes, "task_snapshot": {"task_type": "implementation", "complexity": "medium", "needs_repo_edit": True}, "quota_delta": {"status": "known", "windows": [{"name": "primary", "status": "known", "used_percent_delta": 2}]}}]


def two_claude_accounts(a_confidence="official", a_remaining=90, a_updated=None, b_confidence="unknown", b_remaining=None, b_updated=None):
    a_updated = a_updated or datetime.now(timezone.utc).isoformat()
    b_updated = b_updated or a_updated
    def entry(account_id, confidence, remaining, updated):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100 - remaining, "resets_at": None}]
        return {"provider": "claude", "account_id": account_id, "display_name": "claude", "collection_mode": "automatic", "source": "test", "source_type": "official" if confidence == "official" else "manual", "confidence": confidence, "last_updated": updated, "status": "ok" if windows else "unknown", "windows": windows}
    providers = [
        entry("claude-a", a_confidence, a_remaining, a_updated),
        entry("claude-b", b_confidence, b_remaining, b_updated),
        {"provider": "codex", "display_name": "codex", "collection_mode": "automatic", "source": "test", "source_type": "official", "confidence": "official", "last_updated": a_updated, "status": "ok", "windows": [{"name": "primary", "remaining_percent": 80, "used_percent": 20, "resets_at": None}]},
    ]
    return {"schema_version": "0.1.0", "generated_at": a_updated, "providers": providers}


def duplicate_claude_account(first_remaining, second_remaining, updated=None):
    """Two source records sharing the same account_id="claude-a" -- the
    upstream data-quality bug this test class guards against."""
    updated = updated or datetime.now(timezone.utc).isoformat()

    def entry(remaining):
        windows = [{"name": "primary", "remaining_percent": remaining, "used_percent": 100 - remaining, "resets_at": None}]
        return {"provider": "claude", "account_id": "claude-a", "display_name": "claude", "collection_mode": "automatic", "source": "test", "source_type": "official", "confidence": "official", "last_updated": updated, "status": "ok", "windows": windows}
    providers = [
        entry(first_remaining),
        entry(second_remaining),
        {"provider": "codex", "display_name": "codex", "collection_mode": "automatic", "source": "test", "source_type": "official", "confidence": "official", "last_updated": updated, "status": "ok", "windows": [{"name": "primary", "remaining_percent": 80, "used_percent": 20, "resets_at": None}]},
    ]
    return {"schema_version": "0.1.0", "generated_at": updated, "providers": providers}


class DispatcherTests(unittest.TestCase):
    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def dispatch_case(self, req=None, q=None, records=None): return dispatch(self.store, object(), req or request(), q or quota(), [] if records is None else records)

    def test_new_task_codex_and_drive_consistency(self):
        result = self.dispatch_case(request(task_id="new-explicit-id"), records=history())
        self.assertEqual("codex", result["recommended_provider"]); self.assertEqual(14, result["estimated_minutes"])
        self.assertEqual("codex", result["provider"]); self.assertIsNone(result["model"])
        self.assertIn("selection_reason", result); self.assertIn("quota_evidence", result)
        self.assertEqual("Fix regression", self.store.get("tasks", "p1", "new-explicit-id")["title"])
        self.assertNotIn('"providers"', result["generated_prompt"])

    def test_future_model_contract_is_optional_and_validated(self):
        result = self.dispatch_case(request(title="Model contract", model="gpt-test", fallback_model="gpt-fallback"))
        self.assertEqual("gpt-test", result["model"]); self.assertEqual("gpt-fallback", result["fallback_model"])
        with self.assertRaises(TaskError): request_ok(request(model=""))

    def test_identity_header_round_trips_canonical_task_for_codex_and_claude(self):
        for provider, task_id in (("codex", "canonical-task-id"), ("claude", "canonical_task_id")):
            with self.subTest(provider=provider):
                result = self.dispatch_case(request(task_id=task_id, title="Conversation label differs", preferred_provider=provider, needs_repo_edit=False), quota(80, 80))
                prompt = result["generated_prompt"]
                expected = {"ai": provider.title(), "project": "p1", "task": task_id, "conversation": "not supplied"}
                self.assertEqual(expected, parse_identity_header(prompt))
                self.assertTrue(prompt.startswith(f"AI: {provider.title()}\nProject: p1\nTask: {task_id}\nConversation:"))
                self.assertIn("Project name: Project One", prompt)
                self.assertIn("Task goal: Conversation label differs", prompt)
                self.assertIn("Conversation:", prompt)
                self.assertIn("Session:", prompt)

    def test_existing_continuation_with_and_without_handoff(self):
        task = create_task(self.store, request(task_id="t1", current_progress="Parser fixed", next_action="Run tests"), assign=False)
        self.store.records[("projects", "p1", "p1")]["active_tasks"] = ["t1"]
        no_handoff = self.dispatch_case(request()); self.assertIn("Parser fixed", no_handoff["generated_prompt"]); self.assertIn("Latest handoff: none", no_handoff["generated_prompt"])
        create_handoff(self.store, {"handoff_id": "h1", "task_id": "t1", "project_id": "p1", "from_provider": "codex", "to_provider": "claude", "from_session": "a", "reason": "switch", "completed_work": [], "current_state": "Regression reproduced", "files_changed": [], "commits": [], "tests": [], "known_issues": [], "do_not_touch": ["billing.py"], "next_action": "Patch parser", "acceptance_criteria": [], "minimal_context": "Only parser.py is in scope."})
        continued = self.dispatch_case(request()); self.assertIn("Only parser.py is in scope", continued["generated_prompt"]); self.assertIn("billing.py", continued["generated_prompt"])

    def test_claude_preferred_excluded_and_unknown(self):
        claude = self.dispatch_case(request(task_type="architecture", needs_repo_edit=False, needs_research=True), quota(50, 80))
        self.assertEqual("claude", claude["recommended_provider"])
        preferred = self.dispatch_case(request(title="Manual review", preferred_provider="antigravity"))
        self.assertEqual("antigravity", preferred["recommended_provider"]); self.assertTrue(any("unknown" in item for item in preferred["warnings"]))
        excluded = self.dispatch_case(request(title="No Codex", excluded_provider="codex"), quota(90, 80))
        self.assertNotEqual("codex", excluded["recommended_provider"])

    def test_split_and_no_split(self):
        split = self.dispatch_case(request(title="Large task", expected_minutes=45))
        self.assertTrue(split["split_recommended"]); self.assertEqual(3, split["phase_count"]); self.assertIn("Execute Phase 1 only", split["generated_prompt"])
        small = self.dispatch_case(request(title="Small task", expected_minutes=20), records=history(14))
        self.assertFalse(small["split_recommended"]); self.assertEqual(1, small["phase_count"])

    def test_all_providers_unavailable_still_admits_the_task_as_waiting_quota(self):
        """DASHBOARD_TRUTH_CONNECTED gate 1: quota state must never block Task
        *admission* -- only provider *selection*. Before this fix, dispatch()
        raised TaskError("no eligible provider") before ever calling
        create_task(), so a request arriving while every provider's quota was
        stale/exhausted was silently lost -- no Task, no visible record, no
        way for a later scheduler tick to retry it once quota recovered.
        Reproduces the real shape with a fully stale quota document (every
        provider's last_updated far in the past)."""
        result = self.dispatch_case(
            request(title="Secret task", constraints=["token=sensitive-value", "credential:private"]),
            quota(updated="2020-01-01T00:00:00Z"),
        )
        self.assertIsNone(result["provider"])
        self.assertIsNone(result["recommended_provider"])
        self.assertIsNone(result["generated_prompt"])
        self.assertTrue(result["waiting_quota"])
        self.assertTrue(any("waiting on quota recovery" in warning for warning in result["warnings"]))
        # The Task itself is durably admitted -- not lost -- with no provider
        # assigned yet, so a later automatic retry can find and promote it.
        task = self.store.get("tasks", "p1", result["task_id"])
        self.assertIsNone(task["recommended_provider"])
        # Secret redaction: nothing in the returned result (no prompt was
        # even generated) leaks the raw constraint secrets.
        blob = json.dumps(result)
        for forbidden in ("sensitive-value", "credential:private"):
            self.assertNotIn(forbidden, blob)

    def test_all_providers_unavailable_reuses_an_existing_task_without_overwriting_it(self):
        """A second dispatch() call for a task that already exists (e.g. a
        scheduler re-tick) must not clobber a previously-assigned
        recommended_provider just because quota is momentarily stale on this
        call -- it still reports waiting_quota, but the durable Task record
        is left untouched."""
        create_task(self.store, {
            "task_id": "already-assigned", "project_id": "p1", "title": "Fix regression",
            "task_type": "implementation", "complexity": "medium", "expected_minutes": 20,
            "recommended_provider": "codex", "mode": "code", "effort": "medium",
        }, service=object(), assign=False)
        result = self.dispatch_case(request(task_id="already-assigned"), quota(updated="2020-01-01T00:00:00Z"))
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])
        task = self.store.get("tasks", "p1", "already-assigned")
        self.assertEqual("codex", task["recommended_provider"])

    def test_automatic_routing_selects_only_fresh_reliable_provider(self):
        document = quota(80, 90)
        next(item for item in document["providers"] if item["provider"] == "claude")["last_updated"] = "2020-01-01T00:00:00Z"
        result = self.dispatch_case(request(task_type="architecture", needs_repo_edit=False, needs_research=True), document)
        self.assertEqual("codex", result["provider"])

    def test_explicit_unreliable_provider_is_not_substituted(self):
        """DASHBOARD_TRUTH_CONNECTED gate 1: an explicit preferred_provider
        with no reliable quota must never be silently substituted with a
        different provider, AND must never lose the Task -- it is admitted
        as waiting_quota instead of raising, exactly like the automatic-
        routing "no eligible provider" case."""
        document = quota(80, 90)
        next(item for item in document["providers"] if item["provider"] == "claude")["last_updated"] = "2020-01-01T00:00:00Z"
        result = self.dispatch_case(request(preferred_provider="claude", needs_repo_edit=False), document)
        self.assertIsNone(result["provider"])
        self.assertNotEqual("codex", result["provider"])
        self.assertTrue(result["waiting_quota"])

    def test_account_id_selects_matching_claude_account_quota(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result_a = self.dispatch_case(request(title="Account A", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc)
        self.assertIn("claude-a", result_a["quota_summary"]); self.assertIn("90% remaining", result_a["quota_summary"])
        result_b = self.dispatch_case(request(title="Account B", preferred_provider="claude", needs_repo_edit=False, account_id="claude-b"), doc)
        self.assertIn("claude-b", result_b["quota_summary"]); self.assertIn("40% remaining", result_b["quota_summary"])
        self.assertNotIn("90% remaining", result_b["quota_summary"])

    def test_account_id_selection_is_order_independent(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        doc_swapped = dict(doc); doc_swapped["providers"] = list(reversed(doc["providers"]))
        result = self.dispatch_case(request(title="Account B swapped", preferred_provider="claude", needs_repo_edit=False, account_id="claude-b"), doc_swapped)
        self.assertIn("claude-b", result["quota_summary"]); self.assertIn("40% remaining", result["quota_summary"])

    def test_account_id_reliability_not_laundered_across_accounts(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="unknown", b_remaining=None)
        result = self.dispatch_case(request(title="Unreliable account B", preferred_provider="claude", needs_repo_edit=False, account_id="claude-b"), doc)
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])

    def test_account_id_does_not_silently_switch_stale_to_fresh(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=70, a_updated="2020-01-01T00:00:00Z", b_confidence="official", b_remaining=40, b_updated="2026-08-09T05:00:00Z")
        result = self.dispatch_case(request(title="Stale account A", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc)
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])

    def test_stale_account_recovers_automatically_once_a_fresh_reading_lands(self):
        """DASHBOARD_TRUTH_CONNECTED gate 5 (stale -> recovery), controlled/
        isolated fixture: the SAME account_id, first with a collector
        reading frozen 3 hours old (refused), then with a fresh reading
        (accepted) -- proving routing self-recovers automatically from a
        genuine stale condition the instant fresh data is available again,
        with no other code path or manual override involved. account-b
        stays fresh and eligible throughout, proving the stale condition on
        account-a never contaminates account-b's own independent
        eligibility (the same non-cross-wiring property gate 2 requires)."""
        stale_3h_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        fresh_now = datetime.now(timezone.utc).isoformat()
        doc_stale = two_claude_accounts(a_confidence="official", a_remaining=70, a_updated=stale_3h_ago,
                                        b_confidence="official", b_remaining=40, b_updated=fresh_now)
        stale_result = self.dispatch_case(request(title="Stale then recovered", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc_stale)
        self.assertIsNone(stale_result["provider"])
        self.assertTrue(stale_result["waiting_quota"])
        # account-b, fresh throughout, was never affected by account-a's staleness.
        b_result = self.dispatch_case(request(title="B unaffected by A's staleness", preferred_provider="claude", needs_repo_edit=False, account_id="claude-b"), doc_stale)
        self.assertEqual("claude-b", b_result["account_id"])

        # The collector produces a fresh reading for account-a again --
        # nothing else changes about the request.
        recovered_now = datetime.now(timezone.utc).isoformat()
        doc_recovered = two_claude_accounts(a_confidence="official", a_remaining=65, a_updated=recovered_now,
                                            b_confidence="official", b_remaining=40, b_updated=fresh_now)
        recovered = self.dispatch_case(request(title="Stale then recovered", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc_recovered)
        self.assertEqual("claude-a", recovered["account_id"])
        self.assertIn("65% remaining", recovered["quota_summary"])

    def test_automatic_claude_uses_provider_eligibility_without_pinning_an_account(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=70, a_updated="2020-01-01T00:00:00Z", b_confidence="official", b_remaining=40, b_updated=datetime.now(timezone.utc).isoformat())
        next(item for item in doc["providers"] if item["provider"] == "codex")["last_updated"] = "2020-01-01T00:00:00Z"
        result = self.dispatch_case(request(title="Fresh Claude sibling", preferred_provider="claude", needs_repo_edit=False, task_type="architecture"), doc)
        self.assertEqual("claude", result["provider"])
        self.assertEqual("claude-b", result["account_id"])
        self.assertEqual(["claude-b"], result["provider_availability"]["eligible_account_ids"])

    def test_account_id_unknown_defers_to_auth_preflight(self):
        """An explicit account_id with no captured per-account quota data must
        not fail closed at dispatch() -- dispatch() has no way to know whether
        that account is actually launchable; only ClaudeLauncher's real auth
        preflight can decide that. The explicit account_id is preserved
        verbatim (never substituted/dropped), and its quota evidence is a
        distinct unknown/unavailable entry -- never another account's or the
        legacy representative's real numbers laundered onto it."""
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Missing account", preferred_provider="claude", needs_repo_edit=False, account_id="claude-does-not-exist"), doc)
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])

    def test_account_id_matched_quota_evidence_reflects_that_account_not_legacy_representative(self):
        """Same evidence-integrity property, for the already-matched-account
        case: quota_evidence must reflect the specific requested account_id's
        own data, never the provider-level legacy representative's (which can
        be a different real account's numbers)."""
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result_b = self.dispatch_case(request(title="Account B evidence", preferred_provider="claude", needs_repo_edit=False, account_id="claude-b"), doc)
        claude_evidence = result_b["quota_evidence"]["claude"]
        self.assertEqual(40, claude_evidence["windows"][0]["remaining_percent"])

    def test_account_id_lookup_deterministic_when_source_has_duplicate_records(self):
        """Regression for the duplicate-key bug: if two source records
        share account_id="claude-a", quota["accounts"] must already be
        deduplicated to one entry by the time dispatch() does its next()
        lookup, so the result is the deliberate last-wins record -- not
        whichever duplicate happened to come first."""
        doc = duplicate_claude_account(first_remaining=12, second_remaining=47)
        result = self.dispatch_case(request(title="Duplicate account record", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc)
        self.assertIn("47% remaining", result["quota_summary"])
        self.assertNotIn("12% remaining", result["quota_summary"])
        doc_swapped = duplicate_claude_account(first_remaining=47, second_remaining=12)
        result_swapped = self.dispatch_case(request(title="Duplicate account record swapped", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc_swapped)
        self.assertIn("12% remaining", result_swapped["quota_summary"])
        self.assertNotIn("47% remaining", result_swapped["quota_summary"])

    def test_no_account_id_keeps_legacy_provider_level_behavior(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Legacy path", preferred_provider="claude", needs_repo_edit=False), doc)
        self.assertIn("claude-a", result["quota_summary"]); self.assertNotIn("claude-b", result["quota_summary"])
        self.assertIn("90% remaining", result["quota_summary"])

    def test_codex_dispatch_unaffected_by_account_id(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Codex still works", preferred_provider="codex", account_id="claude-a"), doc)
        self.assertEqual("codex", result["recommended_provider"]); self.assertIn("80% remaining", result["quota_summary"])

    def test_all_claude_accounts_unknown_no_fabricated_selection(self):
        doc = two_claude_accounts(a_confidence="unknown", a_remaining=None, b_confidence="unknown", b_remaining=None)
        result = self.dispatch_case(request(title="All unknown", preferred_provider="claude", needs_repo_edit=False), doc)
        self.assertIsNone(result["provider"])
        self.assertTrue(result["waiting_quota"])

    def test_account_identity_shown_without_credentials(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Attribution only", preferred_provider="claude", needs_repo_edit=False, account_id="claude-a"), doc)
        for blob in (result["quota_summary"], result["generated_prompt"]):
            self.assertIn("claude-a", blob)
            for forbidden in ("config_dir", "token", "credential", "CLAUDE_CONFIG_DIR"):
                self.assertNotIn(forbidden, blob)

    def test_malformed_input_rejected(self):
        with self.assertRaises(TaskError): request_ok({"project_id": "p1"})
        with self.assertRaises(TaskError): request_ok(request(preferred_provider="codex", excluded_provider="codex"))

    # -- P0: Direct Dispatch source_context.goal must reach the generated prompt --

    def test_source_context_goal_reaches_generated_prompt(self):
        """A Direct Dispatch task's real instruction lives in
        source_context.goal (see cloud/dispatch_ingress.py's internal_request)
        -- prompt_for() must surface that actual goal text, not silently drop
        it in favor of the short title. Reproduces the real failure: a
        Golden E2E dispatched with goal="...reply with
        GOLDEN_E2E_VERIFIED_SUCCESS and exit immediately." only ever showed
        the title "Golden E2E Verification" to the provider."""
        task = create_task(self.store, request(
            task_id="t1", title="Golden E2E Verification",
            source_context={"origin": "direct_dispatch_ingress", "goal": "reply with GOLDEN_E2E_VERIFIED_SUCCESS and exit immediately."},
        ), assign=False)
        self.store.records[("projects", "p1", "p1")]["active_tasks"] = ["t1"]
        result = self.dispatch_case(request(title="Golden E2E Verification"))
        self.assertIn("reply with GOLDEN_E2E_VERIFIED_SUCCESS and exit immediately.", result["generated_prompt"])

    def test_title_remains_present_alongside_a_real_goal(self):
        """Title must not be replaced/dropped when a real goal is also shown."""
        create_task(self.store, request(
            task_id="t1", title="Golden E2E Verification",
            source_context={"origin": "direct_dispatch_ingress", "goal": "reply with GOLDEN_E2E_VERIFIED_SUCCESS and exit immediately."},
        ), assign=False)
        self.store.records[("projects", "p1", "p1")]["active_tasks"] = ["t1"]
        result = self.dispatch_case(request(title="Golden E2E Verification"))
        self.assertIn("Golden E2E Verification", result["generated_prompt"])

    def test_legacy_task_with_no_goal_falls_back_to_title_without_crashing(self):
        """A task with no source_context.goal at all (every pre-Direct-Dispatch
        task, and any task created without one) must keep working exactly as
        before -- no crash, no blank instruction line."""
        result = self.dispatch_case(request(task_id="legacy-task", title="Fix the parser"))
        self.assertIn("Fix the parser", result["generated_prompt"])

    # -- P0: working_directory contract (dispatch-time resolved Task snapshot) --

    def test_new_task_persists_working_directory_snapshot_from_project(self):
        """A brand-new Task (the Direct Dispatch shape: no task_id supplied,
        no working_directory field in the request either -- Direct Dispatch's
        own ALLOWED_FIELDS doesn't even accept one) must have its
        working_directory resolved from the already-loaded, trusted Project
        record and persisted onto the Task at creation time, so
        execution_runner.launch_task() never hits a bare KeyError later."""
        result = self.dispatch_case(request(task_id="wd-snapshot-task"))
        task = self.store.get("tasks", "p1", "wd-snapshot-task")
        self.assertIn("working_directory", task)
        self.assertEqual(project()["working_directory"], task["working_directory"])

    def test_new_task_working_directory_never_taken_from_request_payload(self):
        """Security boundary: even if a caller's request dict somehow carried
        a working_directory-shaped key, dispatch() must never read it -- the
        Project record (server-loaded, not client-supplied) is the only
        source. Direct Dispatch's own payload validator already strips this
        field before it reaches here; this proves dispatch() itself does not
        create a second injection path if that upstream allowlist ever
        changes."""
        malicious = request(task_id="wd-injection-task")
        malicious["working_directory"] = "/tmp/attacker-controlled-path"
        result = self.dispatch_case(malicious)
        task = self.store.get("tasks", "p1", "wd-injection-task")
        self.assertEqual(project()["working_directory"], task["working_directory"])
        self.assertNotEqual("/tmp/attacker-controlled-path", task["working_directory"])

    def test_new_task_with_no_project_working_directory_leaves_it_null(self):
        store = MemoryStore()
        proj = project(); proj["working_directory"] = None
        create_project(store, proj)
        dispatch(store, object(), request(task_id="no-wd-task"), quota(), [])
        task = store.get("tasks", "p1", "no-wd-task")
        self.assertIsNone(task.get("working_directory"))

    def test_registered_project_snapshot_uses_registry_not_stale_drive_literal(self):
        """P0 regression (fix/direct-dispatch-working-directory-authority-p0-
        20260822): for a project that IS registered in the Global Project
        Registry, with its workspace env var configured on this machine, the
        Task's working_directory snapshot must come from the registry's
        resolved, current-checkout path -- never from the Drive Project
        record's own working_directory literal, which nothing keeps in sync
        and which is exactly what let a Task launch inside a two-day-stale
        scratch checkout in production."""
        import tempfile
        store = MemoryStore()
        proj = project(); proj["project_id"] = "ai-development-manager"
        proj["working_directory"] = "C:/two-days-stale/scratch-checkout"
        create_project(store, proj)
        with tempfile.TemporaryDirectory() as workspace_root:
            with mock.patch.dict("os.environ", {"ADM_WORKSPACE_ROOT": workspace_root}):
                dispatch(store, object(), request(project_id="ai-development-manager", task_id="registry-wd-task"), quota(), [])
        task = store.get("tasks", "ai-development-manager", "registry-wd-task")
        self.assertNotEqual("C:/two-days-stale/scratch-checkout", task["working_directory"])
        self.assertTrue(task["working_directory"].endswith("ai-development-manager"))

    def test_registered_project_in_cloud_context_never_freezes_stale_drive_literal(self):
        """R2 regression: an independent review of the first fix found that
        it still reproduced the P0 in the real topology -- cloud.dispatch_
        ingress/manager.dispatcher.dispatch() run in Cloud Run, which has no
        HOME-local ADM_WORKSPACE_ROOT at all. A naive resolver would fall
        back to the Drive Project record's stale literal in exactly that
        case, freezing it onto the new Task forever (manager.execution_
        runner never re-resolves an already-non-None field). The correct
        behavior is that a registered project's Task gets working_directory
        = None from dispatch() when the workspace isn't configured here --
        deferring real resolution to the actual HOME execution host."""
        store = MemoryStore()
        proj = project(); proj["project_id"] = "ai-development-manager"
        proj["working_directory"] = "C:/two-days-stale/scratch-checkout"
        create_project(store, proj)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("ADM_WORKSPACE_ROOT", None)
            dispatch(store, object(), request(project_id="ai-development-manager", task_id="cloud-context-task"), quota(), [])
        task = store.get("tasks", "ai-development-manager", "cloud-context-task")
        self.assertIsNone(task["working_directory"])
        self.assertNotEqual("C:/two-days-stale/scratch-checkout", task["working_directory"])

    def test_existing_task_working_directory_is_never_overwritten_by_dispatch(self):
        """Retry/re-dispatch of an *existing* task_id must not silently
        re-derive working_directory from the Project's current value -- the
        Task's own dispatch-time snapshot is immutable once set, so a later
        Project edit can never drift an in-flight/retried Task."""
        create_task(self.store, request(task_id="t1", working_directory="C:/already/resolved"), assign=False)
        self.store.records[("projects", "p1", "p1")]["active_tasks"] = ["t1"]
        self.store.records[("projects", "p1", "p1")]["working_directory"] = "C:/work/project/renamed"
        self.dispatch_case(request())
        task = self.store.get("tasks", "p1", "t1")
        self.assertEqual("C:/already/resolved", task["working_directory"])

    def test_mandatory_rules_are_injected_into_generated_task(self):
        """The caller passed no shared_rules at all: injection must still happen automatically."""
        result = self.dispatch_case(request(title="No manual rules passed"))
        prompt = result["generated_prompt"]
        for rule in mandatory_rules("dispatch"):
            self.assertIn(rule["instruction"], prompt, f"mandatory rule {rule['rule_id']} was not auto-injected")

    def test_dispatch_rejected_when_mandatory_rule_injection_missing(self):
        """Any generated prompt lacking a mandatory rule (bug, drift, bypass) must block dispatch, not warn."""
        with mock.patch("manager.dispatcher.prompt_for", return_value="AI: Codex\nProject: p1\nTask: t1\n\nno mandatory rules present here"):
            with self.assertRaises(TaskError) as ctx:
                self.dispatch_case(request(title="Bypassed injection"))
        self.assertIn("mandatory rule injection missing", str(ctx.exception))

    def test_research_before_build_requires_poc_or_rejection_evidence(self):
        with self.assertRaises(TaskError) as ctx:
            self.dispatch_case(request(title="New architecture spike", research_gate_required=True))
        self.assertIn("research gate", str(ctx.exception))
        ok = self.dispatch_case(request(
            title="New architecture spike with evidence", research_gate_required=True,
            research_evidence={"candidates": [{"name": "libfoo", "rejection_reason": "unmaintained since 2023"}], "poc_attempted": False},
        ))
        self.assertIn("generated_prompt", ok)

    def test_dispatch_uses_bounded_history_lookup_when_history_deadline_given(self):
        """P0 dispatch-two-tick-observability: manager.execution_runner.
        launch_task() runs dispatch() AFTER a Command is already written
        "claimed" but BEFORE reserve_execution() runs -- a real HOME
        production trace showed a ~4.5 minute claimed->reserved gap driven by
        the unbounded list_executions() call below re-downloading every
        historical execution record on every dispatch. When history_deadline
        is given and no explicit `executions` list is supplied, dispatch()
        must use list_executions_bounded() (never the unbounded call) with
        that exact deadline forwarded through."""
        captured = {}

        def fake_bounded(store, project_id, deadline=None, single_request_worst_case=None):
            captured["project_id"] = project_id
            captured["deadline"] = deadline
            captured["single_request_worst_case"] = single_request_worst_case
            return []

        with mock.patch("manager.dispatcher.list_executions_bounded", side_effect=fake_bounded) as bounded, \
             mock.patch("manager.dispatcher.list_executions") as unbounded:
            dispatch(self.store, object(), request(task_id="bounded-history-task"), quota(),
                    executions=None, history_deadline=42.5)
        bounded.assert_called_once()
        unbounded.assert_not_called()
        self.assertEqual("p1", captured["project_id"])
        self.assertEqual(42.5, captured["deadline"])
        self.assertEqual(10.0, captured["single_request_worst_case"])

    def test_dispatch_uses_short_execution_history_store_when_supplied(self):
        captured = {}
        history_store = object()

        def fake_bounded(store, project_id, deadline=None, single_request_worst_case=None):
            captured["store"] = store
            return []

        with mock.patch("manager.dispatcher.list_executions_bounded", side_effect=fake_bounded):
            dispatch(self.store, object(), request(task_id="short-history-store-task"), quota(),
                     executions=None, history_deadline=42.5, execution_history_store=history_store)
        self.assertIs(history_store, captured["store"])

    def test_dispatch_explicit_executions_bypasses_bounded_lookup_even_with_deadline(self):
        """An explicitly supplied `executions` list (every existing test/
        caller that already precomputes its own history) must always win
        over history_deadline -- neither the bounded nor the unbounded
        lookup is ever called in that case."""
        with mock.patch("manager.dispatcher.list_executions_bounded") as bounded, \
             mock.patch("manager.dispatcher.list_executions") as unbounded:
            dispatch(self.store, object(), request(task_id="explicit-history-task"), quota(),
                    executions=[], history_deadline=42.5)
        bounded.assert_not_called()
        unbounded.assert_not_called()

    def test_dispatch_omits_bounded_lookup_when_no_deadline_given(self):
        """No history_deadline (every existing caller that does not pass it,
        e.g. manager.scheduler/manager.runtime_bridge) must keep calling the
        original unbounded list_executions() -- this fix must not silently
        change behavior for callers that never opted into bounding."""
        with mock.patch("manager.dispatcher.list_executions_bounded") as bounded, \
             mock.patch("manager.dispatcher.list_executions", return_value=[]) as unbounded:
            dispatch(self.store, object(), request(task_id="unbounded-history-task"), quota(), executions=None)
        bounded.assert_not_called()
        unbounded.assert_called_once_with(self.store, "p1")

    def test_dispatch_bounds_quota_read_and_fails_closed_on_timeout(self):
        """P0 dispatch-two-tick-final Phase 3: a hanging/slow quota read
        must never block dispatch() (and therefore the caller's downstream
        Task creation) beyond `quota_timeout_seconds` -- it must fail closed
        (TaskError) with an exact, distinguishing reason instead of hanging
        indefinitely."""
        import threading
        import time as time_module

        release = threading.Event()

        def hanging_read_drive_status(service=None):
            release.wait(timeout=5)
            return quota()

        with mock.patch("manager.dispatcher.read_drive_status", side_effect=hanging_read_drive_status):
            before = time_module.monotonic()
            with self.assertRaisesRegex(TaskError, "quota read did not complete"):
                dispatch(self.store, object(), request(task_id="quota-timeout-task"), executions=[],
                        quota_timeout_seconds=0.2)
            elapsed = time_module.monotonic() - before
        release.set()
        self.assertLess(elapsed, 2.0)

    def test_dispatch_quota_timeout_never_guesses_a_provider(self):
        """On a quota-read timeout, dispatch() must never proceed past the
        quota read at all -- no provider selection, no Task creation, no
        quota data (real or fabricated) reaches manager.assignment.decide()."""
        with mock.patch("manager.dispatcher.read_drive_status", side_effect=lambda service=None: __import__("time").sleep(5)):
            with self.assertRaises(TaskError):
                dispatch(self.store, object(), request(task_id="quota-timeout-no-guess"), executions=[],
                        quota_timeout_seconds=0.1)
        with self.assertRaises(TaskError):
            self.store.get("tasks", "p1", "quota-timeout-no-guess")

    def test_dispatch_explicit_quota_document_bypasses_bounded_read(self):
        """An explicitly supplied quota_document (every existing test/caller
        that already precomputes it) must always win over
        quota_timeout_seconds -- the bounded read is never attempted."""
        with mock.patch("manager.dispatcher._read_drive_status_bounded") as bounded, \
             mock.patch("manager.dispatcher.read_drive_status") as unbounded:
            dispatch(self.store, object(), request(task_id="quota-doc-task"), quota(), executions=[],
                    quota_timeout_seconds=5.0)
        bounded.assert_not_called()
        unbounded.assert_not_called()

    def test_dispatch_omits_bounded_quota_read_when_no_timeout_given(self):
        """No quota_timeout_seconds (every existing caller that does not
        pass it) must keep calling the original unbounded read_drive_status()
        directly -- this fix must not silently change behavior for callers
        that never opted into bounding."""
        with mock.patch("manager.dispatcher._read_drive_status_bounded") as bounded, \
             mock.patch("manager.dispatcher.read_drive_status", return_value=quota()) as unbounded:
            dispatch(self.store, object(), request(task_id="quota-no-timeout-task"), executions=[])
        bounded.assert_not_called()
        unbounded.assert_called_once_with(service=mock.ANY)


class QuotaAwareRoutingDispatcherTests(unittest.TestCase):
    """Regression test suite for M1 Slice 2: Forecast Evidence -> Account-aware Quota Routing."""

    def setUp(self):
        self.store = MemoryStore()
        create_project(self.store, project())

    def make_fresh_doc(self, a_rem=80.0, b_rem=20.0, a_resets=None, b_resets=None,
                       a_stale=False, b_stale=False, a_conf="official", b_conf="official",
                       a_extra_windows=None, b_extra_windows=None):
        from datetime import datetime, timezone, timedelta
        now_dt = datetime.now(timezone.utc)
        fresh_ts = (now_dt - timedelta(minutes=2)).isoformat()
        stale_ts = (now_dt - timedelta(hours=3)).isoformat()

        def make_entry(acc_id, rem, resets, stale, conf, extra_windows=None):
            w = []
            if rem is not None:
                w.append({
                    "name": "five_hour",
                    "remaining_percent": rem,
                    "used_percent": 100.0 - rem,
                    "resets_at": resets.isoformat() if isinstance(resets, datetime) else resets,
                })
            if extra_windows:
                w.extend(extra_windows)
            return {
                "provider": "claude",
                "account_id": acc_id,
                "display_name": "Claude Code",
                "collection_mode": "automatic",
                "source": "claude_code_statusline_rate_limits",
                "source_type": "official" if conf == "official" else "manual",
                "confidence": conf,
                "status": "ok" if rem is not None else "unknown",
                "stale": stale,
                "last_updated": stale_ts if stale else fresh_ts,
                "windows": w,
            }

        providers = [
            make_entry("claude-a", a_rem, a_resets, a_stale, a_conf, a_extra_windows),
            make_entry("claude-b", b_rem, b_resets, b_stale, b_conf, b_extra_windows),
            {
                "provider": "codex", "display_name": "Codex", "collection_mode": "automatic",
                "source": "codex_app_server", "source_type": "official", "confidence": "official",
                "status": "ok", "stale": False, "last_updated": fresh_ts,
                "windows": [{"name": "primary", "remaining_percent": 50.0, "used_percent": 50.0, "resets_at": None}],
            },
        ]
        return {"schema_version": "0.1.0", "generated_at": fresh_ts, "providers": providers}

    # 1. A fresh 80%, B fresh 20% -> A prioritized
    def test_fresh_a_80_b_20_selects_a(self):
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=20.0)
        res = dispatch(self.store, object(), request(title="Quota test", preferred_provider="claude", needs_repo_edit=False), doc, [])
        self.assertEqual("claude-a", res["account_id"])
        self.assertIn("claude-a", res["quota_summary"])
        self.assertIn("80", res["quota_summary"])
        self.assertEqual(80.0, res["quota_evidence"]["claude"]["windows"][0]["remaining_percent"])

    # 2. A 80% about to reset with waste risk -> A prioritized for consumption
    def test_a_80_reset_waste_risk_prioritized_over_b_80_healthy(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        reset_soon = now + timedelta(hours=1)
        reset_later = now + timedelta(hours=10)
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=80.0, a_resets=reset_soon, b_resets=reset_later)
        # Add history: A burned 10% in last hour (80% left with 1h to reset -> waste risk)
        h_a = {
            "provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": reset_soon.isoformat()}],
        }
        res = dispatch(self.store, object(), request(title="Waste risk test", preferred_provider="claude", needs_repo_edit=False), doc, [h_a])
        self.assertEqual("claude-a", res["account_id"])
        self.assertIn("claude-a", res["quota_summary"])

    # 3. A 80% likely exhaust before reset, B 40% healthy -> selects B
    def test_a_80_likely_exhaust_selects_b_40_healthy(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        reset_time = now + timedelta(hours=3)
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=40.0, a_resets=reset_time, b_resets=reset_time)
        # A was 130% (burned 50% in 1h, will exhaust in 1.6h < 3h -> CONSERVE)
        h_a = {
            "provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 130.0, "resets_at": reset_time.isoformat()}],
        }
        # B was 45% (burned 5% in 1h -> healthy)
        h_b = {
            "provider": "claude", "account_id": "claude-b", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 45.0, "resets_at": reset_time.isoformat()}],
        }
        res = dispatch(self.store, object(), request(title="Exhaust test", preferred_provider="claude", needs_repo_edit=False), doc, [h_a, h_b])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])

    # 4. A stale 90%, B fresh 40% -> selects B
    def test_stale_a_90_fresh_b_40_selects_b(self):
        doc = self.make_fresh_doc(a_rem=90.0, b_rem=40.0, a_stale=True, b_stale=False)
        res = dispatch(self.store, object(), request(title="Stale test", preferred_provider="claude", needs_repo_edit=False), doc, [])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])
        self.assertNotIn("90", res["quota_summary"])

    # 5. A remaining=None, B fresh 40% -> selects B
    def test_unknown_a_fresh_b_selects_b(self):
        doc = self.make_fresh_doc(a_rem=None, b_rem=40.0, a_conf="unknown", b_conf="official")
        res = dispatch(self.store, object(), request(title="Unknown test", preferred_provider="claude", needs_repo_edit=False), doc, [])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])

    # 6. A forecast insufficient history, but current fresh/reliable -> normal fallback to A (80% vs 20%)
    def test_insufficient_history_fallback_to_current_quota(self):
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=20.0)
        # Empty execution history
        res = dispatch(self.store, object(), request(title="No history test", preferred_provider="claude", needs_repo_edit=False), doc, [])
        self.assertEqual("claude-a", res["account_id"])

    # 7. A auth unavailable (disabled) -> cannot be selected
    def test_auth_unavailable_not_selected(self):
        from manager.claude_account_selector import resolve_claude_account
        registry = [
            {"account_id": "claude-a", "enabled": False, "config_dir": None},
            {"account_id": "claude-b", "enabled": True, "config_dir": r"C:\accounts\b\.claude"},
        ]
        doc = self.make_fresh_doc(a_rem=100.0, b_rem=30.0)
        res = resolve_claude_account(registry, doc)
        self.assertEqual("claude-b", res["account_id"])

    # 8. Capability mismatch -> quota cannot force selection of incompatible provider
    def test_capability_mismatch_prevents_quota_override(self):
        # Implementation task with repo editing -> Codex capability score is high
        doc = self.make_fresh_doc(a_rem=100.0, b_rem=100.0)
        req = request(title="Repo edit task", task_type="implementation", needs_repo_edit=True)
        res = dispatch(self.store, object(), req, doc, [])
        self.assertEqual("codex", res["recommended_provider"])

    # 9. five_hour suggests consume, but weekly clearly conserve -> weekly protection takes effect
    def test_five_hour_suggest_consume_weekly_conserve_weekly_protection(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        reset_5h = now + timedelta(hours=1)
        reset_week = now + timedelta(days=2)

        # Account A: 5h has 80% (resets 1h -> suggest consume), weekly has 20% (burning fast -> conserve)
        a_extra = [{"name": "seven_day", "remaining_percent": 20.0, "used_percent": 80.0, "resets_at": reset_week.isoformat()}]
        # Account B: 5h has 50% (normal use), weekly has 60% (healthy)
        b_extra = [{"name": "seven_day", "remaining_percent": 60.0, "used_percent": 40.0, "resets_at": reset_week.isoformat()}]

        doc = self.make_fresh_doc(a_rem=80.0, b_rem=50.0, a_resets=reset_5h, b_resets=now + timedelta(hours=4),
                                   a_extra_windows=a_extra, b_extra_windows=b_extra)

        h_a = {
            "provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [
                {"name": "five_hour", "remaining_percent": 90.0, "resets_at": reset_5h.isoformat()},
                {"name": "seven_day", "remaining_percent": 22.0, "resets_at": reset_week.isoformat()},
            ],
        }
        h_b = {
            "provider": "claude", "account_id": "claude-b", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [
                {"name": "five_hour", "remaining_percent": 55.0, "resets_at": (now + timedelta(hours=4)).isoformat()},
                {"name": "seven_day", "remaining_percent": 61.0, "resets_at": reset_week.isoformat()},
            ],
        }
        res = dispatch(self.store, object(), request(title="Multi-window test", preferred_provider="claude", needs_repo_edit=False), doc, [h_a, h_b])
        self.assertEqual("claude-b", res["account_id"])

    # 10. Selected account's quota_evidence cannot reference another account
    def test_selected_account_quota_evidence_isolated(self):
        doc = self.make_fresh_doc(a_rem=85.0, b_rem=15.0)
        res = dispatch(self.store, object(), request(title="Isolation test", preferred_provider="claude", needs_repo_edit=False), doc, [])
        self.assertEqual("claude-a", res["account_id"])
        evidence = res["quota_evidence"]["claude"]
        self.assertEqual("claude-a", evidence.get("account_id"))
        self.assertEqual(85.0, evidence["windows"][0]["remaining_percent"])
        self.assertNotIn("claude-b", str(evidence))

    # 11. account-a / account-b history completely isolated
    def test_account_history_isolation(self):
        from datetime import datetime, timezone, timedelta
        from manager.quota_forecast import forecast_account
        now = datetime.now(timezone.utc)
        reset_time = now + timedelta(hours=4)

        cur_a = {"provider": "claude", "account_id": "claude-a", "confidence": "official", "source_type": "official",
                 "source": "claude_code_statusline_rate_limits", "last_updated": now.isoformat(),
                 "windows": [{"name": "five_hour", "remaining_percent": 70.0, "resets_at": reset_time.isoformat()}]}
        cur_b = {"provider": "claude", "account_id": "claude-b", "confidence": "official", "source_type": "official",
                 "source": "claude_code_statusline_rate_limits", "last_updated": now.isoformat(),
                 "windows": [{"name": "five_hour", "remaining_percent": 30.0, "resets_at": reset_time.isoformat()}]}

        # History only has samples for claude-a
        h_a = {"provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
               "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": reset_time.isoformat()}]}

        fc_a = forecast_account(cur_a, history=[h_a], now=now)
        fc_b = forecast_account(cur_b, history=[h_a], now=now)

        self.assertEqual(2, fc_a.windows[0].burn_rate_samples)
        self.assertEqual(20.0, fc_a.windows[0].burn_rate_pct_per_hour)
        # Account B should have 0 history samples used because history was for claude-a!
        self.assertEqual(1, fc_b.windows[0].burn_rate_samples)
        self.assertIsNone(fc_b.windows[0].burn_rate_pct_per_hour)

    # 12. Forecast module unavailable / exception -> Dispatcher maintains safe fallback
    def test_forecast_module_exception_safe_fallback(self):
        from unittest.mock import patch
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=20.0)
        with patch("manager.quota_forecast.forecast_account", side_effect=RuntimeError("forecast math failure")):
            res = dispatch(self.store, object(), request(title="Exception fallback test", preferred_provider="claude", needs_repo_edit=False), doc, [])
            self.assertIn(res["recommended_provider"], ("claude", "codex"))
            self.assertIn("quota_evidence", res)

    # 13. P2 regression: ATTENTION + suggest consume must not output RiskStatus.CONSERVE
    def test_p2_regression_attention_suggest_consume_not_conserve(self):
        from datetime import datetime, timezone, timedelta
        from manager.quota_forecast import RiskStatus, ActionRecommendation
        now = datetime.now(timezone.utc)
        reset_time = now + timedelta(hours=4)
        doc = self.make_fresh_doc(a_rem=55.0, b_rem=10.0, a_resets=reset_time)
        h_a = {
            "provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 65.0, "resets_at": reset_time.isoformat()}],
        }
        res = dispatch(self.store, object(), request(title="P2 test", preferred_provider="claude", needs_repo_edit=False), doc, [h_a])
        fc_info = res["quota_evidence"]["claude"].get("forecast", {})
        self.assertEqual(ActionRecommendation.SUGGEST_CONSUME.value, fc_info.get("overall_action_recommendation"))
        self.assertEqual(RiskStatus.CONSUME_FASTER.value, fc_info.get("overall_risk_status"))
        self.assertNotEqual(RiskStatus.CONSERVE.value, fc_info.get("overall_risk_status"))


    # 14. Dispatcher uses QuotaHistoryStore to power forecast routing
    def test_dispatcher_uses_history_store_for_forecast_routing(self):
        from datetime import datetime, timezone, timedelta
        from manager.quota_history import QuotaHistoryStore
        now = datetime.now(timezone.utc)
        reset_soon = now + timedelta(hours=1)
        reset_later = now + timedelta(hours=10)
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=80.0, a_resets=reset_soon, b_resets=reset_later)

        store = QuotaHistoryStore()
        h_a = {
            "provider": "claude", "account_id": "claude-a", "last_updated": (now - timedelta(hours=1)).isoformat(),
            "windows": [{"name": "five_hour", "remaining_percent": 90.0, "resets_at": reset_soon.isoformat()}],
        }
        store.append_snapshot(h_a)

        res = dispatch(self.store, object(), request(title="Store routing test", preferred_provider="claude", needs_repo_edit=False), doc, [], history_store=store)
        self.assertEqual("claude-a", res["account_id"])
        self.assertIn("claude-a", res["quota_summary"])

    # 15. History store unavailable -> Dispatcher maintains current-quota fallback
    def test_dispatcher_history_store_unavailable_falls_back_safely(self):
        class BrokenHistoryStore:
            def get_history(self, *_, **__):
                raise IOError("storage unreachable")

        doc = self.make_fresh_doc(a_rem=80.0, b_rem=20.0)
        res = dispatch(self.store, object(), request(title="Broken store test", preferred_provider="claude", needs_repo_edit=False), doc, [], history_store=BrokenHistoryStore())
        self.assertEqual("claude-a", res["account_id"])
        self.assertEqual("claude", res["recommended_provider"])


class ProviderCapabilityRoutingTests(unittest.TestCase):
    """ClaudeLauncher v1 only supports the read-only safe profile
    (manager/claude_launcher.py) -- a repo-write task must never be routed
    to Claude, whether by automatic ranking or an explicit/replayed
    preference, and must never crash into a doomed Execution when Claude
    is the only capability-eligible-by-score provider but incompatible."""

    def setUp(self): self.store = MemoryStore(); create_project(self.store, project())

    def dispatch_case(self, req=None, q=None, records=None):
        return dispatch(self.store, object(), req or request(), q or quota(), [] if records is None else records)

    def test_read_only_task_claude_is_eligible(self):
        result = self.dispatch_case(request(title="Read-only", needs_repo_edit=False), quota(codex=None, claude=90))
        self.assertEqual(result["recommended_provider"], "claude")

    def test_repo_write_task_excludes_claude_from_ranking_entirely(self):
        # Claude has excellent quota, codex has none -- if capability
        # filtering didn't exclude Claude before scoring, it would win
        # here on quota score alone. It must never even be considered.
        result = self.dispatch_case(request(title="Repo-write excludes claude"), quota(codex=None, claude=99))
        self.assertIsNone(result["recommended_provider"])
        self.assertTrue(result["waiting_quota"])
        self.assertNotIn("claude", result["alternatives"])
        self.assertNotIn("claude", result["quota_evidence"])

    def test_repo_write_task_selects_codex_when_compatible_and_fresh(self):
        result = self.dispatch_case(request(title="Repo-write picks codex"), quota(codex=80, claude=99))
        self.assertEqual(result["recommended_provider"], "codex")

    def test_repo_write_no_compatible_provider_is_truthful_waiting_not_doomed_execution(self):
        result = self.dispatch_case(request(title="No compatible provider"), quota(codex=None, claude=90))
        self.assertIsNone(result["recommended_provider"])
        self.assertTrue(result["waiting_quota"])
        self.assertIsNone(result["generated_prompt"])
        self.assertIn("Task admitted; automatic provider selection is waiting on quota recovery",
                      "; ".join(result["warnings"]))

    def test_explicit_preferred_claude_for_repo_write_is_waiting_not_silently_launched(self):
        result = self.dispatch_case(
            request(title="Explicit claude repo-write", preferred_provider="claude"),
            quota(codex=None, claude=99),
        )
        self.assertIsNone(result["recommended_provider"])
        self.assertTrue(result["waiting_quota"])

    def test_replayed_provider_is_assigned_does_not_exempt_capability_gate(self):
        # Mirrors manager.execution_runner._dispatch_request()'s re-dispatch
        # replay of an already-assigned Command's provider (provider_is_
        # assigned=True). Unlike quota reliability, which IS exempted there
        # because quota can genuinely change between dispatch and replay,
        # capability incompatibility is static and must never be exempted.
        req = request(title="Replay claude repo-write", preferred_provider="claude", provider_is_assigned=True)
        result = self.dispatch_case(req, quota(codex=None, claude=99))
        self.assertIsNone(result["recommended_provider"])
        self.assertTrue(result["waiting_quota"])

    def test_capability_filtering_happens_before_quota_scoring(self):
        # Claude's quota IS usable here -- the capability-mismatch warning
        # must still fire, proving the filter isn't gated behind (or
        # short-circuited by) the quota-usability check.
        result = self.dispatch_case(request(title="Capability precedes quota"), quota(codex=None, claude=90))
        self.assertIn("does not support repo-write tasks (capability mismatch)", "; ".join(result["warnings"]))


if __name__ == "__main__": unittest.main()
