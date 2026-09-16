"""Failure Atlas: the canonical failure taxonomy NextPlan reads its policy from.

The atlas is data (``failure_atlas.json``), not prose. Structure is checked by
``schema/failure_atlas.schema.json``; the rules in ``semantic_problems`` are
the ones a schema cannot express, and each one exists to make a specific bad
planner outcome impossible to author:

- a failure whose ``next_state`` does not follow from its action;
- a failure that starts another round of work without a finite budget
  (an infinite retry loop);
- a failure that routes to ``MARK_COMPLETE`` (error becomes success);
- a human-gated failure that routes to an automated action;
- a reroute on a failure that forbids rerouting;
- a failure with no defined behaviour once its budget is spent.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from jsonschema import Draft202012Validator

from manager.nextplan import vocabulary as v

ATLAS_PATH = Path(__file__).with_name("failure_atlas.json")
SCHEMA_PATH = Path(__file__).parents[2] / "schema" / "failure_atlas.schema.json"

# The failure classes the Execution Plan requires the atlas to cover. The
# atlas may define more; it may never define fewer.
REQUIRED_CODES = frozenset({
    # Repo / Git
    "wrong_repo", "wrong_worktree", "wrong_branch", "dirty_worktree", "unrelated_dirty_files",
    "stale_base", "merge_conflict", "push_failed", "remote_advanced", "missing_remote", "detached_head",
    # Agent execution
    "agent_timeout", "agent_crash", "agent_lost", "session_not_resumable", "quota_exhausted",
    "rate_limited", "malformed_result", "incomplete_result", "contradictory_result", "false_pass",
    "hallucinated_test_result",
    # Test / Review
    "test_failed", "test_not_run", "partial_test_run", "reviewer_fail", "reviewer_crash",
    "reviewer_not_fresh", "evidence_missing", "regression_unknown",
    # SSOT / Sync
    "drive_unavailable", "github_unavailable", "stale_ssot", "ssot_conflict", "sync_failed",
    "local_only_artifact", "provenance_unknown",
    # Orchestration
    "duplicate_dispatch", "duplicate_execution", "idempotency_violation", "stale_task_state",
    "task_already_completed", "conflicting_owner", "parallel_writer_collision", "blocked_dependency",
    "retry_exhausted", "unsafe_retry", "planner_no_valid_action",
})

_BUDGET_ACTION = {"repair": v.RETURN_TO_WORKER, "review": v.SEND_TO_REVIEW}


class AtlasError(ValueError):
    """The atlas is unusable. ``problems`` lists every rule it breaks."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("invalid Failure Atlas: " + "; ".join(self.problems))


def _entry_actions(entry):
    actions = {entry["safe_default"], *entry["role_overrides"].values()}
    if entry["fallback_action"] is not None:
        actions.add(entry["fallback_action"])
    return actions


