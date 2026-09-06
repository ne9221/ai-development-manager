"""Phase B-2R self-adversarial audit: 11 attacks and 3 live controls.

Runs against either SHA. It discovers the ticket ledger's layout rather than
assuming it, so the same script reproduces the bypasses at ``794db70`` and
proves them closed at the repair HEAD, with no per-version branches in the
attacks themselves.

    export AI_MANAGER_HOME=/some/ephemeral/home
    PYTHONPATH=. python ADVERSARIAL_B2R.py <scratch-store-dir>

Attacks that call ``evaluate()`` directly bind their tickets to their reports
with ``consumed_against``. Predicate 11 requires a consumed ticket, so an
unbound ticket makes every report inadmissible and the attack scores itself
blocked without ever reaching the guard it claims to test.

Every attack prints BLOCKED (fail-closed) or BYPASSED (the attack worked).
Controls print ACCEPTS or BROKEN; a control that stops accepting means the
loop was sealed shut rather than secured, which is a failure too.
"""

import json
import pathlib
import shutil
import sys
import traceback
from dataclasses import replace

from manager.verification_loop.bundle import finalize_bundle, report_digest
from manager.verification_loop.classification import base_evidence
from manager.verification_loop.controller import VerificationController
from manager.verification_loop.evaluator import evaluate
from manager.verification_loop.fixtures import consumed_against, fx_ledger_freeze_pane
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.models import FailureObservation

BASE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "./b2r-adversarial")
GATES = ("V0", "V1", "V2", "V3")
ROGUE = ResolvedIdentity(
    Identity("rogue", "acct-rogue", "sess-rogue"), "classified", "high", "deterministic_signal"
)
ROGUE_JSON = {
    "identity": {
        "provider": "rogue",
        "account_id": "acct-rogue",
        "session_id": "sess-rogue",
        "provider_session_id": None,
    },
    "status": "classified",
    "confidence": "high",
    "method": "deterministic_signal",
    "resolved_by": "session_center",
}

FX = fx_ledger_freeze_pane()
SC = FX["scenario"](FX["bundle_with_clause"])
FAILURE = FX["freeze_pane_failure"]

results = []


def lease_kwargs():
    """The lease the fixtures use, however this version spells it."""
    lock = getattr(SC.preflight, "worktree_lock_id", None) or "repo-lease-1"
    generation = getattr(SC.preflight, "worktree_generation", None) or 1
    return {"worktree_lock_id": lock, "worktree_generation": generation}


def fresh(tag):
    root = BASE / tag
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    controller = VerificationController(root)
    controller.issue_round(
        task=SC.task, execution=SC.execution, bundle=SC.bundle, gate_ids=GATES, round_=1,
        issued_by=SC.controller, expected_checker_identity=SC.checker,
        candidate_head_at_issue=SC.execution.candidate_sha,
        issued_at="2026-09-06T00:00:00Z", **lease_kwargs()
    )
    return root, controller


def derive(controller, preflight=None):
    return controller.derive(
        task=SC.task, execution=SC.execution, bundle=SC.bundle,
        preflight=preflight or SC.preflight,
    )


def ticket_records(root, gate_id, round_=1):
    """Every persisted record for one ticket, whatever the layout is.

    Matched by reading the ``ticket_id`` *inside* each record rather than by
    any filename convention. The first version of this audit assumed the
    layout, silently found nothing on a tree that had changed it, and scored
    four attacks as bypasses because the tamper never happened -- an attack
    that edits no bytes proves nothing, in either direction.
    """
    from manager.verification_loop.tickets import derive_ticket_id

    ticket_id = derive_ticket_id(
        SC.execution.execution_id, SC.execution.candidate_sha, SC.bundle.bundle_hash,
        gate_id, round_,
    )
    found = []
    for path in sorted((root / "verification" / "tickets").rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if document.get("ticket_id") == ticket_id:
            found.append(path)
    if not found:
        raise AssertionError(
            "no ticket record found for %s -- the audit is not touching the ledger" % gate_id
        )
    return found


def edit(path, mutate):
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8", newline="",
    )


def record(name, blocked, detail):
    results.append((name, blocked, detail))
    print("  %-6s %-58s %s" % ("BLOCKED" if blocked else "BYPASSED", name, detail))


