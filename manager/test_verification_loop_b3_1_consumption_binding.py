"""Phase B3-1: the issue record is the authority on what a ticket authorised.

Phase B-2 final independent re-review residual NB-A. ``_fold_ticket`` returned
a ticket's consumption record wholesale as the ticket's current state, and
``ticket_id`` hashes only (execution, candidate, bundle, gate, round). Every
other field the issue record froze -- ``expected_checker_identity`` above all
-- therefore sat outside the id, and an attacker with store-write authority
could append a new, correctly named consumption record naming themselves as
the expected checker. Nothing existing was edited, so the digest-on-read guard
of B-2R could not fire by construction, and the forged PASS was ACCEPTED.

Every test here was written against ``b38ceb2`` first and fails there: the
reproducer ``docs/verification-loop/repro/B31-NBA-FORGED-CONSUMPTION.py``
measures the same attacks at both commits.

**Which guard is under test.** The guard is ``consumption_divergence_field``
as applied by ``_fold_ticket``, and it reports its own stable reason,
``TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE``, naming the rewritten field. Each
attack test asserts *that* reason, not merely "not ACCEPTED": several of the
rewritten fields (lease, task, base SHA) were already stopped by a sibling
admission guard at base, so a test that only asserted the outcome would pass
with this guard removed. Asserting the reason is what makes the guard's
absence visible.
"""

import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from manager.verification_loop import stores
from manager.verification_loop.bundle import canonical_json, record_digest, report_digest
from manager.verification_loop.controller import VerificationController
from manager.verification_loop.fixtures import (
    WORKTREE_GENERATION,
    WORKTREE_LOCK_ID,
    fx_ledger_freeze_pane,
)
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.models import VerificationTicket
from manager.verification_loop.tickets import (
    CONSUMPTION_MUTABLE_FIELDS,
    consumption_divergence_field,
    derive_ticket_id,
    issue_frozen_fields,
)

LEDGER_GATES = ("V0", "V1", "V2", "V3")
GUARD = "TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE"
CONSUMED_AT = "2026-09-07T00:00:01Z"

ROGUE = ResolvedIdentity(
    Identity("rogue", "acct-rogue", "sess-rogue"), "classified", "high", "deterministic_signal"
)

# One rewrite per issue-frozen field. ``test_pin_2`` proves this table covers
# every frozen field, so adding a field to VerificationTicket without deciding
# what a forged consumption of it looks like fails a test rather than
# silently widening the ticket.
FROZEN_FIELD_REWRITES = {
    "expected_checker_identity": ROGUE,
    "forbidden_identity": None,
    "issued_by": ROGUE,
    "issued_at": "1999-01-01T00:00:00Z",
    "candidate_head_at_issue": "c" * 40,
    "worktree_lock_id": "repo-lease-stolen",
    "worktree_generation": 99,
    "task_id": "T-SOMEONE-ELSE",
    "base_sha": "d" * 40,
    "candidate_sha": "e" * 40,
    "execution_id": "E-SOMEONE-ELSE",
    "bundle_hash": "0" * 64,
    "gate_id": "V9",
    "round": 7,
    "ticket_id": "vt-" + "f" * 64,
}


