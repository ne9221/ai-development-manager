"""Regression tests for the Round-2 independent review of 2026-09-16 (R2-A/B1/B2).

Every case here was run against the code at f2ea394 before it was changed, and
every one of them passed there in the wrong direction: documentation examples
proved tests had run, formatted verdicts missed the enum, and reworded
rejections left a payload PASS standing.

The rejection wordings in ``HELD_OUT_REJECTIONS`` are deliberately *not* the
sentences the reviewer reported. The point of the fix is that a rejection is
recognised by structure -- a negated approval, an asserted blocker, a fix
ordered before landing, another round, a rejection whose object is the work --
so held-out phrasings are the only honest test of it. The reviewer's own
sentences are covered by ``REPORTED_REJECTIONS`` as well, but passing those
alone would prove nothing.
"""

import json
import unittest

from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import completion_proof
from manager.nextplan.extract import _map_value, extract, rejection_signal
from manager.nextplan.extract import test_counts as parse_counts  # aliased: pytest collects test_*
from manager.nextplan.planner import new_task_state, plan
from manager.nextplan.verify import TestEvidenceProbe, adm_test_evidence, verify

NO_REVIEW = dict(new_task_state("t-1")["requirements"], requires_review=False)

# Rejections the reviewer actually reported.
REPORTED_REJECTIONS = (
    "I reject the current implementation.",
    "This patch should not be accepted.",
    "Approval is withheld.",
    "Two blockers remain.",
    "Fix this before merging.",
    "This cannot land yet.",
    "Returning this to the implementer.",
    "This is not acceptable in its current form.",
    "Needs another round before it can be approved.",
)

# Held out: none of these appear in the review, and none was written as a
# literal into the matcher. Each exercises a structure, not a sentence.
HELD_OUT_REJECTIONS = (
    "I am not approving this branch.",                              # negated approval verb
    "Blocking findings remain unresolved.",                         # asserted blocker
    "The change must not land until the leak is fixed.",            # negated landing
    "I withhold approval pending a fix.",                           # withheld approval
    "Please send this back to the author.",                         # returned to the author
    "This needs one more pass before I can sign off.",              # another round
    "Sign-off is denied.",                                          # approval noun, negative predicate
    "Three defects block the release.",                             # defects as the blocking subject
    "Not approved.",                                                # bare negated approval
    "This should not merge until the race is closed.",              # negated merge
    "The submission is rejected.",                                  # passive rejection of the work
    "I cannot accept this in its present state.",                   # negated acceptance
    "A further review round is required.",                          # another round, other wording
    "Merging is blocked by the unresolved race.",                   # merging asserted blocked
    "The work is not acceptable yet.",                              # not acceptable
    "Please address the two blocking defects before merge.",        # fix ordered before merge
    "I decline to approve this change.",                            # decline-to-approve
    "This is returned for rework.",                                 # returned for rework
    "Approval will not be granted until the tests pass.",           # approval negated in the future
    "The patch is rejected.",                                       # passive, other work noun
    # Found while adversarially self-reviewing the fix, before any review saw it.
    "I am unable to approve this pull request.",                     # unable-to-approve
    "Several blocking defects are still open.",                      # blockers still open
    "The diff cannot ship in this state.",                           # negated ship
    "Sign-off will not be granted today.",                           # sign-off negated
    "This needs a second review round.",                             # a second round
    "Correct the off-by-one before releasing.",                      # correct before release
    "I refuse to approve the branch.",                               # refuse-to-approve
    "Handing this back to the developer.",                           # hand-back, other verb
    "One blocker is still outstanding.",                             # one blocker outstanding
    "The commit is rejected pending a rerun.",                       # passive, commit
    "Kicking it back to the contributor.",                           # hand-back, other verb again
    "Two blocking issues.",                                          # bare count, no predicate
)

