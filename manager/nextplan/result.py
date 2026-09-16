"""Canonical AI result contract.

Two shapes, deliberately different:

- The **report** is what an agent emits (``schema/ai_result_report.schema.json``,
  ``schema_version: adm-ai-result/1``). Every value in it is only a claim.
- The **normalized result** is what ADM reasons over
  (``schema/ai_result.schema.json``). Every field is a *fact* carrying its
  value, an evidence level and the source that produced it::

      {"value": "3f2a...", "level": "REPORTED", "source": "agent:fenced", "detail": null}

Levels (manager.nextplan.vocabulary): VERIFIED, REPORTED, DERIVED,
CONTRADICTED, UNKNOWN. The level is bound to the source, and
``result_problems`` enforces the binding, so no stage can quietly promote a
claim:

- only a ``probe:*`` source can say VERIFIED or CONTRADICTED;
- an agent's structured or deterministically parsed output is REPORTED;
- a heuristic or a computation over other facts is DERIVED, never higher;
- UNKNOWN always carries ``value: null`` and nothing else ever does.

``recommended_next_action`` is carried for humans and audit only; the planner
never reads it.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from manager.nextplan import vocabulary as v

REPORT_SCHEMA_VERSION = "adm-ai-result/1"
CONTRACT = "adm-normalized-result/1"
SCHEMA_DIR = Path(__file__).parents[2] / "schema"
REPORT_SCHEMA_PATH = SCHEMA_DIR / "ai_result_report.schema.json"
RESULT_SCHEMA_PATH = SCHEMA_DIR / "ai_result.schema.json"

SHA_PATTERN = "^[0-9a-f]{7,40}$"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

ENUMS = {
    "status": ("PASS", "FAIL", "PARTIAL", "BLOCKED", "IN_PROGRESS", "ERROR"),
    "push_status": ("pushed", "not_pushed", "failed", "not_applicable"),
    "git_status": ("clean", "dirty"),
    "ssot_sync": ("synced", "failed", "not_required"),
    "review_status": ("not_requested", "pending", "pass", "fail", "not_applicable"),
    "review_verdict": ("PASS", "FAIL"),
    "terminal_state": ("terminal", "non_terminal"),
}

# Every fact field and the kind of value it holds. The order is the contract's
# canonical field order.
FIELD_KINDS = {
    "task_id": "string", "session_id": "string", "agent": "string", "model": "string", "mode": "string",
    "status": "enum:status", "progress_percent": "percent",
    "repo": "string", "worktree": "string", "branch": "string", "base_sha": "sha", "head_sha": "sha",
    "files_changed": "strlist", "files_created": "strlist", "files_deleted": "strlist",
    "tests_run": "count", "tests_passed": "count", "tests_failed": "count", "tests_skipped": "count",
    "commit_sha": "sha", "commits": "shalist", "push_status": "enum:push_status", "remote_sha": "sha",
    "git_status": "enum:git_status", "dirty_paths": "strlist",
    "ssot_sync_github": "enum:ssot_sync", "ssot_sync_drive": "enum:ssot_sync",
    "review_required": "bool", "review_status": "enum:review_status", "review_verdict": "enum:review_verdict",
    "reviewed_sha": "sha", "terminal_state": "enum:terminal_state",
    "recommended_next_action": "string",
}
FACT_FIELDS = tuple(FIELD_KINDS)
ADVISORY_FIELDS = frozenset({"recommended_next_action"})

TIERS = ("native", "fenced", "deterministic", "heuristic", "none")
REPORTING_TIERS = ("native", "fenced", "deterministic")
SOURCE_PATTERN = re.compile(r"^(agent:(native|fenced|deterministic|heuristic)|derived:[a-z_]+|probe:[a-z_]+|absent)$")
ABSENT = "absent"

# Report keys that are not fact fields but are folded into facts or lists.
REPORT_LIST_KEYS = ("evidence", "blockers", "warnings", "findings")


class ResultError(ValueError):
    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("invalid AI result: " + "; ".join(self.problems))


# -- Schemas (generated from FIELD_KINDS; committed copies are drift-tested) --

def _value_schema(kind):
    if kind == "string":
        return {"type": "string", "minLength": 1}
    if kind == "sha":
        return {"type": "string", "pattern": SHA_PATTERN}
    if kind == "count":
        return {"type": "integer", "minimum": 0}
    if kind == "percent":
        return {"type": "integer", "minimum": 0, "maximum": 100}
    if kind == "bool":
        return {"type": "boolean"}
    if kind == "strlist":
        return {"type": "array", "items": {"type": "string", "minLength": 1}}
    if kind == "shalist":
        return {"type": "array", "items": {"type": "string", "pattern": SHA_PATTERN}}
    if kind.startswith("enum:"):
        return {"enum": list(ENUMS[kind[5:]])}
    raise ValueError(kind)


def _nullable(schema):
    return {"oneOf": [{"type": "null"}, schema]}


def report_schema():
    properties = {"schema_version": {"const": REPORT_SCHEMA_VERSION}}
    for field, kind in FIELD_KINDS.items():
        if field.startswith("ssot_sync_"):
            continue
        properties[field] = _nullable(_value_schema(kind))
    properties["ssot_sync_status"] = _nullable({
        "type": "object", "additionalProperties": False,
        "properties": {"github": _nullable(_value_schema("enum:ssot_sync")),
                       "drive": _nullable(_value_schema("enum:ssot_sync"))},
    })
    properties["evidence"] = {"type": "array", "items": {
        "type": "object", "required": ["kind", "ref"],
        "properties": {"kind": {"type": "string", "minLength": 1}, "ref": {"type": "string", "minLength": 1},
                       "description": {"type": "string"}}}}
    for key in ("blockers", "warnings", "findings"):
        properties[key] = {"type": "array", "items": {"type": "string"}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ADM AI result report (agent-emitted claim)",
        "description": "What an agent emits at the end of a turn, inside a ```adm-result fenced block or as native "
                       "structured output. Every value is a claim; ADM normalizes it into schema/ai_result.schema.json "
                       "and verifies what it can. Generated from manager/nextplan/result.py FIELD_KINDS.",
        "type": "object",
        "required": ["schema_version", "task_id", "status"],
        "properties": properties,
        "additionalProperties": True,
    }


def normalized_schema():
    level = {"enum": list(v.LEVELS)}
    source = {"type": "string", "pattern": SOURCE_PATTERN.pattern}
    facts = {}
    for field, kind in FIELD_KINDS.items():
        facts[field] = {
            "type": "object", "additionalProperties": False,
            "required": ["value", "level", "source", "detail"],
            "properties": {"value": _nullable(_value_schema(kind)), "level": level, "source": source,
                           "detail": {"type": ["string", "null"]}},
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ADM normalized AI result",
        "description": "ADM's canonical view of one agent result event. Every fact carries an evidence level bound to "
                       "its source (see manager/nextplan/result.py). Generated from FIELD_KINDS.",
        "type": "object",
        "additionalProperties": False,
        "required": ["contract", "event_id", "task_id", "role", "extraction", "facts", "evidence", "blockers",
                     "warnings", "findings", "decisions"],
        "properties": {
            "contract": {"const": CONTRACT},
            "event_id": {"type": "string", "minLength": 1},
            "task_id": {"type": "string", "minLength": 1},
            "role": {"enum": list(v.ROLES)},
            "extraction": {
                "type": "object", "additionalProperties": False,
                "required": ["tier", "signals", "raw_sha256", "warnings"],
                "properties": {
                    "tier": {"enum": list(TIERS)},
                    "signals": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    "raw_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
            },
            "facts": {"type": "object", "additionalProperties": False, "required": list(FIELD_KINDS),
                      "properties": facts},
            "evidence": {"type": "array", "items": {"type": "object"}},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "findings": {"type": "array", "items": {"type": "string"}},
            # Structured reviewer decisions exactly as returned, before any
            # binding check. Deliberately NOT a fact: a fact carries an evidence
            # level, and levels are for claims ADM reasons over. Authority is not
            # a claim -- manager.nextplan.contracts.review_authority decides it
            # against ADM's own dispatch record, and only there.
            "decisions": {"type": "array", "items": {"type": "object"}},
        },
    }


@functools.lru_cache(maxsize=1)
def _report_validator():
    return Draft202012Validator(json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8")))


@functools.lru_cache(maxsize=1)
def _result_validator():
    return Draft202012Validator(json.loads(RESULT_SCHEMA_PATH.read_text(encoding="utf-8")))


# -- Facts --------------------------------------------------------------------

def fact(value, level, source, detail=None):
    return {"value": value, "level": level, "source": source, "detail": detail}


def unknown(detail=None):
    return fact(None, v.UNKNOWN, ABSENT, detail)


def blank_result(event_id, task_id, role, tier="none", raw_sha256="0" * 64):
    return {
        "contract": CONTRACT, "event_id": event_id, "task_id": task_id, "role": role,
        "extraction": {"tier": tier, "signals": [], "raw_sha256": raw_sha256, "warnings": []},
        "facts": {field: unknown() for field in FACT_FIELDS},
        "evidence": [], "blockers": [], "warnings": [], "findings": [], "decisions": [],
    }


def fact_problems(field, item):
    """Level/source binding for one fact. Assumes schema-valid shape."""
    problems = []
    level, source, value = item["level"], item["source"], item["value"]
    if (level == v.UNKNOWN) != (value is None):
        problems.append(f"{field}: UNKNOWN must carry value null, and only UNKNOWN may")
    if level == v.UNKNOWN and source != ABSENT and not source.startswith("probe:"):
        problems.append(f"{field}: UNKNOWN from {source} (only 'absent' or a probe that could not decide)")
    if level in (v.VERIFIED, v.CONTRADICTED) and not source.startswith("probe:"):
        problems.append(f"{field}: {level} requires a probe source, got {source}")
    if level == v.REPORTED and source not in tuple(f"agent:{tier}" for tier in REPORTING_TIERS):
        problems.append(f"{field}: REPORTED requires a structured or deterministic agent source, got {source}")
    if level == v.DERIVED and not (source == "agent:heuristic" or source.startswith("derived:")):
        problems.append(f"{field}: DERIVED requires a heuristic or derived source, got {source}")
    if source == "agent:heuristic" and level not in (v.DERIVED,):
        problems.append(f"{field}: a heuristic can only ever produce DERIVED, got {level}")
    return problems


def result_problems(result):
    errors = sorted(_result_validator().iter_errors(result), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        return [f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]
    problems = []
    for field in FACT_FIELDS:
        problems.extend(fact_problems(field, result["facts"][field]))
    return problems


def validate_result(result):
    problems = result_problems(result)
    if problems:
        raise ResultError(problems)
    return result


def report_problems(report):
    errors = sorted(_report_validator().iter_errors(report), key=lambda e: list(map(str, e.absolute_path)))
    return [f"report: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors]


def get(result, field):
    return result["facts"][field]


def value(result, field):
    return result["facts"][field]["value"]


def level(result, field):
    return result["facts"][field]["level"]


def is_verified(result, field):
    return level(result, field) == v.VERIFIED


def with_fact(result, field, item):
    """A copy of ``result`` with one fact replaced. Never mutates the input."""
    if field not in FIELD_KINDS:
        raise ResultError([f"unknown fact field {field!r}"])
    problems = fact_problems(field, item)
    if problems:
        raise ResultError(problems)
    updated = copy.deepcopy(result)
    updated["facts"][field] = dict(item)
    return updated
