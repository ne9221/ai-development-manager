"""Round 8: the published grammar was not the implemented grammar.

Grok's fresh independent review rejected ``57f8ed79`` (docs-only tip
``ec5da6f7``) with two HIGH surfaces of ONE parser defect. Both were reproduced
at ``ec5da6f7`` before anything here was written (scratchpad ``repro_r8.py``):
**7 of 7** attacks reached MARK_COMPLETE beside a bound PASS.

Round 7 published the decisive-field grammar as

    (owner)? (modifier)* (decision-noun)

with three closed sets, and then implemented it three different ways:

* **R7-IR-1a** ``_LABEL_LINE`` captured the label as ``[A-Za-z][A-Za-z \\-]{0,24}``
  -- twenty-five characters -- so ``My current official decision: reject`` (28)
  never reached ``_label_tier`` at all. The grammar was correct and never asked.
* **R7-IR-1b** the copula form required an owner and allowed ``{0,2}``
  modifiers, so ``Current decision is to reject`` and ``My current official
  final decision is to reject`` were invisible.
* **R7-IR-2** ``contracts._summary_statements`` correctly reuses the same
  collector, so it inherited the same hole: a bound PASS whose ``summary`` was
  ``My current official decision: reject`` completed.

The remediation is structural, not vocabulary: one grammar source
(``extract._field_grammar``) from which ``_DECISION_FIELD`` is compiled, used by
``_label_tier`` (full match) and pasted by reference into ``_COPULA_DECISION``.
The label capture has no length cap; what counts is decided only by the
explicit tables and that one compiled grammar. ``_REJECT_VALUES`` is
byte-for-byte unchanged and pinned below by hash.

As in Rounds 5-7, every security-sensitive case ends at a planner action.
"""

import hashlib
import inspect
import itertools
import json
import unittest

from manager.nextplan import contracts
from manager.nextplan import extract as extract_mod
from manager.nextplan import harness as h
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import review_authority_for
from manager.nextplan.extract import decision_statements, extract
from manager.nextplan.planner import plan
from manager.test_nextplan_round7_findings import (EXPECTATION, bound_pass, completes, decide,
                                                   review, reviewing)


# -- the closed sets, read from production so the matrix cannot drift ---------

OWNERS = tuple(sorted(extract_mod._LABEL_OWNERS))
MODIFIERS = tuple(sorted(extract_mod._LABEL_MODIFIERS))
NOUNS = tuple(sorted(extract_mod._DECISION_NOUNS))

# The sets as TRUST-MODEL 7.1 publishes them. If either side changes, this is
# where the change is noticed, and it is a reviewed change to the contract.
PUBLISHED_OWNERS = ("its", "my", "our", "the", "their")
PUBLISHED_MODIFIERS = ("considered", "current", "final", "formal", "official", "overall", "review", "reviewer")
PUBLISHED_NOUNS = ("decision", "disposition", "outcome", "recommendation", "verdict")

# _REJECT_VALUES at ec5da6f7 (53 entries). Rounds 2-4 were lost by widening it.
REJECT_VALUES_SHA256 = "1284e63ef16df8b2fa6d5f4be753817d84cbbe79c3bfeff5546244e14ca14737"


def _modifiers(count, offset):
    """``count`` distinct modifiers starting at ``offset`` -- generated, not named."""
    ring = itertools.cycle(MODIFIERS)
    for _ in range(offset % len(MODIFIERS)):
        next(ring)
    return tuple(itertools.islice(ring, count))


def compositions(counts=(0, 1, 2, 3, 5), owners=(None,) + OWNERS):
    """Every (owner?, modifiers, noun) field the matrix covers, as lowercase text.

    Modifier choice rotates with the noun and owner index so that different
    rows exercise different modifiers; the matrix is generated from the closed
    sets, deliberately not a hand-picked list of examples.
    """
    fields = []
    for owner_index, owner in enumerate(owners):
        for count in counts:
            for noun_index, noun in enumerate(NOUNS):
                words = ((owner,) if owner else ()) + _modifiers(count, owner_index + noun_index) + (noun,)
                fields.append({"owner": owner, "count": count, "noun": noun, "field": " ".join(words)})
    return fields


def colon(field, value="reject"):
    return "%s: %s" % (field[0].upper() + field[1:], value)


def copula(field, value="reject"):
    return "%s is to %s" % (field[0].upper() + field[1:], value)