# Prose that is NOT a rejection. Round 2 wrongly withdrew the first three; the
# rest are the research-before-build and clean-review wordings that must keep
# working (common governance rule 10).
GENUINE_PASS_PROSE = (
    "There are no remaining blocking issues.",
    "0 blocking issues.",
    "Nothing must be fixed first; the branch is clean.",
    "No blocking findings.",
    "Zero blocking issues.",
    "Without blocking findings.",
    "No issues block merge.",
    "I rejected library X after evaluation.",
    "Rejected approach A and selected approach B.",
    "Rejected a stale fixture and regenerated it.",
    "The server rejected the push once; the retry landed.",
    "The parser rejected malformed input, as designed.",
    "All checks pass and the diff is minimal.",
    "No further rounds are needed.",
    "Approval granted; nothing blocks merge.",
    # Ordinary ADM report prose. Withdrawing a PASS from any of these costs a
    # round on honest work, which is a defect in its own right.
    "Pushed to the remote; nothing blocks merge.",
    "I reviewed the approach and rejected the caching layer.",
    "The reviewer approved the change.",
    "No approval needed for this task.",
    "The push was rejected by the remote; retried and pushed.",
    "The blocker was the missing token; resolved.",
    "Rejected three candidate libraries per rule 10.",
    "This change lands on feat/x cleanly.",
    "Fixed the race before the review started.",
    "All blocking issues from round 1 were resolved.",
    "Zero blockers.",
    "I approve; no further work is required.",
    # Hand-back verbs carrying an ordinary object, not the work under review.
    "I handed the token back to the caller.",
    "Pass the config back to the loader.",
    "Gave the lock back after the write.",
    "Returned the buffer to the pool.",
)


def execution_with(output_summary, command="pytest", exit_code=0):
    """A LEGACY record: a shell command string plus whatever it printed.

    Round 5 made this shape incapable of yielding counts at all.
    """
    return {"repo_write_evidence": {
        "files_changed": ["pkg/a.py"], "commits": [h.HEAD], "final_commit_sha": h.HEAD, "branch": "feat/x",
        "worktree_path": "/w", "push_status": "verified", "remote_sha": h.HEAD, "tests_status": "passed",
        "tests": [{"command": command, "exit_code": exit_code, "output_summary": output_summary,
                   "started_at": "2026-09-16T00:00:00Z", "completed_at": "2026-09-16T00:00:01Z"}]}}


def validated(passed=12, failed=0, skipped=0, exit_code=0):
    """The same run as a runner adapter records it: one argv, one process, its own report."""
    return {"repo_write_evidence": {
        "files_changed": ["pkg/a.py"], "commits": [h.HEAD], "final_commit_sha": h.HEAD, "branch": "feat/x",
        "worktree_path": "/w", "push_status": "verified", "remote_sha": h.HEAD, "tests_status": "passed",
        "validation_results": [h.validation_result(passed=passed, failed=failed, skipped=skipped,
                                                   exit_code=exit_code)]}}


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
                    # Round 5: ADM's own record of the reviewer run it dispatched. A
                    # decision authorizes only when it binds back to this.
                    review_dispatch=h.review_dispatch(),
                    generation=1)
    state.update(changes)
    return state


def worker_text(content):
    return extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text", "content": content})


def review_text(prose, verdict="PASS", anchor=True, decision=True):
    """Reviewer prose beside a structured payload that claims a clean PASS.

    Round 4: a payload alone no longer authorizes a PASS (Codex finding R3-1),
    so these cases now also carry the explicit verdict a compliant reviewer
    states. Without it every rejection case below would pass for the wrong
    reason -- blocked by the new gate rather than by the prose -- and would
    stop testing what it was written to test.

    Round 5: for exactly the same reason, they now also carry a bound
    adm-review-result/v1 decision. The prose anchor is no longer authority, so
    without the block these cases would once again be blocked by the gate rather
    than by the wording they exist to exercise. Every rejection case here is
    therefore a live test of the one thing prose may still do: **withdraw** an
    otherwise valid structured PASS.
    """
    if anchor:
        prose = f"Verdict: {verdict}\n" + prose
    payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS",
               "review_verdict": verdict, "reviewed_sha": h.HEAD}
    block = ("\n```adm-review-result\n" + json.dumps(h.review_decision(verdict=verdict)) + "\n```\n"
             if decision else "")
    content = prose + "\n\n```adm-result\n" + json.dumps(payload) + "\n```\n" + block
    got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": v.REVIEWER, "format": "text",
                   "session_id": h.REVIEWER_SESSION, "content": content})
    got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
    return got


# -- Group A: execution evidence must come from a run, not from documentation ----

