"""Reproducer for Phase B-2 final review residual NB-A (forged consumption).

``_fold_ticket`` used to return a ticket's consumption record *wholesale* as the
ticket's current state. ``ticket_id`` hashes only (execution, candidate,
bundle, gate, round), so every other field the issue record froze -- above all
``expected_checker_identity`` -- could be rewritten by appending a new,
correctly named consumption record beside the untouched issue record. No
existing file is edited, so the digest-on-read guard cannot fire by
construction, and the rogue checker named in the forged record is then the
checker the ticket expects.

    PYTHONPATH=. python docs/verification-loop/repro/B31-NBA-FORGED-CONSUMPTION.py <scratch-dir>

Runnable at both b38ceb2 (base) and the B3-1 head: it uses only API present at
both commits, so the two runs are one experiment measured twice. At base the
headline attack A1 derives ACCEPTED and the script exits 1. At head every
forged consumption is refused with TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE and
the script exits 0. The controls must hold at both.
"""

import pathlib
import shutil
import sys
from dataclasses import replace

from manager.verification_loop import stores
from manager.verification_loop.bundle import canonical_json, record_digest, report_digest
from manager.verification_loop.controller import VerificationController
from manager.verification_loop.fixtures import (
    WORKTREE_GENERATION,
    WORKTREE_LOCK_ID,
    fx_ledger_freeze_pane,
)
from manager.verification_loop.identity import Identity, ResolvedIdentity
from manager.verification_loop.tickets import derive_ticket_id

GATES = ("V0", "V1", "V2", "V3")
BASE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "./b31-repro").resolve()
FX = fx_ledger_freeze_pane()
SC = FX["scenario"](FX["bundle_with_clause"])
ROGUE = ResolvedIdentity(
    Identity("rogue", "acct-rogue", "sess-rogue"), "classified", "high", "deterministic_signal"
)
GUARD = "TICKET_CONSUMPTION_DIVERGES_FROM_ISSUE"
CONSUMED_AT = "2026-09-07T00:00:01Z"


def fresh(tag):
    root = BASE / tag
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    controller = VerificationController(root)
    controller.issue_round(
        task=SC.task, execution=SC.execution, bundle=SC.bundle, gate_ids=GATES,
        round_=1, issued_by=SC.controller, expected_checker_identity=SC.checker,
        candidate_head_at_issue=SC.execution.candidate_sha,
        worktree_lock_id=WORKTREE_LOCK_ID, worktree_generation=WORKTREE_GENERATION,
        issued_at="2026-09-06T00:00:00Z",
    )
    return root, controller


def outcome(controller):
    """One string per derivation, so base and head are compared on one axis."""
    try:
        result = controller.derive(
            task=SC.task, execution=SC.execution, bundle=SC.bundle, preflight=SC.preflight
        )
    except stores.VerificationStoreError as exc:
        return "REFUSED " + str(exc).split(":", 1)[0]
    reasons = sorted({reason for _key, reason in result.invalidated_report_reasons})
    return "%s / %s %s" % (result.acceptance_state, result.next_action, reasons or "")


def issued_ticket(controller, gate):
    """The issue record as the ledger holds it, before any consumption."""
    ticket_id = derive_ticket_id(
        SC.execution.execution_id, SC.execution.candidate_sha, SC.bundle.bundle_hash, gate, 1
    )
    for record in controller.tickets._records(ticket_id):
        if record.ticket_seq == 0:
            return record
    raise AssertionError("no issue record for " + gate)


def append_record(root, ticket):
    """What an attacker with store-write authority does: a new, correctly
    named file. Nothing existing is touched, so digest-on-read cannot fire."""
    path = root / "verification" / "tickets" / (record_digest(ticket) + ".json")
    assert not path.exists()
    path.write_text(canonical_json(ticket), encoding="utf-8", newline="")


def forged_consumption(issued, digest, **rewrites):
    return replace(
        issued, status="consumed", consumed_report_digest=digest,
        consumed_at=CONSUMED_AT, ticket_seq=1, **rewrites,
    )


def honest_three(controller):
    for gate in ("V0", "V1", "V2"):
        controller.submit(SC.report(gate))


results = {}


def record(name, value, fixed_when):
    results[name] = (value, fixed_when(value))
    print("  %-26s %-58s %s" % (name, value, "closed" if results[name][1] else "OPEN"))


def refused_by_guard(value):
    return value == "REFUSED " + GUARD


print("=" * 78)
print("A1  forged consumption names the rogue as expected checker; rogue files a PASS")
print("=" * 78)
root, controller = fresh("a1")
honest_three(controller)
rogue_report = SC.report("V3", producer_identity=ROGUE)
rogue_digest = controller.reports.put(rogue_report)
append_record(root, forged_consumption(
    issued_ticket(controller, "V3"), rogue_digest, expected_checker_identity=ROGUE
))
record("A1", outcome(controller), refused_by_guard)

