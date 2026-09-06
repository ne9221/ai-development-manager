"""Reproducer for the Codex Predicate 11 crash-consistency finding.

The controller persists a report and appends the ticket's consumption record as
two separate filesystem steps. A crash between them leaves the ledger in a state
nothing rejects: the report is durable, the ticket is still ``issued``, and
admission only compares the consumed digest when the ticket says ``consumed``.

    PYTHONPATH=. python REPRO_P11_CRASH.py <scratch-dir>

Expected on a fixed tree: REJECTED with TICKET_NOT_CONSUMED, then ACCEPTED once
a legitimate consumption record is appended.
"""

import pathlib
import shutil
import sys

from manager.verification_loop.bundle import canonical_json, report_digest
from manager.verification_loop.controller import VerificationController
from manager.verification_loop.fixtures import (
    WORKTREE_GENERATION,
    WORKTREE_LOCK_ID,
    fx_ledger_freeze_pane,
)

GATES = ("V0", "V1", "V2", "V3")
BASE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "./p11-repro")
FX = fx_ledger_freeze_pane()
SC = FX["scenario"](FX["bundle_with_clause"])


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


def derive(controller):
    return controller.derive(
        task=SC.task, execution=SC.execution, bundle=SC.bundle, preflight=SC.preflight
    )


def persist_report_only(root, report):
    """The first of the controller's two writes, with the second never reached."""
    digest = report_digest(report)
    path = root / "verification" / "reports" / (digest + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report), encoding="utf-8", newline="")
    return digest


print("=" * 78)
print("CRASH WINDOW -- report persisted, consumption record never appended")
print("=" * 78)
root, controller = fresh("crash")
for gate in GATES:
    persist_report_only(root, SC.report(gate))

reports = controller.stored_reports(SC.execution.execution_id)
tickets = controller.stored_tickets()
print("  persisted_reports = %d" % len(reports))
print("  ticket statuses   = %s" % sorted(t.status for t in tickets))
print("  consumed digests  = %s" % sorted(str(t.consumed_report_digest) for t in tickets))

result = derive(controller)
print("  derivation        -> %s / %s" % (result.acceptance_state, result.next_action))
print("  invalidated       = %s" % (result.invalidated_report_reasons or "()",))

reasons = {reason for _key, reason in result.invalidated_report_reasons}
crash_ok = result.acceptance_state != "ACCEPTED" and "TICKET_NOT_CONSUMED" in reasons
print("  STATUS: %s" % ("FIXED" if crash_ok else "REPRODUCED -- unconsumed ticket ACCEPTED"))

print("")
print("=" * 78)
print("RECOVERY -- the missing consumption record is appended afterwards")
print("=" * 78)
for gate in GATES:
    report = SC.report(gate)
    controller.tickets.consume(
        report.ticket_id, report_digest(report), "2026-09-06T00:00:05Z"
    )
recovered = derive(controller)
print("  ticket statuses   = %s" % sorted(t.status for t in controller.stored_tickets()))
print("  derivation        -> %s / %s" % (recovered.acceptance_state, recovered.next_action))
recovery_ok = recovered.acceptance_state == "ACCEPTED"
print("  STATUS: %s" % ("RECOVERS" if recovery_ok else "STUCK -- recovery does not accept"))

print("")
print("=" * 78)
print("CONTROL -- an ordinary round through submit() still accepts")
print("=" * 78)
_root2, control_controller = fresh("control")
for gate in GATES:
    control_controller.submit(SC.report(gate))
control = derive(control_controller)
print("  derivation        -> %s" % control.acceptance_state)
control_ok = control.acceptance_state == "ACCEPTED"
print("  STATUS: %s" % ("OK" if control_ok else "BROKEN"))

print("")
print("crash_closed=%s recovery=%s control=%s" % (crash_ok, recovery_ok, control_ok))
sys.exit(0 if (crash_ok and recovery_ok and control_ok) else 1)
