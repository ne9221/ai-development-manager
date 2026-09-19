"""Round 7: the decisive-label list was a list, and two channels disagreed.

Grok 4.6's fresh independent review rejected ``899d383e`` with four findings.
Every one was reproduced at that commit before anything here was written
(scratchpad ``repro_r7.py``; the measured numbers are quoted in each group).

Round 6 replaced rejection *vocabulary* with decision *shape*, which was right.
But one of the two shapes it wrote down stayed a literal list. The copula form
(``My current decision is to reject``) was built as a small grammar --
``(owner) (modifier)? (noun)`` -- while the label form (``My current decision:
reject``) was a tuple of twenty-six strings. So the same decision, written with
a colon instead of a verb, was invisible:

* **R6-IR-1 (HIGH)** ``decision_statements`` read none of ``My current
  decision:``, ``My final decision:``, ``My overall decision:``, ``My review
  decision:``, ``The current decision:`` or ``Our current decision:``. With a
  bound structured PASS beside them, **9 of 12** measured cases reached
  MARK_COMPLETE, including every one the reviewer named. The fix is the grammar
  the copula form already had, applied to the label form -- **not** six more
  strings in the tuple.
* **R6-IR-2 (MEDIUM)** the decision object's own ``summary`` never reached the
  statement parser, so a bound ``PASS`` carrying ``summary: "Current decision:
  reject"`` completed, 3 of 3. A summary still authorizes nothing; it may only
  withdraw the PASS it sits inside.
* **R6-IR-3 (MEDIUM)** Round 6 kept a wrong-channel fence out of *structured*
  authority but left its body in the withdrawal prose region, so a bound PASS
  beside an ordinary ```json fence quoting ``"verdict": "REJECT"`` stalled --
  5 of 5 non-authoritative fences produced SEND_TO_REVIEW or HUMAN_GATE. That is
  the stall TRUST-MODEL section 6.4 forbids in writing.
* **R6-IR-4** ``classify.review_proof`` passed the reviewer's decision
  statements to ``contracts.review_authority`` and ``classify.signals_for`` did
  not, so the two paths asked the same question and could get different answers.

The through-line is the Round 2-4 lesson, one layer up: a finite grammar is a
contract, and a list of strings is a guess about what people will type. Nothing
here widens the rejection vocabulary -- ``_REJECT_VALUES`` is untouched.

As in Rounds 5 and 6, every security-sensitive case ends at a planner action.
A helper that returns False is not a gate; MARK_COMPLETE being unreachable is.
"""

import json
import unittest

from manager.nextplan import contracts
from manager.nextplan import harness as h
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import completion_proof, review_authority_for
from manager.nextplan.extract import decision_statements, extract
from manager.nextplan.planner import new_task_state, plan


# -- fixtures ------------------------------------------------------------------

EXPECTATION = {"target_sha": h.HEAD, "reviewer_run_id": h.REVIEWER_RUN,
               "provider": h.PROVIDER, "job_id": h.JOB_ID}


def working(**changes):
    state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
    state.update(state=v.WORKING, phase_owner={"role": v.WORKER, "session_id": None})
    state.update(changes)
    return state


def reviewing(**changes):
    candidate = {"head_sha": h.HEAD, "proof": completion_proof(h.worker_result(), {}),
                 "worker_session": h.WORKER_SESSION}
    state = working(state=v.AWAITING_REVIEW, phase_owner={"role": v.REVIEWER, "session_id": None},
                    worker_session=h.WORKER_SESSION, worker_sessions=[h.WORKER_SESSION], candidate=candidate,
                    review_dispatch=h.review_dispatch(), generation=1)
    state.update(changes)
    return state


def review(prose="", decision=None, fence="adm-review-result", role=v.REVIEWER):
    """One reviewer message: prose, a result payload, and optionally a decision."""
    from manager.nextplan import result as r

    payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS",
               "review_verdict": "PASS", "reviewed_sha": h.HEAD}
    block = ""
    if decision is not None:
        for item in (decision if isinstance(decision, list) else [decision]):
            block += "\n```%s\n%s\n```\n" % (fence, json.dumps(item))
    got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": role, "format": "text",
                   "session_id": h.REVIEWER_SESSION,
                   "content": prose + "\n\n```adm-result\n" + json.dumps(payload) + "\n```\n" + block})
    got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
    return got