def polarities(text):
    return [s["polarity"] for s in decision_statements(text)]


# -- Group 0: the reviewer's attacks, at the planner --------------------------

class GroupZeroReviewerAttacks(unittest.TestCase):
    """A1-A7 as Grok wrote them. Measured at ec5da6f7: 7 of 7 MARK_COMPLETE."""

    PROSE = (
        ("A1", "My current official decision: reject"),
        ("A2", "Our considered disposition: reject"),
        ("A3", "Their considered official recommendation: reject"),
        ("A4", "My current official final overall review decision: reject"),
        ("A5", "My current official final decision is to reject"),
        ("A6", "Current decision is to reject"),
    )
    SUMMARY = (("A7", "My current official decision: reject"),)

    def test_every_attack_is_read_as_an_explicit_rejection(self):
        for name, prose in self.PROSE + self.SUMMARY:
            with self.subTest(name):
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(prose))

    def test_no_attack_beside_a_bound_pass_completes(self):
        for name, prose in self.PROSE:
            with self.subTest(name):
                got = decide(prose, decision=bound_pass())
                self.assertNotEqual(v.MARK_COMPLETE, got["action"])
                self.assertEqual(v.SEND_TO_REVIEW, got["action"])
                self.assertIn("contradictory_result", got["reason"])
        for name, summary in self.SUMMARY:
            with self.subTest(name):
                got = decide("", decision=bound_pass(summary=summary))
                self.assertNotEqual(v.MARK_COMPLETE, got["action"])
                self.assertEqual(v.SEND_TO_REVIEW, got["action"])

    def test_each_attack_is_a_conflict_not_a_silence(self):
        context = {"candidate_sha": h.HEAD, "review_dispatch": h.review_dispatch()}
        for name, prose in self.PROSE:
            with self.subTest(name):
                authority = review_authority_for(review(prose, decision=bound_pass()), context)
                self.assertFalse(authority["authorized"])
                self.assertEqual(contracts.CONFLICT, authority["reason"])
        for name, summary in self.SUMMARY:
            with self.subTest(name):
                authority = review_authority_for(review("", decision=bound_pass(summary=summary)), context)
                self.assertFalse(authority["authorized"])
                self.assertEqual(contracts.CONFLICT, authority["reason"])


# -- Group 1: the grammar coverage matrix, colon form -------------------------

class GroupOneColonMatrix(unittest.TestCase):
    """(owner)? x {0,1,2,3,5} modifiers x noun, generated from the closed sets.

    Exceeds BOTH old limits: the 25-character label capture (M1) and, for the
    copula twin below, the required owner (M2) and the ``{0,2}`` modifiers (M3).
    """

    def test_the_matrix_exceeds_every_old_limit(self):
        """Not a test of the parser: a test that the matrix is worth running."""
        lengths = [len(c["field"]) for c in compositions()]
        self.assertTrue(any(n > 25 for n in lengths))
        self.assertTrue(any(n > 32 for n in lengths))
        self.assertTrue(any(n > 40 for n in lengths))
        self.assertTrue(any(c["owner"] is None for c in compositions()))
        self.assertTrue(any(c["count"] >= 3 for c in compositions()))

    def test_every_composition_is_read_as_a_rejection(self):
        for c in compositions():
            with self.subTest(c["field"]):
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(colon(c["field"])))

    def test_every_composition_reads_an_approval_as_an_approval(self):
        """A grammar that only ever blocks is a wall."""
        for c in compositions(counts=(0, 3, 5)):
            with self.subTest(c["field"]):
                self.assertEqual([contracts.STATEMENT_APPROVE], polarities(colon(c["field"], "approve")))

    def test_an_unreadable_value_in_any_composition_still_conflicts(self):
        for c in compositions(counts=(0, 3, 5), owners=(None, "my")):
            with self.subTest(c["field"]):
                self.assertEqual([contracts.STATEMENT_UNREADABLE],
                                 polarities(colon(c["field"], "see the findings below")))

    def test_the_required_rows_do_not_complete_beside_a_bound_pass(self):
        """The eight rows the review demanded, at the planner, for two nouns."""
        rows = compositions(counts=(0, 1, 2, 3, 5), owners=("my",)) + compositions(counts=(0, 1, 3), owners=(None,))
        for c in [r for r in rows if r["noun"] in ("decision", "recommendation")]:
            with self.subTest(c["field"]):
                self.assertFalse(completes(colon(c["field"]), decision=bound_pass()))

    def test_labels_over_each_length_threshold_are_read(self):
        for field, threshold in (("my current official decision", 25),
                                 ("our considered official final verdict", 32),
                                 ("their considered official formal recommendation", 40)):
            with self.subTest(field):
                self.assertGreater(len(field), threshold)
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(colon(field)))
                self.assertFalse(completes(colon(field), decision=bound_pass()))

    def test_markdown_dressing_does_not_hide_a_long_label(self):
        for prose in ("**My current official final decision:** reject",
                      "- Our considered official disposition: reject",
                      "> Their current official recommendation: reject",
                      "### My current official final overall verdict: reject"):
            with self.subTest(prose):
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(prose))


