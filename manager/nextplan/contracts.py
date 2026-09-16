"""Canonical machine-readable contracts. The only things that carry authority.

Rounds 2 to 4 all failed the same way, one layer further in each time: some
piece of *natural language* was allowed to decide. Round 2 let a documented
example prove that tests ran. Round 3 let a fenced payload say ``PASS`` and read
"no rejection matched" as the reviewer agreeing. Round 4 demanded a written
anchor -- and the independent review then completed 20 of 25 fresh rejections
simply by putting ``Verdict: PASS`` above them, because once an anchor existed
the decision was again settled by whichever rejection wordings a regex happened
to cover.

The lesson is not that the wordings were wrong. It is that **a decision derived
from prose is not a decision at all**, because prose has no target, no run
identity and no provenance: nothing in ``Verdict: PASS`` says *which* commit was
reviewed, *which* dispatched run wrote it, or whether its author was even the
reviewer. A heading, a quoted history line and a reviewer's own conclusion are
textually indistinguishable, so no amount of pattern work can separate them.

So authority moves out of the text entirely:

- a reviewer authorizes by returning an ``adm-review-result/v1`` object whose
  ``target_sha`` is the commit ADM is actually holding and whose
  ``reviewer_run_id`` is the run identity **ADM itself issued at dispatch**;
- a validation run proves tests by returning an ``adm-validation-result/v1``
  object whose counts came from a runner adapter bound to one spawned process
  (see ``manager.nextplan.runner``), never from parsing a command's stdout.

Prose survives as explanation and as a diagnostic signal -- it may still
*withdraw* a structured PASS that contradicts it, because a rule that can only
remove a claim is safe. It may never grant one. That asymmetry is the whole
design: forging the text buys nothing, because the text is not the channel.

Neither contract is trusted for being well-shaped. A forged JSON block is no
harder to type than a forged sentence. What a forger cannot supply is the
binding: a ``reviewer_run_id`` ADM handed out, for the exact SHA ADM is holding.
``review_authority`` checks the object against ADM's own dispatch record, and
with no such record on file **nothing can authorize**.
"""

from __future__ import annotations

import re

REVIEW_SCHEMA = "adm-review-result/v1"
VALIDATION_SCHEMA = "adm-validation-result/v1"

VERDICTS = ("PASS", "REJECT")

# Why review_authority decided what it did. The planner routes on this, so a
# rejection ("go fix it") stays distinguishable from an unreadable decision
# ("we do not know what you decided") -- collapsing those is how Round 4 came to
# treat an unreadable verdict as no verdict at all.
AUTHORIZED = "authorized"   # a bound PASS for this exact target
REJECTED = "rejected"       # a bound REJECT, or a PASS carrying blocking findings
CONFLICT = "conflict"       # PASS and REJECT for the same target
INVALID = "invalid"         # malformed, empty or unknown decision value
ABSENT = "absent"           # nothing bound was returned; never consent
REVIEW_MODES = ("read_only",)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

# Findings carry their own severity. One that does not say is treated as
# blocking: an unreadable severity is not a harmless one.
_SEVERITIES = ("blocking", "high", "medium", "low", "info")
_NON_BLOCKING = frozenset({"low", "info"})


def _text(value):
    return value if isinstance(value, str) else None


def _identifier(value):
    token = _text(value)
    return token if token and _ID.match(token) else None


# -- adm-review-result/v1 -----------------------------------------------------

