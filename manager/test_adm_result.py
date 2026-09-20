"""Slice A: the adm-result/v1 producer contract, proven before it is wired.

Every group below is the named group of the Slice-A test plan (1-42), plus
generated matrices where a matrix says more than a fixture. All storage is an
in-memory :class:`MemoryRecordStore`; nothing here touches Drive, the manager
home, or ``terminalize_execution``.

The invariants the future NextPlan consumer must keep are pinned here as
*facts about the record*, not as planner decisions:

* worker success alone is only ``execution.status == "completed"``;
* tests alone are only ``tests.tests_status``;
* reviewer prose alone is only ``review.decision_statements``;
* an unbound PASS is ``review.authority.authorized == False``;
* a result without a candidate binding has ``candidate.candidate_sha == None``.

None of these is an acceptance state, and the record cannot express one.
"""

import copy
import hashlib
import itertools
import json
import struct
import unittest

from manager import adm_result as a
from manager.nextplan import contracts
from manager.nextplan import harness as h
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

PROJECT = "p1"
TASK = h.TASK_ID
WORKER_EXEC = "exec-w1"
REVIEW_EXEC = "exec-r1"
BASE = h.BASE
CAND = h.HEAD
OTHER = h.REPAIRED
RAW = hashlib.sha256(b"raw provider output").hexdigest()
REPO_URL = "https://github.com/ne9221/ai-development-manager.git"
REPO = "github:ne9221/ai-development-manager"
BRANCH_SHORT = "adm/p1/t-1"
BRANCH = "refs/heads/" + BRANCH_SHORT
WORKTREE = "C:/adm/worktrees/p1/t-1"
PRODUCED_AT = "2026-09-20T05:30:00Z"
NO_PFP = {"required": False, "basis": "task is not production-affecting"}
PFP_EVIDENCE = {"record_id": "pfp-record-1", "record_sha256": "1" * 64, "identity": dict(a.PFP_CANONICAL_IDENTITY)}


# -- fixtures -----------------------------------------------------------------------------

def legacy_step(exit_code=0, output="===== 12 passed in 1.00s =====", command="python -m pytest -q", timed_out=False):
    return {"command": command, "executable": "python", "exit_code": exit_code, "output_summary": output,
            "started_at": "2026-09-20T05:05:00Z", "completed_at": "2026-09-20T05:06:00Z", "timed_out": timed_out}


def repo_write_evidence(candidate=CAND, tests=None, tests_status="passed"):
    return {"files_changed": ["manager/x.py"], "commits": [candidate], "final_commit_sha": candidate,
            "branch": BRANCH_SHORT, "worktree_path": WORKTREE, "push_status": "verified", "remote_sha": candidate,
            "tests": [legacy_step()] if tests is None else list(tests), "tests_status": tests_status}


def worker_execution(status="completed", retry_count=0, evidence=True, **overrides):
    record = {
        "execution_id": WORKER_EXEC, "task_id": TASK, "project_id": PROJECT, "provider": "claude",
        "mode": "hands_off", "effort": "high", "reserved_at": "2026-09-20T04:59:00Z",
        "started_at": "2026-09-20T05:00:00Z", "completed_at": "2026-09-20T05:10:00Z",
        "finished_at": "2026-09-20T05:10:00Z", "elapsed_minutes": 10, "status": status,
        "session_id": "claude:s-worker-1", "provider_session_id": "s-worker-1", "account_id": "acct-a",
        "retry_count": retry_count, "retry_of_execution_id": WORKER_EXEC if retry_count else None,
        "quota_before": {"claude": {"used_percent": 10}}, "quota_after": {"claude": {"used_percent": 12}},
        "quota_delta": {"claude": {"used_percent": 2}}, "source_confidence": "official",
        "access": "production_write",
        "lease_evidence": {"authority": "acquired", "lock_id": "repo-" + "0" * 64, "generation": 1,
                           "repository": REPO, "branch": BRANCH, "scope": ["manager/"], "baseline_head": BASE},
        "cleanup_evidence": {"persistence": "complete", "task_claim_release": "released",
                             "writer_release": "released", "errors": [], "extra_audit": "kept-by-digest"},
        "terminal_reason": status, "notes": [], "task_snapshot": {},
        "repo_write_evidence": repo_write_evidence() if evidence else None,
    }
    record.update(overrides)
    return record


def reviewer_execution(status="completed", **overrides):
    record = {
        "execution_id": REVIEW_EXEC, "task_id": TASK, "project_id": PROJECT, "provider": "codex",
        "mode": "read_only", "effort": "high", "reserved_at": "2026-09-20T05:11:00Z",
        "started_at": "2026-09-20T05:12:00Z", "completed_at": "2026-09-20T05:20:00Z",
        "finished_at": "2026-09-20T05:20:00Z", "elapsed_minutes": 8, "status": status,
        "session_id": "codex:s-reviewer-1", "provider_session_id": "s-reviewer-1", "account_id": None,
        "retry_count": 0, "retry_of_execution_id": None,
        "quota_before": None, "quota_after": None, "quota_delta": None, "source_confidence": None,
        "access": "read_only", "lease_evidence": None, "cleanup_evidence": None,
        "terminal_reason": status, "notes": [], "task_snapshot": {}, "repo_write_evidence": None,
    }
    record.update(overrides)
    return record


def repository(**overrides):
    block = {"repository": REPO_URL, "branch": BRANCH_SHORT, "base_sha": BASE, "worktree_path": WORKTREE,
             "worktree_identity": {"worktree_id": "wt-p1--t-1", "branch": BRANCH, "branch_short": BRANCH_SHORT}}
    block.update(overrides)
    return block


def worker_command(**overrides):
    command = {"command_id": "cmd-w1", "project_id": PROJECT, "task_id": TASK, "provider": "claude",
               "model": "claude-fable-5-1", "execution_id": WORKER_EXEC, "status": "completed",
               "result": {"status": "completed", "execution_id": WORKER_EXEC, "session_id": None, "error_kind": None}}
    command.update(overrides)
    return command


def worker_actor():
    return {"provider": "claude", "account_id": "acct-a", "session_id": "claude:s-worker-1",
            "provider_session_id": "s-worker-1", "model": None, "mode": "hands_off", "effort": "high"}


_UNSET = object()


def review_input(decisions=_UNSET, statements=_UNSET, worker=_UNSET, dispatch=_UNSET, **overrides):
    block = {"dispatch": h.review_dispatch() if dispatch is _UNSET else dispatch,
             "decisions": [h.review_decision(target=CAND)] if decisions is _UNSET else decisions,
             "decision_statements": [] if statements is _UNSET else statements,
             "worker_actor": worker_actor() if worker is _UNSET else worker,
             "evidence_artifact": {"ref": "drive:review-artifact-1", "sha256": "2" * 64},
             "reviewed_at": "2026-09-20T05:19:00Z"}
    block.update(overrides)
    return block


DISPATCH_CANDIDATE = {"candidate_sha": CAND, "source": "dispatch_record", "push_status": "verified", "remote_sha": CAND}


def produce_worker(execution=None, **kw):
    kw.setdefault("role", v.WORKER)
    kw.setdefault("lineage", {"trigger": "initial"})
    kw.setdefault("repository", repository())
    kw.setdefault("produced_at", PRODUCED_AT)
    kw.setdefault("pfp", NO_PFP)
    return a.produce_adm_result(worker_execution() if execution is None else execution, **kw)


def produce_reviewer(execution=None, review=_UNSET, **kw):
    kw.setdefault("role", v.REVIEWER)
    kw.setdefault("lineage", {"trigger": "review", "reviews_execution_id": WORKER_EXEC, "reviewer_run_id": h.REVIEWER_RUN})
    kw.setdefault("repository", repository())
    kw.setdefault("produced_at", PRODUCED_AT)
    kw.setdefault("pfp", NO_PFP)
    kw.setdefault("candidate", DISPATCH_CANDIDATE)
    return a.produce_adm_result(reviewer_execution() if execution is None else execution,
                                review=review_input() if review is _UNSET else review, **kw)


def normalized(role=v.WORKER, task_id=TASK, raw_sha256=RAW, facts=None, decisions=(), statements=()):
    result = r.blank_result("evt-1", task_id, role, tier="fenced", raw_sha256=raw_sha256)
    for field, (value, level, source) in (facts or {}).items():
        result = r.with_fact(result, field, r.fact(value, level, source))
    result["decisions"] = [copy.deepcopy(d) for d in decisions]
    result["decision_statements"] = [dict(s) for s in statements]
    return r.validate_result(result)


