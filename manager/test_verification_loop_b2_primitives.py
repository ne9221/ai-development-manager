"""Phase B-2 primitives: paths, identity, bundle, tickets, budget, impact, stores.

These are the pieces the evaluator composes. Testing them directly matters
because several were *derived* in B-1 from a single naive expression -- a raw
``startswith`` for paths, an opaque string for identity -- and the composed
evaluator tests could not distinguish "the rule fired" from "some other check
happened to catch it". The Phase B-1 review found exactly that shape in attack
test 05.
"""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from manager.verification_loop import budget as budget_module
from manager.verification_loop import stores
from manager.verification_loop.bundle import (
    canonical_json,
    compute_bundle_hash,
    finalize_bundle,
    oracle_digest,
    report_digest,
    validate_bundle,
)
from manager.verification_loop.identity import (
    Identity,
    ResolvedIdentity,
    resolution_failure,
    same_actor,
)
from manager.verification_loop.impact import RERUN_ALL, affected_gates
from manager.verification_loop.models import (
    Budget,
    CheckerSpec,
    ExecutionFixture,
    FrozenOracle,
    ImpactRule,
)
from manager.verification_loop.paths import (
    canonical_repo_path,
    canonicalise_all,
    path_matches_prefix,
)
from manager.verification_loop.tickets import (
    derive_ticket_id,
    index_tickets,
    ticket_self_consistency_reason,
)
from manager.verification_loop.fixtures import (
    GOVERNANCE_DIGEST,
    GOVERNANCE_URI,
    GOVERNANCE_VERSION,
    checker_spec,
    fx_ledger_freeze_pane,
)

BACKSLASH = chr(92)


# ---------------------------------------------------------------------------
# Path canonicalisation (B-2 requirement N)
# ---------------------------------------------------------------------------


class PathCanonicalisationTests(unittest.TestCase):
    def test_dot_slash_prefix_is_stripped(self):
        self.assertEqual("manager/x.py", canonical_repo_path("./manager/x.py"))

    def test_backslashes_become_forward_slashes(self):
        raw = "manager" + BACKSLASH + "x.py"
        self.assertEqual("manager/x.py", canonical_repo_path(raw))

    def test_redundant_separators_and_dot_segments_collapse(self):
        self.assertEqual("manager/x.py", canonical_repo_path("manager//./x.py"))
        self.assertEqual("manager", canonical_repo_path("manager/"))

    def test_traversal_is_rejected_not_resolved(self):
        # Resolving would let "manager/../secrets" look like it touches
        # manager/ while landing elsewhere, so it is refused outright.
        self.assertIsNone(canonical_repo_path("manager/../secrets"))
        self.assertIsNone(canonical_repo_path("../outside"))

    def test_absolute_and_drive_paths_are_rejected(self):
        self.assertIsNone(canonical_repo_path("/etc/passwd"))
        self.assertIsNone(canonical_repo_path("C:/Windows"))
        self.assertIsNone(canonical_repo_path("C:relative"))
        self.assertIsNone(canonical_repo_path(BACKSLASH * 2 + "unc" + BACKSLASH + "share"))

    def test_empty_and_non_string_are_rejected(self):
        self.assertIsNone(canonical_repo_path(""))
        self.assertIsNone(canonical_repo_path("   "))
        self.assertIsNone(canonical_repo_path(None))
        self.assertIsNone(canonical_repo_path(42))

    def test_prefix_match_is_case_insensitive(self):
        self.assertTrue(path_matches_prefix("Manager/Foo.py", "manager/"))

    def test_prefix_match_is_anchored_to_a_segment_boundary(self):
        self.assertTrue(path_matches_prefix("manager/foo.py", "manager"))
        self.assertFalse(path_matches_prefix("manager-extra/foo.py", "manager"))

    def test_uncanonicalisable_paths_are_reported_not_dropped(self):
        canonical, rejected = canonicalise_all(["./a.py", "../b.py", "/c.py"])
        self.assertEqual(("a.py",), canonical)
        self.assertEqual(("../b.py", "/c.py"), rejected)


# ---------------------------------------------------------------------------
# Identity triple (B-2 requirement E)
# ---------------------------------------------------------------------------


def _identity(provider="claude", account="acct-1", session="sess-1", **kw):
    defaults = dict(status="classified", confidence="high", method="deterministic_signal")
    defaults.update(kw)
    return ResolvedIdentity(Identity(provider, account, session), **defaults)