def review_problems(block):
    """Every reason ``block`` is not a usable reviewer decision. Shape only.

    Shape is necessary, never sufficient: ``review_authority`` still has to bind
    the object to ADM's own dispatch record. Keeping the two apart means a
    malformed object and a well-formed but unbound one fail for stated,
    different reasons instead of collapsing into one opaque refusal.
    """
    if not isinstance(block, dict):
        return [f"review result is {type(block).__name__}, not an object"]
    problems = []
    if block.get("schema") != REVIEW_SCHEMA:
        problems.append(f"schema is {block.get('schema')!r}, not {REVIEW_SCHEMA!r}")

    # An absent verdict, an empty one and an unrecognised one are all *invalid*,
    # and deliberately not silence. Round 4 let "Current decision: reject" and a
    # "Decision:" with no value stand aside while a PASS anchor completed the
    # task -- which is how "we could not read the decision" came to mean "there
    # was no decision".
    if "verdict" not in block:
        problems.append("verdict is missing")
    else:
        verdict = block.get("verdict")
        if not isinstance(verdict, str) or not verdict.strip():
            problems.append("verdict is empty")
        elif verdict not in VERDICTS:
            problems.append(f"verdict {verdict!r} is not one of {list(VERDICTS)}")

    target = _text(block.get("target_sha"))
    if not target or not _SHA.match(target.lower()):
        problems.append(f"target_sha {block.get('target_sha')!r} is not a commit SHA")
    if not _identifier(block.get("reviewer_run_id")):
        problems.append(f"reviewer_run_id {block.get('reviewer_run_id')!r} is not a run identifier")

    findings = block.get("findings")
    if not isinstance(findings, list):
        problems.append("findings must be a list (an empty list states that there are none)")
    else:
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                problems.append(f"findings[{index}] is not an object")
                continue
            if not _text(finding.get("summary")):
                problems.append(f"findings[{index}] has no summary")
            severity = finding.get("severity")
            if severity is not None and severity not in _SEVERITIES:
                problems.append(f"findings[{index}] severity {severity!r} is unknown")

    provenance = block.get("provenance")
    if not isinstance(provenance, dict):
        problems.append("provenance is missing")
    else:
        for field in ("provider", "job_id"):
            if not _identifier(provenance.get(field)):
                problems.append(f"provenance.{field} {provenance.get(field)!r} is not an identifier")
        if provenance.get("mode") not in REVIEW_MODES:
            problems.append(f"provenance.mode {provenance.get('mode')!r} is not one of {list(REVIEW_MODES)}")
    return problems


def finding_is_blocking(finding):
    severity = finding.get("severity") if isinstance(finding, dict) else None
    if severity is None:
        return True  # unstated severity is blocking; see _SEVERITIES
    return severity not in _NON_BLOCKING


def _binding_problems(block, expectation):
    """``(problems, relevant)``: why this decision is not bound to what ADM dispatched.

    ``relevant`` is False only when the decision is about a different commit.
    Such an object is not evidence about the candidate in hand at all, so it
    neither authorizes nor blocks.
    """
    expected_target = _text((expectation or {}).get("target_sha"))
    expected_run = _identifier((expectation or {}).get("reviewer_run_id"))
    if not expected_target:
        return ["ADM holds no candidate SHA for this task"], True
    if not expected_run:
        # No dispatch record means no run identity was ever issued, so no
        # returned object can be shown to be the review ADM asked for.
        return ["ADM has no reviewer dispatch record to bind this decision to"], True

    target = (_text(block.get("target_sha")) or "").lower()
    expected_target = expected_target.lower()
    # Prefix comparison in the shorter direction only: an abbreviated SHA may
    # stand for the full one, never the reverse.
    on_target = (target == expected_target
                 or (len(target) >= 7 and expected_target.startswith(target))
                 or (len(expected_target) >= 7 and target.startswith(expected_target)))
    if not on_target:
        return [f"decision targets {target!r}, not the candidate {expected_target!r}"], False

    problems = []
    if block.get("reviewer_run_id") != expected_run:
        problems.append(f"reviewer_run_id {block.get('reviewer_run_id')!r} is not the dispatched run {expected_run!r}")
    provenance = block.get("provenance") if isinstance(block.get("provenance"), dict) else {}
    for field in ("provider", "job_id"):
        expected_value = (expectation or {}).get(field)
        if expected_value and provenance.get(field) != expected_value:
            problems.append(f"provenance.{field} {provenance.get(field)!r} is not the dispatched {expected_value!r}")
    return problems, True


