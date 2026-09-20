"""adm-result/v1 producer contract (Slice A).

One immutable, structured record of execution/review TRUTH for one terminal
execution attempt. Its owner is ``manager.execution_lifecycle.terminalize_execution``:
after ``persist_terminal`` and after ADM-owned evidence is attached, before the
Handoff/Task projection, so the terminal bind can carry the adm-result identity
in the same authority epoch. **Slice A does not wire that call.** It builds and
proves the producer only: the schema, the canonical identity, the validation,
and create-only persistence against an injected record-store boundary. Live
wiring, the Drive ``ADM-RESULTS`` area and the NextPlan consumer are Slice B.

What this record is not
-----------------------
It carries no acceptance state and no next action. ``execution.status`` says
whether a process ran to completion; whether the work is accepted is derived
later by the NextPlan consumer from the candidate, test and review facts held
here. A completed worker may still need repair; a failed reviewer execution is
not a REJECT. Fields such as ``accepted``, ``acceptance_state``,
``mark_complete``, ``planner_action`` and ``next_action`` are refused by the
closed schema and additionally by :data:`FORBIDDEN_FIELDS`.

Identity
--------
``result_id = "ar-" + sha256(lp(project_id) lp(task_id) lp(execution_id)
lp(str(retry_count)) lp(role))`` where ``lp(s)`` is the 8-byte big-endian
length of the UTF-8 encoding of ``s`` followed by those bytes
(:func:`length_prefixed`). No delimiter joining: ``("a/b", "c")`` and
``("a", "b/c")`` cannot collide. ``retry_count`` participates because the
Command Watcher reuses an execution_id across retries.

``result_digest = sha256(canonical_bytes(result minus result_digest))``. The
only field excluded is ``result_digest`` itself, so the digest is never
self-referential. :func:`canonical_bytes` is the single canonical
serialization: ``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)`` encoded as UTF-8, with floats and
non-string keys refused so two semantically equal objects cannot serialize
differently.

Persistence
-----------
:func:`persist_adm_result` is create-only against a :class:`RecordStore`:
same ``result_id`` + same digest is an idempotent replay; same ``result_id`` +
different digest is :class:`AdmResultConflict` and nothing is overwritten; a
readback whose bytes do not hash to what was written is
:class:`AdmResultIntegrityError`. There is no update API and no mutable
"latest" record. The future canonical location is Drive
``ADM-RESULTS/<project_id>/<result_id>.json``; nothing here touches Drive.

Reuse, not duplication
----------------------
Repository/branch canonical forms: ``manager.worktree_locks``. Normalized
result: ``manager.nextplan.result`` (``adm-normalized-result/1``). Review
authority: ``manager.nextplan.contracts.review_authority`` and
``manager.nextplan.classify.review_expectation``. Test evidence binding:
``manager.nextplan.verify.adm_test_evidence`` over
``manager.nextplan.runner.ExecutionRegistry``. All read-only.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import struct
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from manager.nextplan import classify
from manager.nextplan import contracts
from manager.nextplan import result as r
from manager.nextplan import runner as runner_mod
from manager.nextplan import verify as verify_mod
from manager.nextplan import vocabulary as v
from manager.worktree_locks import TaskError, canonical_branch, canonical_repository

SCHEMA_VERSION = "adm-result/v1"
PRODUCER_MODULE = "manager.adm_result"
PRODUCER_VERSION = "1.0.0"
AUTHORITY = "manager.execution_lifecycle.terminalize_execution"

SCHEMA_DIR = Path(__file__).parents[1] / "schema"
SCHEMA_PATH = SCHEMA_DIR / "adm_result.schema.json"
SCHEMA_BASE_URI = "adm://schema/"
_REFERENCED_SCHEMAS = ("ai_result.schema.json", "adm_review_result.schema.json")

TERMINAL_STATUSES = ("completed", "failed", "interrupted")
NON_PRODUCIBLE_STATUSES = ("reserved", "running", "cancelled")

TRIGGER_INITIAL = "initial"
TRIGGER_RETRY = "retry"
TRIGGER_REPAIR = "repair"
TRIGGER_CONTINUATION = "continuation"
TRIGGER_REVIEW = "review"
TRIGGERS = (TRIGGER_INITIAL, TRIGGER_RETRY, TRIGGER_REPAIR, TRIGGER_CONTINUATION, TRIGGER_REVIEW)
LINEAGE_LINKS = ("retry_of_execution_id", "repair_of_execution_id", "continues_execution_id",
                 "reviews_execution_id", "reviewer_run_id")
# Exactly the links each trigger implies. Nothing else may be present.
TRIGGER_LINKS = {
    TRIGGER_INITIAL: frozenset(),
    TRIGGER_RETRY: frozenset({"retry_of_execution_id"}),
    TRIGGER_REPAIR: frozenset({"repair_of_execution_id"}),
    TRIGGER_CONTINUATION: frozenset({"continues_execution_id"}),
    TRIGGER_REVIEW: frozenset({"reviews_execution_id", "reviewer_run_id"}),
}

CANDIDATE_SOURCES = ("repo_write_evidence", "dispatch_record", "none")
TESTS_STATUSES = ("passed", "failed", "unproven", "not_required", "not_observed")

# The only field excluded from result_digest.
DIGEST_EXCLUDED_FIELDS = ("result_digest",)

# Planner semantics that must never appear in an adm-result, at any level.
FORBIDDEN_FIELDS = frozenset({"accepted", "acceptance_state", "mark_complete", "planner_action", "next_action"})

# AI-DEVELOPMENT-RULES v0.6.0 rule 47.6 canonical identity tuple. Pinned here so
# attached PFP evidence can be checked against the governed identity; this
# module never runs, modifies or interprets the protocol itself.
PFP_CANONICAL_IDENTITY = {
    "protocol": "production-fix-protocol",
    "version": "2.0.9-candidate",
    "commit": "b9a7afbba6242dabe7c729b8d2e96c9da51d20a4",
    "skill_sha256": "c6144dc439f1ce475699981329587840fe95c2f4b25288264e0a5ee0971c094e",
    "manifest_sha256": "88ec004c0cba555e085c6e09ef45c0761c54545e92a6add69c69c32495eb3c73",
}

_ACTOR_FIELDS = ("provider", "account_id", "session_id", "provider_session_id", "model", "mode", "effort")
_CLEANUP_FIELDS = ("persistence", "persisted", "task_claim_release", "writer_release", "provider_outcome",
                   "errors", "terminal_at", "session_id", "error_kind")
_REGISTRY_FIELDS = ("execution_id", "task_id", "run_id", "argv_sha256", "artifact_sha256", "counts", "exit_code",
                    "started", "timed_out", "issued_by_adm")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_CANONICAL_REPO = re.compile(r"^github:[a-z0-9_.-]+/[a-z0-9_.-]+$")


# -- errors ----------------------------------------------------------------------

class AdmResultError(ValueError):
    """Base class for every refusal this module makes."""


class AdmResultValidationError(AdmResultError):
    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("invalid adm-result: " + "; ".join(self.problems))


class AdmResultConflict(AdmResultError):
    """Same result_id, different immutable payload. Nothing is overwritten."""


class AdmResultIntegrityError(AdmResultError):
    """What was read back is not what was written, or a stored record does not verify."""


class RecordExists(Exception):
    """Raised by a RecordStore.create when the record already exists (create-only)."""


# -- canonical serialization ---------------------------------------------------------

def _check_canonicalizable(value, path="$"):
    if value is None or value is True or value is False or isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise AdmResultValidationError([f"{path}: floats are not canonicalizable; use integers or strings"])
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AdmResultValidationError([f"{path}: object keys must be strings, got {type(key).__name__}"])
            _check_canonicalizable(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_canonicalizable(item, f"{path}[{index}]")
        return
    raise AdmResultValidationError([f"{path}: {type(value).__name__} is not canonicalizable"])


def canonical_bytes(value):
    """The one canonical JSON serialization: sorted keys, no whitespace, raw UTF-8.

    Strings are emitted as they are (no Unicode normalization, ``ensure_ascii``
    off) and encoded as UTF-8. Floats and non-string keys are refused rather
    than serialized ambiguously. NaN/Infinity are refused by ``allow_nan``.
    """
    _check_canonicalizable(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value):
    return sha256_hex(canonical_bytes(value))


# -- identity ---------------------------------------------------------------------------

def length_prefixed(*parts):
    """Unambiguous encoding of a tuple of strings: for each part, 8-byte big-endian
    length of its UTF-8 bytes, then the bytes. No delimiter can collide."""
    encoded = bytearray()
    for part in parts:
        if not isinstance(part, str):
            raise AdmResultValidationError([f"identity tuple component {part!r} is not a string"])
        data = part.encode("utf-8")
        encoded += struct.pack(">Q", len(data)) + data
    return bytes(encoded)


def result_id_for(project_id, task_id, execution_id, retry_count, role):
    if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
        raise AdmResultValidationError([f"retry_count {retry_count!r} is not a non-negative integer"])
    for name, value in (("project_id", project_id), ("task_id", task_id), ("execution_id", execution_id), ("role", role)):
        if not isinstance(value, str) or not value:
            raise AdmResultValidationError([f"identity.{name} {value!r} is not a non-empty string"])
    if role not in v.ROLES:
        raise AdmResultValidationError([f"identity.role {role!r} is not one of {list(v.ROLES)}"])
    digest = sha256_hex(length_prefixed(project_id, task_id, execution_id, str(retry_count), role))
    return f"ar-{digest}"


def result_digest_for(result):
    payload = {key: value for key, value in result.items() if key not in DIGEST_EXCLUDED_FIELDS}
    return canonical_sha256(payload)


# -- schema -------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def _validator():
    registry = Registry()
    for name in _REFERENCED_SCHEMAS:
        document = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        registry = registry.with_resource(SCHEMA_BASE_URI + name, Resource.from_contents(document))
    return Draft202012Validator(schema(), registry=registry)


def schema_problems(result):
    errors = sorted(_validator().iter_errors(result), key=lambda e: list(map(str, e.absolute_path)))
    return [f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]


def _forbidden_fields(value, path="$"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_FIELDS:
                found.append(f"{path}.{key}: planner semantics are not execution truth")
            found.extend(_forbidden_fields(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_forbidden_fields(item, f"{path}[{index}]"))
    return found


# -- cross-field binding ----------------------------------------------------------------------

def _actor_of(record, model=None):
    return {"provider": record.get("provider"), "account_id": record.get("account_id"),
            "session_id": record.get("session_id"), "provider_session_id": record.get("provider_session_id"),
            "model": model, "mode": record.get("mode"), "effort": record.get("effort")}


def _canonical_repo(value):
    if isinstance(value, str) and _CANONICAL_REPO.match(value):
        return value
    try:
        return canonical_repository(value)
    except TaskError as exc:
        raise AdmResultValidationError([f"repository.repository: {exc}"]) from exc


def _canonical_branch(value, label="repository.branch"):
    try:
        return canonical_branch(value)
    except TaskError as exc:
        raise AdmResultValidationError([f"{label}: {exc}"]) from exc


def separation_facts(reviewer_actor, worker_actor):
    """Mechanical reviewer/worker separation. Never inferred from prose."""
    if not isinstance(worker_actor, dict):
        return {"session_differs": None, "account_differs": None, "provider_differs": None, "freshness": "unknown"}

    def differs(field):
        left, right = reviewer_actor.get(field), worker_actor.get(field)
        if left is None or right is None:
            return None
        return left != right

    provider_differs = differs("provider")
    account_differs = differs("account_id")
    if account_differs is None:
        # account_id null means "the single default account" (execution schema);
        # two nulls on different providers are different accounts, two nulls on
        # the same provider are the same one, and one null beside a value is
        # not observed.
        if reviewer_actor.get("account_id") is None and worker_actor.get("account_id") is None:
            account_differs = provider_differs
    session_differs = differs("session_id")
    if session_differs is False:
        freshness = "same_session"
    elif session_differs is True:
        freshness = "fresh"
    else:
        freshness = "unknown"
    return {"session_differs": session_differs, "account_differs": account_differs,
            "provider_differs": provider_differs, "freshness": freshness}


def _normalize_finding(finding):
    return {"summary": finding.get("summary"), "severity": finding.get("severity"),
            "file": finding.get("file"), "line": finding.get("line")}


def binding_problems(result):
    """Every cross-field reason ``result`` is not internally bound. Assumes schema-valid shape."""
    problems = []
    identity, lineage = result["identity"], result["lineage"]
    execution_id, role, retry_count = identity["execution_id"], identity["role"], identity["retry_count"]

    # identity / digest
    expected_id = result_id_for(identity["project_id"], identity["task_id"], execution_id, retry_count, role)
    if result["result_id"] != expected_id:
        problems.append(f"result_id {result['result_id']!r} is not the identity of this tuple ({expected_id!r})")
    expected_digest = result_digest_for(result)
    if result["result_digest"] != expected_digest:
        problems.append("result_digest does not bind this payload")

    # lineage
    trigger = lineage["trigger"]
    present = {link for link in LINEAGE_LINKS if lineage.get(link) is not None}
    if present != TRIGGER_LINKS[trigger]:
        problems.append(f"lineage.trigger {trigger!r} implies links {sorted(TRIGGER_LINKS[trigger])}, got {sorted(present)}")
    if trigger == TRIGGER_RETRY:
        if retry_count < 1:
            problems.append("lineage.trigger retry requires identity.retry_count > 0")
        if role != v.WORKER:
            problems.append("lineage.trigger retry is a worker lineage; a reviewer rerun keeps trigger review")
    elif trigger == TRIGGER_REVIEW:
        if role != v.REVIEWER:
            problems.append("lineage.trigger review requires identity.role reviewer")
        if lineage["reviews_execution_id"] == execution_id:
            problems.append("lineage.reviews_execution_id cannot be the reviewer execution itself")
    else:
        if retry_count != 0:
            problems.append(f"lineage.trigger {trigger!r} requires identity.retry_count 0 (a rerun is trigger retry)")
        if role != v.WORKER:
            problems.append(f"lineage.trigger {trigger!r} requires identity.role worker")
    if role == v.REVIEWER and trigger != TRIGGER_REVIEW:
        problems.append("identity.role reviewer requires lineage.trigger review")
    for link in ("repair_of_execution_id", "continues_execution_id"):
        if lineage.get(link) == execution_id:
            problems.append(f"lineage.{link} cannot name the execution itself")

    # candidate
    candidate = result["candidate"]
    if candidate["push_status"] == "verified" and candidate["candidate_sha"] != candidate["remote_sha"]:
        problems.append("candidate.push_status verified requires candidate_sha == remote_sha")
    if candidate["source"] == "none" and candidate["candidate_sha"] is not None:
        problems.append("candidate.source none cannot carry a candidate_sha")

    # execution
    execution = result["execution"]
    if execution["status"] not in TERMINAL_STATUSES:
        problems.append(f"execution.status {execution['status']!r} is not terminal")
    if execution["status"] == "completed" and execution["failure_classification"] is not None:
        problems.append("a completed execution has no failure_classification")

    # agent output <-> normalized result
    normalized = result["normalized_result"]
    if normalized is not None:
        problems.extend(f"normalized_result: {p}" for p in r.result_problems(normalized))
        if normalized.get("task_id") != identity["task_id"]:
            problems.append(f"normalized_result.task_id {normalized.get('task_id')!r} is not identity.task_id")
        if normalized.get("role") != role:
            problems.append(f"normalized_result.role {normalized.get('role')!r} is not identity.role")
        raw = (normalized.get("extraction") or {}).get("raw_sha256")
        if result["agent_output"]["sha256"] != raw:
            problems.append("agent_output.sha256 does not bind normalized_result.extraction.raw_sha256")

    # verification
    verification = result["verification"]
    if verification is not None:
        verified = verification["verified_result"]
        if verified is not None:
            problems.extend(f"verification.verified_result: {p}" for p in r.result_problems(verified))
            if verified.get("task_id") != identity["task_id"] or verified.get("role") != role:
                problems.append("verification.verified_result is not bound to this identity")
            for field in ("commit_sha", "remote_sha"):
                fact = verified["facts"][field]
                observed = candidate["candidate_sha"] if field == "commit_sha" else candidate["remote_sha"]
                if (fact["level"] == v.VERIFIED and observed is not None
                        and not verify_mod.equivalent(field, fact["value"], observed)):
                    problems.append(f"verification.verified_result.{field} VERIFIED {fact['value']!r} contradicts candidate {observed!r}")
        for index, item in enumerate(verification["observations"]):
            if item["field"] not in r.FIELD_KINDS:
                problems.append(f"verification.observations[{index}].field {item['field']!r} is not a fact field")

    # tests
    tests = result["tests"]
    problems.extend(_tests_problems(tests))

    # review
    review = result["review"]
    if role == v.REVIEWER and execution["status"] == "completed" and review is None:
        problems.append("a completed reviewer execution must carry a review block (decisions may be empty)")
    if role == v.WORKER and review is not None:
        problems.append("a worker result cannot carry a review block")
    if review is not None:
        problems.extend(_review_problems(result, review))

    # pfp
    pfp = result["pfp"]
    evidence = pfp["evidence"]
    if evidence is not None and evidence["identity"] != PFP_CANONICAL_IDENTITY:
        problems.append("pfp.evidence.identity is not the canonical rule-47.6 identity")

    problems.extend(_forbidden_fields(result))
    return problems


def _tests_problems(tests):
    problems = []
    runs, steps = tests["validation_runs"], tests["legacy_steps"]
    for index, run in enumerate(runs):
        if run["argv_sha256"] != runner_mod.argv_digest(run["argv"]):
            problems.append(f"tests.validation_runs[{index}].argv_sha256 does not bind argv")
        if run["bound"] and run["registry"] is None:
            problems.append(f"tests.validation_runs[{index}] is bound to no registry entry")
        if run["bound"] and run["problems"]:
            problems.append(f"tests.validation_runs[{index}] is bound but records problems")
        if run["exit_observed"] and not run["bound"]:
            problems.append(f"tests.validation_runs[{index}] exit status can only be observed through the registry")
        if run["block"]["execution_id"] != run["execution_id"]:
            problems.append(f"tests.validation_runs[{index}].block names a different execution")
    if tests["tests_status"] != derive_tests_status(runs, steps, tests["tests_status"] == "not_required"):
        problems.append(f"tests.tests_status {tests['tests_status']!r} is not what the recorded exit states derive")
    return problems


def derive_tests_status(runs, steps, declared_not_required=False):
    """Only exit states decide. Prose never does."""
    if not runs and not steps:
        return "not_required" if declared_not_required else "not_observed"
    observed_failure = any(step["timed_out"] or step["exit_code"] != 0 for step in steps)
    observed_failure = observed_failure or any(run["exit_observed"] and (run["timed_out"] or run["exit_code"] != 0)
                                               for run in runs)
    if observed_failure:
        return "failed"
    if any(not run["exit_observed"] for run in runs):
        return "unproven"
    return "passed"


def _review_problems(result, review):
    problems = []
    lineage, candidate = result["lineage"], result["candidate"]
    expectation = review["expectation"]
    if expectation["target_sha"] != candidate["candidate_sha"]:
        problems.append("review.expectation.target_sha is not the candidate this result holds")
    if lineage["reviewer_run_id"] is not None and expectation["reviewer_run_id"] != lineage["reviewer_run_id"]:
        problems.append("review.expectation.reviewer_run_id is not lineage.reviewer_run_id")
    if review["reviewer_actor"] != result["actor"]:
        problems.append("review.reviewer_actor is not this result's actor")
    if review["reviewer_separation"] != separation_facts(review["reviewer_actor"], review["worker_actor"]):
        problems.append("review.reviewer_separation is not the mechanical comparison of the two actors")
    for index, decision in enumerate(review["decisions"]):
        shape = contracts.review_problems(decision)
        if shape:
            problems.append(f"review.decisions[{index}] is malformed: " + "; ".join(shape))
    authority = contracts.review_authority(review["decisions"], expectation, review["decision_statements"])
    if review["authority"] != authority:
        problems.append("review.authority is not what contracts.review_authority recomputes")
    blocking, residual = _partition_findings(review["decisions"])
    if review["blocking_findings"] != blocking or review["residual_findings"] != residual:
        problems.append("review findings partition does not match the decisions")
    if result["pfp"]["required"]:
        separation = review["reviewer_separation"]
        if separation["freshness"] != "fresh" or separation["provider_differs"] is not True:
            problems.append("pfp-required review requires a fresh reviewer session on a different provider")
    return problems


def _partition_findings(decisions):
    blocking, residual = [], []
    for decision in decisions:
        for finding in decision.get("findings", []):
            (blocking if contracts.finding_is_blocking(finding) else residual).append(_normalize_finding(finding))
    return blocking, residual


def validate_adm_result(result):
    """Schema (closed at every level) plus every cross-field binding. Raises."""
    if not isinstance(result, dict):
        raise AdmResultValidationError([f"adm-result is {type(result).__name__}, not an object"])
    problems = schema_problems(result)
    if result.get("schema") != SCHEMA_VERSION:
        problems.insert(0, f"schema is {result.get('schema')!r}, not {SCHEMA_VERSION!r}")
    if problems:
        raise AdmResultValidationError(problems)
    problems = binding_problems(result)
    if problems:
        raise AdmResultValidationError(problems)
    return result


# -- production ------------------------------------------------------------------------------------

def _require(condition, message, problems):
    if not condition:
        problems.append(message)


def _candidate_block(execution_record, supplied):
    evidence = execution_record.get("repo_write_evidence")
    derived = None
    if evidence:
        if evidence.get("push_status") == "verified":
            derived = {"candidate_sha": evidence["final_commit_sha"], "source": "repo_write_evidence",
                       "push_status": "verified", "remote_sha": evidence["remote_sha"]}
        elif evidence.get("push_status") == "not_applicable":
            derived = {"candidate_sha": None, "source": "repo_write_evidence",
                       "push_status": "not_applicable", "remote_sha": None}
        else:
            raise AdmResultValidationError([f"repo_write_evidence.push_status {evidence.get('push_status')!r} is not a candidate source"])
    if supplied is None:
        return derived or {"candidate_sha": None, "source": "none", "push_status": "not_applicable", "remote_sha": None}
    if not isinstance(supplied, dict):
        raise AdmResultValidationError(["candidate must be an object or None"])
    block = {"candidate_sha": supplied.get("candidate_sha"), "source": supplied.get("source"),
             "push_status": supplied.get("push_status"), "remote_sha": supplied.get("remote_sha")}
    if block["source"] not in CANDIDATE_SOURCES:
        raise AdmResultValidationError([f"candidate.source {block['source']!r} is not one of {list(CANDIDATE_SOURCES)}"])
    if derived is not None and block != derived:
        # The caller's candidate is not what ADM's own repo-write evidence says.
        # Candidate A can never authorize candidate B.
        raise AdmResultConflict(f"supplied candidate {block['candidate_sha']!r} is not the execution's verified "
                                f"repo-write candidate {derived['candidate_sha']!r}")
    return block


def _validation_block(block):
    return {"schema": block.get("schema"), "execution_id": block.get("execution_id"), "argv": block.get("argv"),
            "runner": block.get("runner"), "started": block.get("started"), "exit_code": block.get("exit_code"),
            "timed_out": bool(block.get("timed_out", False)),
            "tests": None if block.get("tests") is None else {field: block["tests"].get(field) for field in ("passed", "failed", "skipped")},
            "artifact_sha256": block.get("artifact_sha256"), "block_sha256": canonical_sha256(block)}


def _registry_entry(record):
    if record is None:
        return None
    entry = {field: record.get(field) for field in _REGISTRY_FIELDS}
    counts = entry["counts"]
    if isinstance(counts, dict):
        entry["counts"] = {field: counts.get(field) for field in ("passed", "failed", "skipped")}
    entry["started"] = bool(entry["started"])
    entry["timed_out"] = bool(entry["timed_out"])
    entry["issued_by_adm"] = bool(entry["issued_by_adm"])
    return entry


def _tests_block(execution_record, validation_results, registry, task_id):
    evidence = execution_record.get("repo_write_evidence") or {}
    blocks = list(validation_results or ())
    for index, block in enumerate(blocks):
        shape = contracts.validation_problems(block)
        if shape:
            raise AdmResultValidationError([f"validation_results[{index}] is malformed: " + "; ".join(shape)])
    if registry is not None and not isinstance(registry, runner_mod.ExecutionRegistry):
        registry = runner_mod.ExecutionRegistry(records=registry)
    legacy = list(evidence.get("tests") or [])
    adapted = verify_mod.adm_test_evidence({"repo_write_evidence": {"validation_results": blocks, "tests": legacy}},
                                           registry, task_id=task_id) or {"runs": []}
    structured = [run for run in adapted["runs"] if "argv" in run]
    if len(structured) != len(blocks):
        raise AdmResultValidationError(["validation runs were not adapted one-to-one"])
    runs = []
    for block, run in zip(blocks, structured):
        record = registry.lookup(block["execution_id"]) if registry is not None else None
        runs.append({"execution_id": block["execution_id"], "argv": list(block["argv"]),
                     "argv_sha256": runner_mod.argv_digest(block["argv"]), "runner": block["runner"],
                     "bound": bool(run["bound"]), "exit_observed": bool(run["exit_observed"]),
                     "exit_code": run["exit_code"], "timed_out": bool(run["timed_out"]),
                     "registry": _registry_entry(record) if run["bound"] else None,
                     "problems": list(run["problems"]), "block": _validation_block(block)})
    steps = []
    for step in legacy:
        steps.append({"command": step.get("command"), "executable": step.get("executable"),
                      "exit_code": step.get("exit_code"),
                      "output_summary_sha256": sha256_hex(str(step.get("output_summary", "")).encode("utf-8")),
                      "started_at": step.get("started_at"), "completed_at": step.get("completed_at"),
                      "timed_out": bool(step.get("timed_out", False)), "test_step": verify_mod.is_test_step(step)})
    declared = evidence.get("tests_status") if evidence else None
    if evidence:
        legacy_status = derive_tests_status([], steps, declared == "not_required")
        if declared not in ("passed", "failed", "not_required"):
            raise AdmResultValidationError([f"repo_write_evidence.tests_status {declared!r} is not a test status"])
        if legacy_status != declared:
            raise AdmResultValidationError([f"repo_write_evidence.tests_status {declared!r} contradicts the recorded steps ({legacy_status})"])
    status = derive_tests_status(runs, steps, declared == "not_required")
    return {"validation_runs": runs, "legacy_steps": steps, "tests_status": status}


def _review_block(result_actor, candidate, review, normalized):
    if not isinstance(review, dict):
        raise AdmResultValidationError(["review must be an object"])
    dispatch = review.get("dispatch")
    if not isinstance(dispatch, dict):
        raise AdmResultValidationError(["review.dispatch (the ADM-issued reviewer dispatch record) is required"])
    expectation = classify.review_expectation({"candidate_sha": candidate["candidate_sha"], "review_dispatch": dispatch})
    decisions = review.get("decisions")
    statements = review.get("decision_statements")
    if normalized is not None:
        if decisions is None:
            decisions = normalized["decisions"]
        elif list(decisions) != list(normalized["decisions"]):
            raise AdmResultValidationError(["review.decisions differ from normalized_result.decisions"])
        if statements is None:
            statements = normalized["decision_statements"]
        elif list(statements) != list(normalized["decision_statements"]):
            raise AdmResultValidationError(["review.decision_statements differ from normalized_result.decision_statements"])
    decisions = [copy.deepcopy(d) for d in (decisions or [])]
    statements = [{"raw": s.get("raw"), "polarity": s.get("polarity")} for s in (statements or [])]
    for index, decision in enumerate(decisions):
        shape = contracts.review_problems(decision)
        if shape:
            raise AdmResultValidationError([f"review.decisions[{index}] is malformed: " + "; ".join(shape)])
    authority = contracts.review_authority(decisions, expectation, statements)
    blocking, residual = _partition_findings(decisions)
    worker_actor = review.get("worker_actor")
    if worker_actor is not None:
        worker_actor = {field: worker_actor.get(field) for field in _ACTOR_FIELDS}
    artifact = review.get("evidence_artifact")
    if artifact is not None:
        artifact = {"ref": artifact.get("ref"), "sha256": artifact.get("sha256")}
    return {"expectation": expectation, "reviewer_actor": dict(result_actor), "worker_actor": worker_actor,
            "reviewer_separation": separation_facts(result_actor, worker_actor), "decisions": decisions,
            "decision_statements": statements, "authority": authority, "blocking_findings": blocking,
            "residual_findings": residual, "evidence_artifact": artifact, "reviewed_at": review.get("reviewed_at")}


def _cleanup_block(cleanup):
    if cleanup is None:
        return None
    if not isinstance(cleanup, dict):
        raise AdmResultValidationError(["execution.cleanup_evidence must be an object or null"])
    block = {field: cleanup.get(field) for field in _CLEANUP_FIELDS}
    block["errors"] = [str(item) for item in (cleanup.get("errors") or [])]
    if block["persisted"] is not None:
        block["persisted"] = [str(item) for item in block["persisted"]]
    block["cleanup_sha256"] = canonical_sha256(cleanup)
    return block


def _quota_block(execution_record):
    before = execution_record.get("quota_before")
    observed = isinstance(before, dict) and bool(before)
    digest = lambda value: canonical_sha256(value) if isinstance(value, dict) and value else None  # noqa: E731
    return {"status": "observed" if observed else "not_observed",
            "source_confidence": execution_record.get("source_confidence"),
            "snapshots": {"before": digest(before), "after": digest(execution_record.get("quota_after")),
                          "delta": digest(execution_record.get("quota_delta"))}}


def _agent_claim_warnings(normalized, candidate):
    """Prose/claims that disagree with structured truth are recorded, never applied."""
    warnings = []
    if normalized is None:
        return warnings
    claimed = normalized["facts"]["commit_sha"]
    if (claimed["source"].startswith("agent:") and claimed["value"] is not None
            and candidate["candidate_sha"] is not None
            and not verify_mod.equivalent("commit_sha", claimed["value"], candidate["candidate_sha"])):
        warnings.append(f"agent claimed commit_sha {claimed['value']!r}; candidate is {candidate['candidate_sha']!r} (claim not applied)")
    if claimed["source"].startswith("agent:") and claimed["value"] is not None and candidate["source"] == "none":
        warnings.append(f"agent claimed commit_sha {claimed['value']!r}; ADM holds no candidate (claim not applied)")
    pushed = normalized["facts"]["push_status"]
    if pushed["source"].startswith("agent:") and pushed["value"] == "pushed" and candidate["push_status"] != "verified":
        warnings.append("agent claimed push_status pushed; ADM did not verify a push (claim not applied)")
    return warnings


def produce_adm_result(execution_record, *, role, lineage, repository, produced_at, pfp, command=None,
                       candidate=None, agent_output=None, normalized_result=None, verification=None,
                       validation_results=(), validation_registry=None, review=None, human_gate=None,
                       failure_classification=None, warnings=()):
    """Build a validated adm-result/v1 from ADM-owned evidence.

    ``execution_record`` is the authoritative Execution record (terminal).
    ``command`` is the Command record that launched it, if any (binds
    command_id and model, and supplies failure_classification from
    ``result.error_kind``). ``candidate`` may be supplied only from an
    authoritative dispatch record; when the execution carries repo-write
    evidence the two must agree exactly. ``review`` is
    ``{dispatch, decisions?, decision_statements?, worker_actor?,
    evidence_artifact?, reviewed_at}`` and is required for a completed
    reviewer execution. Nothing here is read from provider prose.
    """
    problems = []
    if not isinstance(execution_record, dict):
        raise AdmResultValidationError(["execution_record must be an object"])
    status = execution_record.get("status")
    if status in NON_PRODUCIBLE_STATUSES:
        raise AdmResultValidationError([f"execution.status {status!r} is not terminal; no adm-result is produced for it"])
    if status not in TERMINAL_STATUSES:
        raise AdmResultValidationError([f"execution.status {status!r} is not a terminal execution status"])

    project_id = execution_record.get("project_id")
    task_id = execution_record.get("task_id")
    execution_id = execution_record.get("execution_id")
    retry_count = execution_record.get("retry_count", 0)
    if isinstance(retry_count, bool) or not isinstance(retry_count, int):
        raise AdmResultValidationError([f"execution.retry_count {retry_count!r} is not an integer"])

    model = None
    command_id = None
    if command is not None:
        if not isinstance(command, dict):
            raise AdmResultValidationError(["command must be an object or None"])
        _require(command.get("execution_id") == execution_id, "command.execution_id is not this execution", problems)
        _require(command.get("task_id") == task_id, "command.task_id is not this execution's task", problems)
        _require(command.get("project_id") == project_id, "command.project_id is not this execution's project", problems)
        _require(command.get("provider") == execution_record.get("provider"), "command.provider is not this execution's provider", problems)
        command_id = command.get("command_id")
        model = command.get("model")
        error_kind = (command.get("result") or {}).get("error_kind")
        if failure_classification is None:
            failure_classification = error_kind
        elif error_kind is not None and error_kind != failure_classification:
            problems.append("failure_classification contradicts command.result.error_kind")
    if problems:
        raise AdmResultValidationError(problems)

    # repository: canonical forms from the existing contract, bound to the lease/evidence ADM holds
    if not isinstance(repository, dict):
        raise AdmResultValidationError(["repository must be an object"])
    repo_block = {"repository": _canonical_repo(repository.get("repository")),
                  "branch": _canonical_branch(repository.get("branch")),
                  "base_sha": repository.get("base_sha"), "worktree_path": repository.get("worktree_path"),
                  "worktree_identity": None}
    identity_in = repository.get("worktree_identity")
    if identity_in is not None:
        repo_block["worktree_identity"] = {"worktree_id": identity_in.get("worktree_id"),
                                           "branch": identity_in.get("branch"), "branch_short": identity_in.get("branch_short")}
        if identity_in.get("branch") != repo_block["branch"]:
            problems.append("repository.worktree_identity.branch is not repository.branch")
    lease = execution_record.get("lease_evidence")
    if isinstance(lease, dict):
        _require(lease.get("repository") == repo_block["repository"],
                 f"repository {repo_block['repository']!r} is not the leased repository {lease.get('repository')!r}", problems)
        _require(lease.get("branch") == repo_block["branch"],
                 f"branch {repo_block['branch']!r} is not the leased branch {lease.get('branch')!r}", problems)
        _require(lease.get("baseline_head") == repo_block["base_sha"],
                 f"base_sha {repo_block['base_sha']!r} is not the leased baseline_head {lease.get('baseline_head')!r}", problems)
    evidence = execution_record.get("repo_write_evidence")
    if isinstance(evidence, dict):
        if evidence.get("branch") is not None:
            _require(_canonical_branch(evidence["branch"], "repo_write_evidence.branch") == repo_block["branch"],
                     "repository.branch is not the repo-write evidence branch", problems)
        _require(evidence.get("worktree_path") == repo_block["worktree_path"],
                 "repository.worktree_path is not the repo-write evidence worktree", problems)
    if problems:
        raise AdmResultValidationError(problems)

    candidate_block = _candidate_block(execution_record, candidate)
    actor = _actor_of(execution_record, model)

    output = {"ref": None, "sha256": None}
    if agent_output is not None:
        output = {"ref": agent_output.get("ref"), "sha256": agent_output.get("sha256")}

    normalized = copy.deepcopy(normalized_result) if normalized_result is not None else None
    verification_block = None
    if verification is not None:
        verification_block = {
            "probes_run": list(verification.get("probes_run", [])),
            "probes_unavailable": [{"probe": item.get("probe"), "reason": str(item.get("reason", ""))}
                                   for item in verification.get("probes_unavailable", [])],
            "observations": [{"field": o.get("field"), "outcome": o.get("outcome"), "observed": o.get("observed"),
                              "probe": o.get("probe"), "detail": o.get("detail")}
                             for o in verification.get("observations", [])],
            "signals": sorted(verification.get("signals", [])),
            "verified_result": copy.deepcopy(verification.get("verified_result")),
        }

    tests_block = _tests_block(execution_record, validation_results, validation_registry, task_id)

    lineage_block = {"trigger": (lineage or {}).get("trigger")}
    for link in LINEAGE_LINKS:
        lineage_block[link] = (lineage or {}).get(link)

    review_block = None
    if review is not None:
        review_block = _review_block(actor, candidate_block, review, normalized)

    pfp_block = {"required": (pfp or {}).get("required"), "basis": (pfp or {}).get("basis"), "evidence": None}
    pfp_evidence = (pfp or {}).get("evidence")
    if pfp_evidence is not None:
        identity_in = pfp_evidence.get("identity") or {}
        pfp_block["evidence"] = {"record_id": pfp_evidence.get("record_id"), "record_sha256": pfp_evidence.get("record_sha256"),
                                 "identity": {field: identity_in.get(field) for field in PFP_CANONICAL_IDENTITY}}

    gate = human_gate or {}
    all_warnings = [str(item) for item in warnings]
    all_warnings.extend(_agent_claim_warnings(normalized, candidate_block))
    if pfp_block["required"] and pfp_block["evidence"] is None:
        all_warnings.append("pfp required but no PFP evidence is attached (fail-closed for the consumer)")

    result = {
        "schema": SCHEMA_VERSION,
        "result_id": result_id_for(project_id, task_id, execution_id, retry_count, role) if isinstance(role, str) and role in v.ROLES else None,
        "result_digest": None,
        "produced_at": produced_at,
        "producer": {"module": PRODUCER_MODULE, "version": PRODUCER_VERSION, "authority": AUTHORITY},
        "identity": {"project_id": project_id, "task_id": task_id, "execution_id": execution_id,
                     "retry_count": retry_count, "role": role, "command_id": command_id},
        "lineage": lineage_block,
        "repository": repo_block,
        "candidate": candidate_block,
        "actor": actor,
        "execution": {"status": status, "started_at": execution_record.get("started_at"),
                      "completed_at": execution_record.get("completed_at"),
                      "terminal_reason": execution_record.get("terminal_reason"),
                      "failure_classification": failure_classification,
                      "cleanup": _cleanup_block(execution_record.get("cleanup_evidence")),
                      "record_sha256": canonical_sha256(execution_record)},
        "agent_output": output,
        "normalized_result": normalized,
        "verification": verification_block,
        "tests": tests_block,
        "review": review_block,
        "quota": _quota_block(execution_record),
        "pfp": pfp_block,
        "human_gate": {"required": bool(gate.get("required", False)), "reason": gate.get("reason"),
                       "evidence_ref": gate.get("evidence_ref")},
        "warnings": all_warnings,
    }
    if result["result_id"] is None:
        raise AdmResultValidationError([f"role {role!r} is not one of {list(v.ROLES)}"])
    result["result_digest"] = result_digest_for(result)
    return validate_adm_result(result)


# -- persistence (create-only) ---------------------------------------------------------------------

class RecordStore:
    """The boundary Slice B implements over Drive ``ADM-RESULTS/<project_id>/<result_id>.json``.

    ``read(project_id, result_id) -> bytes | None`` returns the stored bytes.
    ``create(project_id, result_id, payload: bytes) -> None`` creates the
    record and raises :class:`RecordExists` if one is already there. There is
    deliberately no update, no delete and no "latest" pointer.
    """

    def read(self, project_id, result_id):  # pragma: no cover - interface
        raise NotImplementedError

    def create(self, project_id, result_id, payload):  # pragma: no cover - interface
        raise NotImplementedError


def _decode_stored(payload, result_id):
    if not isinstance(payload, (bytes, bytearray)):
        raise AdmResultIntegrityError(f"stored record {result_id} is not bytes")
    try:
        stored = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AdmResultIntegrityError(f"stored record {result_id} is not readable JSON: {exc}") from exc
    try:
        validate_adm_result(stored)
    except AdmResultValidationError as exc:
        raise AdmResultIntegrityError(f"stored record {result_id} does not validate: {exc}") from exc
    if stored["result_id"] != result_id:
        raise AdmResultIntegrityError(f"stored record at {result_id} carries result_id {stored['result_id']}")
    if bytes(payload) != canonical_bytes(stored):
        raise AdmResultIntegrityError(f"stored record {result_id} is not in canonical form")
    return stored


def _reconcile(existing_payload, result, payload):
    existing = _decode_stored(existing_payload, result["result_id"])
    if existing["result_digest"] != result["result_digest"]:
        raise AdmResultConflict(f"result {result['result_id']} already exists with digest {existing['result_digest']}, "
                                f"not {result['result_digest']}; not overwritten")
    if bytes(existing_payload) != payload:
        raise AdmResultIntegrityError(f"result {result['result_id']} exists with the same digest but different bytes")
    return {"result_id": result["result_id"], "result_digest": result["result_digest"], "created": False, "idempotent": True}


def persist_adm_result(store, result):
    """Create-only persistence with readback verification.

    Returns ``{result_id, result_digest, created, idempotent}``. A partial or
    retried creation converges deterministically: if the record turns out to
    exist, it is re-read and either matches (idempotent replay) or conflicts.
    """
    validate_adm_result(result)
    project_id, result_id = result["identity"]["project_id"], result["result_id"]
    payload = canonical_bytes(result)
    existing = store.read(project_id, result_id)
    if existing is not None:
        return _reconcile(existing, result, payload)
    try:
        store.create(project_id, result_id, payload)
    except RecordExists:
        existing = store.read(project_id, result_id)
        if existing is None:
            raise AdmResultIntegrityError(f"result {result_id} reported existing but cannot be read back")
        return _reconcile(existing, result, payload)
    readback = store.read(project_id, result_id)
    if readback is None:
        raise AdmResultIntegrityError(f"result {result_id} cannot be read back after creation")
    if sha256_hex(bytes(readback)) != sha256_hex(payload):
        raise AdmResultIntegrityError(f"result {result_id} readback digest mismatch; the record cannot be trusted")
    return {"result_id": result_id, "result_digest": result["result_digest"], "created": True, "idempotent": False}


def load_adm_result(store, project_id, result_id):
    """Read one record and verify it: schema, bindings, result_id and digest. Never repairs."""
    payload = store.read(project_id, result_id)
    if payload is None:
        raise AdmResultError(f"result {result_id} is absent")
    return _decode_stored(payload, result_id)