class IdentityResolutionTests(unittest.TestCase):
    def test_fully_resolved_identity_is_usable(self):
        self.assertIsNone(resolution_failure(_identity()))

    def test_absent_identity_fails_closed(self):
        self.assertEqual("IDENTITY_ABSENT", resolution_failure(None))

    def test_blank_member_is_absent_not_a_value(self):
        # Two different actors both missing an account_id would otherwise
        # compare equal on that field.
        self.assertEqual("IDENTITY_INCOMPLETE", resolution_failure(_identity(account="  ")))

    def test_unclassified_and_needs_review_are_rejected(self):
        self.assertEqual("IDENTITY_UNCLASSIFIED", resolution_failure(_identity(status="needs_review")))
        self.assertEqual("IDENTITY_UNCLASSIFIED", resolution_failure(_identity(status="unclassified")))

    def test_conflicting_deterministic_signals_is_rejected(self):
        self.assertEqual(
            "IDENTITY_METHOD_UNRELIABLE",
            resolution_failure(_identity(method="conflicting_deterministic_signals")),
        )

    def test_low_confidence_is_rejected(self):
        self.assertEqual("IDENTITY_CONFIDENCE_LOW", resolution_failure(_identity(confidence="medium")))
        self.assertEqual("IDENTITY_CONFIDENCE_LOW", resolution_failure(_identity(confidence=None)))

    def test_self_asserted_identity_is_rejected(self):
        self.assertEqual(
            "IDENTITY_NOT_RESOLVED_BY_SESSION_CENTER",
            resolution_failure(_identity(resolved_by="the_checker_itself")),
        )

    def test_account_swap_is_a_different_actor(self):
        self.assertFalse(same_actor(_identity(account="A"), _identity(account="B")))

    def test_session_swap_is_a_different_actor(self):
        self.assertFalse(same_actor(_identity(session="s1"), _identity(session="s2")))

    def test_same_triple_is_the_same_actor_regardless_of_provider_session_id(self):
        left = ResolvedIdentity(
            Identity("claude", "a", "s", provider_session_id="p1"),
            "classified", "high", "deterministic_signal",
        )
        right = ResolvedIdentity(
            Identity("claude", "a", "s", provider_session_id="p2"),
            "classified", "high", "deterministic_signal",
        )
        self.assertTrue(same_actor(left, right))

    def test_missing_side_is_never_treated_as_independence(self):
        # False here means "cannot establish sameness", which is why callers
        # must reject an unresolved identity separately rather than reading a
        # False as proof the two differ.
        self.assertFalse(same_actor(_identity(), None))
        self.assertFalse(same_actor(None, None))


# ---------------------------------------------------------------------------
# Frozen bundle hashing (B-2 requirement P)
# ---------------------------------------------------------------------------


class BundleHashTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ledger_freeze_pane()
        self.bundle = self.fx["bundle_with_clause"]

    def test_finalized_bundle_hash_matches_its_own_content(self):
        self.assertEqual(compute_bundle_hash(self.bundle), self.bundle.bundle_hash)
        self.assertEqual((), validate_bundle(self.bundle))

    def test_editing_a_frozen_golden_changes_the_bundle_hash(self):
        tampered = replace(
            self.bundle,
            frozen_oracles=(
                FrozenOracle(
                    "ORA-ART-STRUCT",
                    "golden",
                    (("tests/golden/workbook-descriptor.json", "sha-golden-TAMPERED"),),
                ),
            ),
        )
        self.assertNotEqual(compute_bundle_hash(tampered), self.bundle.bundle_hash)
        self.assertIn("BUNDLE_HASH_MISMATCH", validate_bundle(tampered))

    def test_raising_a_budget_changes_the_bundle_hash(self):
        # A budget the budgeted party could raise would not be a budget.
        louder = replace(self.bundle, budget=Budget(max_repair_executions=99))
        self.assertNotEqual(compute_bundle_hash(louder), self.bundle.bundle_hash)

    def test_changing_a_checker_impl_digest_changes_the_bundle_hash(self):
        checkers = dict(self.bundle.checkers)
        checkers["V3"] = replace(checkers["V3"], checker_impl_digest="impl-swapped")
        self.assertNotEqual(
            compute_bundle_hash(replace(self.bundle, checkers=checkers)), self.bundle.bundle_hash
        )

    def test_two_bundles_differing_only_in_clauses_have_different_hashes(self):
        self.assertNotEqual(
            self.fx["bundle_with_clause"].bundle_hash,
            self.fx["bundle_without_clause"].bundle_hash,
        )

    def test_hash_covers_new_fields_by_exclusion_not_by_enumeration(self):
        covered = set(self.bundle.__dataclass_fields__) - {"bundle_hash"}
        payload = canonical_json(
            {name: getattr(self.bundle, name) for name in sorted(covered)}
        )
        for name in covered:
            self.assertIn(name, payload, f"{name} is not covered by the bundle hash")

    def test_oracle_digest_is_derived_from_member_content(self):
        before = oracle_digest(self.bundle, "V3")
        after = oracle_digest(
            replace(
                self.bundle,
                frozen_oracles=(FrozenOracle("ORA-ART-STRUCT", "golden", (("p", "different"),)),),
            ),
            "V3",
        )
        self.assertIsNotNone(before)
        self.assertNotEqual(before, after)

    def test_gate_referencing_an_undefined_oracle_fails_closed(self):
        broken = replace(self.bundle, gate_oracle_refs={"V3": "ORA-DOES-NOT-EXIST"})
        self.assertTrue(oracle_digest(broken, "V3").startswith("MISSING_ORACLE:"))
        self.assertIn("GATE_ORACLE_UNDEFINED:V3", validate_bundle(broken))


