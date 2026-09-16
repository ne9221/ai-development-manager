"""The NextPlan scenario matrix: the twenty situations the plan requires.

Each scenario drives the real pipeline (extract -> verify -> classify -> plan)
with a stub probe standing in for the world, so the assertion is about the
decision ADM would actually take, not about a mocked planner.
"""

import json
import unittest

from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v
from manager.nextplan.classify import completion_proof
from manager.nextplan.pipeline import advance, process
from manager.nextplan.planner import apply, new_task_state, plan
from manager.nextplan.verify import ProbeUnavailable, observation

GIT_WORLD = {"head_sha": h.HEAD, "commit_sha": h.HEAD, "remote_sha": h.HEAD, "push_status": "pushed",
             "git_status": "clean", "branch": "feat/x"}
TEST_WORLD = {"tests_failed": 0, "tests_passed": 12, "tests_run": 12}


class Observes:
    """A probe that reports exactly the world a scenario describes."""

    def __init__(self, name="git", values=None, signals=(), contradict=(), unavailable=None):
        self.name, self._values = name, dict(values or {})
        self._signals, self._contradict, self._unavailable = set(signals), tuple(contradict), unavailable

    def run(self, result, context):
        if self._unavailable:
            raise ProbeUnavailable(self._unavailable)
        found = [observation(field, v.VERIFIED, value, self.name) for field, value in self._values.items()]
        found += [observation(field, v.CONTRADICTED, None, self.name, "the world says otherwise")
                  for field in self._contradict]
        return found, self._signals


def honest_world(**changes):
    probes = [Observes("git", dict(GIT_WORLD, **changes.pop("git", {})), changes.pop("git_signals", ())),
              Observes("test_evidence", dict(TEST_WORLD, **changes.pop("tests", {})), changes.pop("test_signals", ()))]
    return probes


def payload(**changes):
    report = {"schema_version": r.REPORT_SCHEMA_VERSION, "task_id": "t-1", "session_id": h.WORKER_SESSION,
              "agent": "claude", "status": "PASS", "branch": "feat/x", "base_sha": h.BASE, "head_sha": h.HEAD,
              "commit_sha": h.HEAD, "remote_sha": h.HEAD, "push_status": "pushed", "git_status": "clean",
              "tests_run": 12, "tests_passed": 12, "tests_failed": 0, "files_changed": ["pkg/a.py"],
              "evidence": [{"kind": "test_log", "ref": "pytest.txt"}]}
    report.update(changes)
    return report


def envelope(report=None, text=None, role=v.WORKER, session_id=None, generation=0, event_id="evt-1"):
    report = report if report is not None else payload()
    # Round 4: a payload carries the verdict's value but cannot authorize it.
    # A compliant reviewer states its decision in its own words, so that is
    # what the scenario reviewer now sends.
    lead = (f"Verdict: {report['review_verdict']}"
            if role == v.REVIEWER and report.get("review_verdict") else "Work finished.")
    content = text if text is not None else (
        lead + "\n\n```adm-result\n" + json.dumps(report, indent=2) + "\n```\n")
    return {"event_id": event_id, "task_id": "t-1", "role": role, "agent": "claude", "generation": generation,
            "format": "text", "content": content,
            "session_id": session_id or (h.WORKER_SESSION if role == v.WORKER else h.REVIEWER_SESSION)}


def review_payload(verdict="PASS", reviewed=h.HEAD, evidence=True, **changes):
    report = payload(session_id=h.REVIEWER_SESSION, agent="codex", review_verdict=verdict, reviewed_sha=reviewed,
                     review_status=verdict.lower(), status="PASS")
    report["evidence"] = [{"kind": "review_notes", "ref": "review.md"}] if evidence else []
    report.update(changes)
    return report


def task(**changes):
    state = new_task_state("t-1", reroute_candidates=["claude", "codex"], agent="claude")
    state.update(changes)
    return state


def working(**changes):
    changes.setdefault("phase_owner", {"role": v.WORKER, "session_id": None})
    return task(state=v.WORKING, **changes)


