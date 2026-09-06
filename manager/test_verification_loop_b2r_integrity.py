"""Phase B-2R: the ledger has to check the evidence it reads back.

The Phase B-2 independent review found the derivation core sound and the layer
underneath it hollow. Both blocking findings were the same shape -- a record was
trusted because of *where it came from* rather than *what it hashes to*:

* a stored FAIL report edited on disk to PASS turned REPAIR into ACCEPTED, and
  the filename was still the digest of the FAIL content;
* a stored ticket's ``expected_checker_identity`` edited to a rogue actor
  authorised that actor, because ``ticket_id`` hashes only five of the ticket's
  fields and nothing re-derived the rest;
* a report's self-declared ``base_result`` outranked the frozen baseline
  attestation that contradicted it, converting a human-gated contract revision
  into an automated licence to edit the test.

Every test here was written before the repair and run against ``794db70``,
where the security assertions fail. They are grouped by the mechanism they pin
rather than by the finding that motivated them, because the mechanisms are what
a future change can break.

**Single-factor discipline.** The B-1 and B-2 reviews both found tests that
passed for the wrong reason: an earlier guard caught the forgery before the
guard under test could. Where an earlier guard would mask the mechanism, the
fixture is built so that only the guard under test can fire -- most visibly in
``CrossTaskEvidenceReuseTests``, where the ticket is deliberately made
*consistent* with the borrowed report, so ``REPORT_TASK_ID_MISMATCH`` is the one
thing standing between Task A's evidence and Task B's acceptance.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from manager.verification_loop import stores
from manager.verification_loop.bundle import canonical_json, finalize_bundle, report_digest
from manager.verification_loop.classification import base_evidence, classify_observation
from manager.verification_loop.controller import VerificationController, rehydrate
from manager.verification_loop.evaluator import evaluate
from manager.verification_loop.fixtures import (
    WORKTREE_GENERATION,
    consumed_against,
    WORKTREE_LOCK_ID,
    fx_ledger_freeze_pane,
)
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.models import FailureObservation, VerificationReportFixture
from manager.verification_loop.tickets import (
    derive_ticket_id,
    ticket_self_consistency_reason,
)

LEDGER_GATES = ("V0", "V1", "V2", "V3")

ROGUE = ResolvedIdentity(
    Identity("rogue", "acct-rogue", "sess-rogue"), "classified", "high", "deterministic_signal"
)

ROGUE_JSON = {
    "identity": {
        "provider": "rogue",
        "account_id": "acct-rogue",
        "session_id": "sess-rogue",
        "provider_session_id": None,
    },
    "status": "classified",
    "confidence": "high",
    "method": "deterministic_signal",
    "resolved_by": "session_center",
}


def rebundle(scenario, **overrides):
    """A scenario whose bundle is re-finalized, so its hash stays honest."""
    fields = {
        name: getattr(scenario.bundle, name)
        for name in scenario.bundle.__dataclass_fields__
        if name != "bundle_hash"
    }
    fields.update(overrides)
    return replace(scenario, bundle=finalize_bundle(**fields))


def digest_of_document(doc):
    """The digest a stored report document actually earns, recomputed."""
    return report_digest(rehydrate(VerificationReportFixture, doc))


class LedgerTestCase(unittest.TestCase):
    """A controller over a real temporary store, with a control that ACCEPTS."""

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "ephemeral-manager-home"
        self.home.mkdir()
        self.controller = VerificationController(self.home)

    # -- helpers ---------------------------------------------------------
    def issue(self, gates=LEDGER_GATES, round_=1, **overrides):
        kwargs = dict(
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
        kwargs.update(overrides)
        return self.controller.issue_round(**kwargs)

    def derive(self, **kwargs):
        return self.controller.derive(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            preflight=self.scenario.preflight,
            **kwargs,
        )

    def honest_round(self):
        """A complete, honest, ACCEPTED round. Returns the report digests."""
        self.issue()
        return {gate: self.controller.submit(self.scenario.report(gate)) for gate in LEDGER_GATES}

    def report_path(self, digest):
        return self.home / "verification" / "reports" / (digest + ".json")

    def ticket_id_for(self, gate_id, round_=1):
        return derive_ticket_id(
            self.scenario.execution.execution_id,
            self.scenario.execution.candidate_sha,
            self.scenario.bundle.bundle_hash,
            gate_id,
            round_,
        )

    def ticket_records(self, gate_id, round_=1):
        """Every persisted record claiming this ticket's id.

        Records are named by their own content digest, not by ticket id, so
        they are selected by reading them -- which is also how the store finds
        them, and means these tests cannot accidentally depend on a filename
        convention the store does not actually rely on.
        """
        wanted = self.ticket_id_for(gate_id, round_)
        found = []
        for path in sorted((self.home / "verification" / "tickets").glob("*.json")):
            if json.loads(path.read_text(encoding="utf-8"))["ticket_id"] == wanted:
                found.append(path)
        return found

    def record_at_seq(self, gate_id, seq, round_=1):
        """The ticket record at ``seq``. Chosen by content, never by filename."""
        for path in self.ticket_records(gate_id, round_):
            if json.loads(path.read_text(encoding="utf-8"))["ticket_seq"] == seq:
                return path
        raise AssertionError(f"no ticket record at seq {seq} for {gate_id}")

    def rewrite(self, path, mutate):
        """Edit a stored record in place, exactly as a shell would."""
        doc = json.loads(path.read_text(encoding="utf-8"))
        mutate(doc)
        path.write_text(
            json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
            newline="",
        )
        return doc


# ---------------------------------------------------------------------------
# RPT-1..RPT-8: the report ledger verifies its own content address on read
# ---------------------------------------------------------------------------


class ReportLedgerIntegrityTests(LedgerTestCase):
    """A report counts because of what it hashes to, not where it sits."""

    def failing_round(self):
        """V0-V2 PASS, V3 FAIL. Derives REPAIR. Returns the FAIL digest."""
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        return self.controller.submit(
            self.scenario.report(
                "V3",
                result="FAIL",
                failure_observations=(self.fx["freeze_pane_failure"],),
            )
        )

    def test_rpt_1_an_untampered_store_still_reaches_accepted(self):
        # The control. Read-time verification that also rejected honest
        # records would be indistinguishable from a loop sealed shut.
        self.honest_round()
        result = self.derive()
        self.assertEqual("ACCEPTED", result.acceptance_state, result.admitted_failure_classes)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_rpt_1b_an_untampered_failing_store_still_derives_repair(self):
        self.failing_round()
        self.assertEqual("REPAIR", self.derive().next_action)

    def test_rpt_2_a_stored_fail_edited_into_a_pass_is_refused(self):
        digest = self.failing_round()
        self.assertEqual("REPAIR", self.derive().next_action)

        def flip(doc):
            doc["result"] = "PASS"
            doc["failure_observations"] = []

        self.rewrite(self.report_path(digest), flip)
        with self.assertRaises(stores.VerificationStoreError) as caught:
            self.derive()
        self.assertIn("REPORT_CONTENT_DIGEST_MISMATCH", str(caught.exception))

    def test_rpt_3_editing_the_failure_observations_is_refused(self):
        digest = self.failing_round()
        self.rewrite(
            self.report_path(digest),
            lambda doc: doc.__setitem__("failure_observations", []),
        )
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_rpt_4_editing_the_producer_identity_is_refused(self):
        digests = self.honest_round()
        self.rewrite(
            self.report_path(digests["V3"]),
            lambda doc: doc["producer_identity"]["identity"].__setitem__(
                "account_id", "acct-somebody-else"
            ),
        )
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_rpt_5_editing_the_task_execution_or_sha_fields_is_refused(self):
        for field, value in (
            ("task_id", "T-SOMEONE-ELSE"),
            ("execution_id", "E-SOMEONE-ELSE"),
            ("candidate_sha", "c" * 40),
            ("base_sha", "d" * 40),
            ("bundle_hash", "ab-forged"),
        ):
            with self.subTest(field=field):
                self.setUp()
                digests = self.honest_round()
                self.rewrite(
                    self.report_path(digests["V3"]),
                    lambda doc, f=field, v=value: doc.__setitem__(f, v),
                )
                with self.assertRaises(stores.VerificationStoreError):
                    self.derive()

    def test_rpt_6_the_persisted_bytes_must_be_the_canonical_form(self):
        # The contract, stated once and pinned here: the digest covers the
        # canonical *semantic* record, and the persisted bytes must be that
        # canonical serialisation. Re-indenting therefore fails -- not because
        # the meaning changed, but because a store accepting two byte
        # sequences for one digest would have to decide which one it hashed.
        digests = self.honest_round()
        path = self.report_path(digests["V3"])
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(digests["V3"], digest_of_document(doc))
        path.write_text(json.dumps(doc, sort_keys=True, indent=2), encoding="utf-8", newline="")
        with self.assertRaises(stores.VerificationStoreError) as caught:
            self.derive()
        self.assertIn("REPORT_NOT_CANONICAL_BYTES", str(caught.exception))

    def test_rpt_7_a_truncated_or_malformed_record_is_refused(self):
        digests = self.honest_round()
        self.report_path(digests["V0"]).write_text('{"result": "PA', encoding="utf-8")
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_rpt_8_a_record_filed_under_another_records_digest_is_refused(self):
        # Not an edit: an honest, correctly serialised record simply filed
        # under a different record's name. The digest is the claim; the
        # content is the proof.
        digests = self.honest_round()
        honest = self.report_path(digests["V1"]).read_text(encoding="utf-8")
        self.report_path(digests["V2"]).write_text(honest, encoding="utf-8", newline="")
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()


# ---------------------------------------------------------------------------
# TKT-1..TKT-7: the ticket ledger binds the fields ticket_id does not cover
# ---------------------------------------------------------------------------


class TicketLedgerIntegrityTests(LedgerTestCase):
    """``ticket_id`` binds five fields; the ledger has to bind the rest."""

    def issued_record(self, gate_id="V3"):
        records = self.ticket_records(gate_id)
        self.assertEqual(1, len(records), "a freshly issued ticket has exactly one record")
        return records[0]

    def test_tkt_1_rewriting_the_expected_checker_does_not_authorise_a_rogue(self):
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        self.rewrite(
            self.issued_record("V3"),
            lambda doc: doc.__setitem__("expected_checker_identity", ROGUE_JSON),
        )
        # The tamper is caught the moment anything reads the ledger, and
        # submitting a report reads it: consuming a ticket means loading it.
        with self.assertRaises(stores.VerificationStoreError) as caught:
            self.controller.submit(self.scenario.report("V3", producer_identity=ROGUE))
        self.assertIn("TICKET_CONTENT_DIGEST_MISMATCH", str(caught.exception))
        # And it stays caught on the derivation path, which is the one that
        # would otherwise have produced the ACCEPTED.
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_tkt_2_rewriting_the_ticket_task_id_is_refused(self):
        self.issue()
        self.rewrite(
            self.issued_record("V3"), lambda doc: doc.__setitem__("task_id", "T-SOMEONE-ELSE")
        )
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_tkt_3_deleting_the_forbidden_identity_is_refused(self):
        # This is how an executor would authorise itself: the field sits
        # outside ticket_id entirely.
        self.issue()
        self.rewrite(
            self.issued_record("V3"), lambda doc: doc.__setitem__("forbidden_identity", None)
        )
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_tkt_4_rewriting_a_consumed_ticket_back_to_issued_is_refused(self):
        # Reopening a spent ticket is how a second report would be let in, so
        # the consumption record is selected by its sequence rather than by
        # sort order -- editing the issue record to say "issued" changes
        # nothing and would make this test pass without proving anything.
        self.honest_round()
        record = self.record_at_seq("V3", 1)
        self.rewrite(record, lambda doc: doc.__setitem__("status", "issued"))
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_tkt_5_rewriting_the_base_sha_or_lease_fields_is_refused(self):
        for field, value in (
            ("base_sha", "d" * 40),
            ("candidate_head_at_issue", "e" * 40),
            ("worktree_lock_id", "repo-someone-else"),
            ("worktree_generation", 99),
            ("issued_at", "2026-01-01T00:00:00Z"),
        ):
            with self.subTest(field=field):
                self.setUp()
                self.issue()
                self.rewrite(
                    self.issued_record("V3"),
                    lambda doc, f=field, v=value: doc.__setitem__(f, v),
                )
                with self.assertRaises(stores.VerificationStoreError):
                    self.derive()

    def test_tkt_6_a_hand_written_extra_record_is_refused(self):
        # An attacker who cannot edit an existing record may try to add one
        # alongside it.
        self.issue()
        doc = json.loads(self.issued_record("V3").read_text(encoding="utf-8"))
        doc["expected_checker_identity"] = ROGUE_JSON
        (self.home / "verification" / "tickets" / ("0" * 64 + ".json")).write_text(
            json.dumps(doc, sort_keys=True, separators=(",", ":")), encoding="utf-8", newline=""
        )
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_tkt_7_an_untampered_ticket_ledger_still_accepts(self):
        self.honest_round()
        self.assertEqual("ACCEPTED", self.derive().acceptance_state)


# ---------------------------------------------------------------------------
# CSM-1..CSM-9: Phase A v3 admission predicate 11, ticket consumption
# ---------------------------------------------------------------------------


class TicketConsumptionTests(LedgerTestCase):
    """A ticket is consumed by exactly one report digest, recorded at the time."""

    def stored_by_gate(self):
        return {ticket.gate_id: ticket for ticket in self.controller.stored_tickets()}

    def test_csm_1_submitting_a_report_consumes_its_ticket(self):
        self.issue()
        digest = self.controller.submit(self.scenario.report("V0"))
        ticket = self.stored_by_gate()["V0"]
        self.assertEqual("consumed", ticket.status)
        self.assertEqual(digest, ticket.consumed_report_digest)
        self.assertEqual(1, ticket.ticket_seq)

    def test_csm_2_an_unanswered_ticket_stays_issued(self):
        self.issue()
        ticket = self.stored_by_gate()["V3"]
        self.assertEqual("issued", ticket.status)
        self.assertIsNone(ticket.consumed_report_digest)
        self.assertEqual(0, ticket.ticket_seq)

    def test_csm_3_resubmitting_the_identical_report_is_idempotent(self):
        self.issue()
        report = self.scenario.report("V0")
        first = self.controller.submit(report)
        second = self.controller.submit(report)
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.ticket_records("V0")))
        self.assertEqual("consumed", self.stored_by_gate()["V0"].status)

    def test_csm_4_a_second_different_report_cannot_take_a_consumed_ticket(self):
        # Order-independence is the property under test, not "first one wins":
        # if the loser were silently dropped, submitting the PASS first would
        # hide the FAIL and reach ACCEPTED.
        for order in (("FAIL", "PASS"), ("PASS", "FAIL")):
            with self.subTest(order=order):
                self.setUp()
                self.issue()
                for gate in ("V0", "V1", "V2"):
                    self.controller.submit(self.scenario.report(gate))
                for outcome in order:
                    self.controller.submit(
                        self.scenario.report("V3")
                        if outcome == "PASS"
                        else self.scenario.report(
                            "V3",
                            result="FAIL",
                            failure_observations=(self.fx["freeze_pane_failure"],),
                        )
                    )
                result = self.derive()
                self.assertNotEqual("ACCEPTED", result.acceptance_state)
                self.assertIn(
                    "DUPLICATE_REPORT_FOR_TICKET",
                    {reason for _key, reason in result.invalidated_report_reasons},
                )

    def test_csm_5_a_report_whose_digest_is_not_the_consumed_one_is_inadmissible(self):
        # The attacker writes the report file directly, bypassing submit(), so
        # there is no second consumption record to give them away.
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        self.controller.submit(
            self.scenario.report(
                "V3", result="FAIL", failure_observations=(self.fx["freeze_pane_failure"],)
            )
        )
        # Replace the answer wholesale rather than adding a second one: with
        # both on disk the duplicate-ticket guard fires first and predicate 11
        # never gets to speak, which would make this test pass for the wrong
        # reason. Removing the honest report is also the stronger attack.
        honest_digest = report_digest(
            self.scenario.report(
                "V3", result="FAIL", failure_observations=(self.fx["freeze_pane_failure"],)
            )
        )
        self.report_path(honest_digest).unlink()
        forged = self.scenario.report("V3")
        self.report_path(report_digest(forged)).write_text(
            canonical_json(forged), encoding="utf-8", newline=""
        )
        result = self.derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "REPORT_DIGEST_NOT_TICKET_CONSUMED",
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_csm_6_a_consumed_ticket_still_admits_the_report_that_consumed_it(self):
        self.honest_round()
        result = self.derive()
        self.assertEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_csm_7_a_partial_ledger_fails_closed(self):
        # Deleting the issue record must not degrade to "no ticket, therefore
        # no constraint".
        self.honest_round()
        self.record_at_seq("V3", 0).unlink()
        with self.assertRaises(stores.VerificationStoreError):
            self.derive()

    def test_csm_8_predicate_11_is_enforced_in_the_pure_evaluator_too(self):
        # The same predicate without the store, so a future controller that
        # consumes differently cannot quietly lose the check.
        scenario = self.scenario
        reports = [scenario.report(gate) for gate in LEDGER_GATES]
        tickets = tuple(
            scenario.ticket(
                report.gate_id,
                status="consumed",
                consumed_report_digest=report_digest(report),
                consumed_at="2026-09-06T00:00:01Z",
                ticket_seq=1,
            )
            for report in reports
        )
        result = evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight, tickets=tickets,
        )
        self.assertEqual("ACCEPTED", result.acceptance_state)

        wrong = tuple(replace(t, consumed_report_digest="deadbeef") for t in tickets)
        blocked = evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight, tickets=wrong,
        )
        self.assertNotEqual("ACCEPTED", blocked.acceptance_state)
        self.assertEqual(
            {"REPORT_DIGEST_NOT_TICKET_CONSUMED"},
            {reason for _key, reason in blocked.invalidated_report_reasons},
        )

    def test_csm_9_a_ticket_claiming_consumption_without_a_digest_is_self_refuting(self):
        # The reason matters, not only the refusal. A self-refuting ticket is
        # dropped by index_tickets and stops existing, so its reports report
        # NO_MATCHING_TICKET; without that guard the ticket would survive and
        # be refused one layer later by predicate 11. Both fail closed, so
        # asserting only "not ACCEPTED" left the guard untested -- mutation
        # M52 survived the first matrix run for exactly that reason.
        scenario = self.scenario
        reports = [scenario.report(gate) for gate in LEDGER_GATES]
        tickets = tuple(
            scenario.ticket(report.gate_id, status="consumed", ticket_seq=1)
            for report in reports
        )
        self.assertEqual(
            "TICKET_CONSUMED_WITHOUT_DIGEST",
            ticket_self_consistency_reason(tickets[0]),
        )
        result = evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight, tickets=tickets,
        )
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"NO_MATCHING_TICKET"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    # -- crash consistency between the store's two writes ----------------
    #
    # submit() persists the report and appends the ticket's consumption record
    # as two separate filesystem steps. A crash between them leaves durable
    # evidence that no ticket ever authorised, and reading "issued" as "not yet
    # spent, therefore fine" let that state derive ACCEPTED with no invalidation
    # reasons at all. reports.put() below is exactly the first of those two
    # writes, so these tests reproduce the window rather than simulate it.

    def crash_window(self, gates=LEDGER_GATES):
        """Reports durable, consumption records never appended."""
        self.issue()
        for gate in gates:
            self.controller.reports.put(self.scenario.report(gate))

    def test_csm_11_a_persisted_report_whose_ticket_was_never_consumed_is_refused(self):
        self.crash_window()
        self.assertEqual(
            len(LEDGER_GATES), len(self.controller.stored_reports(
                self.scenario.execution.execution_id))
        )
        self.assertEqual(
            {"issued"}, {t.status for t in self.controller.stored_tickets()}
        )
        result = self.derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"TICKET_NOT_CONSUMED"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_csm_12_an_interrupted_round_recovers_when_consumption_is_appended(self):
        # The window is recoverable, not terminal: replaying the missing append
        # makes the same reports admissible. That is what separates an
        # interrupted round from a forged one, and it is why this fails closed
        # rather than invalidating the round outright.
        self.crash_window()
        self.assertNotEqual("ACCEPTED", self.derive().acceptance_state)

        for gate in LEDGER_GATES:
            report = self.scenario.report(gate)
            self.controller.tickets.consume(
                report.ticket_id, report_digest(report), "2026-09-06T00:00:05Z"
            )
        recovered = self.derive()
        self.assertEqual(
            "ACCEPTED", recovered.acceptance_state, recovered.invalidated_report_reasons
        )
        self.assertEqual((), recovered.invalidated_report_reasons)

    def test_csm_13_a_partial_crash_refuses_only_the_unconsumed_gate(self):
        # One gate left in the window, the rest answered properly. The run must
        # not accept, and the reason must name the crash rather than something
        # generic -- otherwise a real interruption is indistinguishable from a
        # gate nobody ever ran.
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        self.controller.reports.put(self.scenario.report("V3"))
        result = self.derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"TICKET_NOT_CONSUMED"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_csm_14_the_crash_guard_does_not_replace_the_digest_comparison(self):
        # Both halves of predicate 11 have to hold independently. A ticket that
        # is consumed but records the wrong digest must still be refused, and
        # for the digest reason -- if the new status guard were doing this work
        # the mismatch protection could rot away unnoticed.
        scenario = self.scenario
        reports = [scenario.report(gate) for gate in LEDGER_GATES]
        wrong = tuple(
            scenario.ticket(
                report.gate_id,
                status="consumed",
                consumed_report_digest="0" * 64,
                consumed_at="2026-09-06T00:00:01Z",
                ticket_seq=1,
            )
            for report in reports
        )
        result = evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight, tickets=wrong,
        )
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"REPORT_DIGEST_NOT_TICKET_CONSUMED"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_csm_15_a_correctly_consumed_round_still_accepts(self):
        # The control for all four above. A guard that also refused honest
        # rounds would close the window by closing the loop.
        scenario = self.scenario
        reports = [scenario.report(gate) for gate in LEDGER_GATES]
        result = evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight,
            tickets=consumed_against(
                scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
            ),
        )
        self.assertEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_csm_10_two_conflicting_consumptions_invalidate_the_ticket(self):
        # csm_4 proves the outcome but not this mechanism: with both reports on
        # disk the duplicate-ticket guard rejects them first, so the fold could
        # pick a winner and nothing would notice (mutation M45 survived on
        # exactly that masking). Here the losing report is deleted, which is
        # what an attacker would do, leaving the ticket ledger as the only
        # remaining evidence that two answers were given.
        self.issue()
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        passing = self.scenario.report("V3")
        failing = self.scenario.report(
            "V3", result="FAIL", failure_observations=(self.fx["freeze_pane_failure"],)
        )
        self.controller.submit(passing)
        failing_digest = self.controller.submit(failing)
        self.report_path(failing_digest).unlink()

        ticket = {t.gate_id: t for t in self.controller.stored_tickets()}["V3"]
        self.assertEqual("invalidated", ticket.status)
        self.assertIsNone(ticket.consumed_report_digest)

        result = self.derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "TICKET_NOT_OPEN",
            {reason for _key, reason in result.invalidated_report_reasons},
        )


# ---------------------------------------------------------------------------
# BASE-1..BASE-9: the frozen attestation outranks a self-declared base_result
# ---------------------------------------------------------------------------


class BaselineAttestationPrecedenceTests(unittest.TestCase):
    """A checker may corroborate the frozen baseline. It may not overrule it."""

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])

    def evidence(self, observation, scenario=None):
        scenario = scenario or self.scenario
        report = scenario.report("V3", result="FAIL", failure_observations=(observation,))
        return base_evidence(observation, scenario.execution, scenario.bundle, report)

    def outcome(self, observation, scenario=None):
        scenario = scenario or self.scenario
        reports = [scenario.report(gate) for gate in LEDGER_GATES]
        reports[3] = scenario.report("V3", result="FAIL", failure_observations=(observation,))
        return evaluate(
            scenario.task, scenario.execution, scenario.bundle, reports,
            preflight=scenario.preflight,
            tickets=consumed_against(
                scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
            ),
        )

    def attesting(self, *signatures):
        return rebundle(
            self.scenario,
            baseline=replace(
                self.scenario.bundle.baseline, known_baseline_failures=signatures
            ),
        )

    def test_base_1_attestation_says_pass_and_the_report_claims_fail(self):
        # The blocking finding as a test: an attestation covering this
        # base_sha says the signature passed; the report says it failed. That
        # is a contradiction, not a measurement.
        result, reason = self.evidence(
            FailureObservation(
                signature="freeze_pane_mismatch",
                base_result="FAIL",
                base_signature="freeze_pane_mismatch",
            )
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("BASE_EVIDENCE_CONTRADICTION", reason)

    def test_base_1b_the_contradiction_is_not_a_test_edit_licence(self):
        outcome = self.outcome(
            FailureObservation(
                signature="freeze_pane_mismatch",
                base_result="FAIL",
                base_signature="freeze_pane_mismatch",
            )
        )
        self.assertNotEqual("REPAIR_TEST_ONLY", outcome.next_action)
        self.assertNotIn("TEST_DEFECT", outcome.admitted_failure_classes)

    def test_base_2_attestation_says_fail_and_the_report_claims_pass(self):
        result, reason = self.evidence(
            FailureObservation(signature="freeze_pane_mismatch", base_result="PASS"),
            scenario=self.attesting("freeze_pane_mismatch"),
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("BASE_EVIDENCE_CONTRADICTION", reason)

    def test_base_2b_a_contradicted_regression_claim_is_not_a_regression(self):
        outcome = self.outcome(
            FailureObservation(signature="freeze_pane_mismatch", base_result="PASS"),
            scenario=self.attesting("freeze_pane_mismatch"),
        )
        self.assertNotIn("REGRESSION", outcome.admitted_failure_classes)

    def test_base_3_agreement_on_pass_is_the_attested_answer(self):
        observation = FailureObservation(
            signature="freeze_pane_mismatch", base_result="PASS"
        )
        result, reason = self.evidence(observation)
        self.assertEqual("PASS", result)
        self.assertEqual("ATTESTED_ABSENT_FROM_BASELINE_FAILURES", reason)
        outcome = self.outcome(observation)
        self.assertIn("REGRESSION", outcome.admitted_failure_classes)
        self.assertEqual("REPAIR", outcome.next_action)

    def test_base_4_agreement_on_fail_admits_td_1(self):
        scenario = self.attesting("flaky_thing")
        observation = FailureObservation(
            signature="flaky_thing", base_result="FAIL", base_signature="flaky_thing"
        )
        result, reason = self.evidence(observation, scenario=scenario)
        self.assertEqual("FAIL", result)
        self.assertEqual("ATTESTED_KNOWN_BASELINE_FAILURE", reason)
        klass, determination = classify_observation(
            observation, "V3", scenario.bundle, scenario.execution,
            scenario.report("V3", result="FAIL", failure_observations=(observation,)),
        )
        self.assertEqual("TEST_DEFECT", klass)
        self.assertEqual("NOT_REGRESSION", determination)

    def test_base_5_a_claim_with_nothing_frozen_to_corroborate_it_is_unknown(self):
        # An attestation naming no attesting report proves nothing by absence,
        # so a bare claim has no corroboration at all.
        scenario = rebundle(
            self.scenario,
            baseline=replace(self.scenario.bundle.baseline, attested_by_report_digest=""),
        )
        result, reason = self.evidence(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="flaky_thing"
            ),
            scenario=scenario,
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("BASE_RESULT_UNATTESTED", reason)

    def test_base_5b_no_baseline_at_all_is_unknown(self):
        result, reason = self.evidence(
            FailureObservation(signature="flaky_thing", base_result="FAIL"),
            scenario=rebundle(self.scenario, baseline=None),
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("NO_BASELINE_ATTESTATION", reason)

    def test_base_6_a_different_environment_cannot_be_compared(self):
        scenario = rebundle(
            self.scenario,
            baseline=replace(
                self.scenario.bundle.baseline,
                environment_fingerprint=replace(
                    self.scenario.bundle.baseline.environment_fingerprint,
                    checkout_path_length=200,
                ),
            ),
        )
        result, reason = self.evidence(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="flaky_thing"
            ),
            scenario=scenario,
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("ENVIRONMENT_NOT_EQUIVALENT", reason)

    def test_base_7_a_different_base_sha_cannot_be_compared(self):
        scenario = rebundle(
            self.scenario, baseline=replace(self.scenario.bundle.baseline, base_sha="f" * 40)
        )
        result, reason = self.evidence(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="flaky_thing"
            ),
            scenario=scenario,
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("BASELINE_SHA_MISMATCH", reason)

    def test_base_8_a_claim_about_a_different_failure_is_incoherent(self):
        # TD-1 requires the *same* normalised signature at base. A claim whose
        # base_signature names another failure is not evidence about this one.
        result, reason = self.evidence(
            FailureObservation(
                signature="flaky_thing", base_result="FAIL", base_signature="something_else"
            ),
            scenario=self.attesting("flaky_thing"),
        )
        self.assertEqual("UNKNOWN", result)
        self.assertEqual("BASE_SIGNATURE_MISMATCH", reason)

    def test_base_9_an_observation_claiming_nothing_reads_the_attestation(self):
        result, reason = self.evidence(FailureObservation(signature="freeze_pane_mismatch"))
        self.assertEqual("PASS", result)
        self.assertEqual("ATTESTED_ABSENT_FROM_BASELINE_FAILURES", reason)


# ---------------------------------------------------------------------------
# TASK-1..TASK-3: cross-Task evidence reuse (review finding N-1)
# ---------------------------------------------------------------------------


class CrossTaskEvidenceReuseTests(unittest.TestCase):
    """``task_id`` is not in the ticket-id hash, so the report check is the barrier.

    The reviewer's mutant R3 (delete ``REPORT_TASK_ID_MISMATCH``) survived all
    195 Phase B-2 tests. These are the tests that kill it, and they are
    deliberately single-factor: the ticket is made *consistent* with the
    borrowed report so ``TICKET_TASK_MISMATCH`` cannot fire first and pass the
    suite for the wrong reason.
    """

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.a = fx["scenario"](fx["bundle_with_clause"])
        self.b = replace(
            self.a,
            task=replace(self.a.task, task_id="T-LED-02"),
            execution=replace(self.a.execution, task_id="T-LED-02"),
        )

    def test_task_1_two_tasks_share_a_ticket_id(self):
        # Not a defect on its own -- it is the reason the report-level check
        # must exist, and the reason this suite has to lock it.
        self.assertEqual(self.a.ticket("V3").ticket_id, self.b.ticket("V3").ticket_id)

    def test_task_2_task_as_evidence_cannot_be_spent_on_task_b(self):
        reports = [self.a.report(gate) for gate in LEDGER_GATES]
        # Tickets carry Task A's id, matching the reports exactly, so every
        # ticket-level check passes and only the report/execution comparison
        # is left standing.
        tickets = consumed_against(
            self.a.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
        )
        result = evaluate(
            self.b.task, self.b.execution, self.b.bundle, reports,
            preflight=self.b.preflight, tickets=tickets,
        )
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"REPORT_TASK_ID_MISMATCH"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_task_3_the_control_still_accepts_within_one_task(self):
        reports = [self.b.report(gate) for gate in LEDGER_GATES]
        result = evaluate(
            self.b.task, self.b.execution, self.b.bundle, reports,
            preflight=self.b.preflight,
            tickets=consumed_against(
                self.b.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
            ),
        )
        self.assertEqual("ACCEPTED", result.acceptance_state)


# ---------------------------------------------------------------------------
# LEASE-1..LEASE-5: the worktree lease is compared, not merely recorded
# ---------------------------------------------------------------------------


class WorktreeLeaseTests(LedgerTestCase):
    """Head equality is not worktree identity (review finding N-2)."""

    def preflight(self, **overrides):
        return replace(self.scenario.preflight, **overrides)

    def rounded(self, preflight):
        self.honest_round()
        return self.controller.derive(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            preflight=preflight,
        )

    def test_lease_1_a_generation_change_at_identical_head_invalidates_the_round(self):
        result = self.rounded(self.preflight(worktree_generation=WORKTREE_GENERATION + 1))
        self.assertEqual("ROUND_INVALIDATED", result.acceptance_state)
        self.assertIn(
            "WORKTREE_GENERATION_CHANGED_DURING_ROUND",
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_lease_2_a_lock_id_change_at_identical_head_invalidates_the_round(self):
        result = self.rounded(self.preflight(worktree_lock_id="repo-a-different-worktree"))
        self.assertEqual("ROUND_INVALIDATED", result.acceptance_state)
        self.assertIn(
            "WORKTREE_LOCK_ID_CHANGED_DURING_ROUND",
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_lease_3_a_stale_generation_invalidates_the_round(self):
        # The ticket was issued under a newer lease than the one now held.
        self.issue(worktree_generation=WORKTREE_GENERATION + 5)
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        result = self.derive()
        self.assertEqual("ROUND_INVALIDATED", result.acceptance_state)

    def test_lease_4_a_matching_lease_accepts(self):
        result = self.rounded(self.scenario.preflight)
        self.assertEqual("ACCEPTED", result.acceptance_state, result.invalidated_report_reasons)

    def test_lease_5_an_unreported_lease_is_not_a_matching_lease(self):
        # "We could not check" must never read as "it was fine" -- the rule
        # PreflightFacts already states for the production marker.
        result = self.rounded(self.preflight(worktree_lock_id=None, worktree_generation=None))
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "WORKTREE_LEASE_NOT_REPORTED",
            {reason for _key, reason in result.invalidated_report_reasons},
        )


# ---------------------------------------------------------------------------
# SC-1..SC-3: self-contradictory reports (review finding N-3)
# ---------------------------------------------------------------------------


class ReportSelfContradictionTests(unittest.TestCase):
    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])

    def run_with(self, reports):
        return evaluate(
            self.scenario.task, self.scenario.execution, self.scenario.bundle, reports,
            preflight=self.scenario.preflight,
            tickets=consumed_against(
                self.scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
            ),
        )

    def test_sc_1_a_pass_carrying_failures_is_inadmissible(self):
        reports = [self.scenario.report(gate) for gate in LEDGER_GATES]
        reports[3] = self.scenario.report(
            "V3",
            result="PASS",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )
        result = self.run_with(reports)
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "REPORT_SELF_CONTRADICTION",
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_sc_2_a_pass_with_no_failures_still_accepts(self):
        reports = [self.scenario.report(gate) for gate in LEDGER_GATES]
        self.assertEqual("ACCEPTED", self.run_with(reports).acceptance_state)

    def test_sc_3_a_fail_carrying_failures_is_still_admissible(self):
        reports = [self.scenario.report(gate) for gate in LEDGER_GATES]
        reports[3] = self.scenario.report(
            "V3", result="FAIL", failure_observations=(self.fx["freeze_pane_failure"],)
        )
        result = self.run_with(reports)
        self.assertEqual("REPAIR", result.next_action)
        self.assertEqual((), result.invalidated_report_reasons)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