class ConsumptionBindingTestCase(unittest.TestCase):
    """A controller over a real temporary store, plus an attacker's pen."""

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "ephemeral-manager-home"
        self.home.mkdir()
        self.controller = VerificationController(self.home)

    # -- honest side -------------------------------------------------------
    def issue(self, gates=LEDGER_GATES, round_=1):
        return self.controller.issue_round(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            gate_ids=gates,
            round_=round_,
            issued_by=self.scenario.controller,
            expected_checker_identity=self.scenario.checker,
            candidate_head_at_issue=self.scenario.execution.candidate_sha,
            worktree_lock_id=WORKTREE_LOCK_ID,
            worktree_generation=WORKTREE_GENERATION,
            issued_at="2026-09-06T00:00:00Z",
        )

    def derive(self):
        return self.controller.derive(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            preflight=self.scenario.preflight,
        )

    def honest_three(self):
        """V0..V2 answered honestly; V3 left for the attacker."""
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))

    def issued_record(self, gate_id, round_=1):
        ticket_id = derive_ticket_id(
            self.scenario.execution.execution_id,
            self.scenario.execution.candidate_sha,
            self.scenario.bundle.bundle_hash,
            gate_id,
            round_,
        )
        for record in self.controller.tickets._records(ticket_id):
            if record.ticket_seq == stores.ISSUE_SEQ:
                return record
        raise AssertionError(f"no issue record for {gate_id}")

    def consumption_records(self, gate_id, round_=1):
        ticket_id = derive_ticket_id(
            self.scenario.execution.execution_id,
            self.scenario.execution.candidate_sha,
            self.scenario.bundle.bundle_hash,
            gate_id,
            round_,
        )
        return [
            record
            for record in self.controller.tickets._records(ticket_id)
            if record.ticket_seq == stores.CONSUME_SEQ
        ]

    # -- attacker side -----------------------------------------------------
    def append_record(self, ticket):
        """A new, correctly named ticket record. No existing file is touched,
        so the B-2R digest-on-read guard cannot be what refuses it."""
        path = self.home / "verification" / "tickets" / (record_digest(ticket) + ".json")
        self.assertFalse(path.exists(), "the attack must append, never overwrite")
        path.write_text(canonical_json(ticket), encoding="utf-8", newline="")
        # Sanity: the file verifies against its own name, exactly as an honest
        # record would, so whatever refuses it is not the content-address check.
        self.assertEqual(
            record_digest(ticket),
            record_digest(stores._verify_record(path, VerificationTicket, path.stem, "TICKET")),
        )
        return path

    def forged_consumption(self, issued, digest, **rewrites):
        return replace(
            issued,
            status="consumed",
            consumed_report_digest=digest,
            consumed_at=CONSUMED_AT,
            ticket_seq=stores.CONSUME_SEQ,
            **rewrites,
        )

    def assert_refused_by_guard(self, field_name, action):
        with self.assertRaises(stores.EvidenceIntegrityError) as caught:
            action()
        message = str(caught.exception)
        self.assertIn(GUARD, message)
        self.assertIn(repr(field_name), message)
        return message


# ---------------------------------------------------------------------------
# NBA-1..NBA-6: forged consumption records
# ---------------------------------------------------------------------------


