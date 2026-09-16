"""Agent output -> normalized result, without ever inventing success.

Sources are tried in a fixed order and each one caps the evidence level it can
produce:

  1. native      the output *is* a report object (``format: json``) or a
                 stream-json result event carries ``structured_output``  REPORTED
  2. fenced      a ```adm-result block (or a ```json block whose object has
                 ``schema_version: adm-ai-result/1``)                     REPORTED
  3. deterministic  anchored key/value lines and pytest/unittest summaries REPORTED
  4. heuristic   constrained keyword rules, only when nothing structured
                 was even attempted                                        DERIVED
  5. none        everything UNKNOWN

Nothing here can produce VERIFIED; only manager.nextplan.verify can.

Two rules keep this from being permissive:

- **Disagreement is never resolved optimistically.** When the payload and a
  deterministic line (or two deterministic lines) disagree about a field, the
  field becomes UNKNOWN and ``extract.conflicting_statements`` is signalled.
- **A broken structured attempt is not guessed at.** If the agent tried to emit
  a payload and it was invalid or truncated, the heuristic tier does not run
  for status: we ask again rather than read "looks done" into prose.

Negative heuristics (quota / rate-limit phrases) always run: acting on them
only ever routes away from completion.
"""

from __future__ import annotations

import hashlib
import json
import re

from jsonschema import Draft202012Validator

from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

_FIELD_VALIDATORS = {field: Draft202012Validator(r._value_schema(kind)) for field, kind in r.FIELD_KINDS.items()}
_TIER_ORDER = {tier: index for index, tier in enumerate(r.TIERS)}

# Facts only a reviewer has the authority to assert, whatever tier they arrive in.
REVIEW_AUTHORITY_FIELDS = frozenset({"review_verdict", "reviewed_sha"})

_FENCE_OPEN = re.compile(r"^\s*```[ \t]*(adm-result|json)[ \t]*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")

# -- deterministic vocabulary ---------------------------------------------------

_KEY_ALIASES = {
    "status": "status", "狀態": "status", "result": "status", "結果": "status", "final status": "status",
    "最終狀態": "status", "verdict": "review_verdict", "review verdict": "review_verdict",
    "reviewer verdict": "review_verdict", "審查結果": "review_verdict",
    "commit": "commit_sha", "commit sha": "commit_sha", "commit_sha": "commit_sha",
    "head": "head_sha", "head sha": "head_sha", "head_sha": "head_sha", "local head": "head_sha",
    "remote sha": "remote_sha", "remote_sha": "remote_sha", "remote head": "remote_sha",
    "base": "base_sha", "base sha": "base_sha", "base_sha": "base_sha",
    "reviewed sha": "reviewed_sha", "reviewed_sha": "reviewed_sha",
    "branch": "branch", "分支": "branch",
    "push": "push_status", "push status": "push_status", "push_status": "push_status", "github sync": "push_status",
    "git status": "git_status", "git_status": "git_status",
    "tests": "tests", "test": "tests", "測試": "tests",
    "drive sync": "ssot_sync_drive", "drive": "ssot_sync_drive",
    "progress": "progress_percent", "進度": "progress_percent", "目前進度": "progress_percent",
    "blockers": "blockers", "blocker": "blockers",
}
_KV_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?(?P<key>[A-Za-z_ 一-鿿]{1,24}?)(?:\*\*)?\s*[:：]\s*(?P<val>.*?)\s*$")

_STATUS_WORDS = {
    "PASS": "PASS", "PASSED": "PASS", "SUCCESS": "PASS", "OK": "PASS", "成功": "PASS", "通過": "PASS",
    "DONE": "PASS", "COMPLETE": "PASS", "COMPLETED": "PASS", "完成": "PASS", "已完成": "PASS",
    "READY_FOR_INDEPENDENT_REVIEW": "PASS",
    "FAIL": "FAIL", "FAILED": "FAIL", "FAILURE": "FAIL", "失敗": "FAIL",
    "PARTIAL": "PARTIAL", "PARTIALLY_READY": "PARTIAL", "部分完成": "PARTIAL",
    "BLOCKED": "BLOCKED", "阻塞": "BLOCKED",
    "IN_PROGRESS": "IN_PROGRESS", "進行中": "IN_PROGRESS",
    "ERROR": "ERROR",
}
_VERDICT_WORDS = {"PASS": "PASS", "APPROVE": "PASS", "APPROVED": "PASS", "ACCEPTED": "PASS",
                  "FAIL": "FAIL", "REJECT": "FAIL", "REJECTED": "FAIL", "CHANGES_REQUIRED": "FAIL"}