def review_authority(decisions, expectation):
    """Does this reviewer output authorize completing ``expectation``'s target?

    Returns ``{authorized, blocked, verdict, problems, considered}``.
    ``authorized`` and ``blocked`` are never both true, and an output that is
    neither is simply not a decision -- the planner treats that as a missing
    required field, never as consent.

    The conflict rules (Round 5 requirement B), in order:

    * an authoritative REJECT, or a PASS carrying any blocking finding, blocks;
    * PASS and REJECT for the same target is a conflict, and blocks;
    * an empty, unknown or malformed decision is invalid, and blocks;
    * an unbound decision (wrong run, wrong provenance) never authorizes, and
      is not allowed to block either -- otherwise a forged REJECT could stall
      any task;
    * a decision for another SHA is irrelevant: it neither authorizes nor blocks;
    * no decision at all never authorizes.
    """
    problems, verdicts, considered, invalid = [], set(), 0, False
    for index, block in enumerate(decisions or ()):
        label = f"decision[{index}]"
        shape = review_problems(block)
        if shape:
            # Malformed is invalid, and invalid blocks. Treating an unreadable
            # decision as absent is precisely the fail-open this round closes.
            invalid = True
            problems.extend(f"{label}: {problem}" for problem in shape)
            continue
        binding, relevant = _binding_problems(block, expectation)
        if binding:
            problems.extend(f"{label}: {problem}" for problem in binding)
            del relevant  # unbound and off-target are both simply not evidence here
            continue
        considered += 1
        verdicts.add(block["verdict"])
        blocking = [f for f in block["findings"] if finding_is_blocking(f)]
        if blocking and block["verdict"] == "PASS":
            problems.append(f"{label}: verdict PASS with {len(blocking)} blocking finding(s)")
            verdicts.add("REJECT")

    if invalid:
        return {"authorized": False, "blocked": True, "verdict": None, "reason": INVALID,
                "problems": problems or ["an invalid reviewer decision was returned"], "considered": considered}
    if verdicts == {"PASS"}:
        return {"authorized": True, "blocked": False, "verdict": "PASS", "reason": AUTHORIZED,
                "problems": problems, "considered": considered}
    if "REJECT" in verdicts:
        conflict = "PASS" in verdicts
        return {"authorized": False, "blocked": True, "verdict": "REJECT",
                "reason": CONFLICT if conflict else REJECTED,
                "problems": problems + ["conflicting decisions for the same target" if conflict
                                        else "the reviewer rejected"], "considered": considered}
    return {"authorized": False, "blocked": False, "verdict": None, "reason": ABSENT,
            "problems": problems or ["no bound reviewer decision was returned"], "considered": considered}


# -- adm-validation-result/v1 -------------------------------------------------

def validation_problems(block):
    """Every reason ``block`` is not a usable record of one test execution."""
    if not isinstance(block, dict):
        return [f"validation result is {type(block).__name__}, not an object"]
    problems = []
    if block.get("schema") != VALIDATION_SCHEMA:
        problems.append(f"schema is {block.get('schema')!r}, not {VALIDATION_SCHEMA!r}")
    if not _identifier(block.get("execution_id")):
        problems.append(f"execution_id {block.get('execution_id')!r} is not an identifier")

    argv = block.get("argv")
    if not isinstance(argv, list) or not argv or not all(_text(a) for a in argv):
        # An argument vector, never a shell string. A shell string is exactly
        # what let "echo documentation; pytest & echo ===== 12 passed ====="
        # be designated a test run: the words of a printed argument are
        # indistinguishable from the words of an executed program.
        problems.append("argv must be a non-empty list of strings (an argument vector, never a shell string)")
    if not _text(block.get("runner")):
        problems.append("runner is missing")
    if block.get("started") is not True:
        problems.append("started must be true; a process that did not start proves nothing")
    exit_code = block.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        problems.append(f"exit_code {exit_code!r} is not an integer")

    tests = block.get("tests")
    if tests is None:
        return problems  # a run may legitimately report no counts; it then proves no tests ran
    if not isinstance(tests, dict):
        problems.append("tests must be an object")
        return problems
    for field in ("passed", "failed", "skipped"):
        count = tests.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            problems.append(f"tests.{field} {count!r} is not a count")
    return problems


def validation_counts(block):
    """``{passed, failed, skipped}`` for a valid run that reported counts, else None."""
    if validation_problems(block):
        return None
    tests = block.get("tests")
    if not isinstance(tests, dict):
        return None
    return {"passed": tests["passed"], "failed": tests["failed"], "skipped": tests["skipped"]}