class ForgedConsumptionTests(ConsumptionBindingTestCase):
    def test_nba_1_a_consumption_naming_a_rogue_expected_checker_is_refused(self):
        # The headline NB-A vector (review probe T8a). ACCEPTED at b38ceb2.
        self.honest_three()
        rogue_report = self.scenario.report("V3", producer_identity=ROGUE)
        rogue_digest = self.controller.reports.put(rogue_report)
        self.append_record(
            self.forged_consumption(
                self.issued_record("V3"), rogue_digest, expected_checker_identity=ROGUE
            )
        )
        self.assert_refused_by_guard("expected_checker_identity", self.derive)
        # The refusal is on read, so it also stops anything that folds the
        # ticket -- not only the derivation path.
        self.assert_refused_by_guard(
            "expected_checker_identity", lambda: self.controller.tickets.get(rogue_report.ticket_id)
        )

    def test_nba_2_every_issue_frozen_field_is_bound(self):
        # Single-factor per subtest: the report is honest, the issue record is
        # untouched, and exactly one frozen field differs in the appended
        # consumption record. Fields inside ticket_id are included too -- the
        # record still claims the right ticket_id, so the store still files it
        # under this ticket, and this guard, not self-consistency, is what
        # refuses it before the pure layer ever sees it.
        for field_name, value in FROZEN_FIELD_REWRITES.items():
            if field_name == "ticket_id":
                continue  # a record claiming another id is another ticket's; see nba_3
            with self.subTest(field=field_name):
                self.setUp()
                self.honest_three()
                honest = self.scenario.report("V3")
                digest = self.controller.reports.put(honest)
                self.append_record(
                    self.forged_consumption(
                        self.issued_record("V3"), digest, **{field_name: value}
                    )
                )
                self.assert_refused_by_guard(field_name, self.derive)

    def test_nba_3_a_consumption_transplanted_onto_another_ticket_is_refused(self):
        # V2's honest consumption record, re-filed under V3's ticket id with
        # V3's report digest. Everything else in it still describes V2, and
        # the first frozen field that says so is the gate. (Review probe T6.)
        self.honest_three()
        v3_digest = self.controller.reports.put(self.scenario.report("V3"))
        (v2_consumed,) = self.consumption_records("V2")
        self.append_record(
            replace(
                v2_consumed,
                ticket_id=self.scenario.report("V3").ticket_id,
                consumed_report_digest=v3_digest,
            )
        )
        self.assert_refused_by_guard("gate_id", self.derive)

    def test_nba_4_forged_consumption_plus_forged_pass_never_accepts(self):
        # The attacker also erases the forbidden identity so that no sibling
        # identity guard (EXECUTOR_EQUALS_CHECKER, CHECKER_IS_FORBIDDEN_IDENTITY)
        # could be what stops the forged PASS. ACCEPTED at b38ceb2.
        self.honest_three()
        forged_pass = self.scenario.report("V3", producer_identity=ROGUE)
        forged_digest = self.controller.reports.put(forged_pass)
        self.append_record(
            self.forged_consumption(
                self.issued_record("V3"),
                forged_digest,
                expected_checker_identity=ROGUE,
                forbidden_identity=None,
            )
        )
        with self.assertRaises(stores.EvidenceIntegrityError) as caught:
            self.derive()
        self.assertIn(GUARD, str(caught.exception))

    def test_nba_5_tampering_outranks_the_conflict_fold(self):
        # An honest consumption already exists; the attacker appends a
        # divergent one. Two consumptions would ordinarily fold to
        # "invalidated" -- a legitimate contest -- but a record the store never
        # wrote is not a contest, it is tampering, and the ledger says so
        # instead of quietly penalising the honest claimant.
        self.honest_three()
        self.controller.submit(self.scenario.report("V3"))
        rogue_digest = self.controller.reports.put(
            self.scenario.report("V3", producer_identity=ROGUE)
        )
        self.append_record(
            self.forged_consumption(
                self.issued_record("V3"), rogue_digest, expected_checker_identity=ROGUE
            )
        )
        self.assert_refused_by_guard("expected_checker_identity", self.derive)

    def test_nba_6_the_issue_record_keeps_its_authority_after_an_honest_consumption(self):
        # Control for the guard's *direction*: an honest consumption record
        # agrees with its issue record on every frozen field, so the folded
        # ticket carries the issue record's identity, not something new.
        self.honest_three()
        self.controller.submit(self.scenario.report("V3"))
        issued = self.issued_record("V3")
        folded = self.controller.tickets.get(issued.ticket_id)
        self.assertEqual("consumed", folded.status)
        for name in issue_frozen_fields():
            self.assertEqual(getattr(issued, name), getattr(folded, name), name)
        self.assertIsNone(consumption_divergence_field(issued, folded))


# ---------------------------------------------------------------------------
# CRASH-1..CRASH-4: predicate 11 crash consistency is untouched
# ---------------------------------------------------------------------------


class CrashConsistencyStillHoldsTests(ConsumptionBindingTestCase):
    def crash_window(self):
        self.honest_three()
        report = self.scenario.report("V3")
        self.controller.reports.put(report)
        return report

    def test_crash_1_report_without_consumption_is_pending_not_accepted(self):
        self.crash_window()
        result = self.derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual("CONTINUE_VERIFICATION", result.next_action)
        self.assertIn(
            "TICKET_NOT_CONSUMED", {reason for _key, reason in result.invalidated_report_reasons}
        )

    def test_crash_2_appending_only_the_missing_consumption_recovers(self):
        report = self.crash_window()
        before = sorted(p.name for p in self.controller.reports.directory.glob("*.json"))
        self.controller.tickets.consume(report.ticket_id, report_digest(report), CONSUMED_AT)
        recovered = self.derive()
        self.assertEqual("ACCEPTED", recovered.acceptance_state, recovered.invalidated_report_reasons)
        after = sorted(p.name for p in self.controller.reports.directory.glob("*.json"))
        self.assertEqual(before, after, "recovery must not rewrite any stored report")

    def test_crash_3_identical_consumption_replay_is_idempotent(self):
        report = self.crash_window()
        self.controller.tickets.consume(report.ticket_id, report_digest(report), CONSUMED_AT)
        before = sorted(p.name for p in self.controller.tickets.directory.glob("*.json"))
        self.controller.tickets.consume(
            report.ticket_id, report_digest(report), "2026-09-07T09:09:09Z"
        )
        after = sorted(p.name for p in self.controller.tickets.directory.glob("*.json"))
        self.assertEqual(before, after)
        self.assertEqual("ACCEPTED", self.derive().acceptance_state)

    def test_crash_4_conflicting_consumptions_invalidate_in_either_order(self):
        fingerprints = []
        for order in (("PASS", "FAIL"), ("FAIL", "PASS")):
            self.setUp()
            self.honest_three()
            for outcome in order:
                if outcome == "PASS":
                    self.controller.submit(self.scenario.report("V3"))
                else:
                    self.controller.submit(
                        self.scenario.report(
                            "V3",
                            result="FAIL",
                            failure_observations=(self.fx["freeze_pane_failure"],),
                        )
                    )
            ticket = {t.gate_id: t for t in self.controller.stored_tickets()}["V3"]
            self.assertEqual("invalidated", ticket.status)
            result = self.derive()
            self.assertNotEqual("ACCEPTED", result.acceptance_state)
            fingerprints.append(
                (
                    result.acceptance_state,
                    result.next_action,
                    result.invalidated_report_reasons,
                    result.satisfied_gates,
                    result.missing_required_gates,
                    result.derivation_key,
                )
            )
        self.assertEqual(fingerprints[0], fingerprints[1])