_NONE_WORDS = re.compile(r"^(none|n/?a|無|沒有|-|—)\.?$", re.I)

_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)\b")
_PYTEST_SUMMARY = re.compile(r"^=+\s.*\b(?:passed|failed|errors?)\b.*\bin\s+[\d.]+s.*=+\s*$")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*$")
_UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)\s*$")

# -- heuristic vocabulary (constrained) ---------------------------------------------

_QUOTA = re.compile(r"usage limit|quota (?:is )?(?:exhausted|exceeded)|insufficient_quota|out of credits|"
                    r"free-models-per-day|額度(?:已)?(?:用完|耗盡)", re.I)
_RATE = re.compile(r"\b429\b|rate[- ]?limit(?:ed)?", re.I)
_BLOCKED_PROSE = re.compile(r"\bblocked\b|\bcannot proceed\b|無法繼續|阻塞", re.I)
_DONE_PROSE = re.compile(r"\ball (?:tests )?pass(?:ed|ing)?\b|\b(?:done|completed|finished)\b|已完成|完成", re.I)
_FAIL_PROSE = re.compile(r"\bfail(?:ed|ing|ure)?\b|失敗", re.I)
# An explicit contrary judgement in the agent's own prose. Only ever used to
# withdraw a positive claim, never to create one.
_CONTRARY_VERDICT = re.compile(
    r"changes[ _-]required|should not (?:ship|merge|land)|do not merge|\breject(?:ed|s)?\b|"
    r"\bnot ready\b|needs? (?:more )?work|不應(?:該)?合併|需要(?:再)?修改|尚未完成", re.I)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _same(field, left, right):
    if r.FIELD_KINDS[field] == "sha" and isinstance(left, str) and isinstance(right, str):
        return left.startswith(right) or right.startswith(left)
    return left == right


def _longer(left, right):
    return left if isinstance(left, str) and isinstance(right, str) and len(left) >= len(right) else right


# -- source 1/2: payloads -----------------------------------------------------------------

def _stream_json(content):
    """(text, native_payload, warnings) from Claude-style stream-json."""
    last = None
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            last = event
    if last is None:
        return "", None, ["stream-json: no result event"]
    warnings = ["stream-json: provider reported is_error"] if last.get("is_error") else []
    native = last.get("structured_output")
    text = last.get("result") if isinstance(last.get("result"), str) else ""
    return text, native if isinstance(native, dict) else None, warnings


def _fenced_blocks(lines):
    """[(lang, body, closed, line_indexes)] for every ```adm-result / ```json block."""
    blocks, index = [], 0
    while index < len(lines):
        opened = _FENCE_OPEN.match(lines[index])
        if not opened:
            index += 1
            continue
        start = index
        body, index = [], index + 1
        while index < len(lines) and not _FENCE_CLOSE.match(lines[index]):
            body.append(lines[index])
            index += 1
        closed = index < len(lines)
        blocks.append((opened.group(1), "\n".join(body), closed, range(start, min(index + 1, len(lines)))))
        index += 1
    return blocks


def _fenced_payload(lines, signals, fenced_lines):
    """The single agreed payload, or None. ``attempted`` says whether one was tried.

    Every line of every structured block is added to ``fenced_lines`` so the
    prose tiers never re-read payload text as if it were narrative.
    """
    payloads, attempted = [], False
    for lang, body, closed, indexes in _fenced_blocks(lines):
        ours = lang == "adm-result" or r.REPORT_SCHEMA_VERSION in body
        if not ours:
            continue
        fenced_lines.update(indexes)
        attempted = True
        if not closed:
            signals.add("extract.payload_truncated")
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            signals.add("extract.payload_invalid_json")
            continue
        if not isinstance(payload, dict):
            signals.add("extract.payload_invalid_json")
            continue
        payloads.append(payload)
    distinct = {json.dumps(p, sort_keys=True) for p in payloads}
    if len(distinct) > 1:
        signals.add("extract.conflicting_statements")
        return None, attempted
    return (payloads[-1] if payloads else None), attempted