print("")
print("=" * 78)
print("A2  every other issue-frozen field rewritten by a consumption record")
print("=" * 78)
IMMUTABLE_REWRITES = {
    "forbidden_identity": None,
    "issued_by": ROGUE,
    "issued_at": "1999-01-01T00:00:00Z",
    "candidate_head_at_issue": "c" * 40,
    "worktree_lock_id": "repo-lease-stolen",
    "worktree_generation": 99,
    "task_id": "T-SOMEONE-ELSE",
    "base_sha": "d" * 40,
    "candidate_sha": "e" * 40,
    "execution_id": "E-SOMEONE-ELSE",
    "bundle_hash": "0" * 64,
    "gate_id": "V9",
    "round": 7,
}
for index, (field_name, value) in enumerate(IMMUTABLE_REWRITES.items()):
    # Short tags on purpose: the ledger files records flat precisely because
    # this repository is tested near the Windows path-length cliff.
    root, controller = fresh("a2-%02d" % index)
    honest_three(controller)
    honest = SC.report("V3")
    digest = controller.reports.put(honest)
    forged = forged_consumption(issued_ticket(controller, "V3"), digest, **{field_name: value})
    append_record(root, forged)
    record("A2." + field_name, outcome(controller), refused_by_guard)

print("")
print("=" * 78)
print("A3  V2's honest consumption transplanted onto V3's ticket id")
print("=" * 78)
root, controller = fresh("a3")
honest_three(controller)
v3_digest = controller.reports.put(SC.report("V3"))
v2_consumed = [
    r for r in controller.tickets._records(SC.report("V2").ticket_id) if r.ticket_seq == 1
][0]
append_record(root, replace(
    v2_consumed, ticket_id=SC.report("V3").ticket_id, consumed_report_digest=v3_digest
))
record("A3", outcome(controller), refused_by_guard)

print("")
print("=" * 78)
print("A4  forged consumption plus forged PASS, forbidden identity erased as well")
print("=" * 78)
root, controller = fresh("a4")
honest_three(controller)
forged_pass = SC.report("V3", producer_identity=ROGUE)
forged_digest = controller.reports.put(forged_pass)
append_record(root, forged_consumption(
    issued_ticket(controller, "V3"), forged_digest,
    expected_checker_identity=ROGUE, forbidden_identity=None,
))
record("A4", outcome(controller), lambda v: not v.startswith("ACCEPTED"))

print("")
print("=" * 78)
print("CONTROLS  (must hold identically at base and head)")
print("=" * 78)
root, controller = fresh("c1")
for gate in GATES:
    controller.submit(SC.report(gate))
record("C1.honest", outcome(controller), lambda v: v.startswith("ACCEPTED / ACCEPTED"))

root, controller = fresh("c2")
honest_three(controller)
controller.submit(SC.report("V3", result="FAIL", failure_observations=(FX["freeze_pane_failure"],)))
record("C2.honest_fail", outcome(controller), lambda v: v.startswith("REJECTED_NEEDS_REPAIR / REPAIR"))

root, controller = fresh("c3")
honest_three(controller)
v3 = SC.report("V3")
controller.reports.put(v3)
record("C3a.crash_window", outcome(controller),
       lambda v: "TICKET_NOT_CONSUMED" in v and not v.startswith("ACCEPTED"))
controller.tickets.consume(v3.ticket_id, report_digest(v3), CONSUMED_AT)
record("C3b.recovery", outcome(controller), lambda v: v.startswith("ACCEPTED / ACCEPTED"))
before = sorted(p.name for p in (root / "verification" / "tickets").glob("*.json"))
controller.tickets.consume(v3.ticket_id, report_digest(v3), "2026-09-07T09:09:09Z")
after = sorted(p.name for p in (root / "verification" / "tickets").glob("*.json"))
record("C3c.replay", "idempotent=%s %s" % (before == after, outcome(controller)),
       lambda v: v.startswith("idempotent=True ACCEPTED"))

orders = []
for tag, order in (("c4-pf", ("PASS", "FAIL")), ("c4-fp", ("FAIL", "PASS"))):
    root, controller = fresh(tag)
    honest_three(controller)
    for res in order:
        if res == "PASS":
            controller.submit(SC.report("V3"))
        else:
            controller.submit(SC.report("V3", result="FAIL",
                              failure_observations=(FX["freeze_pane_failure"],)))
    ticket = {t.gate_id: t for t in controller.stored_tickets()}["V3"]
    orders.append((ticket.status, outcome(controller)))
record("C4.conflict", "%s | same_both_orders=%s" % (orders[0][0], orders[0] == orders[1]),
       lambda v: v.startswith("invalidated | same_both_orders=True"))

print("")
attacks = {k: v for k, v in results.items() if k.startswith("A")}
controls = {k: v for k, v in results.items() if k.startswith("C")}
open_attacks = sorted(k for k, (_v, ok) in attacks.items() if not ok)
# An attack the target guard did not refuse is "open" for this script's
# purpose, but only some of those are false-accepts; the rest were stopped by
# a sibling guard and are reported separately so the base measurement is
# honest about which guard did the work.
false_accepts = sorted(k for k, (v, _ok) in attacks.items() if v.startswith("ACCEPTED"))
sibling_blocked = sorted(k for k in open_attacks if k not in false_accepts)
broken_controls = sorted(k for k, (_v, ok) in controls.items() if not ok)
print("attacks_refused_by_target_guard=%d/%d controls_ok=%d/%d" % (
    len(attacks) - len(open_attacks), len(attacks),
    len(controls) - len(broken_controls), len(controls)))
print("false_accepts=%s" % false_accepts)
print("blocked_by_sibling_guard_only=%s" % sibling_blocked)
print("broken_controls=%s" % broken_controls)
sys.exit(0 if not open_attacks and not broken_controls else 1)
