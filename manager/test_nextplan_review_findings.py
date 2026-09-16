"""Regression tests for the independent adversarial review of 2026-09-16.

Each test reproduces one finding as it was reported (verdict REJECT, two
CRITICAL false-pass paths) and pins the fixed behaviour. All three were
confirmed by running the code before the fix, not by inspection.
"""

import json
import unittest

from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import completion_proof
from manager.nextplan.extract import _map_value, extract
from manager.nextplan.planner import new_task_state, plan
from manager.nextplan.verify import TestEvidenceProbe, adm_test_evidence, verify

NO_REVIEW = dict(new_task_state("t-1")["requirements"], requires_review=False)


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


def _evidence(**changes):
    base = {"files_changed": ["pkg/a.py"], "commits": [h.HEAD], "final_commit_sha": h.HEAD, "branch": "feat/x",
            "worktree_path": "/w", "push_status": "verified", "remote_sha": h.HEAD, "tests_status": "passed"}
    base.update(changes)
    return {"repo_write_evidence": base}


def execution_with(output_summary, command="pytest", exit_code=0):
    """A LEGACY record: a shell command string plus whatever it printed.

    Round 5 made this shape incapable of producing counts, whatever it printed.
    It is kept because it is still the shape ADM records today, and the tests
    that use it now pin the fail-closed reading.
    """
    return _evidence(tests=[{"command": command, "exit_code": exit_code, "output_summary": output_summary,
                             "started_at": "2026-09-16T00:00:00Z", "completed_at": "2026-09-16T00:00:01Z"}])


def validated(passed=12, failed=0, skipped=0, exit_code=0):
    """The same run recorded by a runner adapter: an argv, one process, its own report."""
    return _evidence(validation_results=[h.validation_result(passed=passed, failed=failed, skipped=skipped,
                                                             exit_code=exit_code)])


def worker_evidence(execution):
    """Worker facts verified by ADM's recorded run, with no test claims of its own."""
    bare = h.worker_result(verified=True, drop=("tests_run", "tests_passed", "tests_failed"))
    verified, _ = verify(bare, {"test_evidence": adm_test_evidence(execution)}, [TestEvidenceProbe()])
    return verified


def printed_output_proves_nothing(case, output, command="pytest"):
    """A legacy record printing ``output`` yields no counts and cannot complete.

    Asserted for every summary format the parser understands, because the point
    of Round 5 is that recognising the format was never the same as knowing a
    test process produced it.
    """
    evidence = adm_test_evidence(execution_with(output, command=command))
    case.assertNotIn("passed", evidence["runs"][0])
    verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                         {"test_evidence": evidence}, [TestEvidenceProbe()])
    case.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
    case.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])


class FindingOneNoEvidenceAnyTestRan(unittest.TestCase):
    """CRITICAL: a validation command that ran zero tests completed the task."""

    def test_the_tests_actually_ran_item_is_never_silently_absent(self):
        result = r.with_fact(h.worker_result(drop=("tests_run", "tests_passed")), "tests_failed",
                             r.fact(0, v.VERIFIED, "probe:test_evidence"))
        proof = completion_proof(result, {})
        self.assertTrue(any("actually ran" in item["requirement"] for item in proof))
        self.assertFalse(all(item["ok"] for item in proof))

    def test_an_exit_code_alone_cannot_complete_a_task(self):
        result = r.with_fact(h.worker_result(drop=("tests_run", "tests_passed")), "tests_failed",
                             r.fact(0, v.VERIFIED, "probe:test_evidence"))
        decision = plan(working(requirements=NO_REVIEW), h.event(result))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_a_command_that_runs_no_tests_yields_no_count(self):
        evidence = adm_test_evidence(execution_with("", command="true"))
        self.assertNotIn("passed", evidence["runs"][0])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
        self.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_a_real_validation_run_still_completes(self):
        # Round 5: the printed summary alone no longer proves anything (that is
        # what the echo exploit abused), so this case now runs through the
        # adapter. The claim under test is unchanged -- a genuine run must still
        # be able to complete a task, or the gate would just be a wall.
        printed_output_proves_nothing(self, "== 12 passed in 3.10s ==")
        verified = worker_evidence(validated(passed=12))
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_a_failing_validation_run_is_still_caught(self):
        # A nonzero exit is caught on BOTH channels: the legacy record still
        # carries its exit code even though it can no longer carry counts.
        for evidence in (adm_test_evidence(execution_with("== 2 failed, 10 passed in 3.10s ==", exit_code=1)),
                         adm_test_evidence(validated(passed=10, failed=2, exit_code=1))):
            with self.subTest(evidence["runs"][0].get("execution_id") or "legacy"):
                verified, report = verify(h.worker_result(), {"test_evidence": evidence}, [TestEvidenceProbe()])
                self.assertIn("verify.tests.failed", report["signals"])
                self.assertNotEqual(v.MARK_COMPLETE,
                                    plan(working(requirements=NO_REVIEW), h.event(verified))["action"])


