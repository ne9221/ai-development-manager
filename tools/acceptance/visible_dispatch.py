#!/usr/bin/env python3
"""Black-box regression gate for the visible direct-dispatch contract.

This deliberately composes the production-facing tests instead of duplicating
their fakes or production code.  No provider launcher is invoked.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


GATES = (
    ("Gate 1 — Request visibility", "cloud.test_dispatch_ingress.DispatchIngressTests.test_valid_request_creates_queued_task_and_command"),
    ("Gate 2 — Task/Command identity", "cloud.test_dispatch_ingress.DispatchIngressTests.test_command_with_mismatched_request_id_fails_closed"),
    ("Gate 3 — State truth", "manager.test_command_watcher.CommandWatcherTests.test_command_becomes_running_only_after_running_gate_callback"),
    ("Gate 4 — Running truth", "manager.test_execution_lifecycle.ExecutionLifecycleTests.test_advisory_check_result_cannot_authorize_running"),
    ("Gate 5 — Dashboard truth", "manager.test_dashboard_core.DirectDispatchDashboardVisibilityTests.test_running_execution_moves_ingress_task_to_in_progress_with_active_session"),
    ("Gate 6 — Quota truth", "manager.test_dashboard_core.FleetQuotaTruthClassificationTests.test_stale_telemetry_never_emits_quota_exhausted"),
    ("Gate 8 — Restart regression", "manager.test_command_watcher.CommandWatcherTests.test_restart_never_relaunches_claimed_or_running_command"),
    ("Gate 9 — Duplicate request", "cloud.test_dispatch_ingress.DispatchIngressTests.test_simultaneous_duplicate_requests_create_exactly_one_task_and_command"),
    ("Gate 10 — Cross-account", "cloud.test_dispatch_ingress.DirectDispatchClaudeAccountIdentityTests.test_command_account_id_lets_command_watcher_bypass_auto_quota_gate"),
)


def gate_7_production_identity() -> None:
    """Fail until the installed Watcher proves its tested/activated SHA."""
    runner = ROOT / "manager" / "run_command_watcher.ps1"
    text = runner.read_text(encoding="utf-8")
    required = ("ADM_WATCHER_GIT_SHA", "ADM_TESTED_GIT_SHA", "ADM_ACTIVATED_GIT_SHA")
    missing = [name for name in required if name not in text]
    if missing:
        raise AssertionError(
            "Watcher production identity is not enforceable; missing " + ", ".join(missing)
        )


def run(stream=None) -> bool:
    loader = unittest.defaultTestLoader
    result = unittest.TestResult()
    outcomes = []
    for label, name in GATES:
        suite = loader.loadTestsFromName(name)
        suite.run(result)
        failed = bool(result.failures or result.errors)
        outcomes.append((label, not failed, result.failures[-1:] + result.errors[-1:] if failed else []))
        result.failures.clear(); result.errors.clear()
    try:
        gate_7_production_identity()
    except Exception as exc:  # This is intentionally a hard acceptance gate.
        outcomes.insert(6, ("Gate 7 — Production identity", False, [(None, str(exc))]))
    else:
        outcomes.insert(6, ("Gate 7 — Production identity", True, []))

    output = stream or sys.stdout
    print("VISIBLE DISPATCH ACCEPTANCE:", file=output)
    for label, passed, details in outcomes:
        print(f"{'PASS' if passed else 'FAIL'}  {label}", file=output)
        if details:
            print(f"       {details[0][1].splitlines()[-1]}", file=output)
    passed = all(item[1] for item in outcomes)
    print("PASS" if passed else "FAIL", file=output)
    return passed


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