# -- Group 2: the grammar coverage matrix, copula form ------------------------

class GroupTwoCopulaMatrix(unittest.TestCase):
    """Same matrix, written with a verb. Owner optional; modifiers unbounded.

    Each statement's ``raw`` must begin with the reviewer's WHOLE field. A
    modifier cap on the copula form (M3) is otherwise invisible once the owner
    is optional: the regex simply re-anchors two modifiers before the noun and
    records a truncated field, so the rejection is still read but the record
    of *what* the reviewer wrote is wrong.
    """

    def assertWholeField(self, prose, field):
        statements = decision_statements(prose)
        self.assertEqual([contracts.STATEMENT_REJECT], [s["polarity"] for s in statements])
        self.assertTrue(statements[0]["raw"].lower().startswith(field),
                        "read %r, not the whole field %r" % (statements[0]["raw"], field))

    def test_every_composition_is_read_as_a_rejection(self):
        for c in compositions():
            with self.subTest(c["field"]):
                self.assertWholeField(copula(c["field"]), c["field"])

    def test_ownerless_copula_fields_are_read(self):
        """R7-IR-1c: 'Current decision: reject' was read, 'Current decision is to reject' was not."""
        for c in compositions(owners=(None,)):
            with self.subTest(c["field"]):
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(copula(c["field"])))
                # "reject the patch" is NOT in _REJECT_VALUES and this round does
                # not put it there: it reads as a decision ADM cannot map, which
                # conflicts under the Round-6 rule. Read, not necessarily mapped.
                self.assertEqual([contracts.STATEMENT_UNREADABLE],
                                 polarities(copula(c["field"], "reject the patch")))

    def test_three_and_five_modifier_copula_fields_are_read(self):
        """R7-IR-1b: the copula form allowed {0,2} modifiers."""
        for c in compositions(counts=(3, 5)):
            with self.subTest(c["field"]):
                self.assertWholeField(copula(c["field"]), c["field"])

    def test_other_copulas_read_the_same_field(self):
        for verb in ("is", "was", "remains", "stands as", "will be"):
            prose = "My current official final decision %s reject" % verb
            with self.subTest(verb):
                self.assertWholeField(prose, "my current official final decision")

    def test_the_required_rows_do_not_complete_beside_a_bound_pass(self):
        rows = compositions(counts=(0, 1, 2, 3, 5), owners=("my",)) + compositions(counts=(0, 1, 3), owners=(None,))
        for c in [r for r in rows if r["noun"] in ("decision", "recommendation")]:
            with self.subTest(c["field"]):
                self.assertFalse(completes(copula(c["field"]), decision=bound_pass()))

    def test_fields_over_each_length_threshold_are_read(self):
        for field, threshold in (("my current official decision", 25),
                                 ("our considered official final verdict", 32),
                                 ("their considered official formal recommendation", 40)):
            with self.subTest(field):
                self.assertGreater(len(field), threshold)
                self.assertWholeField(copula(field), field)
                self.assertFalse(completes(copula(field), decision=bound_pass()))


# -- Group 3: one grammar, two syntaxes ---------------------------------------

