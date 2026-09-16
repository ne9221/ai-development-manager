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
from manager.nextplan.extract import extract
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
                    generation=1)
    state.update(changes)
    return state


def execution_with(output_summary, command="pytest", exit_code=0):
    return {"repo_write_evidence": {
        "files_changed": ["pkg/a.py"], "commits": [h.HEAD], "final_commit_sha": h.HEAD, "branch": "feat/x",
        "worktree_path": "/w", "push_status": "verified", "remote_sha": h.HEAD, "tests_status": "passed",
        "tests": [{"command": command, "exit_code": exit_code, "output_summary": output_summary,
                   "started_at": "2026-09-16T00:00:00Z", "completed_at": "2026-09-16T00:00:01Z"}]}}


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
        evidence = adm_test_evidence(execution_with("== 12 passed in 3.10s =="))
        self.assertEqual(12, evidence["runs"][0]["passed"])
        verified, _ = verify(h.worker_result(drop=("tests_run", "tests_passed", "tests_failed")),
                             {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertEqual((12, v.VERIFIED), (r.value(verified, "tests_run"), r.level(verified, "tests_run")))
        self.assertEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])

    def test_a_failing_validation_run_is_still_caught(self):
        evidence = adm_test_evidence(execution_with("== 2 failed, 10 passed in 3.10s ==", exit_code=1))
        verified, report = verify(h.worker_result(), {"test_evidence": evidence}, [TestEvidenceProbe()])
        self.assertIn("verify.tests.failed", report["signals"])
        self.assertNotEqual(v.MARK_COMPLETE, plan(working(requirements=NO_REVIEW), h.event(verified))["action"])


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

    def review_text(self, verdict="PASS", prose="I found a real bug. CHANGES REQUIRED - this should not ship."):
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS",
                   "review_verdict": verdict, "reviewed_sha": h.HEAD}
        return prose + "\n\nFor reference the format looks like this:\n\n```adm-result\n" + json.dumps(payload) + "\n```\n"

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
        got = self.extract_review(self.review_text(prose="I reviewed the diff and the tests. No blocking findings."))
        self.assertEqual(("PASS", v.REPORTED), (r.value(got, "review_verdict"), r.level(got, "review_verdict")))
        decision = plan(reviewing(), h.event(got, role=v.REVIEWER, session_id=h.REVIEWER_SESSION, generation=1))
        self.assertEqual(v.MARK_COMPLETE, decision["action"])

    def test_a_worker_saying_the_opposite_of_its_own_payload_is_caught_too(self):
        payload = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "status": "PASS"}
        content = ("This is not ready - needs more work on the parser.\n\n```adm-result\n"
                   + json.dumps(payload) + "\n```\n")
        got = extract({"event_id": "e", "task_id": "t-1", "role": v.WORKER, "format": "text", "content": content})
        self.assertEqual(v.UNKNOWN, r.level(got, "status"))
        self.assertIn("extract.conflicting_statements", got["extraction"]["signals"])


if __name__ == "__main__":
    unittest.main()