class FindingTwoHedgedWordsCollapse(unittest.TestCase):
    """CRITICAL: a qualified answer was read as the unqualified one."""

    def extract_text(self, content, role=v.WORKER):
        return extract({"event_id": "e", "task_id": "t-1", "role": role, "format": "text", "content": content})

    def test_a_hedged_status_is_unknown_not_pass(self):
        for text in ("Status: PASS_WITH_CAVEATS", "Status: PASS (with residuals)", "Status: PASS_PENDING_REVIEW"):
            with self.subTest(text):
                got = self.extract_text(text)
                self.assertNotEqual("PASS", r.value(got, "status"))
                self.assertEqual(v.UNKNOWN, r.level(got, "status"))

    def test_a_hedged_verdict_is_unknown_not_approval(self):
        for text in ("Verdict: APPROVED_WITH_COMMENTS", "Verdict: APPROVE_AFTER_FIXES"):
            with self.subTest(text):
                got = self.extract_text(text, role=v.REVIEWER)
                self.assertNotEqual("PASS", r.value(got, "review_verdict"))

    def test_the_plain_words_still_work(self):
        self.assertEqual("PASS", r.value(self.extract_text("Status: PASS"), "status"))
        self.assertEqual("PASS", r.value(self.extract_text("Verdict: APPROVE", role=v.REVIEWER), "review_verdict"))
        self.assertEqual("FAIL", r.value(self.extract_text("Verdict: CHANGES_REQUIRED", role=v.REVIEWER),
                                         "review_verdict"))


class FindingTwoQuotedPayloadBecomesTheVerdict(unittest.TestCase):
    """CRITICAL: an example payload overrode the reviewer's actual judgement."""

    def review_text(self, verdict="PASS", prose="I found a real bug. CHANGES REQUIRED - this should not ship.",
                    decision=True):
        """Round 5: a compliant reviewer also returns a bound adm-review-result/v1.

        The block is what authorizes now, so without it every case here would
        pass for the wrong reason -- blocked by a missing decision rather than by
        the contradiction each one is actually about.
        """
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS",
                   "review_verdict": verdict, "reviewed_sha": h.HEAD}
        block = ("\n```adm-review-result\n" + json.dumps(h.review_decision(verdict=verdict)) + "\n```\n"
                 if decision else "")
        return (prose + "\n\nFor reference the format looks like this:\n\n```adm-result\n"
                + json.dumps(payload) + "\n```\n" + block)

    def extract_review(self, content):
        got = extract({"event_id": "evt-r1", "task_id": "t-1", "role": v.REVIEWER, "format": "text",
                       "session_id": h.REVIEWER_SESSION, "content": content})
        got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
        return got

    def test_prose_that_contradicts_the_payload_withdraws_the_claim(self):
        got = self.extract_review(self.review_text())
        self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))
        self.assertIn("extract.conflicting_statements", got["extraction"]["signals"])

    def test_such_a_review_can_never_complete_the_task(self):
        got = self.extract_review(self.review_text())
        decision = plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])
        self.assertEqual("contradictory_result", decision["failure_code"])

    def test_a_blockquoted_payload_is_not_read_at_all(self):
        quoted = "See the format:\n\n> ```adm-result\n> {\"schema_version\": \"adm-ai-result/1\"}\n> ```\n"
        got = self.extract_review(quoted)
        self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))

    def test_a_genuine_approval_is_still_accepted(self):
        # Round 4: the approval must now be stated, not inferred from the
        # absence of a rejection (Codex finding R3-1).
        got = self.extract_review(self.review_text(
            prose="Verdict: PASS\nI reviewed the diff and the tests. No blocking findings."))
        self.assertEqual(("PASS", v.REPORTED), (r.value(got, "review_verdict"), r.level(got, "review_verdict")))
        decision = plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
        self.assertEqual(v.MARK_COMPLETE, decision["action"])

    def test_research_rejection_evidence_is_not_a_contrary_verdict(self):
        """Rule 10 reports say "rejected the library"; that is not a verdict."""
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
        for prose in ("I evaluated three libraries and rejected all of them: none handles Windows paths.",
                      "Rejected the wrapper approach after a PoC; built it directly instead.",
                      "The parser rejects malformed payloads, as required."):
            with self.subTest(prose):
                content = prose + "\n\n```adm-result\n" + json.dumps(payload) + "\n```\n"
                got = extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text",
                               "content": content})
                self.assertEqual(("PASS", v.REPORTED), (r.value(got, "status"), r.level(got, "status")))
                self.assertNotIn("extract.conflicting_statements", got["extraction"]["signals"])

    def test_a_verdict_shaped_reject_still_withdraws_the_claim(self):
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
        content = "REJECT - the migration loses rows.\n\n```adm-result\n" + json.dumps(payload) + "\n```\n"
        got = extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text", "content": content})
        self.assertEqual(v.UNKNOWN, r.level(got, "status"))

    def test_a_worker_saying_the_opposite_of_its_own_payload_is_caught_too(self):
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
        content = ("This is not ready - needs more work on the parser.\n\n```adm-result\n"
                   + json.dumps(payload) + "\n```\n")
        got = extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text", "content": content})
        self.assertEqual(v.UNKNOWN, r.level(got, "status"))
        self.assertIn("extract.conflicting_statements", got["extraction"]["signals"])


