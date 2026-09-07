"""Phase B-2R self-adversarial audit: 11 attacks and 3 live controls.

Runs unmodified at the Phase B-2 base ``794db70`` and at every later head, so
the same script reproduces the bypasses at base and proves them closed at the
repair HEAD. It discovers the ticket ledger's layout rather than assuming it,
and the attacks themselves carry no per-version branches.

    export AI_MANAGER_HOME=/some/ephemeral/home
    PYTHONPATH=. python docs/verification-loop/repro/B2R-ADVERSARIAL-AUDIT.py <scratch-store-dir>

Version adapter (Phase B3-2, residual NB-B). Attacks that call ``evaluate()``
directly must hand it tickets in whatever state *this* version's admission
treats as "answered". At the repair HEAD that is a consumed ticket naming the
report's digest (predicate 11), built by ``fixtures.consumed_against``; at
base that helper does not exist and admission admits a report against its
*issued* ticket, so the base-equivalent binding is the issued ticket itself.
``bound()`` detects which contract is present from the ticket dataclass, never
copies the production implementation, and refuses to guess at an unknown
combination. Control C3 runs the honest round through exactly that binding, so
a wrong adapter shows up as BROKEN at the version where it is wrong instead of
scoring the attacks that use it as blocked.

Every attack prints BLOCKED (fail-closed), BYPASSED (the attack worked) or
N/A (the field the attack forges does not exist at this version, so the
tamper would edit nothing the loop reads -- an attack that edits no bytes
proves nothing in either direction). Controls print OK or BROKEN; a control
that stops accepting means the loop was sealed shut rather than secured,
which is a failure too. Exit status is 0 only when no attack bypasses and
every control is healthy; at base the documented exit status is 1.
"""

import dataclasses
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
from manager.verification_loop.fixtures import fx_ledger_freeze_pane
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.models import FailureObservation
from manager.verification_loop.tickets import VerificationTicket

try:  # head-only helper; its absence is the base contract, not an error
    from manager.verification_loop.fixtures import consumed_against as _consumed_against
except ImportError:
    _consumed_against = None

TICKET_FIELDS = frozenset(field.name for field in dataclasses.fields(VerificationTicket))
HAS_CONSUMPTION_DIGEST = "consumed_report_digest" in TICKET_FIELDS
if _consumed_against is not None and HAS_CONSUMPTION_DIGEST:
    VERSION_PROFILE = "consumption-bound tickets (predicate 11; fixtures.consumed_against present)"
elif _consumed_against is None and not HAS_CONSUMPTION_DIGEST:
    VERSION_PROFILE = "issued-ticket admission (Phase B-2 base; no consumption digest on tickets)"
else:
    sys.exit(
        "UNKNOWN VERSION: consumed_against present=%s, consumed_report_digest on tickets=%s; "
        "the adapter cannot claim an equivalent binding here -- refusing to score attacks"
        % (_consumed_against is not None, HAS_CONSUMPTION_DIGEST)
    )


def bound(tickets, reports):
    """Tickets in the state this version's admission treats as answered.

    HEAD: ``consumed_against`` marks each issued ticket consumed by the report
    that claims it (status consumed + the report's digest). BASE: admission
    requires ``status == "issued"`` and has no digest to compare, so the issued
    ticket *is* the answered state and is returned unchanged. Control C3
    measures that this binding admits the honest round at whichever version is
    running; the equivalence is proven there, not assumed here.
    """
    if _consumed_against is not None:
        return _consumed_against(tickets, reports)
    return tuple(tickets)


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


class NotApplicable(Exception):
    """The attack's precondition (a field, a helper) does not exist at this version."""


def record(name, verdict, detail):
    results.append((name, verdict, detail))
    print("  %-8s %-58s %s" % (verdict, name, detail))


def attack(name, run):
    """An attack is blocked if it raises a store error or fails to reach ACCEPTED."""
    try:
        outcome = run()
    except NotApplicable as why:
        record(name, "N/A", str(why))
        return
    except Exception as exc:  # a fail-closed ledger raises; that is a block
        record(name, "BLOCKED", type(exc).__name__ + ": " + str(exc).split(":")[0])
        return
    record(name, "BLOCKED" if outcome != "ACCEPTED" else "BYPASSED", "derived " + str(outcome))