def _payload_facts(payload, tier, signals, warnings, role):
    facts, lists = {}, {"evidence": [], "blockers": [], "warnings": [], "findings": []}
    for problem in r.report_problems(payload):
        warnings.append(problem)
    for field in r.FACT_FIELDS:
        if field in REVIEW_AUTHORITY_FIELDS and role != v.REVIEWER:
            # Only a reviewer can assert a verdict. A worker payload carrying
            # one is dropped here as well as in the deterministic tier, so the
            # rule does not depend on which tier the claim arrived through.
            if payload.get(field) is not None:
                warnings.append(f"{field}: ignored, only a reviewer may assert it")
            continue
        if field.startswith("ssot_sync_"):
            sync = payload.get("ssot_sync_status")
            raw = sync.get(field[len("ssot_sync_"):]) if isinstance(sync, dict) else None
        else:
            raw = payload.get(field)
        if raw is None:
            continue
        if _FIELD_VALIDATORS[field].is_valid(raw):
            facts[field] = raw
        else:
            warnings.append(f"{field}: invalid reported value dropped")
    for key in ("task_id", "status"):
        if key not in facts:
            signals.add("result.required_field_missing")
    for key in ("blockers", "warnings", "findings"):
        items = payload.get(key)
        if isinstance(items, list):
            lists[key] = [item for item in items if isinstance(item, str)]
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        lists["evidence"] = [item for item in evidence
                             if isinstance(item, dict) and isinstance(item.get("kind"), str) and isinstance(item.get("ref"), str)]
    return facts, lists


# -- source 3: deterministic lines ---------------------------------------------------------------

def test_counts(text):
    """Public: parse "N passed, N failed, N skipped" style counts out of text.

    Shared with manager.nextplan.verify so a test summary recorded by ADM is
    read by exactly the same parser as one an agent quotes.
    """
    return _counts(text)


def _counts(text):
    found = {}
    for number, word in _COUNT.findall(text):
        word = "errors" if word.startswith("error") else word
        found[word] = found.get(word, 0) + int(number)
    if not found:
        return {}
    failed = found.get("failed", 0) + found.get("errors", 0)
    counts = {"tests_passed": found.get("passed", 0), "tests_failed": failed}
    if "skipped" in found:
        counts["tests_skipped"] = found["skipped"]
    return counts


def _map_value(field, raw, role):
    token = raw.strip().strip("*`").strip()
    upper = token.upper().replace(" ", "_")
    # Exact matches only. A prefix fallback used to collapse a hedge into a
    # clean verdict -- "PASS_WITH_CAVEATS" became PASS and
    # "APPROVED_WITH_COMMENTS" became a passing review. A qualified answer is
    # not the unqualified one; unmapped words fall through to a warning and
    # stay UNKNOWN.
    if field == "status":
        return _STATUS_WORDS.get(upper)
    if field == "review_verdict":
        return _VERDICT_WORDS.get(upper)
    if field in ("commit_sha", "head_sha", "remote_sha", "base_sha", "reviewed_sha"):
        match = _HEX.search(token.lower())
        return match.group(0) if match else None
    if field == "branch":
        match = re.search(r"[A-Za-z0-9._/-]+", token)
        return match.group(0) if match else None
    if field == "push_status":
        lowered = token.lower()
        if re.search(r"not pushed|未\s*push|未推送|no push", lowered):
            return "not_pushed"
        if re.search(r"fail|失敗|rejected|denied", lowered):
            return "failed"
        if re.search(r"pushed|已\s*push|已推送|synced|success", lowered):
            return "pushed"
        return None
    if field == "git_status":
        lowered = token.lower()
        if re.search(r"\bdirty\b|uncommitted|未提交|有變更", lowered):
            return "dirty"
        if re.search(r"\bclean\b|乾淨|nothing to commit", lowered):
            return "clean"
        return None
    if field == "ssot_sync_drive":
        lowered = token.lower()
        if re.search(r"not required|n/a|不需要", lowered):
            return "not_required"
        if re.search(r"fail|失敗|unavailable|error", lowered):
            return "failed"
        if re.search(r"synced|已同步|read-?back (?:ok|pass|verified)", lowered):
            return "synced"
        return None
    if field == "progress_percent":
        match = re.search(r"(\d{1,3})\s*%", token)
        return int(match.group(1)) if match and int(match.group(1)) <= 100 else None
    return None