def reviewing(**changes):
    candidate = {"head_sha": h.HEAD, "proof": completion_proof(h.worker_result(), {}), "worker_session": h.WORKER_SESSION}
    base = task(state=v.AWAITING_REVIEW, phase_owner={"role": v.REVIEWER, "session_id": None},
                worker_session=h.WORKER_SESSION, worker_sessions=[h.WORKER_SESSION], candidate=candidate, generation=1)
    base.update(changes)
    return base


class ScenarioMatrixTests(unittest.TestCase):
    def decide(self, state, env, **kwargs):
        return advance(state, env, **kwargs)

    # 1
    def test_s01_verified_work_goes_to_review(self):
        record = self.decide(working(), envelope(), probes=honest_world())
        self.assertEqual(v.SEND_TO_REVIEW, record["decision"]["action"])
        self.assertTrue(record["decision"]["constraints"]["fresh_reviewer"])
        self.assertEqual(v.AWAITING_REVIEW, record["state_after"]["state"])

    # 2
    def test_s02_pass_with_unknown_tests_is_not_review_as_complete(self):
        probes = [Observes("git", GIT_WORLD)]  # nothing can speak for the tests
        record = self.decide(working(), envelope(), probes=probes)
        self.assertNotIn(record["decision"]["action"], (v.SEND_TO_REVIEW, v.MARK_COMPLETE))
        self.assertEqual("evidence_missing", record["decision"]["failure_code"])
        self.assertEqual(v.REPORTED, r.level(record["verified"], "tests_failed"))

    # 3
    def test_s03_reviewer_fail_returns_to_the_original_worker(self):
        record = self.decide(reviewing(), envelope(review_payload("FAIL"), role=v.REVIEWER, generation=1,
                                                   event_id="evt-r1"))
        self.assertEqual((v.RETURN_TO_WORKER, "reviewer_fail"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertEqual(h.WORKER_SESSION, record["decision"]["constraints"]["target_session"])
        self.assertEqual(v.REPAIRING, record["state_after"]["state"])

    # 4
    def test_s04_repair_goes_to_a_fresh_reviewer(self):
        after_fail = self.decide(reviewing(), envelope(review_payload("FAIL"), role=v.REVIEWER, generation=1,
                                                       event_id="evt-r1"))["state_after"]
        repaired = payload(head_sha=h.REPAIRED, commit_sha=h.REPAIRED, remote_sha=h.REPAIRED)
        world = honest_world(git={"head_sha": h.REPAIRED, "commit_sha": h.REPAIRED, "remote_sha": h.REPAIRED})
        record = self.decide(after_fail, envelope(repaired, generation=after_fail["generation"], event_id="evt-w2"),
                             probes=world)
        self.assertEqual(v.SEND_TO_REVIEW, record["decision"]["action"])
        self.assertIn(h.REVIEWER_SESSION, record["decision"]["constraints"]["exclude_sessions"])
        self.assertEqual(h.REPAIRED, record["state_after"]["candidate"]["head_sha"])

    # 5
    def test_s05_failed_push_retries_and_never_completes(self):
        # The agent reports the push failed and the world agrees: the commit is
        # local only. "failed" and "not_pushed" are the same observable fact.
        probes = [Observes("git", {"head_sha": h.HEAD, "commit_sha": h.HEAD, "git_status": "clean",
                                   "branch": "feat/x", "push_status": "not_pushed"},
                           ["verify.push.local_ahead_of_remote"]),
                  Observes("test_evidence", TEST_WORLD)]
        report = payload(status="FAIL", push_status="failed", remote_sha=None)
        record = self.decide(working(), envelope(report), probes=probes)
        self.assertEqual((v.RETRY_SAME_AGENT, "push_failed"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertNotEqual(v.CONTRADICTED, r.level(record["verified"], "push_status"))
        self.assertNotEqual(v.COMPLETE, record["state_after"]["state"])

    # 6
    def test_s06_quota_exhaustion_reroutes_or_waits(self):
        record = self.decide(working(), envelope(), probes=honest_world(), execution={"kind": "quota_exhausted"})
        self.assertEqual((v.REROUTE_AGENT, "codex"),
                         (record["decision"]["action"], record["decision"]["constraints"]["agent"]))
        alone = self.decide(working(reroute_candidates=["claude"]), envelope(), probes=honest_world(),
                            execution={"kind": "quota_exhausted"})
        self.assertEqual(v.WAIT_DEPENDENCY, alone["decision"]["action"])

    # 7
    def test_s07_unrelated_dirty_files_block_this_worktree(self):
        # The agent says nothing about the tree; the world shows dirt this task
        # does not own, so this is a blocked worktree, not a false claim.
        probes = honest_world(git={"git_status": "dirty", "dirty_paths": ["other/project.py"]},
                              git_signals=["verify.git_status.dirty_out_of_scope"])
        record = self.decide(working(), envelope(payload(git_status=None)), probes=probes)
        self.assertEqual((v.MARK_BLOCKED, "unrelated_dirty_files"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertEqual(v.BLOCKED, record["state_after"]["state"])

    # 8
    def test_s08_drive_unavailable_with_github_complete_is_a_partial_sync(self):
        requirements = dict(new_task_state("t-1")["requirements"], requires_drive_sync=True)
        probes = honest_world() + [Observes("drive_readback", {}, ["verify.drive.unreachable"])]
        record = self.decide(working(requirements=requirements),
                             envelope(payload(ssot_sync_status={"github": "synced", "drive": "synced"})), probes=probes)
        self.assertEqual((v.WAIT_DEPENDENCY, "drive_unavailable"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertTrue(r.is_verified(record["verified"], "remote_sha"))
        self.assertNotEqual(v.VERIFIED, r.level(record["verified"], "ssot_sync_drive"))

    # 9
    def test_s09_duplicate_result_is_an_idempotent_no_op(self):
        first = self.decide(working(), envelope(), probes=honest_world())
        second = self.decide(first["state_after"], envelope(generation=first["state_after"]["generation"]),
                             probes=honest_world())
        self.assertEqual((v.NO_OP, "duplicate_result"),
                         (second["decision"]["action"], second["decision"]["failure_code"]))
        self.assertEqual(first["state_after"], second["state_after"])

    # 10
    def test_s10_a_stale_result_never_overwrites_a_newer_transition(self):
        first = self.decide(working(), envelope(), probes=honest_world())
        stale = self.decide(first["state_after"], envelope(generation=0, event_id="evt-late"), probes=honest_world())
        self.assertEqual((v.NO_OP, "stale_task_state"),
                         (stale["decision"]["action"], stale["decision"]["failure_code"]))
        self.assertEqual(v.AWAITING_REVIEW, stale["state_after"]["state"])
        self.assertEqual(first["state_after"]["candidate"], stale["state_after"]["candidate"])

    # 11
    def test_s11_a_terminal_task_rejects_further_transitions(self):
        done = task(state=v.COMPLETE, generation=4)
        record = self.decide(done, envelope(generation=4, event_id="evt-after"), probes=honest_world())
        self.assertEqual((v.NO_OP, "task_already_completed"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertEqual(done, record["state_after"])

    # 12
    def test_s12_failing_tests_beat_the_agent_saying_done(self):
        probes = [Observes("git", GIT_WORLD),
                  Observes("test_evidence", {}, ["verify.tests.failed"], contradict=["tests_failed", "status"])]
        record = self.decide(working(), envelope(), probes=probes)
        self.assertEqual((v.RETURN_TO_WORKER, "false_pass"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertNotEqual(v.COMPLETE, record["state_after"]["state"])

    # 13
    def test_s13_a_review_pass_without_evidence_is_not_verified(self):
        record = self.decide(reviewing(), envelope(review_payload(reviewed=None, evidence=False), role=v.REVIEWER,
                                                   generation=1, event_id="evt-r1"))
        self.assertNotEqual(v.MARK_COMPLETE, record["decision"]["action"])
        self.assertEqual("evidence_missing", record["decision"]["failure_code"])
        self.assertEqual(v.SEND_TO_REVIEW, record["decision"]["action"])

    # 14
    def test_s14_an_agent_crash_retries_within_policy(self):
        record = self.decide(working(), envelope(text="(process died)"), execution={"kind": "crashed"})
        self.assertEqual((v.RETRY_SAME_AGENT, "agent_crash"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertEqual({"key": "code:agent_crash", "used": 0, "max": 2, "exhausted": False},
                         record["decision"]["budget"])

    # 15
    def test_s15_an_exhausted_retry_budget_reaches_a_human(self):
        state, actions = working(), []
        for index in range(3):
            env = envelope(text="(process died)", generation=state["generation"], event_id=f"evt-crash-{index}")
            record = self.decide(state, env, execution={"kind": "crashed"})
            actions.append(record["decision"]["action"])
            state = record["state_after"]
        self.assertEqual([v.RETRY_SAME_AGENT, v.RETRY_SAME_AGENT, v.HUMAN_GATE], actions)
        self.assertEqual(v.HUMAN_GATE_STATE, state["state"])

    # 16
    def test_s16_a_conflicting_writer_is_blocked(self):
        owned = working(phase_owner={"role": v.WORKER, "session_id": h.WORKER_SESSION})
        record = self.decide(owned, envelope(session_id="s-someone-else"), probes=honest_world())
        self.assertEqual((v.MARK_BLOCKED, "conflicting_owner"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))

    # 17
    def test_s17_the_wrong_repository_is_a_hard_stop(self):
        probes = honest_world(git_signals=["verify.repo.identity_mismatch"])
        record = self.decide(working(), envelope(), probes=probes)
        self.assertEqual((v.HUMAN_GATE, "wrong_repo"),
                         (record["decision"]["action"], record["decision"]["failure_code"]))
        self.assertIsNone(record["state_after"]["phase_owner"])

    # 18
    def test_s18_a_malformed_result_routes_safely(self):
        broken = "All done!\n```adm-result\n{\"schema_version\": \"adm-ai-result/1\", \"status\": \"PASS\",,}\n```\n"
        record = self.decide(working(), envelope(text=broken), probes=honest_world())
        self.assertEqual("malformed_result", record["decision"]["failure_code"])
        self.assertEqual(v.CONTINUE_WORKER, record["decision"]["action"])
        self.assertEqual(v.UNKNOWN, r.level(record["verified"], "status"))

    # 19
    def test_s19_partial_completion_continues(self):
        record = self.decide(working(), envelope(payload(status="PARTIAL", progress_percent=60)), probes=honest_world())
        self.assertEqual(v.CONTINUE_WORKER, record["decision"]["action"])
        self.assertEqual(v.WORKING, record["state_after"]["state"])

    # 20
    def test_s20_a_verified_candidate_approved_by_a_fresh_reviewer_completes(self):
        worker = self.decide(working(), envelope(), probes=honest_world())
        self.assertEqual(v.SEND_TO_REVIEW, worker["decision"]["action"])
        state = worker["state_after"]
        review = self.decide(state, envelope(review_payload(), role=v.REVIEWER, generation=state["generation"],
                                             event_id="evt-r1"))
        self.assertEqual(v.MARK_COMPLETE, review["decision"]["action"])
        self.assertEqual(v.COMPLETE, review["state_after"]["state"])
        self.assertTrue(all(item["ok"] for item in review["decision"]["proof"]["candidate"]))
        after = self.decide(review["state_after"], envelope(generation=review["state_after"]["generation"],
                                                            event_id="evt-extra"), probes=honest_world())
        self.assertEqual(v.NO_OP, after["decision"]["action"])


class ScenarioCoverageTests(unittest.TestCase):
    def test_the_matrix_covers_all_twenty_scenarios(self):
        names = [name for name in dir(ScenarioMatrixTests) if name.startswith("test_s")]
        self.assertEqual(20, len(names))
        self.assertEqual({f"{index:02d}" for index in range(1, 21)}, {name[6:8] for name in names})


if __name__ == "__main__":
    unittest.main()