class FindingARunnerAnchoredTestEvidence(unittest.TestCase):
    """Remediation Round 2 Finding A: only recognized runner outputs yield verified test counts."""

    def test_gate_passed_prose_cannot_produce_verified_test_counts(self):
        evidence = adm_test_evidence(execution_with("Gate 3 passed. Environment looks sane.", command="bash scripts/verify_env.sh"))
        self.assertNotIn("passed", evidence["runs"][0])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
        decision = plan(working(requirements=NO_REVIEW), h.event(verified))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_see_section_prose_cannot_produce_verified_test_counts(self):
        evidence = adm_test_evidence(execution_with("See section 12 passed to the parser for details.", command="cat docs/USAGE.md"))
        self.assertNotIn("passed", evidence["runs"][0])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
        decision = plan(working(requirements=NO_REVIEW), h.event(verified))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_example_summary_line_cannot_produce_verified_test_counts(self):
        evidence = adm_test_evidence(execution_with("Example summary line: = 99 passed in 1.00s =", command="echo"))
        self.assertNotIn("passed", evidence["runs"][0])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
        decision = plan(working(requirements=NO_REVIEW), h.event(verified))
        self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_exit_zero_with_no_recognizable_runner_output_is_unknown(self):
        for out in ("The migration passed validation.", "Step 5 failed to upload but retry succeeded.", "validation ok"):
            with self.subTest(out):
                evidence = adm_test_evidence(execution_with(out, command="bash run.sh", exit_code=0))
                self.assertNotIn("passed", evidence["runs"][0])
                verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                                     {"test_evidence": evidence}, [TestEvidenceProbe()])
                self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
                self.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    # Round 5 rewrote the five tests below. They used to assert that a genuine
    # runner summary *printed on stdout* produced VERIFIED counts. Codex then ran
    #
    #     echo documentation; pytest & echo ===== 12 passed in 3.10s =====
    #
    # through ADM's real validation path: only the echos executed, exit 0, and ADM
    # recorded VERIFIED tests_run=12 and completed the task. Designation was
    # decided for the command string while the counts were taken from the whole
    # output, so nothing tied a number to a process -- and no parser can repair
    # that, because `pytest && echo "===== 999 passed ====="` defeats a perfect
    # one. Counts now come only from a runner adapter bound to one spawned
    # process (manager.nextplan.runner).
    #
    # Each test therefore pins BOTH halves, and neither is weaker than what it
    # replaced: the printed summary must no longer verify anything, and the same
    # run recorded through the adapter must still complete the task. The summary
    # formats themselves are still parsed correctly -- see
    # test_nextplan_round5_findings.ParserStillReadsRunnerFormats -- the parser was
    # demoted, not deleted.

    def test_genuine_pytest_equals_summary_produces_verified_and_completes(self):
        printed_output_proves_nothing(self, "===== 12 passed in 3.10s =====")
        verified = worker_evidence(validated(passed=12))
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_genuine_pytest_comma_summary_produces_verified(self):
        printed_output_proves_nothing(self, "5 failed, 2927 passed")
        verified = worker_evidence(validated(passed=2927, failed=5, exit_code=1))
        self.assertEqual((2932, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(5, r.value(verified, "tests_failed"))

    def test_genuine_pytest_timed_comma_summary_produces_verified(self):
        printed_output_proves_nothing(self, "12 passed, 3 skipped in 1.21s")
        verified = worker_evidence(validated(passed=12, skipped=3))
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(3, r.value(verified, "tests_skipped"))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_mixed_output_extracts_only_genuine_summary(self):
        # The original point of this case was that a real summary buried in
        # narrative is still found. Round 5's point is stronger and subsumes it:
        # narrative and summary are indistinguishable in a shell record, so the
        # whole record proves nothing however it is mixed.
        mixed = (
            "Running validation...\n"
            "Gate 3 passed. Environment looks sane.\n"
            "See section 12 passed to the parser for details.\n"
            "===== 12 passed in 3.10s =====\n"
            "Finished.\n"
        )
        printed_output_proves_nothing(self, mixed)
        verified = worker_evidence(validated(passed=12))
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_unittest_complete_block_produces_verified_and_completes(self):
        printed_output_proves_nothing(self, "Ran 12 tests in 0.500s\n\nOK", command="python -m unittest")
        verified = worker_evidence(validated(passed=12))
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_unittest_ran_alone_without_outcome_is_unknown(self):
        """Trust model: 'Ran 12 tests' without OK/FAILED cannot prove tests passed."""
        evidence = adm_test_evidence(execution_with("Ran 12 tests in 0.500s", command="python -m unittest"))
        self.assertNotIn("passed", evidence["runs"][0])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual(v.UNKNOWN, r.level(verified, "tests_run"))
        self.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])