class MemoryRecordStore(a.RecordStore):
    """Create-only fake of the future Drive ADM-RESULTS area, with fault hooks."""

    def __init__(self):
        self.records = {}
        self.creates = 0
        self.corrupt_reads_of = set()
        self.fail_create_after_write = False
        self.race_with = None  # payload another writer stores first

    def read(self, project_id, result_id):
        payload = self.records.get((project_id, result_id))
        if payload is not None and result_id in self.corrupt_reads_of:
            return payload[:-1] + (b"}" if payload[-1:] != b"}" else b" ")
        return payload

    def create(self, project_id, result_id, payload):
        self.creates += 1
        if self.race_with is not None:
            self.records[(project_id, result_id)] = self.race_with
            self.race_with = None
            raise a.RecordExists(result_id)
        if (project_id, result_id) in self.records:
            raise a.RecordExists(result_id)
        self.records[(project_id, result_id)] = bytes(payload)
        if self.fail_create_after_write:
            self.fail_create_after_write = False
            raise RuntimeError("connection dropped after the write landed")


def independent_result_id(project_id, task_id, execution_id, retry_count, role):
    """A second, hand-rolled encoding of the published algorithm."""
    blob = b""
    for part in (project_id, task_id, execution_id, str(retry_count), role):
        data = part.encode("utf-8")
        blob += struct.pack(">Q", len(data)) + data
    return "ar-" + hashlib.sha256(blob).hexdigest()


def object_paths(value, path=()):
    """Every path at which ``value`` holds a dict."""
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from object_paths(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from object_paths(item, path + (index,))


def at(value, path):
    for key in path:
        value = value[key]
    return value


def assert_invalid(test, result, fragment=None):
    with test.assertRaises(a.AdmResultValidationError) as caught:
        a.validate_adm_result(result)
    if fragment:
        test.assertIn(fragment, str(caught.exception))
    return caught.exception


# -- 1-3: schema closure and version ----------------------------------------------------------

class GroupSchemaClosure(unittest.TestCase):

    def test_1_schema_closed_at_top_level(self):
        schema = a.schema()
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(sorted(schema["required"]), sorted(schema["properties"]))
        result = produce_worker()
        result["extra"] = "anything"
        assert_invalid(self, result, "Additional properties")

    def test_2_every_object_node_in_the_schema_is_closed(self):
        closed, open_nodes = 0, []

        def walk(node, path):
            nonlocal closed
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node and "if" not in path:
                    if node.get("additionalProperties") is False:
                        closed += 1
                    else:
                        open_nodes.append("/".join(map(str, path)))
                for key, item in node.items():
                    walk(item, path + (key,))
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, path + (index,))

        schema = a.schema()
        walk(schema["properties"], ("properties",))
        walk(schema["$defs"], ("$defs",))
        # `then` refinements under if/then restate a subset of already-closed properties.
        open_nodes = [p for p in open_nodes if "/then" not in p and "/if" not in p]
        self.assertEqual([], open_nodes)
        self.assertGreater(closed, 30)

    def test_2_nested_unknown_field_rejected_at_every_object_path(self):
        results = {"worker": produce_worker(normalized_result=normalized(), agent_output={"ref": "out", "sha256": RAW},
                                            validation_results=[h.validation_result(execution_id="exec-v1")],
                                            validation_registry=h.execution_registry([h.validation_result(execution_id="exec-v1")]),
                                            command=worker_command()),
                   "reviewer": produce_reviewer(pfp={"required": True, "basis": "production fix", "evidence": PFP_EVIDENCE})}
        checked = 0
        for label, result in results.items():
            for path in object_paths(result):
                if "observed" in path:
                    continue  # the one free-form value: a probe observation
                mutated = copy.deepcopy(result)
                at(mutated, path)["zz_unknown"] = 1
                mutated["result_digest"] = a.result_digest_for(mutated)
                with self.subTest(result=label, path="/".join(map(str, path)) or "<root>"):
                    with self.assertRaises(a.AdmResultValidationError) as caught:
                        a.validate_adm_result(mutated)
                    self.assertIn("zz_unknown", str(caught.exception))
                    checked += 1
        self.assertGreater(checked, 40)

    def test_2_normalized_result_and_review_decision_reuse_closed_contracts(self):
        result = produce_worker(normalized_result=normalized(), agent_output={"ref": "out", "sha256": RAW})
        result["normalized_result"]["zz_unknown"] = 1
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "zz_unknown")
        decision = h.review_decision(target=CAND)
        decision["blocking"] = True
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(review=review_input(decisions=[decision]))
        self.assertIn("blocking", str(caught.exception))

    def test_3_schema_version_is_exactly_v1(self):
        self.assertEqual("adm-result/v1", a.SCHEMA_VERSION)
        self.assertEqual({"const": "adm-result/v1"}, a.schema()["properties"]["schema"])
        self.assertEqual("adm-result/v1", produce_worker()["schema"])

    def test_35_unknown_schema_version_rejected(self):
        for version in ("adm-result/v2", "adm-result/1", "adm-result", "", None, 1):
            result = produce_worker()
            result["schema"] = version
            result["result_digest"] = a.result_digest_for(result)
            with self.subTest(version=version):
                assert_invalid(self, result, "schema")

    def test_36_arbitrary_unknown_fields_rejected(self):
        for name in ("status_text", "provider_note", "accepted_by", "x"):
            result = produce_worker()
            result[name] = "prose"
            result["result_digest"] = a.result_digest_for(result)
            with self.subTest(field=name):
                assert_invalid(self, result, name)


# -- 4-7: canonical bytes, digest, identity ------------------------------------------------------

class GroupIdentityAndDigest(unittest.TestCase):

    def test_4_canonical_serialization_is_pinned(self):
        value = {"b": 1, "a": {"z": [1, 2, None, True], "y": "é/\"quote\""}, "c": ""}
        expected = '{"a":{"y":"é/\\"quote\\"","z":[1,2,null,true]},"b":1,"c":""}'.encode("utf-8")
        self.assertEqual(expected, a.canonical_bytes(value))
        self.assertEqual(hashlib.sha256(expected).hexdigest(), a.canonical_sha256(value))
        reordered = {"c": "", "a": {"y": "é/\"quote\"", "z": [1, 2, None, True]}, "b": 1}
        self.assertEqual(a.canonical_bytes(value), a.canonical_bytes(reordered))
        self.assertNotEqual(a.canonical_bytes(value), a.canonical_bytes({**value, "c": " "}))

    def test_4_canonical_serialization_refuses_ambiguous_values(self):
        for bad in ({"x": 1.0}, {"x": float("nan")}, {1: "int key"}, {"x": {"y": [0.5]}}, {"x": object()}):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(a.AdmResultValidationError):
                    a.canonical_bytes(bad)

    def test_5_result_digest_binds_everything_but_itself(self):
        result = produce_worker()
        payload = {k: val for k, val in result.items() if k != "result_digest"}
        independent = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(independent, result["result_digest"])
        self.assertEqual(("result_digest",), a.DIGEST_EXCLUDED_FIELDS)
        self.assertEqual(result["result_digest"], produce_worker()["result_digest"])
        changed = produce_worker(warnings=["one more warning"])
        self.assertNotEqual(result["result_digest"], changed["result_digest"])
        self.assertEqual(result["result_id"], changed["result_id"])
        tampered = copy.deepcopy(result)
        tampered["warnings"].append("edited after digest")
        assert_invalid(self, tampered, "result_digest")

    def test_5_same_semantic_object_same_digest(self):
        result = produce_worker()
        reloaded = json.loads(a.canonical_bytes(result).decode("utf-8"))
        shuffled = {k: reloaded[k] for k in reversed(list(reloaded))}
        self.assertEqual(result["result_digest"], a.result_digest_for(shuffled))
        self.assertEqual(a.canonical_bytes(result), a.canonical_bytes(shuffled))

    def test_6_result_id_is_length_prefixed_and_pinned(self):
        self.assertEqual(independent_result_id(PROJECT, TASK, WORKER_EXEC, 0, "worker"),
                         a.result_id_for(PROJECT, TASK, WORKER_EXEC, 0, "worker"))
        self.assertEqual(a.result_id_for(PROJECT, TASK, WORKER_EXEC, 0, "worker"), produce_worker()["result_id"])
        # Pinned vector, computed by hand from the published encoding.
        blob = (struct.pack(">Q", 2) + b"p1" + struct.pack(">Q", 3) + b"t-1" + struct.pack(">Q", 7) + b"exec-w1"
                + struct.pack(">Q", 1) + b"0" + struct.pack(">Q", 6) + b"worker")
        self.assertEqual("ar-" + hashlib.sha256(blob).hexdigest(), a.result_id_for(PROJECT, TASK, WORKER_EXEC, 0, "worker"))
        self.assertEqual(blob, a.length_prefixed(PROJECT, TASK, WORKER_EXEC, "0", "worker"))

    def test_6_no_delimiter_collision(self):
        left = ("a/b", "c", "e", 0, "worker")
        right = ("a", "b/c", "e", 0, "worker")
        self.assertEqual("/".join(map(str, left)), "/".join(map(str, right)))  # naive joining would collide
        self.assertNotEqual(a.result_id_for(*left), a.result_id_for(*right))

    def test_6_every_tuple_component_changes_the_id(self):
        base = (PROJECT, TASK, WORKER_EXEC, 0, "worker")
        variants = [("p2", TASK, WORKER_EXEC, 0, "worker"), (PROJECT, "t-2", WORKER_EXEC, 0, "worker"),
                    (PROJECT, TASK, "exec-w2", 0, "worker"), (PROJECT, TASK, WORKER_EXEC, 1, "worker"),
                    (PROJECT, TASK, WORKER_EXEC, 0, "reviewer")]
        ids = {a.result_id_for(*base)}
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotIn(a.result_id_for(*variant), ids)
                ids.add(a.result_id_for(*variant))
        self.assertEqual(6, len(ids))

    def test_7_retry_count_participates_in_result_id(self):
        first = produce_worker(worker_execution(retry_count=0))
        retry = produce_worker(worker_execution(retry_count=1),
                               lineage={"trigger": "retry", "retry_of_execution_id": WORKER_EXEC})
        self.assertEqual(first["identity"]["execution_id"], retry["identity"]["execution_id"])
        self.assertNotEqual(first["result_id"], retry["result_id"])
        self.assertEqual(independent_result_id(PROJECT, TASK, WORKER_EXEC, 1, "worker"), retry["result_id"])
        for count in range(0, 4):
            with self.subTest(retry_count=count):
                self.assertEqual(independent_result_id(PROJECT, TASK, WORKER_EXEC, count, "worker"),
                                 a.result_id_for(PROJECT, TASK, WORKER_EXEC, count, "worker"))

    def test_6_result_id_refuses_bad_components(self):
        for args in ((PROJECT, TASK, WORKER_EXEC, -1, "worker"), (PROJECT, TASK, WORKER_EXEC, True, "worker"),
                     (PROJECT, TASK, WORKER_EXEC, "0", "worker"), (PROJECT, TASK, WORKER_EXEC, 0, "tester"),
                     ("", TASK, WORKER_EXEC, 0, "worker"), (None, TASK, WORKER_EXEC, 0, "worker")):
            with self.subTest(args=args):
                with self.assertRaises(a.AdmResultValidationError):
                    a.result_id_for(*args)

    def test_result_id_tampering_is_detected(self):
        result = produce_worker()
        result["result_id"] = a.result_id_for(PROJECT, TASK, WORKER_EXEC, 1, "worker")
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "result_id")