def decide(prose="", decision=None, fence="adm-review-result"):
    got = review(prose, decision, fence)
    return plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))


def completes(prose="", decision=None, fence="adm-review-result"):
    return decide(prose, decision, fence)["action"] == v.MARK_COMPLETE


def bound_pass(**changes):
    """The bound PASS a compliant reviewer returns: the thing under attack here."""
    block = h.review_decision()
    block.update(changes)
    return block


# -- Group 1: the decisive label is a grammar ----------------------------------

class GroupOneDecisionFieldGrammar(unittest.TestCase):
    """R6-IR-1 (HIGH). Measured at 899d383e: 9 of 12 completed."""

    # The six the reviewer named. Each is an ordinary decision field, and each
    # read as zero statements at 899d383e.
    NAMED = (
        "My current decision: reject",
        "My final decision: reject",
        "My overall decision: reject",
        "My review decision: reject",
        "The current decision: reject",
        "Our current decision: reject",
    )
    # Combinations that were NOT in the reviewer's corpus. A grammar has to
    # close these too; a list of strings closes exactly what was listed, which
    # is how a fix becomes the next round's finding.
    MUTATION = (
        "Our final recommendation: reject",
        "The overall disposition: do not complete",
        "My reviewer verdict: reject",
        "Final outcome: send back for revision",
        "Our overall verdict: changes required",
        "The final disposition: do not merge",
        "Its review outcome: rejected",
        "My official decision: not approved",
    )
    # Already read at 899d383e. They must not regress while the grammar is built
    # around them.
    ALREADY_READ = (
        "Current decision: reject",
        "My decision: reject",
        "Verdict: reject",
        "Final call: do not complete",
    )

    def test_every_named_decision_field_is_read(self):
        for prose in self.NAMED:
            with self.subTest(prose):
                statements = decision_statements(prose)
                self.assertEqual(1, len(statements), statements)
                self.assertEqual(contracts.STATEMENT_REJECT, statements[0]["polarity"])

    def test_combinations_outside_the_review_corpus_are_read_too(self):
        """Read as a decision. Not necessarily read as a *rejection*.

        "Final outcome: send back for revision" maps to ``unreadable``, because
        "send back for revision" is not in ``_REJECT_VALUES`` and this round
        does not put it there. That is the Round-6 rule working as designed: an
        unmappable value in a decisive field is a conflict, not silence, so it
        blocks for the same reason and the vocabulary stays closed.
        """
        for prose in self.MUTATION:
            with self.subTest(prose):
                statements = decision_statements(prose)
                self.assertTrue(statements, "no decision statement read")
                self.assertIn(statements[0]["polarity"],
                              (contracts.STATEMENT_REJECT, contracts.STATEMENT_UNREADABLE))

    def test_none_of_them_completes_beside_a_bound_pass(self):
        """The finding itself: a bound PASS plus an explicit reject field."""
        for prose in self.NAMED + self.MUTATION + self.ALREADY_READ:
            with self.subTest(prose):
                self.assertFalse(completes(prose, decision=bound_pass()))

    def test_the_contradiction_is_a_conflict_not_a_silence(self):
        for prose in self.NAMED + self.MUTATION:
            with self.subTest(prose):
                got = review(prose, decision=bound_pass())
                authority = review_authority_for(got, {
                    "candidate_sha": h.HEAD, "review_dispatch": h.review_dispatch()})
                self.assertFalse(authority["authorized"])
                self.assertEqual(contracts.CONFLICT, authority["reason"])

    def test_an_unreadable_value_in_a_grammatical_field_still_conflicts(self):
        """The Round-6 rule, now reaching the fields the grammar added."""
        for prose in ("My current decision: see the findings below",
                      "Our final disposition: pending further discussion",
                      "The overall outcome:"):
            with self.subTest(prose):
                statements = decision_statements(prose)
                self.assertTrue(statements, "no decision statement read")
                self.assertEqual(contracts.STATEMENT_UNREADABLE, statements[0]["polarity"])
                self.assertFalse(completes(prose, decision=bound_pass()))

    def test_a_genuine_approval_in_the_same_grammar_still_completes(self):
        """A grammar that only ever blocks is a wall, not a contract."""
        for prose in ("My current decision: approve",
                      "Our final decision: pass",
                      "The overall verdict: approved",
                      "My review decision: LGTM",
                      "Final outcome: accepted"):
            with self.subTest(prose):
                statements = decision_statements(prose)
                self.assertEqual([contracts.STATEMENT_APPROVE],
                                 [s["polarity"] for s in statements], statements)
                self.assertTrue(completes(prose, decision=bound_pass()))