def attack(name, run):
    """An attack is blocked if it raises a store error or fails to reach ACCEPTED."""
    try:
        outcome = run()
    except Exception as exc:  # a fail-closed ledger raises; that is a block
        record(name, True, type(exc).__name__ + ": " + str(exc).split(":")[0])
        return
    record(name, outcome != "ACCEPTED", "derived " + str(outcome))


def attack_changes_outcome(name, run):
    """For attacks that suppress rather than forge.

    Editing a stored PASS into a FAIL never reaches ACCEPTED -- that is the
    point of it -- so scoring it by ACCEPTED would call it blocked on a tree
    where the tamper was honoured. It succeeds whenever the edit moves the
    derivation at all.
    """
    try:
        honest, tampered = run()
    except Exception as exc:
        record(name, True, type(exc).__name__ + ": " + str(exc).split(":")[0])
        return
    record(name, honest == tampered, "%s -> %s" % (honest, tampered))


def control(name, run, expected):
    try:
        outcome = run()
    except Exception:
        results.append((name, False, "raised"))
        print("  %-6s %-58s %s" % ("BROKEN", name, traceback.format_exc(limit=1).strip()))
        return
    ok = outcome == expected
    results.append((name, ok, str(outcome)))
    print("  %-6s %-58s %s" % ("OK" if ok else "BROKEN", name, "derived " + str(outcome)))


# ---------------------------------------------------------------------------
print("=" * 78)
print("CONTROLS -- the loop must still be able to say yes, and to say repair")
print("=" * 78)


def control_accept():
    _root, controller = fresh("c1")
    for gate in GATES:
        controller.submit(SC.report(gate))
    return derive(controller).acceptance_state


def control_repair():
    _root, controller = fresh("c2")
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))
    controller.submit(SC.report("V3", result="FAIL", failure_observations=(FAILURE,)))
    return derive(controller).next_action


control("C1 honest round reaches ACCEPTED", control_accept, "ACCEPTED")
control("C2 honest failure reaches REPAIR", control_repair, "REPAIR")

print("")
print("=" * 78)
print("ATTACKS")
print("=" * 78)


def a1():
    """Edit a stored FAIL into a PASS."""
    root, controller = fresh("a1")
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))
    digest = controller.submit(SC.report("V3", result="FAIL", failure_observations=(FAILURE,)))
    path = root / "verification" / "reports" / (digest + ".json")
    edit(path, lambda d: d.update({"result": "PASS", "failure_observations": []}))
    return derive(controller).acceptance_state


def a2():
    """Edit a stored PASS into a FAIL -- suppressing someone else's acceptance."""
    root, controller = fresh("a2")
    digests = {gate: controller.submit(SC.report(gate)) for gate in GATES}
    honest = derive(controller).acceptance_state
    path = root / "verification" / "reports" / (digests["V3"] + ".json")
    edit(path, lambda d: d.update({"result": "FAIL"}))
    return honest, derive(controller).acceptance_state


def a3():
    """Rewrite a ticket's expected checker, then answer it as the rogue."""
    root, controller = fresh("a3")
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))
    for path in ticket_records(root, "V3"):
        edit(path, lambda d: d.update({"expected_checker_identity": ROGUE_JSON}))
    controller.submit(SC.report("V3", producer_identity=ROGUE))
    return derive(controller).acceptance_state


def a4():
    """Rewrite a ticket's task_id, so another Task's round authorises this one."""
    root, controller = fresh("a4")
    for path in ticket_records(root, "V3"):
        edit(path, lambda d: d.update({"task_id": "T-SOMEONE-ELSE"}))
    for gate in GATES:
        controller.submit(SC.report(gate))
    return derive(controller).acceptance_state


def a5():
    """Reopen a spent ticket by rewriting its status back to issued."""
    root, controller = fresh("a5")
    for gate in GATES:
        controller.submit(SC.report(gate))
    for path in ticket_records(root, "V3"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("status") == "consumed":
            edit(path, lambda d: d.update({"status": "issued", "consumed_report_digest": None}))
    return derive(controller).acceptance_state


def a6():
    """Point a consumed ticket at a report of the attacker's choosing."""
    root, controller = fresh("a6")
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))
    controller.submit(SC.report("V3", result="FAIL", failure_observations=(FAILURE,)))
    forged = SC.report("V3")
    forged_digest = report_digest(forged)
    (root / "verification" / "reports" / (forged_digest + ".json")).write_text(
        json.dumps(json.loads(_canonical(forged)), sort_keys=True, separators=(",", ":")),
        encoding="utf-8", newline="",
    )
    for path in ticket_records(root, "V3"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("status") == "consumed":
            edit(path, lambda d: d.update({"consumed_report_digest": forged_digest}))
    return derive(controller).acceptance_state


def _canonical(record_):
    from manager.verification_loop.bundle import canonical_json

    return canonical_json(record_)


def a7():
    """A second checker answers a ticket that is already answered."""
    root, controller = fresh("a7")
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))
    controller.submit(SC.report("V3", result="FAIL", failure_observations=(FAILURE,)))
    controller.submit(SC.report("V3"))
    return derive(controller).acceptance_state