def attack_changes_outcome(name, run):
    """For attacks that suppress rather than forge.

    Editing a stored PASS into a FAIL never reaches ACCEPTED -- that is the
    point of it -- so scoring it by ACCEPTED would call it blocked on a tree
    where the tamper was honoured. It succeeds whenever the edit moves the
    derivation at all.
    """
    try:
        honest, tampered = run()
    except NotApplicable as why:
        record(name, "N/A", str(why))
        return
    except Exception as exc:
        record(name, "BLOCKED", type(exc).__name__ + ": " + str(exc).split(":")[0])
        return
    record(name, "BLOCKED" if honest == tampered else "BYPASSED", "%s -> %s" % (honest, tampered))


def control(name, run, expected):
    try:
        outcome = run()
    except Exception:
        results.append((name, "BROKEN", "raised"))
        print("  %-8s %-58s %s" % ("BROKEN", name, traceback.format_exc(limit=1).strip()))
        return
    ok = outcome == expected
    results.append((name, "OK" if ok else "BROKEN", str(outcome)))
    print("  %-8s %-58s %s" % ("OK" if ok else "BROKEN", name, "derived " + str(outcome)))


# ---------------------------------------------------------------------------
print("=" * 78)
print("VERSION PROFILE: " + VERSION_PROFILE)
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


def control_bound_evaluate():
    """The adapter's binding admits the honest round through the pure evaluator.

    This is the equivalence proof for ``bound()``: A8, A9 and A11 hand
    ``evaluate()`` tickets bound this way, so if the binding were wrong at this
    version those attacks would score BLOCKED on TICKET_NOT_CONSUMED (or an
    equivalent refusal) without reaching their guard. Here the same binding
    must instead reach ACCEPTED on honest evidence.
    """
    reports = [SC.report(gate) for gate in GATES]
    outcome = evaluate(
        SC.task, SC.execution, SC.bundle, reports, preflight=SC.preflight,
        tickets=bound(SC.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports),
    )
    return outcome.acceptance_state


control("C1 honest round reaches ACCEPTED", control_accept, "ACCEPTED")
control("C2 honest failure reaches REPAIR", control_repair, "REPAIR")
control("C3 adapter-bound tickets admit the honest round", control_bound_evaluate, "ACCEPTED")
CONTROLS = 3

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
            def reopen(d):
                d["status"] = "issued"
                if "consumed_report_digest" in d:  # absent at base; never invent it
                    d["consumed_report_digest"] = None
            edit(path, reopen)
    return derive(controller).acceptance_state


def a6():
    """Point a consumed ticket at a report of the attacker's choosing."""
    if not HAS_CONSUMPTION_DIGEST:
        raise NotApplicable(
            "tickets carry no consumed_report_digest at this version; the tamper would edit nothing the loop reads"
        )
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
        tickets=bound(scenario.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports),
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
        tickets=bound(SC.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports),
    )
    return outcome.acceptance_state


def a10():
    """Same HEAD, different worktree lease generation."""
    _root, controller = fresh("a10")
    for gate in GATES:
        controller.submit(SC.report(gate))
    current = getattr(SC.preflight, "worktree_generation", None)
    if current is None:
        # This version's PreflightFacts carries no lease at all, so a moved
        # lease is indistinguishable from the original: derive with the facts
        # as they are and let the loop show whether it can notice. It cannot.
        return derive(controller).acceptance_state
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
        tickets=bound(SC.tickets_for(*[(r.gate_id, r.round) for r in reports]), reports),
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
attacks = results[CONTROLS:]
blocked = sum(1 for _n, v, _d in attacks if v == "BLOCKED")
bypassed = sum(1 for _n, v, _d in attacks if v == "BYPASSED")
not_applicable = sum(1 for _n, v, _d in attacks if v == "N/A")
controls_ok = sum(1 for _n, v, _d in results[:CONTROLS] if v == "OK")
print("%d/%d attacks blocked, %d bypassed, %d not applicable at this version; %d/%d controls still healthy"
      % (blocked, len(attacks), bypassed, not_applicable, controls_ok, CONTROLS))
print("=" * 78)
sys.exit(0 if bypassed == 0 and controls_ok == CONTROLS else 1)