class GroupOneBOrdinaryProseIsNotADecision(unittest.TestCase):
    """The other side of R6-IR-1: a noun phrase is not a decision field."""

    # Ordinary review sentences that contain a decision noun without announcing
    # a decision. A grammar that reads these is a grammar that stalls every
    # review, which is the failure mode on the far side of fail-closed.
    NOT_DECISIONS = (
        "Background: our review of the architecture took two days.",
        "The recommendation engine module was refactored.",
        "Note: the decision table is generated from the atlas.",
        "Scope: current decision paths are covered by the new tests.",
        "Files changed: manager/nextplan/decision_support.py",
        "The outcome variable is now typed.",
        "Coverage: final outcome handling is exercised end to end.",
        "My review took longer than estimated.",
        "Our recommendation engine benchmarks improved by 12%.",
    )

    def test_a_decision_noun_in_ordinary_prose_reads_as_nothing(self):
        for prose in self.NOT_DECISIONS:
            with self.subTest(prose):
                self.assertEqual([], decision_statements(prose))

    def test_and_therefore_a_genuine_approval_still_completes(self):
        for prose in self.NOT_DECISIONS:
            with self.subTest(prose):
                self.assertTrue(completes(prose, decision=bound_pass()))

    def test_the_owner_form_inherits_the_bare_forms_treatment(self):
        """The one measured widening of over-refusal, recorded rather than hidden.

        "Recommendation: add a regression test." already read as a decision ADM
        cannot map at 899d383e, because "recommendation" was already a decisive
        label. Round 7 makes "Our recommendation: ..." read the same way, which
        is the grammar doing its job: the owner form is the same field with a
        possessive in front of it, and it is now treated as the same field.

        A full sweep of ordinary reviewer prose (scratchpad ``fp_sweep.py``)
        found exactly three differences between base and head, and this is the
        only one that is not a finding being closed. It fails closed: it costs a
        round, never a completion.
        """
        for bare, owned in (("Recommendation: add a regression test.",
                             "Our recommendation: add a regression test."),
                            ("Outcome: still investigating.",
                             "The overall outcome: still investigating.")):
            with self.subTest(owned):
                self.assertEqual([s["polarity"] for s in decision_statements(bare)],
                                 [s["polarity"] for s in decision_statements(owned)])

    def test_reporting_nouns_are_not_promoted_by_a_modifier(self):
        """The other side of it: a count is not a decision, however it is labelled."""
        for prose in ("Final result: 3 passed, 0 failed.",
                      "Result: 12 passed.",
                      "Final status: all green.",
                      "Overall: this is a good change.",
                      "Test outcome: 12 passed."):
            with self.subTest(prose):
                self.assertEqual([], decision_statements(prose))

    def test_the_rejection_vocabulary_was_not_widened(self):
        """Rounds 2-4 were lost by adding wordings. This round adds none."""
        from manager.nextplan import extract as extract_mod

        for token in ("send back for revision", "not complete", "another look",
                      "do not sign", "come back"):
            with self.subTest(token):
                self.assertNotIn(token, extract_mod._REJECT_VALUES)


# -- Group 2: the summary may withdraw the PASS it sits inside -----------------