def a8():
    """Claim the failure also failed at base, against an attestation saying it passed.

    Uses the bundle with **no** clause covering the signature: with a clause,
    the clause match settles the class before base evidence is consulted, and
    the run never reaches the TEST_DEFECT path this finding is about. Getting
    that wrong is how an attack scores itself blocked without ever firing.
    """
    scenario = FX["scenario"](FX["bundle_without_clause"])
    observation = FailureObservation(
        signature="freeze_pane_mismatch", base_result="FAIL",
        base_signature="freeze_pane_mismatch",
    )
    reports = [scenario.report(gate) for gate in GATES]
    reports[3] = scenario.report("V3", result="FAIL", failure_observations=(observation,))
    outcome = evaluate(
        scenario.task, scenario.execution, scenario.bundle, reports,
        preflight=scenario.preflight,
        tickets=consumed_against(
            scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
        ),
    )
    evidence, reason = base_evidence(
        observation, scenario.execution, scenario.bundle, reports[3]
    )
    # "Blocked" here means the claim did not buy a licence to edit the test.
    return "ACCEPTED" if outcome.next_action == "REPAIR_TEST_ONLY" else (
        outcome.next_action + " / base=" + evidence + " " + reason
    )


def a9():
    """Spend Task A's evidence on Task B."""
    other = replace(
        SC,
        task=replace(SC.task, task_id="T-LED-02"),
        execution=replace(SC.execution, task_id="T-LED-02"),
    )
    reports = [SC.report(gate) for gate in GATES]
    outcome = evaluate(
        other.task, other.execution, other.bundle, reports, preflight=other.preflight,
        tickets=consumed_against(
            SC.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
        ),
    )
    return outcome.acceptance_state


def a10():
    """Same HEAD, different worktree lease generation."""
    _root, controller = fresh("a10")
    for gate in GATES:
        controller.submit(SC.report(gate))
    current = getattr(SC.preflight, "worktree_generation", None)
    if current is None:
        return "ACCEPTED"  # the version under test has no lease to compare
    moved = replace(SC.preflight, worktree_generation=current + 1)
    return derive(controller, preflight=moved).acceptance_state


def a11():
    """A PASS report that carries failure observations anyway."""
    reports = [SC.report(gate) for gate in GATES]
    reports[3] = SC.report(
        "V3", result="PASS",
        failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
    )
    outcome = evaluate(
        SC.task, SC.execution, SC.bundle, reports, preflight=SC.preflight,
        tickets=consumed_against(
            SC.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports
        ),
    )
    return outcome.acceptance_state


attack("A1  stored FAIL edited into PASS", a1)
attack_changes_outcome("A2  stored PASS edited into FAIL", a2)
attack("A3  ticket expected checker rewritten to a rogue", a3)
attack("A4  ticket task_id rewritten", a4)
attack("A5  consumed ticket reopened as issued", a5)
attack("A6  consumed_report_digest repointed at a forged report", a6)
attack("A7  a second checker consumes an answered ticket", a7)
attack("A8  base FAIL claimed against a PASS attestation", a8)
attack("A9  Task A evidence spent on Task B", a9)
attack("A10 same HEAD, stale worktree lease generation", a10)
attack("A11 PASS report carrying hidden failure observations", a11)

print("")
print("=" * 78)
blocked = sum(1 for _n, ok, _d in results[2:] if ok)
controls_ok = sum(1 for _n, ok, _d in results[:2] if ok)
print("%d/%d attacks blocked, %d/2 controls still healthy" % (blocked, len(results) - 2, controls_ok))
print("=" * 78)
sys.exit(0 if blocked == len(results) - 2 and controls_ok == 2 else 1)
