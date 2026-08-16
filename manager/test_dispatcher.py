import unittest
from copy import deepcopy

from manager.dispatcher import dispatch, request_ok
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


def quota(codex=80, claude=None, updated="2026-08-09T05:00:00Z"):
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


def two_claude_accounts(a_confidence="official", a_remaining=90, a_updated="2026-08-09T05:00:00Z", b_confidence="unknown", b_remaining=None, b_updated="2026-08-09T05:00:00Z"):
    def entry(account_id, confidence, remaining, updated):
        windows = [] if remaining is None else [{"name": "primary", "remaining_percent": remaining, "used_percent": 100 - remaining, "resets_at": None}]
        return {"provider": "claude", "account_id": account_id, "display_name": "claude", "collection_mode": "automatic", "source": "test", "source_type": "official" if confidence == "official" else "manual", "confidence": confidence, "last_updated": updated, "status": "ok" if windows else "unknown", "windows": windows}
    providers = [
        entry("claude-a", a_confidence, a_remaining, a_updated),
        entry("claude-b", b_confidence, b_remaining, b_updated),
        {"provider": "codex", "display_name": "codex", "collection_mode": "automatic", "source": "test", "source_type": "official", "confidence": "official", "last_updated": a_updated, "status": "ok", "windows": [{"name": "primary", "remaining_percent": 80, "used_percent": 20, "resets_at": None}]},
    ]
    return {"schema_version": "0.1.0", "generated_at": a_updated, "providers": providers}