class GroupTwoSummaryContradiction(unittest.TestCase):
    """R6-IR-2 (MEDIUM). Measured at 899d383e: 3 of 3 completed."""

    CONTRADICTING = (
        "Current decision: reject",
        "My final decision: reject",
        "Verdict: changes required",
        "I reject this patch.",
        "My overall decision: do not merge",
    )

    def test_a_contradicting_summary_does_not_complete(self):
        for summary in self.CONTRADICTING:
            with self.subTest(summary):
                self.assertFalse(completes("", decision=bound_pass(summary=summary)))

    def test_it_is_a_conflict_and_never_an_authorization(self):
        for summary in self.CONTRADICTING:
            with self.subTest(summary):
                got = review("", decision=bound_pass(summary=summary))
                authority = review_authority_for(got, {
                    "candidate_sha": h.HEAD, "review_dispatch": h.review_dispatch()})
                self.assertFalse(authority["authorized"])
                self.assertIn(authority["reason"], (contracts.CONFLICT, contracts.INVALID))

    def test_an_unreadable_decision_field_in_the_summary_conflicts_too(self):
        """Round 4's rule, applied where the summary lives."""
        for summary in ("Decision: see the findings", "My final verdict: to be determined"):
            with self.subTest(summary):
                self.assertFalse(completes("", decision=bound_pass(summary=summary)))

    # The contract the reviewer wrote down: a summary is human prose. It must
    # not become a second rejection scanner, or every honest summary that
    # mentions a resolved problem stalls the task.
    ORDINARY = (
        "The previous blocker was fixed.",
        "This review rejects the old approach, but the submitted patch now satisfies the contract.",
        "No blocking issues remain.",
        "Two earlier rounds were rejected; this one is clean.",
        "The failing test now passes.",
        "Nothing blocks completion.",
    )

    def test_an_ordinary_summary_does_not_stall_the_task(self):
        for summary in self.ORDINARY:
            with self.subTest(summary):
                self.assertTrue(completes("", decision=bound_pass(summary=summary)))

    def test_an_ordinary_summary_reads_as_no_decision_at_all(self):
        """Same parser as the prose rule -- deliberately not a second one."""
        for summary in self.ORDINARY:
            with self.subTest(summary):
                self.assertEqual([], decision_statements(summary))

    def test_an_approving_summary_cannot_upgrade_anything(self):
        """A summary withdraws. It has never been able to grant."""
        unbound = bound_pass(reviewer_run_id="run-not-ours", summary="Decision: approve")
        authority = contracts.review_authority([unbound], EXPECTATION)
        self.assertFalse(authority["authorized"])
        self.assertFalse(authority["blocked"])

    def test_a_summary_on_an_unbound_decision_cannot_block_either(self):
        """Or a forged block with a rejecting summary would stall any task."""
        unbound = bound_pass(reviewer_run_id="run-not-ours", summary="Decision: reject")
        authority = contracts.review_authority([unbound], EXPECTATION)
        self.assertFalse(authority["authorized"])
        self.assertFalse(authority["blocked"])

    def test_the_documented_optional_summary_still_validates(self):
        """R6-F3 closed the object; this must not reopen or narrow it."""
        self.assertEqual([], contracts.review_problems(bound_pass(summary="All clear.")))


# -- Group 3: a wrong-channel fence is quotation, in both directions ----------