# ---------------------------------------------------------------------------
# CTRL-1..CTRL-2: the honest paths are unchanged
# ---------------------------------------------------------------------------


class HonestPathControlTests(ConsumptionBindingTestCase):
    def test_ctrl_1_honest_round_still_accepts(self):
        self.issue()
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        result = self.derive()
        self.assertEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_ctrl_2_honest_failure_still_routes_to_repair(self):
        self.honest_three()
        self.controller.submit(
            self.scenario.report(
                "V3", result="FAIL", failure_observations=(self.fx["freeze_pane_failure"],)
            )
        )
        result = self.derive()
        self.assertEqual("REJECTED_NEEDS_REPAIR", result.acceptance_state)
        self.assertEqual("REPAIR", result.next_action)


# ---------------------------------------------------------------------------
# PURE-1..PURE-3 and PIN-1..PIN-2: the binding as a pure function
# ---------------------------------------------------------------------------


class ConsumptionDivergenceFieldTests(unittest.TestCase):
    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.scenario = fx["scenario"](fx["bundle_with_clause"])
        self.issued = self.scenario.ticket("V3")

    def honest_consumption(self, **rewrites):
        transition = dict(
            status="consumed",
            consumed_report_digest="1" * 64,
            consumed_at=CONSUMED_AT,
            ticket_seq=1,
        )
        transition.update(rewrites)
        return replace(self.issued, **transition)

    def test_pure_1_an_honest_consumption_does_not_diverge(self):
        self.assertIsNone(consumption_divergence_field(self.issued, self.honest_consumption()))

    def test_pure_2_each_frozen_field_is_named_when_rewritten(self):
        for field_name, value in FROZEN_FIELD_REWRITES.items():
            with self.subTest(field=field_name):
                forged = self.honest_consumption(**{field_name: value})
                self.assertEqual(field_name, consumption_divergence_field(self.issued, forged))

    def test_pure_3_the_mutable_fields_are_free_to_differ(self):
        forged = self.honest_consumption(
            status="invalidated", consumed_report_digest="2" * 64,
            consumed_at="2030-01-01T00:00:00Z", ticket_seq=5,
        )
        self.assertIsNone(consumption_divergence_field(self.issued, forged))

    def test_pin_1_exactly_four_fields_are_mutable_by_consumption(self):
        self.assertEqual(
            {"status", "consumed_report_digest", "consumed_at", "ticket_seq"},
            set(CONSUMPTION_MUTABLE_FIELDS),
        )
        all_fields = {f.name for f in fields(VerificationTicket)}
        self.assertEqual(all_fields, set(issue_frozen_fields()) | set(CONSUMPTION_MUTABLE_FIELDS))
        self.assertTrue(set(CONSUMPTION_MUTABLE_FIELDS).isdisjoint(issue_frozen_fields()))

    def test_pin_2_the_rewrite_table_covers_every_frozen_field(self):
        # A field added to VerificationTicket later must be given a forged
        # shape here, so the behavioural tests above keep covering it.
        self.assertEqual(set(issue_frozen_fields()), set(FROZEN_FIELD_REWRITES))