class GroupThreeStructuralEquivalence(unittest.TestCase):
    """The colon and copula forms derive from ONE grammar source.

    Not two lists that happen to agree today: the compiled decisive grammar is
    a single object, the copula regex embeds its source verbatim, and the
    behavioural invariant is checked on generated compositions AND on
    near-misses, in both directions.
    """

    def test_the_copula_form_embeds_the_compiled_decisive_grammar_verbatim(self):
        self.assertIn(extract_mod._DECISION_FIELD.pattern, extract_mod._COPULA_DECISION.pattern)

    def test_the_decisive_grammar_is_the_published_composition(self):
        self.assertEqual(extract_mod._field_grammar(extract_mod._DECISION_NOUNS),
                         extract_mod._DECISION_FIELD.pattern)
        # (owner)? (modifier)* (noun): optional owner group, starred modifier group.
        pattern = extract_mod._DECISION_FIELD.pattern
        self.assertTrue(pattern.startswith("(?:(?:"), pattern)
        self.assertIn(r"\s+)?(?:(?:", pattern)        # the owner is optional
        self.assertIn(r"\s+)*(?P<noun>", pattern)     # modifiers are unbounded

    def test_the_label_tier_consults_that_same_grammar(self):
        source = inspect.getsource(extract_mod._label_tier)
        self.assertIn("_DECISION_FIELD.fullmatch", source)

    def test_no_length_cap_stands_between_a_label_and_the_grammar(self):
        """M1: reintroducing ``{0,24}`` turns this red without touching the grammar."""
        self.assertNotIn("{0,24}", extract_mod._LABEL_LINE.pattern)
        self.assertNotIn("{0,2}", extract_mod._COPULA_DECISION.pattern)

    # Near-misses: one part outside its closed set, or the parts out of order.
    # None of these is a field, in either syntax.
    NEAR_MISSES = (
        "my tentative decision",           # modifier not in the set
        "my current decision engine",      # head noun not in the set
        "current my decision",             # owner not first
        "my current result",               # reporting noun, not a decision noun
        "the recommendation engine",
        "our decision my verdict",         # two fields glued together
        "a current decision",              # 'a' is not an owner
    )
    # Those near-misses that END in a field. The label form is line-anchored:
    # the whole text before the colon is the field, so these are not fields.
    # The copula form is a sentence, and reads the grammatical run that ends at
    # the noun -- so "My tentative decision is to reject" reads "decision is to
    # reject", a rejection. That is the one syntax difference between the two
    # forms, it fails closed, and it is stated here rather than hidden.
    SUFFIXED = ("my tentative decision", "current my decision", "our decision my verdict", "a current decision")

    def test_decisive_in_one_syntax_iff_decisive_in_the_other(self):
        """Generated compositions are decisive in both; near-misses in neither."""
        for c in compositions():
            with self.subTest(c["field"]):
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(colon(c["field"])))
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(copula(c["field"])))
        for field in self.NEAR_MISSES:
            with self.subTest(field):
                self.assertEqual([], polarities(colon(field)))
                if field not in self.SUFFIXED:
                    self.assertEqual([], polarities(copula(field)))

    def test_the_only_syntax_difference_is_the_anchor_and_it_fails_closed(self):
        for field in self.SUFFIXED:
            with self.subTest(field):
                self.assertEqual([], polarities(colon(field)))
                self.assertEqual([contracts.STATEMENT_REJECT], polarities(copula(field)))
                self.assertFalse(completes(copula(field), decision=bound_pass()))

    def test_the_grammar_agrees_with_the_tier_function_on_every_generated_field(self):
        """``_label_tier`` (colon) and ``_COPULA_DECISION``'s decisive branch (copula)
        are the same predicate over the same WHOLE text."""
        for text in [c["field"] for c in compositions()] + list(self.NEAR_MISSES):
            with self.subTest(text):
                by_tier = extract_mod._label_tier(text) == extract_mod._DECISIVE
                match = extract_mod._COPULA_DECISION.fullmatch(text + " is reject")
                by_copula = bool(match and match.group("noun"))
                self.assertEqual(by_tier, by_copula)

    def test_the_closed_sets_are_the_published_ones(self):
        self.assertEqual(PUBLISHED_OWNERS, OWNERS)
        self.assertEqual(PUBLISHED_MODIFIERS, MODIFIERS)
        self.assertEqual(PUBLISHED_NOUNS, NOUNS)


# -- Group 4: the summary inherits the collector, and only the collector ------