class GroupThreeWrongChannelIsInert(unittest.TestCase):
    """R6-IR-3 (MEDIUM). Measured at 899d383e: 5 of 5 stalled."""

    # Each of these is a reviewer quoting something -- a schema example, a
    # transcript, another tool's output. TRUST-MODEL 6.4 says a decision in the
    # wrong fence neither authorizes nor blocks. Round 6 implemented the first
    # half only.
    QUOTATIONS = {
        "json": '```json\n{"verdict": "REJECT"}\n```',
        "yaml": '```yaml\nverdict: REJECT\n```',
        "text": '```text\nCurrent decision: reject\n```',
        "markdown": '```markdown\nI reject this patch.\n```',
        "example": '```example\nThe reviewer rejects this change.\n```',
        "bare": '```\nTwo blockers remain.\n```',
        "console": '```console\n$ adm review --verdict REJECT\n```',
    }

    def test_a_quoted_rejection_does_not_stall_a_genuine_approval(self):
        for lang, fence in self.QUOTATIONS.items():
            with self.subTest(lang):
                self.assertTrue(completes(fence, decision=bound_pass()))

    def test_a_quoted_rejection_is_not_collected_as_a_statement(self):
        for lang, fence in self.QUOTATIONS.items():
            with self.subTest(lang):
                got = review(fence, decision=bound_pass())
                self.assertEqual([], got.get("decision_statements") or [])

    def test_a_quoted_rejection_is_not_collected_as_authority_either(self):
        """The half Round 6 already did. It must stay done."""
        for lang in ("json", "yaml", "text", "markdown", "example"):
            with self.subTest(lang):
                got = review("", decision=bound_pass(), fence=lang)
                self.assertEqual([], got["decisions"])
                self.assertFalse(completes("", decision=bound_pass(), fence=lang))

    def test_the_reviewers_own_voice_outside_a_fence_still_withdraws(self):
        """The rule is about the channel, not about weakening withdrawal."""
        for prose in ("Current decision: reject",
                      "I reject this patch.",
                      "Two blockers remain."):
            with self.subTest(prose):
                self.assertFalse(completes(prose, decision=bound_pass()))

    def test_a_fence_cannot_hide_a_decision_that_would_otherwise_be_read(self):
        """The Round-3 bypass, now closed by the channel rather than by the net.

        Round 3 kept fenced text in the withdrawal region because a payload
        ``review_verdict: PASS`` was still a claim worth withdrawing. Since
        Round 5 a payload cannot authorize anything, so there is nothing left for
        a fenced rejection to withdraw -- and the reviewer's real decision has a
        channel of its own, which a fence is not.
        """
        got = review('```json\n{"verdict": "REJECT"}\n```', decision=None)
        self.assertEqual([], got["decisions"])
        self.assertFalse(completes('```json\n{"verdict": "REJECT"}\n```', decision=None))

    def test_an_unclosed_fence_cannot_swallow_the_reviewers_own_rejection(self):
        """A hole this rule would otherwise have opened, closed alongside it.

        Only CLOSED fences are quotation. An unclosed one is not a quotation but
        a malformed one, and its body runs to the end of the message -- so if
        ``unfenced`` skipped it too, a reviewer could bury its own rejection by
        opening a fence and never closing it, and the bound PASS beside it would
        stand. Reading an unclosed fence costs a round at worst; skipping it
        costs a completion.
        """
        from manager.nextplan import result as r

        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1",
                   "status": "PASS", "review_verdict": "PASS", "reviewed_sha": h.HEAD}
        content = ("Review complete.\n\n"
                   "```adm-result\n" + json.dumps(payload) + "\n```\n\n"
                   "```adm-review-result\n" + json.dumps(bound_pass()) + "\n```\n\n"
                   "```json\n"                      # opened, never closed
                   '{"example": true}\n\n'
                   "Current decision: reject\n")
        got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": v.REVIEWER,
                       "format": "text", "session_id": h.REVIEWER_SESSION, "content": content})
        got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
        self.assertTrue(got["decision_statements"], "the rejection was swallowed")
        action = plan(reviewing(), h.event(got, role=v.REVIEWER,
                                           session_id=h.REVIEWER_SESSION, generation=1))["action"]
        self.assertNotEqual(v.MARK_COMPLETE, action)

    def test_the_authoritative_fence_still_fails_closed(self):
        """Narrowing the wrong channel must not soften the right one."""
        blocking = [{"severity": "high", "blocking": True, "summary": "s", "detail": "d"}]
        for name, decision in (
                ("reject", bound_pass(verdict="REJECT")),
                ("pass with a blocking finding", bound_pass(findings=blocking)),
                ("unknown verdict", bound_pass(verdict="MAYBE")),
                ("unknown field", dict(bound_pass(), can_merge=False)),
                ("wrong run", bound_pass(reviewer_run_id="run-not-ours")),
        ):
            with self.subTest(name):
                self.assertFalse(completes("", decision=decision))

    def test_a_malformed_authoritative_fence_still_blocks(self):
        got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": v.REVIEWER, "format": "text",
                       "session_id": h.REVIEWER_SESSION,
                       "content": "Review done.\n\n```adm-review-result\n{not json\n```\n"})
        got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
        action = plan(reviewing(), h.event(got, role=v.REVIEWER,
                                           session_id=h.REVIEWER_SESSION, generation=1))["action"]
        self.assertNotEqual(v.MARK_COMPLETE, action)

    def test_the_wrong_channel_is_still_reported_rather_than_dropped(self):
        for lang in ("json", "yaml", "text"):
            with self.subTest(lang):
                got = review("", decision=bound_pass(), fence=lang)
                self.assertTrue(any("adm-review-result" in w and "ignored" in w
                                    for w in got["extraction"]["warnings"]),
                                got["extraction"]["warnings"])


# -- Group 4: one authority invocation ----------------------------------------