def duplicate_claude_account(first_remaining, second_remaining, updated="2026-08-09T05:00:00Z"):
    """Two source records sharing the same account_id="claude-a" -- the
    upstream data-quality bug this test class guards against."""
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
                result = self.dispatch_case(request(task_id=task_id, title="Conversation label differs", preferred_provider=provider), quota(80, 80))
                prompt = result["generated_prompt"]
                expected = {"ai": provider.title(), "project": "p1", "task": task_id}
                self.assertEqual(expected, parse_identity_header(prompt))
                self.assertTrue(prompt.startswith(f"AI: {provider.title()}\nProject: p1\nTask: {task_id}\n\n"))
                self.assertIn("Project name: Project One", prompt)
                self.assertIn("Task goal: Conversation label differs", prompt)
                self.assertNotIn("Conversation:", prompt)

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

    def test_zero_samples_stale_quota_and_secret_redaction(self):
        stale = self.dispatch_case(request(title="Secret task", constraints=["token=sensitive-value", "credential:private"]), quota(updated="2020-01-01T00:00:00Z"))
        self.assertNotIn("sensitive-value", stale["generated_prompt"]); self.assertNotIn("private", stale["generated_prompt"]); self.assertTrue(stale["warnings"])
        self.assertEqual(20, stale["estimated_minutes"])

    def test_account_id_selects_matching_claude_account_quota(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result_a = self.dispatch_case(request(title="Account A", preferred_provider="claude", account_id="claude-a"), doc)
        self.assertIn("claude-a", result_a["quota_summary"]); self.assertIn("90% remaining", result_a["quota_summary"])
        result_b = self.dispatch_case(request(title="Account B", preferred_provider="claude", account_id="claude-b"), doc)
        self.assertIn("claude-b", result_b["quota_summary"]); self.assertIn("40% remaining", result_b["quota_summary"])
        self.assertNotIn("90% remaining", result_b["quota_summary"])

    def test_account_id_selection_is_order_independent(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        doc_swapped = dict(doc); doc_swapped["providers"] = list(reversed(doc["providers"]))
        result = self.dispatch_case(request(title="Account B swapped", preferred_provider="claude", account_id="claude-b"), doc_swapped)
        self.assertIn("claude-b", result["quota_summary"]); self.assertIn("40% remaining", result["quota_summary"])

    def test_account_id_reliability_not_laundered_across_accounts(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="unknown", b_remaining=None)
        result_b = self.dispatch_case(request(title="Unreliable account B", preferred_provider="claude", account_id="claude-b"), doc)
        self.assertIn("claude-b", result_b["quota_summary"]); self.assertIn("quota unknown", result_b["quota_summary"])
        self.assertIn("confidence unknown", result_b["quota_summary"])
        self.assertTrue(any("unknown" in item or "stale" in item for item in result_b["warnings"]))

    def test_account_id_does_not_silently_switch_stale_to_fresh(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=70, a_updated="2020-01-01T00:00:00Z", b_confidence="official", b_remaining=40, b_updated="2026-08-09T05:00:00Z")
        result_a = self.dispatch_case(request(title="Stale account A", preferred_provider="claude", account_id="claude-a"), doc)
        self.assertIn("claude-a", result_a["quota_summary"]); self.assertNotIn("claude-b", result_a["quota_summary"])
        self.assertNotIn("40% remaining", result_a["quota_summary"])
        self.assertTrue(any("stale" in item or "unknown" in item for item in result_a["warnings"]))

    def test_account_id_unknown_defers_to_auth_preflight(self):
        """An explicit account_id with no captured per-account quota data must
        not fail closed at dispatch() -- dispatch() has no way to know whether
        that account is actually launchable; only ClaudeLauncher's real auth
        preflight can decide that. The explicit account_id is preserved
        verbatim (never substituted/dropped), and its quota evidence is a
        distinct unknown/unavailable entry -- never another account's or the
        legacy representative's real numbers laundered onto it."""
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Missing account", preferred_provider="claude", account_id="claude-does-not-exist"), doc)
        self.assertEqual("claude-does-not-exist", result["account_id"])
        self.assertIn("claude-does-not-exist", result["quota_summary"])
        self.assertIn("quota unknown", result["quota_summary"])
        self.assertNotIn("90% remaining", result["quota_summary"])
        self.assertNotIn("40% remaining", result["quota_summary"])
        self.assertTrue(any("unknown" in item or "stale" in item for item in result["warnings"]))
        # quota_evidence is persisted onto the Task/Execution/Command records
        # (execution_runner.reserve_execution, dispatcher.create_task) as the
        # durable audit trail -- it must never show another account's real
        # numbers for an account dispatch() could not itself find data for.
        claude_evidence = result["quota_evidence"]["claude"]
        self.assertEqual("unknown", claude_evidence["confidence"])
        self.assertEqual([], claude_evidence["windows"])

    def test_account_id_matched_quota_evidence_reflects_that_account_not_legacy_representative(self):
        """Same evidence-integrity property, for the already-matched-account
        case: quota_evidence must reflect the specific requested account_id's
        own data, never the provider-level legacy representative's (which can
        be a different real account's numbers)."""
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result_b = self.dispatch_case(request(title="Account B evidence", preferred_provider="claude", account_id="claude-b"), doc)
        claude_evidence = result_b["quota_evidence"]["claude"]
        self.assertEqual(40, claude_evidence["windows"][0]["remaining_percent"])

    def test_account_id_lookup_deterministic_when_source_has_duplicate_records(self):
        """Regression for the duplicate-key bug: if two source records
        share account_id="claude-a", quota["accounts"] must already be
        deduplicated to one entry by the time dispatch() does its next()
        lookup, so the result is the deliberate last-wins record -- not
        whichever duplicate happened to come first."""
        doc = duplicate_claude_account(first_remaining=12, second_remaining=47)
        result = self.dispatch_case(request(title="Duplicate account record", preferred_provider="claude", account_id="claude-a"), doc)
        self.assertIn("47% remaining", result["quota_summary"])
        self.assertNotIn("12% remaining", result["quota_summary"])
        doc_swapped = duplicate_claude_account(first_remaining=47, second_remaining=12)
        result_swapped = self.dispatch_case(request(title="Duplicate account record swapped", preferred_provider="claude", account_id="claude-a"), doc_swapped)
        self.assertIn("12% remaining", result_swapped["quota_summary"])
        self.assertNotIn("47% remaining", result_swapped["quota_summary"])

    def test_no_account_id_keeps_legacy_provider_level_behavior(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Legacy path", preferred_provider="claude"), doc)
        self.assertNotIn("claude-a", result["quota_summary"]); self.assertNotIn("claude-b", result["quota_summary"])
        self.assertIn("90% remaining", result["quota_summary"])

    def test_codex_dispatch_unaffected_by_account_id(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Codex still works", preferred_provider="codex", account_id="claude-a"), doc)
        self.assertEqual("codex", result["recommended_provider"]); self.assertIn("80% remaining", result["quota_summary"])

    def test_all_claude_accounts_unknown_no_fabricated_selection(self):
        doc = two_claude_accounts(a_confidence="unknown", a_remaining=None, b_confidence="unknown", b_remaining=None)
        result = self.dispatch_case(request(title="All unknown", preferred_provider="claude"), doc)
        self.assertEqual("claude", result["recommended_provider"]); self.assertIn("quota unknown", result["quota_summary"])
        self.assertNotIn("claude-a", result["quota_summary"]); self.assertNotIn("claude-b", result["quota_summary"])

    def test_account_identity_shown_without_credentials(self):
        doc = two_claude_accounts(a_confidence="official", a_remaining=90, b_confidence="official", b_remaining=40)
        result = self.dispatch_case(request(title="Attribution only", preferred_provider="claude", account_id="claude-a"), doc)
        for blob in (result["quota_summary"], result["generated_prompt"]):
            self.assertIn("claude-a", blob)
            for forbidden in ("config_dir", "token", "credential", "CLAUDE_CONFIG_DIR"):
                self.assertNotIn(forbidden, blob)

    def test_malformed_input_rejected(self):
        with self.assertRaises(TaskError): request_ok({"project_id": "p1"})
        with self.assertRaises(TaskError): request_ok(request(preferred_provider="codex", excluded_provider="codex"))


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
        res = dispatch(self.store, object(), request(title="Quota test", preferred_provider="claude"), doc, [])
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
        res = dispatch(self.store, object(), request(title="Waste risk test", preferred_provider="claude"), doc, [h_a])
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
        res = dispatch(self.store, object(), request(title="Exhaust test", preferred_provider="claude"), doc, [h_a, h_b])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])

    # 4. A stale 90%, B fresh 40% -> selects B
    def test_stale_a_90_fresh_b_40_selects_b(self):
        doc = self.make_fresh_doc(a_rem=90.0, b_rem=40.0, a_stale=True, b_stale=False)
        res = dispatch(self.store, object(), request(title="Stale test", preferred_provider="claude"), doc, [])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])
        self.assertNotIn("90", res["quota_summary"])

    # 5. A remaining=None, B fresh 40% -> selects B
    def test_unknown_a_fresh_b_selects_b(self):
        doc = self.make_fresh_doc(a_rem=None, b_rem=40.0, a_conf="unknown", b_conf="official")
        res = dispatch(self.store, object(), request(title="Unknown test", preferred_provider="claude"), doc, [])
        self.assertEqual("claude-b", res["account_id"])
        self.assertIn("claude-b", res["quota_summary"])

    # 6. A forecast insufficient history, but current fresh/reliable -> normal fallback to A (80% vs 20%)
    def test_insufficient_history_fallback_to_current_quota(self):
        doc = self.make_fresh_doc(a_rem=80.0, b_rem=20.0)
        # Empty execution history
        res = dispatch(self.store, object(), request(title="No history test", preferred_provider="claude"), doc, [])
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
        res = dispatch(self.store, object(), request(title="Multi-window test", preferred_provider="claude"), doc, [h_a, h_b])
        self.assertEqual("claude-b", res["account_id"])

    # 10. Selected account's quota_evidence cannot reference another account
    def test_selected_account_quota_evidence_isolated(self):
        doc = self.make_fresh_doc(a_rem=85.0, b_rem=15.0)
        res = dispatch(self.store, object(), request(title="Isolation test", preferred_provider="claude"), doc, [])
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
            res = dispatch(self.store, object(), request(title="Exception fallback test", preferred_provider="claude"), doc, [])
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
        res = dispatch(self.store, object(), request(title="P2 test", preferred_provider="claude"), doc, [h_a])
        fc_info = res["quota_evidence"]["claude"].get("forecast", {})
        self.assertEqual(ActionRecommendation.SUGGEST_CONSUME.value, fc_info.get("overall_action_recommendation"))
        self.assertEqual(RiskStatus.CONSUME_FASTER.value, fc_info.get("overall_risk_status"))
        self.assertNotEqual(RiskStatus.CONSERVE.value, fc_info.get("overall_risk_status"))


if __name__ == "__main__": unittest.main()