class GroupFourSummaryReuse(unittest.TestCase):
    """R7-IR-2. The summary reads through ``extract.decision_statements``; there
    is no second parser to keep in step. So every long composition the
    collector reads, the summary withdraws with."""

    LONG = [c for c in compositions(counts=(1, 3, 5)) if len(c["field"]) > 25]

    def test_the_summary_still_reads_through_the_shared_collector(self):
        """M5: a summary-specific scanner, or none, turns this red."""
        source = inspect.getsource(contracts._summary_statements)
        self.assertIn("extract.decision_statements", source)
        self.assertNotIn("re.compile", source)

    def test_every_long_colon_composition_in_a_summary_withdraws_the_pass(self):
        self.assertTrue(self.LONG, "matrix produced no field over 25 characters")
        for c in self.LONG:
            with self.subTest(c["field"]):
                got = review("", decision=bound_pass(summary=colon(c["field"])))
                authority = review_authority_for(got, {"candidate_sha": h.HEAD,
                                                       "review_dispatch": h.review_dispatch()})
                self.assertFalse(authority["authorized"])
                self.assertEqual(contracts.CONFLICT, authority["reason"])

    def test_every_long_copula_composition_in_a_summary_withdraws_the_pass(self):
        for c in self.LONG:
            with self.subTest(c["field"]):
                got = review("", decision=bound_pass(summary=copula(c["field"])))
                authority = review_authority_for(got, {"candidate_sha": h.HEAD,
                                                       "review_dispatch": h.review_dispatch()})
                self.assertFalse(authority["authorized"])
                self.assertEqual(contracts.CONFLICT, authority["reason"])

    def test_long_summaries_do_not_complete_at_the_planner(self):
        for summary in ("My current official decision: reject",
                        "Our considered official final verdict: reject",
                        "Their considered official formal recommendation: reject",
                        "My current official final decision is to reject",
                        "Current decision is to reject"):
            with self.subTest(summary):
                got = decide("", decision=bound_pass(summary=summary))
                self.assertNotEqual(v.MARK_COMPLETE, got["action"])

    def test_an_approving_summary_never_grants_authority(self):
        for summary in ("My current official decision: approve",
                        "Our considered official final verdict is to approve"):
            with self.subTest(summary):
                unbound = bound_pass(reviewer_run_id="run-not-ours", summary=summary)
                authority = contracts.review_authority([unbound], EXPECTATION)
                self.assertFalse(authority["authorized"])
                self.assertFalse(authority["blocked"])
                rejected = bound_pass(verdict="REJECT", summary=summary)
                authority = contracts.review_authority([rejected], EXPECTATION)
                self.assertFalse(authority["authorized"])
                self.assertTrue(authority["blocked"])

    def test_an_unbound_forged_rejecting_summary_cannot_stall(self):
        for summary in ("My current official decision: reject",
                        "Their considered official formal recommendation is to reject"):
            with self.subTest(summary):
                unbound = bound_pass(reviewer_run_id="run-not-ours", summary=summary)
                authority = contracts.review_authority([unbound], EXPECTATION)
                self.assertFalse(authority["authorized"])
                self.assertFalse(authority["blocked"])
                off_target = bound_pass(target_sha="0123456789abcdef0123456789abcdef01234567", summary=summary)
                authority = contracts.review_authority([off_target], EXPECTATION)
                self.assertFalse(authority["authorized"])
                self.assertFalse(authority["blocked"])

    ORDINARY = (
        "The previous blocker was fixed.",
        "This review rejects the old approach, but the submitted patch now satisfies the contract.",
        "Our recommendation engine benchmarks improved by 12%.",
        "The current official documentation was updated alongside the fix.",
        "My considered view is that the tests are real.",
        "Nothing blocks completion.",
    )

    def test_an_ordinary_summary_does_not_withdraw_the_pass(self):
        for summary in self.ORDINARY:
            with self.subTest(summary):
                self.assertEqual([], decision_statements(summary))
                self.assertTrue(completes("", decision=bound_pass(summary=summary)))


# -- Group 5: what must NOT have moved ----------------------------------------