class FindingBReviewerRejectionProseAndNormalization(unittest.TestCase):
    """Remediation Round 2 Finding B: reviewer rejection prose & normalization withdraw payload PASS."""

    def review_text(self, prose, verdict="PASS", decision=True):
        """Round 5: a compliant reviewer also returns a bound adm-review-result/v1."""
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS",
                   "review_verdict": verdict, "reviewed_sha": h.HEAD}
        block = ("\n```adm-review-result\n" + json.dumps(h.review_decision(verdict=verdict)) + "\n```\n"
                 if decision else "")
        return (prose + "\n\nFor reference here is the payload format:\n\n```adm-result\n"
                + json.dumps(payload) + "\n```\n" + block)

    def extract_review(self, content):
        got = extract({"event_id": "evt-r2", "task_id": "t-1", "role": v.REVIEWER, "format": "text",
                       "session_id": h.REVIEWER_SESSION, "content": content})
        got["evidence"] = [{"kind": "review_notes", "ref": "review.md"}]
        return got

    def test_map_value_normalization(self):
        self.assertEqual("FAIL", _map_value("review_verdict", "rejected", v.REVIEWER))
        self.assertEqual("FAIL", _map_value("review_verdict", "rejected.", v.REVIEWER))
        self.assertEqual("FAIL", _map_value("review_verdict", "REJECTED", v.REVIEWER))
        self.assertEqual("FAIL", _map_value("review_verdict", "REJECTED:", v.REVIEWER))
        self.assertEqual("FAIL", _map_value("review_verdict", "rejected!", v.REVIEWER))
        self.assertEqual("FAIL", _map_value("review_verdict", "reject.", v.REVIEWER))
        self.assertIsNone(_map_value("review_verdict", "rejected library X", v.REVIEWER))
        self.assertIsNone(_map_value("review_verdict", "PASS_WITH_CAVEATS", v.REVIEWER))
        self.assertIsNone(_map_value("review_verdict", "APPROVED_WITH_COMMENTS", v.REVIEWER))

    def test_reviewer_contrary_prose_rejections_withdraw_payload_pass(self):
        rejection_cases = [
            "The reviewer rejects this implementation.",
            "I cannot approve this as it stands.",
            "I found two blocking issues that must be fixed first.",
            "This must be fixed before it can land.",
            "Verdict: rejected.",
            "I am withholding approval until the race is closed.",
        ]
        for prose in rejection_cases:
            with self.subTest(prose):
                content = self.review_text(prose=prose, verdict="PASS")
                got = self.extract_review(content)
                self.assertEqual(v.UNKNOWN, r.level(got, "review_verdict"))
                self.assertNotEqual("PASS", r.value(got, "review_verdict"))
                decision = plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
                self.assertNotEqual(v.MARK_COMPLETE, decision["action"])

    def test_false_positive_guardrails_preserve_legitimate_pass(self):
        research_prose_cases = [
            "rejected library X after evaluation",
            "rejected approach A and selected approach B",
            "rejected stale fixture",
            "rejected invalid input",
            "server rejected the push",
            "the parser rejected malformed input",
            "we rejected candidate A",
            "the API rejected the request",
        ]
        for prose in research_prose_cases:
            with self.subTest(prose):
                # Round 4: a genuine PASS is now stated, not left to be inferred
                # from the absence of a rejection (Codex finding R3-1). What this
                # test guards is unchanged: research-rejection prose must not
                # withdraw the approval the reviewer did state.
                content = self.review_text(prose=f"Verdict: PASS\nAnalysis: {prose}.\nI reviewed the implementation and all tests pass.")
                got = self.extract_review(content)
                self.assertEqual(("PASS", v.REPORTED), (r.value(got, "review_verdict"), r.level(got, "review_verdict")))
                self.assertNotIn("extract.conflicting_statements", got["extraction"]["signals"])
                decision = plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
                self.assertEqual(v.MARK_COMPLETE, decision["action"])


if __name__ == "__main__":
    unittest.main()