# -- 8-11: exact binding ------------------------------------------------------------------------

class GroupBinding(unittest.TestCase):

    def test_8_candidate_binds_exactly_to_repo_write_evidence(self):
        result = produce_worker()
        self.assertEqual({"candidate_sha": CAND, "source": "repo_write_evidence", "push_status": "verified", "remote_sha": CAND},
                         result["candidate"])
        same = produce_worker(candidate={"candidate_sha": CAND, "source": "repo_write_evidence", "push_status": "verified", "remote_sha": CAND})
        self.assertEqual(result["result_digest"], same["result_digest"])

    def test_8_no_change_evidence_carries_no_candidate(self):
        execution = worker_execution()
        execution["repo_write_evidence"] = {"files_changed": [], "commits": [], "final_commit_sha": None, "branch": BRANCH_SHORT,
                                            "worktree_path": WORKTREE, "push_status": "not_applicable", "remote_sha": None,
                                            "tests": [legacy_step()], "tests_status": "passed"}
        result = produce_worker(execution)
        self.assertEqual({"candidate_sha": None, "source": "repo_write_evidence", "push_status": "not_applicable", "remote_sha": None},
                         result["candidate"])

    def test_8_failed_execution_without_evidence_has_no_candidate(self):
        result = produce_worker(worker_execution(status="failed", evidence=False), failure_classification="provider_error")
        self.assertEqual({"candidate_sha": None, "source": "none", "push_status": "not_applicable", "remote_sha": None}, result["candidate"])
        self.assertEqual("not_observed", result["tests"]["tests_status"])

    def test_8_candidate_verified_requires_remote_equality(self):
        result = produce_worker()
        result["candidate"]["remote_sha"] = OTHER
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "candidate_sha == remote_sha")

    def test_9_task_mismatch_rejected(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(command=worker_command(task_id="t-2"))
        self.assertIn("command.task_id", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(normalized_result=normalized(task_id="t-2"), agent_output={"ref": "out", "sha256": RAW})
        self.assertIn("normalized_result.task_id", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(verification={"probes_run": [], "probes_unavailable": [], "observations": [], "signals": [],
                                         "verified_result": normalized(task_id="t-2")})

    def test_10_execution_mismatch_rejected(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(command=worker_command(execution_id="exec-other"))
        self.assertIn("command.execution_id", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(lineage={"trigger": "review", "reviews_execution_id": REVIEW_EXEC, "reviewer_run_id": h.REVIEWER_RUN})
        self.assertIn("reviews_execution_id", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(command=worker_command(project_id="p2"))
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(command=worker_command(provider="codex"))

    def test_11_repository_mismatch_rejected(self):
        cases = {
            "repository": repository(repository="https://github.com/ne9221/other-repo"),
            "branch": repository(branch="adm/p1/t-9"),
            "base_sha": repository(base_sha=OTHER),
            "worktree_path": repository(worktree_path="C:/elsewhere"),
            "worktree_identity.branch": repository(worktree_identity={"worktree_id": "wt", "branch": "refs/heads/x", "branch_short": "x"}),
        }
        for label, block in cases.items():
            with self.subTest(mismatch=label):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_worker(repository=block)

    def test_11_repository_canonical_forms_are_the_existing_contract(self):
        result = produce_worker(repository=repository(repository="git@github.com:ne9221/ai-development-manager.git", branch=BRANCH))
        self.assertEqual(REPO, result["repository"]["repository"])
        self.assertEqual(BRANCH, result["repository"]["branch"])
        for bad in ("https://gitlab.com/x/y", "not a repo", "", None):
            with self.subTest(repository=bad):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_worker(worker_execution(lease_evidence=None), repository=repository(repository=bad))
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(worker_execution(lease_evidence=None), repository=repository(branch="HEAD"))

    def test_identity_binds_command_and_model_without_fabrication(self):
        result = produce_worker(command=worker_command())
        self.assertEqual("cmd-w1", result["identity"]["command_id"])
        self.assertEqual("claude-fable-5-1", result["actor"]["model"])
        bare = produce_worker()
        self.assertIsNone(bare["identity"]["command_id"])
        self.assertIsNone(bare["actor"]["model"])
        self.assertEqual({"provider": "claude", "account_id": "acct-a", "session_id": "claude:s-worker-1",
                          "provider_session_id": "s-worker-1", "model": None, "mode": "hands_off", "effort": "high"}, bare["actor"])

    def test_record_sha256_binds_the_execution_record(self):
        execution = worker_execution()
        result = produce_worker(execution)
        self.assertEqual(a.canonical_sha256(execution), result["execution"]["record_sha256"])
        execution["notes"].append("one more note")
        self.assertNotEqual(result["execution"]["record_sha256"], produce_worker(execution)["execution"]["record_sha256"])
        self.assertEqual(a.canonical_sha256(execution["cleanup_evidence"]), result["execution"]["cleanup"]["cleanup_sha256"])
        self.assertNotIn("extra_audit", result["execution"]["cleanup"])


# -- 12-13: terminal gate ---------------------------------------------------------------------------

class GroupTerminalGate(unittest.TestCase):

    def test_12_non_terminal_execution_rejected(self):
        for status in ("reserved", "running"):
            with self.subTest(status=status):
                with self.assertRaises(a.AdmResultValidationError) as caught:
                    produce_worker(worker_execution(status=status))
                self.assertIn("not terminal", str(caught.exception))

    def test_13_cancelled_execution_rejected(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(worker_execution(status="cancelled"))
        self.assertIn("cancelled", str(caught.exception))
        self.assertEqual(("reserved", "running", "cancelled"), a.NON_PRODUCIBLE_STATUSES)
        self.assertEqual(("completed", "failed", "interrupted"), a.TERMINAL_STATUSES)

    def test_12_unknown_status_rejected(self):
        for status in ("done", "", None, "COMPLETED"):
            with self.subTest(status=status):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_worker(worker_execution(status=status))

    def test_terminal_states_all_produce(self):
        for status in ("completed", "failed", "interrupted"):
            with self.subTest(status=status):
                result = produce_worker(worker_execution(status=status, evidence=(status == "completed")),
                                        failure_classification=None if status == "completed" else "provider_error")
                self.assertEqual(status, result["execution"]["status"])
        result = produce_worker()
        result["execution"]["status"] = "running"
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "status")

    def test_execution_success_is_not_acceptance(self):
        result = produce_worker()
        self.assertEqual("completed", result["execution"]["status"])
        for key in ("accepted", "acceptance_state", "complete", "state", "next_action"):
            self.assertNotIn(key, result)
            self.assertNotIn(key, result["execution"])

    def test_completed_execution_carries_no_failure_classification(self):
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(failure_classification="provider_error")
        failed = produce_worker(worker_execution(status="failed", evidence=False),
                                command=worker_command(status="failed", result={"status": "error", "execution_id": WORKER_EXEC,
                                                                                 "session_id": None, "error_kind": "claim_timeout"}))
        self.assertEqual("claim_timeout", failed["execution"]["failure_classification"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(worker_execution(status="failed", evidence=False), failure_classification="other",
                           command=worker_command(status="failed", result={"status": "error", "execution_id": WORKER_EXEC,
                                                                            "session_id": None, "error_kind": "claim_timeout"}))


# -- 14-19: lineage ---------------------------------------------------------------------------------

class GroupLineage(unittest.TestCase):

    def test_14_legal_initial_lineage(self):
        result = produce_worker()
        self.assertEqual({"trigger": "initial", "retry_of_execution_id": None, "repair_of_execution_id": None,
                          "continues_execution_id": None, "reviews_execution_id": None, "reviewer_run_id": None}, result["lineage"])

    def test_15_legal_retry_lineage(self):
        result = produce_worker(worker_execution(retry_count=2), lineage={"trigger": "retry", "retry_of_execution_id": WORKER_EXEC})
        self.assertEqual("retry", result["lineage"]["trigger"])
        self.assertEqual(WORKER_EXEC, result["lineage"]["retry_of_execution_id"])
        self.assertEqual(2, result["identity"]["retry_count"])
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(worker_execution(retry_count=0), lineage={"trigger": "retry", "retry_of_execution_id": WORKER_EXEC})
        self.assertIn("retry_count > 0", str(caught.exception))

    def test_16_legal_repair_lineage(self):
        result = produce_worker(worker_execution(execution_id="exec-w2"),
                                lineage={"trigger": "repair", "repair_of_execution_id": WORKER_EXEC})
        self.assertEqual(WORKER_EXEC, result["lineage"]["repair_of_execution_id"])
        self.assertNotEqual(result["result_id"], produce_worker()["result_id"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(lineage={"trigger": "repair", "repair_of_execution_id": WORKER_EXEC})  # cannot repair itself
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(worker_execution(execution_id="exec-w2", retry_count=1),
                           lineage={"trigger": "repair", "repair_of_execution_id": WORKER_EXEC})  # a rerun is a retry

    def test_17_legal_continuation_lineage(self):
        result = produce_worker(worker_execution(execution_id="exec-w2"),
                                lineage={"trigger": "continuation", "continues_execution_id": WORKER_EXEC})
        self.assertEqual(WORKER_EXEC, result["lineage"]["continues_execution_id"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(lineage={"trigger": "continuation", "continues_execution_id": WORKER_EXEC})

    def test_18_legal_review_lineage(self):
        result = produce_reviewer()
        self.assertEqual({"trigger": "review", "retry_of_execution_id": None, "repair_of_execution_id": None,
                          "continues_execution_id": None, "reviews_execution_id": WORKER_EXEC,
                          "reviewer_run_id": h.REVIEWER_RUN}, result["lineage"])
        rerun = produce_reviewer(reviewer_execution(retry_count=1, retry_of_execution_id=REVIEW_EXEC))
        self.assertEqual(1, rerun["identity"]["retry_count"])
        self.assertNotEqual(result["result_id"], rerun["result_id"])

    def test_19_illegal_mixed_lineage_rejected_generated(self):
        links = a.LINEAGE_LINKS
        values = {"retry_of_execution_id": WORKER_EXEC, "repair_of_execution_id": "exec-w0",
                  "continues_execution_id": "exec-w0", "reviews_execution_id": WORKER_EXEC, "reviewer_run_id": h.REVIEWER_RUN}
        checked = legal = 0
        for trigger, role, retry_count in itertools.product(a.TRIGGERS, v.ROLES, (0, 1)):
            for size in range(len(links) + 1):
                for present in itertools.combinations(links, size):
                    lineage = {"trigger": trigger, **{link: values[link] for link in present}}
                    expected_links = set(present) == a.TRIGGER_LINKS[trigger]
                    if trigger == "retry":
                        expected = expected_links and role == v.WORKER and retry_count > 0
                    elif trigger == "review":
                        expected = expected_links and role == v.REVIEWER
                    else:
                        expected = expected_links and role == v.WORKER and retry_count == 0
                    if role == v.WORKER:
                        execution = worker_execution(execution_id="exec-w9" if trigger in ("repair", "continuation") else WORKER_EXEC,
                                                     retry_count=retry_count)
                        kwargs = {}
                    else:
                        execution = reviewer_execution(retry_count=retry_count, retry_of_execution_id=REVIEW_EXEC if retry_count else None)
                        kwargs = {"candidate": DISPATCH_CANDIDATE, "review": review_input()}
                    with self.subTest(trigger=trigger, role=role, retry_count=retry_count, links=present):
                        checked += 1
                        try:
                            result = a.produce_adm_result(execution, role=role, lineage=lineage, repository=repository(),
                                                          produced_at=PRODUCED_AT, pfp=NO_PFP, **kwargs)
                        except a.AdmResultValidationError:
                            self.assertFalse(expected, "a legal lineage was refused")
                        else:
                            self.assertTrue(expected, "an illegal lineage was accepted")
                            legal += 1
                            self.assertEqual(set(present), {l for l in links if result["lineage"][l] is not None})
        self.assertEqual(5 * 2 * 2 * 32, checked)
        self.assertEqual(6, legal)  # one legal (role, retry_count, links) per worker trigger; review at retry_count 0 and 1

    def test_19_no_vague_rerun_trigger(self):
        self.assertEqual(("initial", "retry", "repair", "continuation", "review"), a.TRIGGERS)
        self.assertNotIn("rerun", a.schema()["properties"]["lineage"]["properties"]["trigger"]["enum"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(lineage={"trigger": "rerun"})

    def test_reviewer_role_requires_review_trigger(self):
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(lineage={"trigger": "initial"})
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(lineage={"trigger": "review", "reviews_execution_id": "exec-w0", "reviewer_run_id": h.REVIEWER_RUN})


# -- 20-23: persistence ------------------------------------------------------------------------------

class GroupPersistence(unittest.TestCase):

    def test_20_idempotent_replay_accepted(self):
        store = MemoryRecordStore()
        result = produce_worker()
        first = a.persist_adm_result(store, result)
        self.assertEqual({"result_id": result["result_id"], "result_digest": result["result_digest"], "created": True, "idempotent": False}, first)
        again = a.persist_adm_result(store, produce_worker())
        self.assertEqual({"result_id": result["result_id"], "result_digest": result["result_digest"], "created": False, "idempotent": True}, again)
        self.assertEqual(1, store.creates)
        self.assertEqual(1, len(store.records))
        self.assertEqual(a.canonical_bytes(result), store.records[(PROJECT, result["result_id"])])

    def test_21_same_result_id_different_payload_rejected(self):
        store = MemoryRecordStore()
        result = produce_worker()
        a.persist_adm_result(store, result)
        before = dict(store.records)
        different = produce_worker(warnings=["a different immutable payload"])
        self.assertEqual(result["result_id"], different["result_id"])
        with self.assertRaises(a.AdmResultConflict):
            a.persist_adm_result(store, different)
        self.assertEqual(before, store.records)
        self.assertEqual(1, store.creates)

    def test_22_same_identity_different_candidate_rejected(self):
        store = MemoryRecordStore()
        a.persist_adm_result(store, produce_worker())
        execution = worker_execution()
        execution["repo_write_evidence"] = repo_write_evidence(candidate=OTHER)
        other = produce_worker(execution)
        self.assertEqual(OTHER, other["candidate"]["candidate_sha"])
        with self.assertRaises(a.AdmResultConflict):
            a.persist_adm_result(store, other)
        self.assertEqual(CAND, a.load_adm_result(store, PROJECT, other["result_id"])["candidate"]["candidate_sha"])

    def test_23_readback_digest_mismatch_rejected(self):
        store = MemoryRecordStore()
        result = produce_worker()
        store.corrupt_reads_of.add(result["result_id"])
        with self.assertRaises(a.AdmResultIntegrityError):
            a.persist_adm_result(store, result)
        with self.assertRaises(a.AdmResultIntegrityError):
            a.load_adm_result(store, PROJECT, result["result_id"])
        store.corrupt_reads_of.clear()
        self.assertEqual(result, a.load_adm_result(store, PROJECT, result["result_id"]))

    def test_23_stored_record_tampered_in_place_is_integrity_error(self):
        store = MemoryRecordStore()
        result = produce_worker()
        a.persist_adm_result(store, result)
        key = (PROJECT, result["result_id"])
        tampered = json.loads(store.records[key].decode("utf-8"))
        tampered["candidate"]["candidate_sha"] = OTHER
        tampered["candidate"]["remote_sha"] = OTHER
        store.records[key] = a.canonical_bytes(tampered)
        with self.assertRaises(a.AdmResultIntegrityError):
            a.load_adm_result(store, PROJECT, result["result_id"])
        with self.assertRaises(a.AdmResultIntegrityError):
            a.persist_adm_result(store, result)
        store.records[key] = b"{not json"
        with self.assertRaises(a.AdmResultIntegrityError):
            a.load_adm_result(store, PROJECT, result["result_id"])
        store.records[key] = json.dumps(result, indent=2).encode("utf-8")  # right content, not canonical bytes
        with self.assertRaises(a.AdmResultIntegrityError):
            a.load_adm_result(store, PROJECT, result["result_id"])

    def test_partial_creation_converges_or_conflicts(self):
        store = MemoryRecordStore()
        result = produce_worker()
        store.fail_create_after_write = True
        with self.assertRaises(RuntimeError):
            a.persist_adm_result(store, result)
        retried = a.persist_adm_result(store, result)
        self.assertEqual({"created": False, "idempotent": True}, {k: retried[k] for k in ("created", "idempotent")})
        store = MemoryRecordStore()
        store.race_with = a.canonical_bytes(result)
        raced = a.persist_adm_result(store, result)
        self.assertEqual({"created": False, "idempotent": True}, {k: raced[k] for k in ("created", "idempotent")})
        store = MemoryRecordStore()
        store.race_with = a.canonical_bytes(produce_worker(warnings=["the other writer's payload"]))
        with self.assertRaises(a.AdmResultConflict):
            a.persist_adm_result(store, result)

    def test_no_update_api_and_no_latest_record(self):
        for name in dir(a):
            self.assertFalse(name.startswith(("update", "overwrite", "upsert", "put", "delete", "latest")), name)
        for name in ("update", "put", "delete", "overwrite", "latest", "set"):
            self.assertFalse(hasattr(a.RecordStore, name))
        store = MemoryRecordStore()
        result = produce_worker()
        a.persist_adm_result(store, result)
        self.assertEqual([(PROJECT, result["result_id"])], list(store.records))

    def test_persist_refuses_an_invalid_result_before_touching_the_store(self):
        store = MemoryRecordStore()
        result = produce_worker()
        result["acceptance_state"] = "accepted"
        with self.assertRaises(a.AdmResultValidationError):
            a.persist_adm_result(store, result)
        self.assertEqual({}, store.records)
        with self.assertRaises(a.AdmResultError):
            a.load_adm_result(store, PROJECT, produce_worker()["result_id"])

    def test_persisted_bytes_are_canonical_and_round_trip(self):
        for result in (produce_worker(), produce_reviewer(), produce_worker(normalized_result=normalized(), agent_output={"ref": "out", "sha256": RAW})):
            store = MemoryRecordStore()
            with self.subTest(result_id=result["result_id"]):
                a.persist_adm_result(store, result)
                payload = store.records[(PROJECT, result["result_id"])]
                self.assertEqual(payload, a.canonical_bytes(result))
                self.assertEqual(result, a.load_adm_result(store, PROJECT, result["result_id"]))


# -- 24-30: review ---------------------------------------------------------------------------------------

class GroupReview(unittest.TestCase):

    def test_24_pass_review_valid_path(self):
        result = produce_reviewer()
        review = result["review"]
        self.assertEqual({"target_sha": CAND, "reviewer_run_id": h.REVIEWER_RUN, "provider": h.PROVIDER, "job_id": h.JOB_ID}, review["expectation"])
        self.assertEqual({"authorized": True, "blocked": False, "verdict": "PASS", "reason": contracts.AUTHORIZED,
                          "problems": [], "considered": 1}, review["authority"])
        self.assertEqual([], review["blocking_findings"])
        self.assertEqual(result["actor"], review["reviewer_actor"])
        self.assertEqual(worker_actor(), review["worker_actor"])
        self.assertEqual({"ref": "drive:review-artifact-1", "sha256": "2" * 64}, review["evidence_artifact"])
        self.assertEqual("2026-09-20T05:19:00Z", review["reviewed_at"])
        self.assertNotIn("accepted", result)

    def test_25_reject_review_valid_path(self):
        finding = {"summary": "tests missing", "severity": "high", "file": "manager/x.py", "line": 3}
        result = produce_reviewer(review=review_input(decisions=[h.review_decision("REJECT", target=CAND, findings=[finding])]))
        authority = result["review"]["authority"]
        self.assertEqual({"authorized": False, "blocked": True, "verdict": "REJECT", "reason": contracts.REJECTED}, {k: authority[k] for k in ("authorized", "blocked", "verdict", "reason")})
        self.assertEqual([finding], result["review"]["blocking_findings"])
        self.assertEqual([], result["review"]["residual_findings"])
        self.assertEqual("completed", result["execution"]["status"])  # the reviewer ran fine; the work was rejected

    def test_25_pass_with_blocking_finding_is_rejected_and_findings_partitioned(self):
        findings = [{"summary": "must fix"}, {"summary": "nit", "severity": "low"}, {"summary": "info", "severity": "info", "file": "f", "line": 1}]
        result = produce_reviewer(review=review_input(decisions=[h.review_decision("PASS", target=CAND, findings=findings)]))
        self.assertFalse(result["review"]["authority"]["authorized"])
        self.assertTrue(result["review"]["authority"]["blocked"])
        self.assertEqual(contracts.CONFLICT, result["review"]["authority"]["reason"])  # PASS beside a blocking finding is two decisions
        self.assertEqual([{"summary": "must fix", "severity": None, "file": None, "line": None}], result["review"]["blocking_findings"])
        self.assertEqual(2, len(result["review"]["residual_findings"]))

    def test_26_blocked_environment_is_a_failed_execution_not_a_verdict(self):
        result = produce_reviewer(reviewer_execution(status="failed"), review=None, failure_classification="blocked_environment")
        self.assertEqual("failed", result["execution"]["status"])
        self.assertEqual("blocked_environment", result["execution"]["failure_classification"])
        self.assertIsNone(result["review"])
        self.assertEqual(("PASS", "REJECT"), contracts.VERDICTS)
        self.assertEqual(["PASS", "REJECT"], json.loads((a.SCHEMA_DIR / "adm_review_result.schema.json").read_text(encoding="utf-8"))["properties"]["verdict"]["enum"])
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(review=review_input(decisions=[h.review_decision("BLOCKED_ENVIRONMENT", target=CAND)]))
        self.assertIn("BLOCKED_ENVIRONMENT", str(caught.exception))
        enums = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("enum"), list):
                    enums.extend(node["enum"])
                if "const" in node:
                    enums.append(node["const"])
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(a.schema())
        self.assertNotIn("BLOCKED_ENVIRONMENT", enums)
        self.assertIn("blocked_environment", json.dumps(a.schema()["properties"]["execution"]["properties"]["failure_classification"]))

    def test_27_reviewer_target_mismatch_does_not_authorize(self):
        result = produce_reviewer(review=review_input(decisions=[h.review_decision(target=OTHER)]))
        authority = result["review"]["authority"]
        self.assertFalse(authority["authorized"])
        self.assertFalse(authority["blocked"])
        self.assertEqual(contracts.ABSENT, authority["reason"])
        self.assertTrue(any(OTHER in p and CAND in p for p in authority["problems"]))
        self.assertEqual(CAND, result["candidate"]["candidate_sha"])
        self.assertEqual(CAND, result["review"]["expectation"]["target_sha"])

    def test_27_expectation_target_is_always_the_held_candidate(self):
        result = produce_reviewer()
        result["review"]["expectation"]["target_sha"] = OTHER
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "target_sha")

    def test_28_reviewer_run_id_binding(self):
        forged = produce_reviewer(review=review_input(decisions=[h.review_decision(target=CAND, reviewer_run_id="run-forged")]))
        self.assertFalse(forged["review"]["authority"]["authorized"])
        self.assertFalse(forged["review"]["authority"]["blocked"])
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(lineage={"trigger": "review", "reviews_execution_id": WORKER_EXEC, "reviewer_run_id": "run-other"})
        self.assertIn("reviewer_run_id", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(review=review_input(dispatch=None))
        # No dispatch record means ADM never issued a reviewer_run_id: a review
        # lineage cannot even be produced, let alone authorize.
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(review=review_input(dispatch={"reviewer_run_id": None, "provider": None, "job_id": None}))
        self.assertIn("reviewer_run_id", str(caught.exception))

    def test_29_reviewer_separation_facts_are_structured(self):
        fresh = produce_reviewer()["review"]["reviewer_separation"]
        self.assertEqual({"session_differs": True, "account_differs": None, "provider_differs": True, "freshness": "fresh"}, fresh)
        same = produce_reviewer(reviewer_execution(provider="claude", session_id="claude:s-worker-1", provider_session_id="s-worker-1",
                                                   account_id="acct-a"),
                                review=review_input(dispatch=h.review_dispatch(provider="claude"),
                                                    decisions=[h.review_decision(target=CAND, provider="claude")]))
        self.assertEqual({"session_differs": False, "account_differs": False, "provider_differs": False, "freshness": "same_session"},
                         same["review"]["reviewer_separation"])
        self.assertTrue(same["review"]["authority"]["authorized"])  # authority is the contract's; separation is a separate fact
        other_account = produce_reviewer(reviewer_execution(provider="claude", account_id="acct-b"),
                                         review=review_input(dispatch=h.review_dispatch(provider="claude"),
                                                             decisions=[h.review_decision(target=CAND, provider="claude")]))
        self.assertEqual({"session_differs": True, "account_differs": True, "provider_differs": False, "freshness": "fresh"},
                         other_account["review"]["reviewer_separation"])
        unknown = produce_reviewer(review=review_input(worker=None))
        self.assertEqual({"session_differs": None, "account_differs": None, "provider_differs": None, "freshness": "unknown"},
                         unknown["review"]["reviewer_separation"])

    def test_29_separation_cannot_be_asserted_by_prose_or_edited(self):
        statements = [{"raw": "I am a fresh independent reviewer on a different provider", "polarity": "approve"}]
        same = produce_reviewer(reviewer_execution(provider="claude", session_id="claude:s-worker-1", provider_session_id="s-worker-1",
                                                   account_id="acct-a"),
                                review=review_input(dispatch=h.review_dispatch(provider="claude"), statements=statements,
                                                    decisions=[h.review_decision(target=CAND, provider="claude")]))
        self.assertEqual("same_session", same["review"]["reviewer_separation"]["freshness"])
        edited = produce_reviewer()
        edited["review"]["reviewer_separation"]["freshness"] = "fresh"
        edited["review"]["reviewer_separation"]["session_differs"] = True
        edited["review"]["worker_actor"]["session_id"] = edited["actor"]["session_id"]
        edited["result_digest"] = a.result_digest_for(edited)
        assert_invalid(self, edited, "reviewer_separation")

    def test_30_missing_or_malformed_review_fails_closed(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(review=None)
        self.assertIn("review block", str(caught.exception))
        absent = produce_reviewer(review=review_input(decisions=[]))
        self.assertEqual({"authorized": False, "blocked": False, "verdict": None, "reason": contracts.ABSENT}, {k: absent["review"]["authority"][k] for k in ("authorized", "blocked", "verdict", "reason")})
        malformed = [dict(h.review_decision(target=CAND)), dict(h.review_decision(target=CAND)), dict(h.review_decision(target=CAND)), "PASS"]
        del malformed[0]["findings"]
        malformed[1]["provenance"] = {"provider": h.PROVIDER, "job_id": h.JOB_ID, "mode": "read_write"}
        malformed[2]["target_sha"] = "not-a-sha"
        for index, decision in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_reviewer(review=review_input(decisions=[decision]))
        for bad in ("PASS", [], 1):
            with self.subTest(review=bad):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_reviewer(review=bad)

    def test_30_conflicting_decisions_are_conflict(self):
        result = produce_reviewer(review=review_input(decisions=[h.review_decision("PASS", target=CAND), h.review_decision("REJECT", target=CAND)]))
        self.assertEqual(contracts.CONFLICT, result["review"]["authority"]["reason"])
        self.assertTrue(result["review"]["authority"]["blocked"])
        withdrawn = produce_reviewer(review=review_input(statements=[{"raw": "Current decision: reject", "polarity": "reject"}]))
        self.assertEqual(contracts.CONFLICT, withdrawn["review"]["authority"]["reason"])
        self.assertFalse(withdrawn["review"]["authority"]["authorized"])

    def test_34_unknown_review_verdict_rejected(self):
        for verdict in ("APPROVED", "pass", "", None, "FAIL", "BLOCKED"):
            with self.subTest(verdict=verdict):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_reviewer(review=review_input(decisions=[h.review_decision(verdict, target=CAND)]))
        result = produce_reviewer()
        result["review"]["authority"]["verdict"] = "APPROVED"
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result)

    def test_authority_is_recomputable_and_not_editable(self):
        result = produce_reviewer(review=review_input(decisions=[h.review_decision("REJECT", target=CAND)]))
        result["review"]["authority"] = {"authorized": True, "blocked": False, "verdict": "PASS", "reason": "authorized", "problems": [], "considered": 1}
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "review_authority")

    def test_worker_result_cannot_carry_a_review(self):
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(review=review_input())

    def test_review_decisions_are_the_normalized_decisions(self):
        decision = h.review_decision(target=CAND)
        norm = normalized(role=v.REVIEWER, decisions=[decision], statements=[{"raw": "Decision: approve", "polarity": "approve"}])
        result = produce_reviewer(normalized_result=norm, agent_output={"ref": "out", "sha256": RAW},
                                  review=review_input(decisions=None, statements=None))
        self.assertEqual([decision], result["review"]["decisions"])
        self.assertEqual([{"raw": "Decision: approve", "polarity": "approve"}], result["review"]["decision_statements"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(normalized_result=norm, agent_output={"ref": "out", "sha256": RAW},
                             review=review_input(decisions=[h.review_decision("REJECT", target=CAND)]))


# -- 31-33: tests evidence ----------------------------------------------------------------------------------

class GroupTests(unittest.TestCase):

    def test_31_validation_run_structured_evidence(self):
        block = h.validation_result(execution_id="exec-v1", passed=12, failed=0, skipped=1)
        registry = h.execution_registry([block])
        result = produce_worker(validation_results=[block], validation_registry=registry)
        (run,) = result["tests"]["validation_runs"]
        entry = registry.lookup("exec-v1")
        self.assertTrue(run["bound"])
        self.assertTrue(run["exit_observed"])
        self.assertEqual(0, run["exit_code"])
        self.assertEqual(entry["argv_sha256"], run["argv_sha256"])
        self.assertEqual({"execution_id": "exec-v1", "task_id": TASK, "run_id": h.RUN_ID, "argv_sha256": entry["argv_sha256"],
                          "artifact_sha256": "0" * 64, "counts": {"passed": 12, "failed": 0, "skipped": 1}, "exit_code": 0,
                          "started": True, "timed_out": False, "issued_by_adm": True}, run["registry"])
        self.assertEqual([], run["problems"])
        self.assertEqual(a.canonical_sha256(block), run["block"]["block_sha256"])
        self.assertEqual("passed", result["tests"]["tests_status"])
        self.assertNotIn("nonce", json.dumps(run))
        self.assertNotIn("artifact_path", json.dumps(run))

    def test_31_counts_come_from_the_registry_not_the_block(self):
        block = h.validation_result(execution_id="exec-v1", passed=999, failed=0)
        registry = h.execution_registry([h.validation_result(execution_id="exec-v1", passed=12, failed=0)])
        result = produce_worker(validation_results=[block], validation_registry=registry)
        (run,) = result["tests"]["validation_runs"]
        self.assertEqual({"passed": 12, "failed": 0, "skipped": 0}, run["registry"]["counts"])
        self.assertEqual({"passed": 999, "failed": 0, "skipped": 0}, run["block"]["tests"])  # audit copy, not source
        self.assertTrue(run["bound"])

    def test_31_unbound_validation_run_yields_no_counts_and_unproven_status(self):
        forged = h.validation_result(execution_id="exec-forged", passed=999)
        for registry in (None, h.execution_registry([h.validation_result(execution_id="exec-v1")])):
            with self.subTest(registry=type(registry).__name__):
                result = produce_worker(validation_results=[forged], validation_registry=registry)
                (run,) = result["tests"]["validation_runs"]
                self.assertFalse(run["bound"])
                self.assertFalse(run["exit_observed"])
                self.assertIsNone(run["registry"])
                self.assertTrue(run["problems"])
                self.assertEqual("unproven", result["tests"]["tests_status"])
        wrong_argv = h.validation_result(execution_id="exec-v1", argv=("python", "-m", "pytest", "-x"))
        registry = h.execution_registry([h.validation_result(execution_id="exec-v1")])
        result = produce_worker(validation_results=[wrong_argv], validation_registry=registry)
        self.assertFalse(result["tests"]["validation_runs"][0]["bound"])
        self.assertEqual("unproven", result["tests"]["tests_status"])

    def test_31_registry_exit_status_overrides_the_block(self):
        block = h.validation_result(execution_id="exec-v1", exit_code=0)
        registry = h.execution_registry([h.validation_result(execution_id="exec-v1", exit_code=1, passed=10, failed=2)])
        result = produce_worker(validation_results=[block], validation_registry=registry)
        (run,) = result["tests"]["validation_runs"]
        self.assertEqual(1, run["exit_code"])
        self.assertEqual("failed", result["tests"]["tests_status"])

    def test_32_missing_or_malformed_tests_fail_closed(self):
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = []
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(execution)  # says passed, recorded nothing
        self.assertIn("tests_status", str(caught.exception))
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = [legacy_step(exit_code=1)]
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(execution)  # says passed, recorded a failure
        execution = worker_execution()
        execution["repo_write_evidence"]["tests_status"] = "not_required"
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(execution)  # says not required, recorded a run
        execution = worker_execution()
        execution["repo_write_evidence"]["tests_status"] = "green"
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(execution)
        malformed = [dict(h.validation_result(execution_id="exec-v1")), dict(h.validation_result(execution_id="exec-v1")), "pytest passed"]
        malformed[0]["argv"] = "python -m pytest"
        malformed[1]["started"] = False
        for index, block in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_worker(validation_results=[block], validation_registry=h.execution_registry([h.validation_result(execution_id="exec-v1")]))

    def test_32_honest_statuses(self):
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = [legacy_step(exit_code=1)]
        execution["repo_write_evidence"]["tests_status"] = "failed"
        self.assertEqual("failed", produce_worker(execution)["tests"]["tests_status"])
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = [legacy_step(timed_out=True, exit_code=None)]
        execution["repo_write_evidence"]["tests_status"] = "failed"
        self.assertEqual("failed", produce_worker(execution)["tests"]["tests_status"])
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = []
        execution["repo_write_evidence"]["tests_status"] = "not_required"
        self.assertEqual("not_required", produce_worker(execution)["tests"]["tests_status"])
        result = produce_worker()
        result["tests"]["tests_status"] = "failed"
        result["result_digest"] = a.result_digest_for(result)
        assert_invalid(self, result, "tests_status")

    def test_33_legacy_prose_cannot_mint_counts(self):
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = [legacy_step(output="echo documentation; pytest & echo ===== 999 passed in 3.10s =====")]
        result = produce_worker(execution)
        (step,) = result["tests"]["legacy_steps"]
        self.assertNotIn("counts", step)
        self.assertNotIn("passed", step)
        self.assertNotIn("999", json.dumps(result["tests"]))
        self.assertEqual(hashlib.sha256(b"echo documentation; pytest & echo ===== 999 passed in 3.10s =====").hexdigest(), step["output_summary_sha256"])
        self.assertEqual("passed", result["tests"]["tests_status"])  # exit 0 is the one fact a legacy record proves
        self.assertNotIn("counts", a.schema()["$defs"]["legacy_step"]["properties"])
        self.assertEqual([], result["tests"]["validation_runs"])

    def test_33_legacy_exit_state_is_kept(self):
        execution = worker_execution()
        execution["repo_write_evidence"]["tests"] = [legacy_step(exit_code=2, output="===== 12 passed =====")]
        execution["repo_write_evidence"]["tests_status"] = "failed"
        result = produce_worker(execution)
        self.assertEqual(2, result["tests"]["legacy_steps"][0]["exit_code"])
        self.assertTrue(result["tests"]["legacy_steps"][0]["test_step"])
        self.assertEqual("failed", result["tests"]["tests_status"])

    def test_tests_status_matrix(self):
        bound_pass = h.validation_result(execution_id="exec-v1")
        bound_fail = h.validation_result(execution_id="exec-v2", exit_code=1, failed=1)
        registry = h.execution_registry([bound_pass, bound_fail])
        unbound = h.validation_result(execution_id="exec-x")
        cases = [
            ([], [], "passed", "passed"), ([], [legacy_step()], "passed", "passed"),
            ([bound_pass], [], "passed", "passed"), ([bound_pass, unbound], [], "passed", "unproven"),
            ([bound_fail], [], "passed", "failed"), ([bound_fail, unbound], [], "passed", "failed"),
            ([unbound], [legacy_step(exit_code=1)], "failed", "failed"), ([], [legacy_step(exit_code=1)], "failed", "failed"),
        ]
        for blocks, steps, declared, expected in cases:
            execution = worker_execution()
            execution["repo_write_evidence"]["tests"] = steps or [legacy_step()]
            execution["repo_write_evidence"]["tests_status"] = declared
            with self.subTest(blocks=[b["execution_id"] for b in blocks], steps=[s["exit_code"] for s in steps], declared=declared):
                result = produce_worker(execution, validation_results=blocks, validation_registry=registry)
                self.assertEqual(expected, result["tests"]["tests_status"])

    def test_no_mutation_or_regression_fields_in_v1(self):
        names = set()

        def walk(node):
            if isinstance(node, dict):
                names.update(node.get("properties", {}))
                names.update(node.get("required", []) if isinstance(node.get("required"), list) else [])
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(a.schema())
        for name in sorted(names):
            for word in ("mutation", "mutant", "adversarial", "base_head", "regression"):
                self.assertNotIn(word, name)


# -- 38-42: PFP, human gate, planner boundary, prose ------------------------------------------------------

class GroupBoundaries(unittest.TestCase):

    def test_38_pfp_required_representation(self):
        result = produce_reviewer(pfp={"required": True, "basis": "production-affecting fix (rule 46)", "evidence": PFP_EVIDENCE})
        self.assertEqual({"required": True, "basis": "production-affecting fix (rule 46)", "evidence": PFP_EVIDENCE}, result["pfp"])
        self.assertEqual([], [w for w in result["warnings"] if "pfp" in w])
        missing = produce_reviewer(pfp={"required": True, "basis": "production-affecting fix (rule 46)"})
        self.assertIsNone(missing["pfp"]["evidence"])
        self.assertTrue(any("pfp required" in w for w in missing["warnings"]))
        self.assertNotIn("satisfied", json.dumps(missing["pfp"]))
        self.assertEqual({"protocol": "production-fix-protocol", "version": "2.0.9-candidate",
                          "commit": "b9a7afbba6242dabe7c729b8d2e96c9da51d20a4",
                          "skill_sha256": "c6144dc439f1ce475699981329587840fe95c2f4b25288264e0a5ee0971c094e",
                          "manifest_sha256": "88ec004c0cba555e085c6e09ef45c0761c54545e92a6add69c69c32495eb3c73"}, a.PFP_CANONICAL_IDENTITY)

    def test_39_pfp_evidence_mismatch_fails(self):
        for field, value in (("commit", OTHER), ("skill_sha256", "3" * 64), ("manifest_sha256", "4" * 64),
                             ("version", "2.0.8"), ("protocol", "other-protocol")):
            evidence = copy.deepcopy(PFP_EVIDENCE)
            evidence["identity"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_reviewer(pfp={"required": True, "basis": "rule 46", "evidence": evidence})
        evidence = copy.deepcopy(PFP_EVIDENCE)
        evidence["record_sha256"] = "nope"
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(pfp={"required": True, "basis": "rule 46", "evidence": evidence})
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(pfp={"required": "yes", "basis": "rule 46"})
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(pfp={"required": True, "basis": ""})

    def test_39_pfp_required_review_needs_independent_fresh_reviewer(self):
        pfp = {"required": True, "basis": "rule 46", "evidence": PFP_EVIDENCE}
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_reviewer(reviewer_execution(provider="claude", session_id="claude:s-worker-1", provider_session_id="s-worker-1", account_id="acct-a"),
                             review=review_input(dispatch=h.review_dispatch(provider="claude"), decisions=[h.review_decision(target=CAND, provider="claude")]), pfp=pfp)
        self.assertIn("different provider", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(reviewer_execution(provider="claude", account_id="acct-b"),
                             review=review_input(dispatch=h.review_dispatch(provider="claude"), decisions=[h.review_decision(target=CAND, provider="claude")]), pfp=pfp)
        with self.assertRaises(a.AdmResultValidationError):
            produce_reviewer(review=review_input(worker=None), pfp=pfp)  # unknown separation is not enough
        self.assertTrue(produce_reviewer(pfp=pfp)["review"]["authority"]["authorized"])
        # Not PFP-tier: the same-session fact is recorded, not refused.
        recorded = produce_reviewer(reviewer_execution(provider="claude", session_id="claude:s-worker-1", provider_session_id="s-worker-1", account_id="acct-a"),
                                    review=review_input(dispatch=h.review_dispatch(provider="claude"), decisions=[h.review_decision(target=CAND, provider="claude")]))
        self.assertEqual("same_session", recorded["review"]["reviewer_separation"]["freshness"])

    def test_40_human_gate_records_evidence_only(self):
        result = produce_worker(human_gate={"required": True, "reason": "destructive migration needs approval", "evidence_ref": "drive:gate-1"})
        self.assertEqual({"required": True, "reason": "destructive migration needs approval", "evidence_ref": "drive:gate-1"}, result["human_gate"])
        self.assertEqual({"required": False, "reason": None, "evidence_ref": None}, produce_worker()["human_gate"])
        self.assertEqual(["required", "reason", "evidence_ref"], list(a.schema()["properties"]["human_gate"]["properties"]))
        for key in ("next_action", "decision", "approved", "planner_action"):
            tampered = copy.deepcopy(result)
            tampered["human_gate"][key] = "MARK_COMPLETE"
            tampered["result_digest"] = a.result_digest_for(tampered)
            with self.subTest(key=key):
                assert_invalid(self, tampered, key)

    def test_41_no_acceptance_state_or_next_action_anywhere(self):
        text = json.dumps(a.schema())
        for name in a.FORBIDDEN_FIELDS:
            self.assertNotIn(f'"{name}"', text)
        self.assertEqual({"accepted", "acceptance_state", "mark_complete", "planner_action", "next_action"}, set(a.FORBIDDEN_FIELDS))
        for name in v.ACTIONS:
            self.assertNotIn(name, text)
        result = produce_reviewer()
        for name in sorted(a.FORBIDDEN_FIELDS):
            for path in ((), ("execution",), ("review",), ("human_gate",), ("candidate",)):
                tampered = copy.deepcopy(result)
                at(tampered, path)[name] = True
                tampered["result_digest"] = a.result_digest_for(tampered)
                with self.subTest(field=name, path=path):
                    assert_invalid(self, tampered, name)

    def test_41_observed_value_cannot_carry_planner_semantics(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(verification={"probes_run": ["git"], "probes_unavailable": [], "signals": [], "verified_result": None,
                                         "observations": [{"field": "head_sha", "outcome": "VERIFIED", "observed": {"next_action": "MARK_COMPLETE"}, "probe": "git", "detail": None}]})
        self.assertIn("next_action", str(caught.exception))

    def test_42_agent_prose_cannot_overwrite_structured_truth(self):
        claim = normalized(facts={"commit_sha": (OTHER, v.REPORTED, "agent:fenced"), "push_status": ("pushed", v.REPORTED, "agent:fenced"),
                                  "status": ("PASS", v.REPORTED, "agent:fenced")})
        result = produce_worker(normalized_result=claim, agent_output={"ref": "out", "sha256": RAW})
        self.assertEqual(CAND, result["candidate"]["candidate_sha"])
        self.assertEqual(OTHER, result["normalized_result"]["facts"]["commit_sha"]["value"])
        self.assertTrue(any(OTHER in w and "not applied" in w for w in result["warnings"]))
        self.assertEqual("completed", result["execution"]["status"])
        no_candidate = produce_worker(worker_execution(status="failed", evidence=False), normalized_result=claim,
                                      agent_output={"ref": "out", "sha256": RAW}, failure_classification="provider_error")
        self.assertIsNone(no_candidate["candidate"]["candidate_sha"])
        self.assertTrue(any("holds no candidate" in w for w in no_candidate["warnings"]))
        self.assertTrue(any("did not verify a push" in w for w in no_candidate["warnings"]))

    def test_42_verified_structured_truth_must_agree_with_the_candidate(self):
        verified = normalized(facts={"commit_sha": (OTHER, v.VERIFIED, "probe:git")})
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(verification={"probes_run": ["git"], "probes_unavailable": [], "observations": [], "signals": [], "verified_result": verified})
        self.assertIn("contradicts candidate", str(caught.exception))
        agreeing = normalized(facts={"commit_sha": (CAND[:12], v.VERIFIED, "probe:git"), "remote_sha": (CAND, v.VERIFIED, "probe:git")})
        result = produce_worker(verification={"probes_run": ["git"], "probes_unavailable": [{"probe": "drive_readback", "reason": "no target"}],
                                              "observations": [{"field": "head_sha", "outcome": "VERIFIED", "observed": CAND, "probe": "git", "detail": None}],
                                              "signals": ["verify.tests.failed"], "verified_result": agreeing})
        self.assertEqual(["verify.tests.failed"], result["verification"]["signals"])
        self.assertEqual(agreeing, result["verification"]["verified_result"])
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(verification={"probes_run": [], "probes_unavailable": [], "signals": [], "verified_result": None,
                                         "observations": [{"field": "not_a_fact", "outcome": "VERIFIED", "observed": 1, "probe": "git", "detail": None}]})

    def test_42_agent_output_binds_the_normalized_result(self):
        with self.assertRaises(a.AdmResultValidationError) as caught:
            produce_worker(normalized_result=normalized(), agent_output={"ref": "out", "sha256": "5" * 64})
        self.assertIn("raw_sha256", str(caught.exception))
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(normalized_result=normalized())  # no output reference at all
        with self.assertRaises(a.AdmResultValidationError):
            produce_worker(agent_output={"ref": "out", "sha256": None})
        result = produce_worker(agent_output={"ref": "drive:output-1", "sha256": RAW})
        self.assertEqual({"ref": "drive:output-1", "sha256": RAW}, result["agent_output"])
        self.assertEqual({"ref": None, "sha256": None}, produce_worker()["agent_output"])

    def test_37_stale_candidate_rejected(self):
        with self.assertRaises(a.AdmResultConflict):
            produce_worker(candidate={"candidate_sha": OTHER, "source": "dispatch_record", "push_status": "verified", "remote_sha": OTHER})
        with self.assertRaises(a.AdmResultConflict):
            produce_worker(candidate={"candidate_sha": None, "source": "none", "push_status": "not_applicable", "remote_sha": None})
        stale_dispatch = {"candidate_sha": OTHER, "source": "dispatch_record", "push_status": "verified", "remote_sha": OTHER}
        result = produce_reviewer(candidate=stale_dispatch)  # reviewer decided CAND, ADM holds OTHER
        self.assertFalse(result["review"]["authority"]["authorized"])
        self.assertEqual(OTHER, result["review"]["expectation"]["target_sha"])
        for bad in ({"candidate_sha": CAND, "source": "prose", "push_status": "verified", "remote_sha": CAND},
                    {"candidate_sha": CAND, "source": "dispatch_record", "push_status": "verified", "remote_sha": None},
                    {"candidate_sha": None, "source": "dispatch_record", "push_status": "not_observed", "remote_sha": None},
                    {"candidate_sha": CAND, "source": "none", "push_status": "not_applicable", "remote_sha": None}):
            with self.subTest(candidate=bad):
                with self.assertRaises(a.AdmResultError):
                    produce_reviewer(candidate=bad)
        observed = produce_reviewer(candidate={"candidate_sha": CAND, "source": "dispatch_record", "push_status": "not_observed", "remote_sha": None})
        self.assertEqual("not_observed", observed["candidate"]["push_status"])

    def test_quota_is_evidence_only(self):
        execution = worker_execution()
        result = produce_worker(execution)
        self.assertEqual({"status": "observed", "source_confidence": "official",
                          "snapshots": {"before": a.canonical_sha256(execution["quota_before"]), "after": a.canonical_sha256(execution["quota_after"]),
                                        "delta": a.canonical_sha256(execution["quota_delta"])}}, result["quota"])
        unknown = produce_reviewer()["quota"]
        self.assertEqual({"status": "not_observed", "source_confidence": None, "snapshots": {"before": None, "after": None, "delta": None}}, unknown)
        text = json.dumps(a.schema()["properties"]["quota"])
        for word in ("available", "route", "percent", "reset"):
            self.assertNotIn(word, text.lower().replace("availability claim", ""))

    def test_producer_block_and_authority_owner(self):
        result = produce_worker()
        self.assertEqual({"module": "manager.adm_result", "version": a.PRODUCER_VERSION, "authority": "manager.execution_lifecycle.terminalize_execution"}, result["producer"])
        self.assertEqual(PRODUCED_AT, result["produced_at"])
        for bad in ("2026-09-20", "yesterday", "2026-09-20T05:30:00"):
            with self.subTest(produced_at=bad):
                with self.assertRaises(a.AdmResultValidationError):
                    produce_worker(produced_at=bad)

    def test_future_consumer_invariants_are_facts_not_decisions(self):
        worker_only = produce_worker()
        self.assertEqual("completed", worker_only["execution"]["status"])
        self.assertIsNone(worker_only["review"])
        prose_only = produce_reviewer(review=review_input(decisions=[], statements=[{"raw": "Verdict: PASS", "polarity": "approve"}]))
        self.assertFalse(prose_only["review"]["authority"]["authorized"])
        unbound = produce_reviewer(review=review_input(decisions=[h.review_decision(target=CAND, reviewer_run_id="run-forged")]))
        self.assertFalse(unbound["review"]["authority"]["authorized"])
        no_candidate = produce_worker(worker_execution(status="failed", evidence=False), failure_classification="provider_error")
        self.assertIsNone(no_candidate["candidate"]["candidate_sha"])
        for record in (worker_only, prose_only, unbound, no_candidate):
            self.assertFalse(a.FORBIDDEN_FIELDS & set(record))
            self.assertNotIn("state", record)


if __name__ == "__main__":
    unittest.main()