class BundleMonotonicityTests(unittest.TestCase):
    def _bundle(self, matrix):
        return finalize_bundle(
            bundle_id="ab-x",
            bundle_version="1.0.0",
            governance_source_uri=GOVERNANCE_URI,
            governance_version=GOVERNANCE_VERSION,
            governance_digest=GOVERNANCE_DIGEST,
            gate_requirements_by_risk=matrix,
            checkers={g: checker_spec(g) for g in ("V0", "V1", "V2")},
        )

    def test_monotone_matrix_validates(self):
        self.assertEqual(
            (), validate_bundle(self._bundle({"low": ("V0",), "high": ("V0", "V1", "V2")}))
        )

    def test_higher_tier_dropping_a_lower_tier_gate_is_a_contract_defect(self):
        reasons = validate_bundle(self._bundle({"medium": ("V0", "V1"), "high": ("V2",)}))
        self.assertTrue(any(r.startswith("NON_MONOTONE_GATE_MATRIX") for r in reasons), reasons)

    def test_required_gate_without_a_checker_is_a_contract_defect(self):
        bundle = finalize_bundle(
            bundle_id="ab-x",
            bundle_version="1.0.0",
            governance_source_uri=GOVERNANCE_URI,
            governance_version=GOVERNANCE_VERSION,
            governance_digest=GOVERNANCE_DIGEST,
            gate_requirements_by_risk={"low": ("V0", "V9")},
            checkers={"V0": checker_spec("V0")},
        )
        self.assertIn("REQUIRED_GATE_WITHOUT_CHECKER:V9", validate_bundle(bundle))

    def test_production_tier_declaring_no_gates_is_not_a_monotonicity_breach(self):
        # Production routes to PFP rather than being verified, so declaring no
        # gates for it is correct, not a weaker tier.
        bundle = self._bundle({"low": ("V0",), "high": ("V0", "V1"), "production": ()})
        self.assertEqual((), validate_bundle(bundle))


# ---------------------------------------------------------------------------
# Tickets (B-2 requirement G)
# ---------------------------------------------------------------------------