class GroupATestEvidenceProvenance(unittest.TestCase):
    """R2-A: structurally valid runner output that is only an example."""

    def test_a_fenced_pytest_summary_is_not_evidence(self):
        self.assertEqual({}, parse_counts("Here is the format:\n\n```\n===== 12 passed in 3.10s =====\n```\n"))

    def test_a_fenced_summary_in_a_language_block_is_not_evidence(self):
        self.assertEqual({}, parse_counts("```console\n===== 12 passed in 3.10s =====\n```\n"))

    def test_a_quoted_pytest_summary_is_not_evidence(self):
        self.assertEqual({}, parse_counts("As the docs show:\n\n> ===== 12 passed in 3.10s =====\n"))

    def test_a_sample_block_that_says_nothing_executed_is_not_evidence(self):
        content = "Sample successful output:\nRan 12 tests in 0.500s\nOK\n(dry run: nothing executed)\n"
        self.assertEqual({}, parse_counts(content))
        self.assertEqual(v.UNKNOWN, r.level(worker_text(content), "tests_run"))

    def test_an_example_followed_by_no_tests_configured_is_not_evidence(self):
        content = ("Example:\nRan 99 tests in 0.500s\nOK\n\n"
                   "Actual: validation script completed, no tests configured.\n")
        self.assertEqual({}, parse_counts(content))
        self.assertEqual(v.UNKNOWN, r.level(worker_text(content), "tests_run"))

    def test_an_earlier_example_never_shadows_a_later_real_run(self):
        """The unittest block used to be searched top-down and returned first."""
        for later, expected in (("===== 0 passed in 0.01s =====", {"tests_passed": 0, "tests_failed": 0}),
                                ("===== 7 passed in 1.00s =====", {"tests_passed": 7, "tests_failed": 0}),
                                ("===== 2 failed, 5 passed in 1.00s =====", {"tests_passed": 5, "tests_failed": 2})):
            with self.subTest(later):
                content = "Docs example:\nRan 99 tests in 0.500s\nOK\n\n" + later + "\n"
                self.assertEqual(expected, parse_counts(content))

    def test_a_later_real_failure_is_preserved(self):
        content = "Docs example:\nRan 99 tests in 0.500s\nOK\n\n===== 2 failed, 5 passed in 1.00s =====\n"
        got = worker_text(content)
        self.assertEqual(2, r.value(got, "tests_failed"))
        self.assertNotEqual(99, r.value(got, "tests_run"))

    def test_the_last_unittest_block_wins_over_an_earlier_one(self):
        content = "Ran 99 tests in 0.500s\nOK\n\nRan 3 tests in 0.100s\nFAILED (failures=1)\n"
        self.assertEqual({"tests_passed": 2, "tests_failed": 1, "tests_skipped": 0}, parse_counts(content))

    def test_zero_tests_never_proves_that_tests_ran(self):
        # The parser is unchanged and still reads the summary exactly as before.
        counts = parse_counts("===== 0 passed in 0.01s =====")
        self.assertEqual({"tests_passed": 0, "tests_failed": 0}, counts)
        bare = h.worker_result(verified=False, drop=("tests_run", "tests_passed", "tests_failed"))

        # Round 5: the legacy record cannot say how many tests ran at all, so the
        # requirement fails for want of evidence rather than on the number.
        legacy, _ = verify(bare, {"test_evidence": adm_test_evidence(execution_with("===== 0 passed in 0.01s ====="))},
                           [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(legacy, "tests_run"))
        self.assertFalse(next(i for i in completion_proof(legacy, {}) if "actually ran" in i["requirement"])["ok"])

        # And the original claim, on the channel that can now carry counts: a run
        # that genuinely executed zero tests is VERIFIED at zero, and zero still
        # does not prove that tests ran.
        verified, _ = verify(bare, {"test_evidence": adm_test_evidence(validated(passed=0))}, [TestEvidenceProbe()])
        self.assertEqual((0, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        proof = completion_proof(verified, {})
        ran = next(item for item in proof if "actually ran" in item["requirement"])
        self.assertFalse(ran["ok"])

    def test_a_zero_test_run_cannot_complete_the_task(self):
        evidence = adm_test_evidence(execution_with("===== 0 passed in 0.01s ====="))
        bare = h.worker_result(verified=False, drop=("tests_run", "tests_passed", "tests_failed"))
        verified, _ = verify(bare, {"test_evidence": evidence}, [TestEvidenceProbe()])
        decision = plan(working(requirements=NO_REVIEW), h.event(verified))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_subtests_are_metadata_not_a_second_test_population(self):
        """Real output of this branch's own suite: 187 primary, 954 subtests."""
        self.assertEqual({"tests_passed": 187, "tests_failed": 0},
                         parse_counts("187 passed, 954 subtests passed in 23.22s"))

    def test_subtests_do_not_hide_a_failure(self):
        self.assertEqual({"tests_passed": 180, "tests_failed": 7},
                         parse_counts("7 failed, 180 passed, 954 subtests passed in 23.22s"))

    def test_a_pasted_session_is_still_only_a_claim(self):
        """The backstop: prose can be REPORTED, and REPORTED can never complete.

        A full pytest session pasted with no lead-in is indistinguishable from
        the agent's own run and is read as its claim, which is the honest
        reading. So is one whose lead-in is separated from its summary line by a
        blank line: that shape is exactly case A8 ("example, blank, the real
        result"), and nothing in the text tells the two apart. That is the
        residual, and it is survivable only because of what is asserted here --
        extraction can reach REPORTED, completion needs a probe, and a REPORTED
        count never satisfies the proof.
        """
        session = ("======================= test session starts ==================\n"
                   "collected 12 items\ntests/test_a.py ...........   [100%]\n"
                   "======================== 12 passed in 3.10s ==================\n")
        got = worker_text(session)
        self.assertEqual(v.REPORTED, r.level(got, "tests_passed"))
        self.assertFalse(all(item["ok"] for item in completion_proof(got, {})))
        self.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(got))["action"])

    def test_a_pasted_session_under_a_lead_in_or_a_fence_is_not_evidence(self):
        session = ("======================= test session starts ==================\n"
                   "collected 12 items\ntests/test_a.py ...........   [100%]\n"
                   "======================== 12 passed in 3.10s ==================\n")
        self.assertEqual({}, parse_counts("Here is a sample run:\n\n" + session))
        self.assertEqual({}, parse_counts("```\n" + session + "```\n"))

    def test_real_runner_output_still_parses(self):
        for text, expected in (
                ("===== 200 passed in 24.66s =====", {"tests_passed": 200, "tests_failed": 0}),
                ("Ran 161 tests in 3.20s\nOK", {"tests_passed": 161, "tests_failed": 0, "tests_skipped": 0}),
                ("Ran 10 tests in 3.20s\nFAILED (failures=2)",
                 {"tests_passed": 8, "tests_failed": 2, "tests_skipped": 0}),
                ("5 failed, 2890 passed in 300.00s", {"tests_passed": 2890, "tests_failed": 5})):
            with self.subTest(text):
                self.assertEqual(expected, parse_counts(text))


# -- Group B: normalization must survive formatting combinations -----------------

class GroupBVerdictNormalization(unittest.TestCase):
    """R2-B1: one strip pass left the trailing markers behind the punctuation."""

    def test_formatted_verdicts_reach_the_enum(self):
        for token in ("**rejected**.", "*rejected*.", "__rejected__.", "`rejected`.", "**fail**.",
                      "**reject**!", "***rejected***...", "~~rejected~~,", "“rejected”.",
                      "**REJECTED**;", "`fail`!", "**changes_required**."):
            with self.subTest(token):
                self.assertEqual("FAIL", _map_value("review_verdict", token, v.REVIEWER))

    def test_formatted_statuses_reach_the_enum(self):
        for token, expected in (("**PASS**.", "PASS"), ("*failed*!", "FAIL"), ("`BLOCKED`,", "BLOCKED"),
                                ("**done**...", "PASS"), ("__partial__.", "PARTIAL")):
            with self.subTest(token):
                self.assertEqual(expected, _map_value("status", token, v.WORKER))

    def test_nested_and_mixed_wrappers_still_reach_the_enum(self):
        for token in ('**"rejected"**.', "_`rejected`_!", " ***REJECTED*** . ", "[rejected].",
                      "**  rejected  **.", "<rejected>;"):
            with self.subTest(token):
                self.assertEqual("FAIL", _map_value("review_verdict", token, v.REVIEWER))

    def test_a_formatted_multi_word_phrase_is_not_a_verdict(self):
        for token in ("**rejected library X**.", "*rejected approach A*", "`rejected the fixture`",
                      "**pass with caveats**.", "**approved with comments**."):
            with self.subTest(token):
                self.assertIsNone(_map_value("review_verdict", token, v.REVIEWER))

    def test_normalization_terminates_on_pathological_input(self):
        """The loop is capped, so deep alternation stops rather than looping."""
        self.assertEqual("FAIL", _map_value("review_verdict", "*" * 200 + "rejected" + "*" * 200, v.REVIEWER))
        alternating = "*." * 100 + "rejected" + ".*" * 100
        self.assertIsNone(_map_value("review_verdict", alternating, v.REVIEWER))

    def test_a_formatted_rejection_withdraws_a_payload_pass(self):
        for token in ("**rejected**.", "*rejected*.", "__rejected__.", "`rejected`.", "***rejected***..."):
            with self.subTest(token):
                got = review_text("Verdict: " + token)
                self.assertNotEqual("PASS", r.value(got, "review_verdict"))
                decision = plan(reviewing(),
                                h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
                self.assertNotEqual(v.MARK_COMPLETE, decision["action"])


# -- Group C: held-out rejections -----------------------------------------------

class GroupCHeldOutRejections(unittest.TestCase):
    """R2-B2: a reworded rejection must not leave a payload PASS standing."""

    def test_held_out_rejections_are_recognised(self):
        for prose in HELD_OUT_REJECTIONS:
            with self.subTest(prose):
                self.assertIsNotNone(rejection_signal(prose))

    def test_held_out_rejections_withdraw_the_payload_verdict(self):
        for prose in HELD_OUT_REJECTIONS:
            with self.subTest(prose):
                got = review_text(prose)
                self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))
                self.assertIn("extract.conflicting_statements", got["extraction"]["signals"])

    def test_held_out_rejections_can_never_mark_the_task_complete(self):
        for prose in HELD_OUT_REJECTIONS:
            with self.subTest(prose):
                got = review_text(prose)
                decision = plan(reviewing(),
                                h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
                self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_the_reported_rejections_are_recognised_too(self):
        for prose in REPORTED_REJECTIONS:
            with self.subTest(prose):
                got = review_text(prose)
                self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))

    def test_a_rejection_inside_a_fence_still_withdraws(self):
        """Withdrawal widens: narrowing it to prose would be a new bypass."""
        got = review_text("Notes:\n\n```\nTwo blockers remain.\n```\n")
        self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))

    def test_a_worker_rejecting_its_own_work_is_caught(self):
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
        content = ("A blocker still remains in the migration.\n\n```adm-result\n"
                   + json.dumps(payload) + "\n```\n")
        got = worker_text(content)
        self.assertEqual(v.UNKNOWN, r.level(got, "status"))

    def test_the_withdrawal_records_what_it_matched(self):
        got = review_text("Two blockers remain.")
        self.assertIn("blockers", (r.get(got, "review_verdict")["detail"] or ""))