class GroupFourAuthorityConvergence(unittest.TestCase):
    """R6-IR-4. Two call sites asked one question and could disagree."""

    CONTEXT = {"candidate_sha": h.HEAD, "review_dispatch": h.review_dispatch(),
               "reviewer_session": h.REVIEWER_SESSION, "worker_sessions": [h.WORKER_SESSION],
               "prior_reviewer_sessions": []}

    def test_both_paths_go_through_the_same_helper(self):
        """Not a style point: divergence here is what the finding was."""
        import inspect

        from manager.nextplan import classify

        for fn in (classify.review_proof, classify.signals_for):
            with self.subTest(fn.__name__):
                self.assertIn("review_authority_for", inspect.getsource(fn))
                self.assertNotIn("contracts.review_authority", inspect.getsource(fn))

    def test_the_proof_and_the_signals_agree_on_a_contradicted_pass(self):
        from manager.nextplan import classify

        for prose in ("My current decision: reject", "Current decision: reject"):
            with self.subTest(prose):
                got = review(prose, decision=bound_pass())
                proof = classify.review_proof(got, self.CONTEXT)
                bound = [item for item in proof if "bound reviewer decision" in item["requirement"]]
                self.assertEqual([False], [item["ok"] for item in bound])
                signals = classify.signals_for(got, None, {}, {"kind": "completed"}, self.CONTEXT)
                self.assertIn("extract.conflicting_statements", signals)

    def test_they_agree_on_a_genuine_approval_too(self):
        from manager.nextplan import classify

        got = review("The patch is correct.", decision=bound_pass())
        proof = classify.review_proof(got, self.CONTEXT)
        bound = [item for item in proof if "bound reviewer decision" in item["requirement"]]
        self.assertEqual([True], [item["ok"] for item in bound])
        signals = classify.signals_for(got, None, {}, {"kind": "completed"}, self.CONTEXT)
        self.assertNotIn("extract.conflicting_statements", signals)
        self.assertNotIn("result.review_verdict_fail", signals)

    def test_a_contradicted_pass_now_routes_as_a_contradiction(self):
        """Convergence has one visible consequence, and it is an improvement.

        At 899d383e the contradiction reached the planner through
        ``review_proof`` alone, so it fell through to the generic "review result
        neither failed nor approved a verified candidate" catch-all and
        HUMAN_GATE. With ``signals_for`` asking the same question, it now
        arrives as the atlas code written for exactly this --
        ``contradictory_result``, whose reviewer route is SEND_TO_REVIEW once
        and then HUMAN_GATE if it is not resolved.

        Neither route completes. The difference is that the second one is
        designed, and says in the record what went wrong.
        """
        decision = decide("My current decision: reject", decision=bound_pass())
        self.assertEqual(v.SEND_TO_REVIEW, decision["action"])
        self.assertIn("contradictory_result", decision["reason"])

    def test_the_helper_reads_the_statements_off_the_result(self):
        got = review("My final decision: reject", decision=bound_pass())
        self.assertTrue(got["decision_statements"], "extract recorded no statement")
        self.assertEqual(contracts.CONFLICT, review_authority_for(got, self.CONTEXT)["reason"])


# -- Group 5: the three fixes hold together -----------------------------------

class GroupFiveTrustContract(unittest.TestCase):

    def test_the_only_way_to_complete_is_still_a_clean_bound_pass(self):
        self.assertTrue(completes("The patch is correct and the tests are real.",
                                  decision=bound_pass()))

    def test_and_every_way_the_reviewer_can_say_otherwise_is_heard(self):
        """One decision, four places it can be written. All four must land."""
        for name, prose, decision in (
                ("prose label", "My current decision: reject", bound_pass()),
                ("prose copula", "My current decision is to reject the patch.", bound_pass()),
                ("first person", "I reject this patch.", bound_pass()),
                ("object summary", "", bound_pass(summary="My current decision: reject")),
        ):
            with self.subTest(name):
                self.assertFalse(completes(prose, decision=decision))

    def test_nothing_in_this_round_lets_prose_grant_anything(self):
        """The Round-5 asymmetry, re-checked after widening what prose can say."""
        for prose in ("My current decision: approve",
                      "Our final verdict: PASS",
                      "I approve this patch."):
            with self.subTest(prose):
                got = review(prose, decision=None)
                self.assertEqual([], got["decisions"])
                self.assertFalse(completes(prose, decision=None))

    def test_a_forged_statement_still_cannot_stall_a_task(self):
        """Statements are consulted only where a bound PASS already exists."""
        authority = contracts.review_authority(
            [], EXPECTATION, decision_statements("Decision: reject"))
        self.assertFalse(authority["authorized"])
        self.assertFalse(authority["blocked"])
        self.assertEqual(contracts.ABSENT, authority["reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