class GroupFivePositiveControls(unittest.TestCase):
    """P1-P6 from the review, plus the two accepted Round-7 semantics."""

    def test_p1_clean_bound_pass_with_ordinary_approval_prose_completes(self):
        self.assertTrue(completes("The patch is correct and well tested. Looks good to me.", decision=bound_pass()))

    def test_p2_a_reporting_noun_is_not_promoted_however_it_is_modified(self):
        for prose in ("Final result: 3 passed",
                      "My current official final result: 3 passed",
                      "Our considered official status: green",
                      "Their current official final overall conclusion: looks fine",
                      "The final result is 3 passed",
                      "My considered official assessment is that it looks fine"):
            with self.subTest(prose):
                self.assertEqual([], decision_statements(prose))
                self.assertTrue(completes(prose, decision=bound_pass()))

    def test_p3_a_recommending_verb_is_not_a_decision_field(self):
        self.assertEqual([], decision_statements("I recommend adding a regression test."))
        self.assertTrue(completes("I recommend adding a regression test.", decision=bound_pass()))

    def test_p4_a_quoted_long_rejection_in_a_closed_wrong_channel_fence_completes(self):
        """R6-IR-3 accepted semantics: a closed fence is quotation in both directions."""
        for fence in ('```text\nMy current official decision: reject\n```',
                      '```markdown\nCurrent decision is to reject\n```',
                      '```json\n{"verdict": "REJECT"}\n```'):
            with self.subTest(fence):
                got = review(fence, decision=bound_pass())
                self.assertEqual([], got.get("decision_statements") or [])
                self.assertTrue(completes(fence, decision=bound_pass()))

    def test_p5_a_wrong_channel_pass_alone_cannot_authorize(self):
        for lang in ("json", "yaml", "text"):
            with self.subTest(lang):
                got = review("", decision=bound_pass(), fence=lang)
                self.assertEqual([], got["decisions"])
                self.assertFalse(completes("", decision=bound_pass(), fence=lang))

    def test_p6_an_unclosed_fence_cannot_hide_a_long_rejection(self):
        from manager.nextplan import result as r

        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1",
                   "status": "PASS", "review_verdict": "PASS", "reviewed_sha": h.HEAD}
        content = ("Review complete.\n\n"
                   "```adm-result\n" + json.dumps(payload) + "\n```\n\n"
                   "```adm-review-result\n" + json.dumps(bound_pass()) + "\n```\n\n"
                   "```json\n"                      # opened, never closed
                   '{"example": true}\n\n'
                   "My current official final decision: reject\n")
        got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": v.REVIEWER,
                       "format": "text", "session_id": h.REVIEWER_SESSION, "content": content})
        got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
        self.assertTrue(got["decision_statements"], "the rejection was swallowed")
        action = plan(reviewing(), h.event(got, role=v.REVIEWER,
                                           session_id=h.REVIEWER_SESSION, generation=1))["action"]
        self.assertNotEqual(v.MARK_COMPLETE, action)

    def test_the_single_authority_path_agrees_on_a_long_contradiction(self):
        """R6-IR-4 accepted topology: proof and signals ask one helper, and agree."""
        from manager.nextplan import classify

        for fn in (classify.review_proof, classify.signals_for):
            self.assertIn("review_authority_for", inspect.getsource(fn))
            self.assertNotIn("contracts.review_authority", inspect.getsource(fn))
        context = {"candidate_sha": h.HEAD, "review_dispatch": h.review_dispatch(),
                   "reviewer_session": h.REVIEWER_SESSION, "worker_sessions": [h.WORKER_SESSION],
                   "prior_reviewer_sessions": []}
        got = review("Their considered official recommendation: reject", decision=bound_pass())
        proof = classify.review_proof(got, context)
        bound = [item for item in proof if "bound reviewer decision" in item["requirement"]]
        self.assertEqual([False], [item["ok"] for item in bound])
        signals = classify.signals_for(got, None, {}, {"kind": "completed"}, context)
        self.assertIn("extract.conflicting_statements", signals)

    def test_the_rejection_vocabulary_is_byte_for_byte_unchanged(self):
        digest = hashlib.sha256("\n".join(sorted(extract_mod._REJECT_VALUES)).encode("utf-8")).hexdigest()
        self.assertEqual(53, len(extract_mod._REJECT_VALUES))
        self.assertEqual(REJECT_VALUES_SHA256, digest)

    def test_ordinary_prose_with_decision_nouns_still_reads_as_nothing(self):
        for prose in ("Background: our review of the architecture took two days.",
                      "The recommendation engine module was refactored.",
                      "Note: the decision table is generated from the atlas.",
                      "Scope: current decision paths are covered by the new tests.",
                      "Coverage: final outcome handling is exercised end to end.",
                      "I went through the whole implementation and the current decision paths: all covered.",
                      "My review took longer than estimated."):
            with self.subTest(prose):
                self.assertEqual([], decision_statements(prose))
                self.assertTrue(completes(prose, decision=bound_pass()))


if __name__ == "__main__":
    unittest.main()