# -- Group D: false-positive protection ------------------------------------------

class GroupDGenuinePassSurvives(unittest.TestCase):
    """Withdrawing an honest PASS is itself a defect; Round 2 did it three times."""

    def test_genuine_prose_is_not_a_rejection(self):
        for prose in GENUINE_PASS_PROSE:
            with self.subTest(prose):
                self.assertIsNone(rejection_signal(prose))

    def test_genuine_prose_keeps_the_payload_verdict(self):
        for prose in GENUINE_PASS_PROSE:
            with self.subTest(prose):
                got = review_text(prose)
                self.assertEqual(("PASS", v.REPORTED),
                                 (r.value(got, "review_verdict"), r.level(got, "review_verdict")))
                self.assertNotIn("extract.conflicting_statements", got["extraction"]["signals"])

    def test_a_clean_review_still_completes_the_task(self):
        for prose in ("I reviewed the diff and the tests. No blocking findings.",
                      "There are no remaining blocking issues.",
                      "0 blocking issues.",
                      "Nothing must be fixed first; the branch is clean."):
            with self.subTest(prose):
                got = review_text(prose)
                decision = plan(reviewing(),
                                h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
                self.assertEqual(v.MARK_COMPLETE, decision["action"])

    def test_negation_is_resolved_inside_the_clause_not_across_it(self):
        """"no" in an earlier clause must not excuse a blocker in a later one."""
        self.assertIsNone(rejection_signal("No tests were skipped."))
        self.assertIsNotNone(rejection_signal("No tests were skipped, but two blockers remain."))

    def test_research_rejection_beside_a_real_one_still_withdraws(self):
        got = review_text("I rejected library X after evaluation. Two blockers remain.")
        self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))


if __name__ == "__main__":
    unittest.main()