def semantic_problems(document, required_codes=REQUIRED_CODES):
    """Cross-field rules. Assumes the document already passed the schema."""
    problems = []
    seen = set()
    for entry in document["entries"]:
        code = entry["failure_code"]
        if code in seen:
            problems.append(f"duplicate failure_code {code!r}")
        seen.add(code)
    for code in sorted(set(required_codes) - seen):
        problems.append(f"required failure_code missing: {code}")
    owners = {}
    for entry in document["entries"]:
        for signal in entry["detection_signal"]:
            if signal in owners and owners[signal] != entry["failure_code"]:
                problems.append(f"detection signal {signal!r} belongs to both {owners[signal]} and "
                                f"{entry['failure_code']}; a signal must classify to exactly one failure")
            owners.setdefault(signal, entry["failure_code"])

    for entry in document["entries"]:
        code = entry["failure_code"]
        safe, fallback, overrides = entry["safe_default"], entry["fallback_action"], entry["role_overrides"]
        policy, terminal = entry["max_retry_policy"], entry["terminal_if_unresolved"]
        actions = _entry_actions(entry)

        expected_state = v.ACTION_NEXT_STATE[safe]
        if entry["next_state"] != expected_state:
            problems.append(f"{code}: next_state {entry['next_state']} does not follow from safe_default "
                            f"{safe} (expected {expected_state})")

        if v.MARK_COMPLETE in actions or terminal == v.MARK_COMPLETE:
            problems.append(f"{code}: a failure can never route to MARK_COMPLETE")

        if fallback is not None and fallback == safe:
            problems.append(f"{code}: fallback_action repeats safe_default")

        round_consuming = actions & v.ROUND_CONSUMING_ACTIONS
        if round_consuming:
            if not entry["retryable"] or policy["max_attempts"] < 1:
                problems.append(f"{code}: {sorted(round_consuming)} start another round, so the failure must be "
                                "retryable with max_attempts >= 1 (a finite budget)")
        elif entry["retryable"] or policy["max_attempts"] != 0:
            problems.append(f"{code}: no action starts another round, so retryable must be false and max_attempts 0")

        if safe == v.NO_OP:
            if fallback is not None or overrides or terminal != v.NO_OP:
                problems.append(f"{code}: a NO_OP failure must have no fallback, no role overrides and terminal NO_OP")
        elif terminal not in v.UNRESOLVED_ACTIONS:
            problems.append(f"{code}: terminal_if_unresolved {terminal} is not a hold action "
                            f"({sorted(v.UNRESOLVED_ACTIONS)})")

        if entry["human_gate_required"]:
            if safe != v.HUMAN_GATE:
                problems.append(f"{code}: human_gate_required but safe_default is {safe}, not HUMAN_GATE")
            automated = actions & v.AUTOMATED_ACTIONS
            if automated:
                problems.append(f"{code}: human-gated failure routes to automated action(s) {sorted(automated)}")

        if not entry["reroute_allowed"] and v.REROUTE_AGENT in actions:
            problems.append(f"{code}: reroute_allowed is false but an action is REROUTE_AGENT")

        budget_action = _BUDGET_ACTION.get(policy["budget"])
        if budget_action is not None and safe != budget_action:
            problems.append(f"{code}: budget {policy['budget']!r} is only for safe_default {budget_action}")
    return problems


@functools.lru_cache(maxsize=1)
def _schema_validator():
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def validate_atlas(document, required_codes=REQUIRED_CODES):
    """Raise AtlasError listing every structural and semantic problem."""
    errors = sorted(_schema_validator().iter_errors(document), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        raise AtlasError([f"schema: /{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors])
    problems = semantic_problems(document, required_codes)
    if problems:
        raise AtlasError(problems)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_digest(document):
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Atlas:
    """A validated, read-only atlas. Entries cannot be mutated by callers."""

    def __init__(self, document, required_codes=REQUIRED_CODES):
        validate_atlas(document, required_codes)
        self.version = document["atlas_version"]
        self.digest = canonical_digest(document)
        self.order = tuple(entry["failure_code"] for entry in document["entries"])
        self._index = {code: index for index, code in enumerate(self.order)}
        self._entries = MappingProxyType({entry["failure_code"]: _freeze(entry) for entry in document["entries"]})

    def __contains__(self, code):
        return code in self._entries

    @property
    def codes(self):
        return self.order

    def entry(self, code):
        try:
            return self._entries[code]
        except KeyError:
            raise AtlasError([f"unknown failure_code {code!r}"]) from None

    def rank(self, code):
        """Sort key: most severe first, then declaration order. Total and stable."""
        return (v.SEVERITY_RANK[self.entry(code)["severity"]], self._index[code])

    def governing(self, codes):
        """The single failure that decides the action, independent of input order."""
        codes = set(codes)
        if not codes:
            return None
        return min(codes, key=self.rank)

    def action_for(self, code, role):
        entry = self.entry(code)
        return entry["role_overrides"].get(role, entry["safe_default"])


def load_atlas(path=ATLAS_PATH):
    return Atlas(json.loads(Path(path).read_text(encoding="utf-8")))


@functools.lru_cache(maxsize=1)
def default_atlas():
    return load_atlas()