class TicketIdTests(unittest.TestCase):
    def test_id_is_deterministic_for_the_same_tuple(self):
        first = derive_ticket_id("E1", "sha1", "ab-1", "V0", 1)
        second = derive_ticket_id("E1", "sha1", "ab-1", "V0", 1)
        self.assertEqual(first, second)

    def test_every_component_changes_the_id(self):
        base = derive_ticket_id("E1", "sha1", "ab-1", "V0", 1)
        self.assertNotEqual(base, derive_ticket_id("E2", "sha1", "ab-1", "V0", 1))
        self.assertNotEqual(base, derive_ticket_id("E1", "sha2", "ab-1", "V0", 1))
        self.assertNotEqual(base, derive_ticket_id("E1", "sha1", "ab-2", "V0", 1))
        self.assertNotEqual(base, derive_ticket_id("E1", "sha1", "ab-1", "V1", 1))
        self.assertNotEqual(base, derive_ticket_id("E1", "sha1", "ab-1", "V0", 2))

    def test_encoding_is_unambiguous_across_field_boundaries(self):
        # With a plain separator these two would hash identically, letting a
        # caller move a boundary and land on someone else's ticket id.
        self.assertNotEqual(
            derive_ticket_id("a|b", "s", "h", "g", 1),
            derive_ticket_id("a", "b|s", "h", "g", 1),
        )

    def test_forged_ticket_id_is_self_refuting(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        forged = scenario.ticket("V0", ticket_id="vt-whatever-i-like")
        self.assertEqual(
            "TICKET_ID_NOT_DERIVED_FROM_CONTENT", ticket_self_consistency_reason(forged)
        )

    def test_duplicate_ticket_ids_drop_both_rather_than_picking_one(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        ticket = scenario.ticket("V0")
        by_id, rejected = index_tickets([ticket, replace(ticket, worktree_generation=2)])
        self.assertNotIn(ticket.ticket_id, by_id)
        self.assertIn((ticket.ticket_id, "DUPLICATE_TICKET_ID"), rejected)


# ---------------------------------------------------------------------------
# Budgets (B-2 requirement L)
# ---------------------------------------------------------------------------


def _execution(execution_id, **kw):
    defaults = dict(
        task_id="T",
        base_sha="b" * 40,
        candidate_sha="a" * 40,
        diff_paths=(),
        status="completed",
        acceptance_bundle_ref="ab-x",
    )
    defaults.update(kw)
    return ExecutionFixture(execution_id=execution_id, **defaults)


class BudgetFoldTests(unittest.TestCase):
    def test_counters_fold_over_the_whole_chain_not_one_execution(self):
        chain = (
            _execution("E1"),
            _execution("E2", repair_of_execution_id="E1"),
            _execution("E3", repair_of_execution_id="E2"),
        )
        consumed = budget_module.consumption(chain, {})
        self.assertEqual(2, consumed.repair_executions)

    def test_a_new_repair_execution_cannot_reset_the_repair_budget(self):
        # The whole point: counting on the current Execution alone would make
        # every repair start from zero and the loop would never terminate.
        chain = tuple(
            [_execution("E1")]
            + [_execution(f"E{i}", repair_of_execution_id=f"E{i-1}") for i in range(2, 6)]
        )
        exhausted = budget_module.consumption(chain, {}).exhausted_against(
            Budget(max_repair_executions=3)
        )
        self.assertIn("repair_executions", exhausted)

    def test_retry_and_repair_draw_on_separate_budgets(self):
        chain = (
            _execution("E1"),
            _execution("E2", retry_of_execution_id="E1"),
            _execution("E3", retry_of_execution_id="E2"),
        )
        consumed = budget_module.consumption(chain, {})
        self.assertEqual(2, consumed.transient_retries)
        self.assertEqual(0, consumed.repair_executions)

    def test_review_rounds_come_from_the_highest_report_round_seen(self):
        fx = fx_ledger_freeze_pane()
        scenario = fx["scenario"](fx["bundle_with_clause"])
        consumed = budget_module.consumption(
            (scenario.execution,),
            {scenario.execution.execution_id: (scenario.report("V0", 3),)},
        )
        self.assertEqual(3, consumed.review_rounds)

    def test_only_round_consuming_actions_are_blocked(self):
        exhausted = ("repair_executions", "transient_retries", "review_rounds")
        for action in ("REPAIR", "REPAIR_TEST_ONLY", "RETRY_SAME_CANDIDATE"):
            self.assertTrue(budget_module.blocks(action, exhausted)[0], action)
        for action in ("CONTINUE_VERIFICATION", "ACCEPTED", "ROUTE_TO_PFP", "ESCALATE_HUMAN"):
            self.assertFalse(budget_module.blocks(action, exhausted)[0], action)

    def test_retry_is_not_blocked_by_the_repair_budget_alone(self):
        blocked, _why = budget_module.blocks("RETRY_SAME_CANDIDATE", ("repair_executions",))
        self.assertFalse(blocked)


# ---------------------------------------------------------------------------
# Impact rules (B-2 requirement O)
# ---------------------------------------------------------------------------


class ImpactRuleTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ledger_freeze_pane()
        self.bundle = self.fx["bundle_with_clause"]
        self.required = ("V0", "V1", "V2", "V3")

    def test_matched_paths_narrow_the_rerun_set(self):
        gates, policy = affected_gates(
            ("manager/artifact/writer.py",), self.bundle, self.required
        )
        self.assertEqual("BY_PATH", policy)
        self.assertEqual(("V0", "V1", "V2", "V3"), gates)

    def test_an_unmatched_path_forces_rerun_all(self):
        gates, policy = affected_gates(
            ("manager/artifact/writer.py", "somewhere/unknown.py"), self.bundle, self.required
        )
        self.assertEqual(RERUN_ALL, policy)
        self.assertEqual(self.required, gates)

    def test_an_uncanonicalisable_path_forces_rerun_all(self):
        gates, policy = affected_gates(("../escape.py",), self.bundle, self.required)
        self.assertEqual(RERUN_ALL, policy)
        self.assertEqual(self.required, gates)

    def test_a_bundle_with_no_impact_rules_forces_rerun_all(self):
        gates, policy = affected_gates(
            ("manager/artifact/writer.py",), replace(self.bundle, impact_rules=()), self.required
        )
        self.assertEqual(RERUN_ALL, policy)
        self.assertEqual(self.required, gates)

    def test_narrowing_never_exceeds_what_the_tier_requires(self):
        bundle = replace(
            self.bundle, impact_rules=(ImpactRule("manager/artifact", ("V1", "V9")),)
        )
        gates, _policy = affected_gates(("manager/artifact/w.py",), bundle, ("V0", "V1"))
        self.assertNotIn("V9", gates)


# ---------------------------------------------------------------------------
# Stores: create-only writes and the production boundary (B-2 requirements H/S/V)
# ---------------------------------------------------------------------------


class StoreProductionBoundaryTests(unittest.TestCase):
    def test_a_marked_production_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "runtime-home"
            marker.mkdir()
            (marker / ".adm-production-runtime.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(stores.ProductionStoreRefused):
                stores.FileTicketStore(marker)

    def test_a_subdirectory_of_a_marked_checkout_is_refused(self):
        # The guard walks ancestors, so resolving deeper does not escape it.
        with tempfile.TemporaryDirectory() as root:
            marked = Path(root) / "prod"
            nested = marked / "a" / "b"
            nested.mkdir(parents=True)
            (marked / ".adm-production-runtime.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(stores.ProductionStoreRefused):
                stores.FileReportStore(nested)

    def test_the_canonical_user_level_manager_home_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            canonical = Path(root) / ".ai-development-manager"
            canonical.mkdir()
            with mock.patch.object(
                stores.manager_home, "canonical_manager_home", return_value=canonical
            ):
                with self.assertRaises(stores.ProductionStoreRefused):
                    stores.FileTicketStore(canonical)
                with self.assertRaises(stores.ProductionStoreRefused):
                    stores.FileTicketStore(canonical / "nested")

    def test_a_temporary_home_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileTicketStore(root)
            self.assertTrue(str(store.directory).endswith(os.path.join("verification", "tickets")))


class StoreCreateOnlyTests(unittest.TestCase):
    def setUp(self):
        self.fx = fx_ledger_freeze_pane()
        self.scenario = self.fx["scenario"](self.fx["bundle_with_clause"])

    def test_reissuing_an_identical_ticket_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileTicketStore(root)
            ticket = self.scenario.ticket("V0")
            store.issue(ticket)
            store.issue(ticket)
            self.assertEqual((ticket.ticket_id,), tuple(store.list_ids()))

    def test_a_different_ticket_under_the_same_id_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileTicketStore(root)
            ticket = self.scenario.ticket("V0")
            store.issue(ticket)
            conflicting = replace(ticket, worktree_generation=99)
            with self.assertRaises(stores.VerificationStoreError):
                store.issue(conflicting)

    def test_a_self_inconsistent_ticket_is_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileTicketStore(root)
            with self.assertRaises(stores.VerificationStoreError):
                store.issue(self.scenario.ticket("V0", ticket_id="vt-forged"))
            self.assertEqual((), tuple(store.list_ids()))

    def test_a_traversal_shaped_ticket_id_cannot_escape_the_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileTicketStore(root)
            with self.assertRaises(stores.VerificationStoreError):
                store._path("../../escape")

    def test_reports_are_content_addressed_so_a_verdict_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            store = stores.FileReportStore(root)
            failing = self.scenario.report("V0", result="FAIL")
            passing = self.scenario.report("V0", result="PASS")
            first = store.put(failing)
            second = store.put(passing)
            self.assertNotEqual(first, second)
            # Both survive: the failing verdict is still there to be derived from.
            self.assertEqual({first, second}, set(store.list_digests()))
            self.assertEqual(report_digest(failing), first)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