def _deterministic(lines, role, warnings, skip):
    """({field: [values]}, set of consumed line indexes, blockers). Lines in ``skip`` are payload."""
    seen, consumed, blockers = {}, set(skip), []

    def add(field, value, index):
        if value is None:
            return
        seen.setdefault(field, [])
        if not any(_same(field, value, prior) for prior in seen[field]):
            seen[field].append(value)
        consumed.add(index)

    for index, line in enumerate(lines):
        if index in skip:
            continue
        stripped = line.strip()
        if _PYTEST_SUMMARY.match(stripped):
            for field, value in _counts(stripped).items():
                add(field, value, index)
            continue
        ran = _UNITTEST_RAN.match(stripped)
        if ran:
            add("tests_run", int(ran.group(1)), index)
            continue
        failed = _UNITTEST_FAILED.match(stripped)
        if failed:
            numbers = [int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", failed.group(1))]
            add("tests_failed", sum(numbers), index)
            continue
        match = _KV_LINE.match(line)
        if not match:
            continue
        field = _KEY_ALIASES.get(match.group("key").strip().lower())
        raw = match.group("val")
        if field is None or not raw.strip():
            continue
        if field == "tests":
            counts = _counts(raw)
            for name, value in counts.items():
                add(name, value, index)
            if counts:
                continue
            if re.search(r"not run|未執行|沒有跑|no tests", raw, re.I):
                add("tests_run", 0, index)
            continue
        if field == "blockers":
            consumed.add(index)
            if not _NONE_WORDS.match(raw.strip()):
                blockers.append(raw.strip())
            continue
        if field == "review_verdict" and role != v.REVIEWER:
            continue
        mapped = _map_value(field, raw, role)
        if mapped is None:
            warnings.append(f"deterministic: unmapped {field} value {raw.strip()[:60]!r}")
            continue
        add(field, mapped, index)
    return seen, consumed, blockers


# -- source 4: constrained heuristics -------------------------------------------------------------

def _heuristic_status(lines, consumed):
    text = "\n".join(line for index, line in enumerate(lines) if index not in consumed)
    blocked, done, failed = (bool(p.search(text)) for p in (_BLOCKED_PROSE, _DONE_PROSE, _FAIL_PROSE))
    if blocked:
        return "BLOCKED"
    if done and not failed:
        return "PASS"
    if failed and not done:
        return "FAIL"
    return None


# -- entry point ---------------------------------------------------------------------------------------

def extract(output):
    """Normalize one agent output envelope.

    ``output``: ``{event_id, task_id, role, format ('text'|'json'|'stream-json'),
    content, session_id?}``. ``task_id``/``role``/``session_id`` come from ADM's
    own dispatch record, never from the agent.
    """
    content = (output.get("content") or "").replace("\r\n", "\n")
    role = output["role"]
    signals, warnings = set(), []
    facts, lists = {}, {"evidence": [], "blockers": [], "warnings": [], "findings": []}
    tier = "none"

    text, native = content, None
    fmt = output.get("format", "text")
    if fmt == "stream-json":
        text, native, stream_warnings = _stream_json(content)
        warnings.extend(stream_warnings)
    elif fmt == "json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
            signals.add("extract.payload_invalid_json")
        if isinstance(parsed, dict) and parsed.get("schema_version", "").startswith("adm-ai-result"):
            native, text = parsed, ""
        elif parsed is not None:
            warnings.append("json output is not an adm-ai-result report")
            text = ""

    lines = text.split("\n") if text else []
    fenced_lines = set()
    payload, attempted = (native, True) if native is not None else _fenced_payload(lines, signals, fenced_lines)
    if native is None and fmt == "json" and "extract.payload_invalid_json" in signals:
        attempted = True
    if payload is not None:
        tier = "native" if native is not None else "fenced"
        facts, lists = _payload_facts(payload, tier, signals, warnings, role)

    sources = {field: f"agent:{tier}" for field in facts}
    deterministic, consumed, blockers = _deterministic(lines, role, warnings, fenced_lines)
    for field, values in deterministic.items():
        candidate = values[0]
        if len(values) > 1:
            signals.add("extract.conflicting_statements")
            facts.pop(field, None)
            sources[field] = f"conflict:{values!r}"
            continue
        if field in facts:
            if _same(field, facts[field], candidate):
                facts[field] = _longer(facts[field], candidate)
                continue
            signals.add("extract.conflicting_statements")
            sources[field] = f"conflict: payload={facts.pop(field)!r} text={candidate!r}"
            continue
        if field in sources:  # already a conflict
            continue
        facts[field] = candidate
        sources[field] = "agent:deterministic"
        if tier in ("none", "heuristic"):
            tier = "deterministic"
    if blockers and not lists["blockers"]:
        lists["blockers"] = blockers

    result = r.blank_result(output["event_id"], output["task_id"], role, raw_sha256=_sha256(content))
    for field, value in facts.items():
        result["facts"][field] = r.fact(value, v.REPORTED, sources[field])
    for field, source in sources.items():
        if source.startswith("conflict"):
            result["facts"][field] = r.unknown(source[:300])

    # tests_run is arithmetic over reported counts when not stated: DERIVED.
    if r.level(result, "tests_run") == v.UNKNOWN and "tests_run" not in sources:
        passed, failed = r.value(result, "tests_passed"), r.value(result, "tests_failed")
        if r.level(result, "tests_passed") == v.REPORTED and r.level(result, "tests_failed") == v.REPORTED:
            result["facts"]["tests_run"] = r.fact(passed + failed, v.DERIVED, "derived:test_count_sum")

    # Prose the agent wrote outside any payload or parsed line.
    prose = "\n".join(line for index, line in enumerate(lines) if index not in consumed)

    # A positive claim is withdrawn when the same message states the opposite
    # in prose: a payload saying review_verdict PASS next to "CHANGES REQUIRED"
    # is a contradiction, not an approval. This can only ever remove a claim,
    # which is why a heuristic is allowed to do it.
    if _CONTRARY_VERDICT.search(prose):
        for field in ("status", "review_verdict"):
            if r.value(result, field) == "PASS" and r.level(result, field) in (v.REPORTED, v.DERIVED):
                result["facts"][field] = r.unknown(
                    f"claimed PASS, but the same message states the opposite in prose"[:300])
                signals.add("extract.conflicting_statements")

    # Negative heuristics read only what the agent said about its own run:
    # prose outside payloads, plus declared blockers. Payload findings or code
    # that merely *mention* rate limiting must not trip them.
    heuristic_text = "\n".join([prose if payload is None else "", *lists["blockers"]])
    if _QUOTA.search(heuristic_text):
        signals.add("result.quota_exhausted")
    elif _RATE.search(heuristic_text):
        signals.add("result.rate_limited")
    if (not attempted and r.level(result, "status") == v.UNKNOWN and "status" not in sources):
        status = _heuristic_status(lines, consumed)
        if status is not None:
            result["facts"]["status"] = r.fact(status, v.DERIVED, "agent:heuristic")
            if tier == "none":
                tier = "heuristic"
    if tier == "none" and signals & {"result.quota_exhausted", "result.rate_limited"}:
        tier = "heuristic"
    if payload is None and tier in ("none", "heuristic"):
        signals.add("extract.no_parseable_payload")

    reported_task = r.value(result, "task_id")
    reported_session = r.value(result, "session_id")
    if reported_task is not None and reported_task != output["task_id"]:
        signals.add("result.identity_mismatch")
    if output.get("session_id") and reported_session is not None and reported_session != output["session_id"]:
        signals.add("result.identity_mismatch")

    result["extraction"].update(tier=tier, signals=sorted(signals), warnings=warnings)
    for key, items in lists.items():
        result[key] = items
    return r.validate_result(result)
