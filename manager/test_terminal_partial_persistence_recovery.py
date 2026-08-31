"""R17 Terminal Partial-Persistence Durable Recovery.

Live-confirmed defect: dispatch-cgate5-r17-20260831T111932Z -- Execution
completed, Command completed, Task blocked, cleanup_evidence={persistence:
'partial', persisted:['execution'], task_claim_release:'retained',
errors:['Drive verification failed: ...']}. State was unchanged across 40+
minutes and multiple natural Command Watcher ticks, with no provider
process running and no self-heal.

Root cause: manager.command_watcher.process_command() short-circuited any
Command already `completed`/`failed` in Drive unconditionally, before ever
consulting its Execution's own cleanup_evidence:

    if command["status"] in ("completed", "failed"):
        return {"status": command["status"], "skipped": True}

retry_incomplete_terminal_persistence() (in manager.execution_lifecycle)
and the claim-convergence logic in manager.command_watcher._reconcile_active
already existed and are fully correct -- but they were only ever reachable
while the *Command* itself was still claimed/running/attention. The moment
terminalize_execution() persists 'execution' successfully but raises before
'handoff'/'task' finish their own write-then-readback verification, the
Command it belongs to has typically ALREADY been written as terminal
(completed/failed) by its own caller, so every later natural watcher tick
hit the short-circuit above and never called _reconcile_active /
retry_incomplete_terminal_persistence / recover_task_claim again. This is a
pure code-path-reachability bug, not a persistence-retry-logic bug.

Fix: process_command()'s completed/failed short-circuit now reads the
Execution's durable cleanup_evidence first (one extra Drive read); only
when that evidence shows outstanding work (persistence incomplete, or the
task claim not yet released/not_required) does it fall through to the
exact same _reconcile_terminal_execution() body _reconcile_active() already
used -- retry persistence, converge the task claim, and re-derive the
Command's own terminal truth. An already fully-converged completed/failed
Command remains exactly as cheap as before (one extra read, no writes, no
claim-registry round trip).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from manager.command_watcher import process_command
from manager.execution_lifecycle import enter_running_gate, retry_incomplete_terminal_persistence, terminalize_execution
from manager.execution_recovery import recover_task_claim
from manager.executions import reserve_execution
from manager.task_claims import TaskClaimConflict, claim_task_execution, check_task_execution_claim
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry


PROJECT_ID = "p1"
TASK_ID = "t1"
EXECUTION_ID = "command-cmd-1"
SESSION_ID = "codex:01a05796-1b5a-7fe2-bf89-0a0bacab751c"

REPO_ROOT = Path(__file__).resolve().parents[1]
# The canonical production base this R17 fix branches from (see the branch's
# own commit chain) -- used to load the REAL, unmodified base-SHA
# manager/command_watcher.py for an authentic FAIL-before, never a
# hand-written stand-in for what that code used to do.
BASE_SHA = "4d53f8c019c3b2e13846fbecb8fc71cb53bf9c66"
GIT = shutil.which("git")


_BASE_WORKTREE = {}

_FAIL_BEFORE_DRIVER = '''
import json
from unittest.mock import patch

from manager.command_watcher import process_command
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.tasks import create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry

PROJECT_ID = "p1"
TASK_ID = "t1"
EXECUTION_ID = "command-cmd-1"
SESSION_ID = "codex:01a05796-1b5a-7fe2-bf89-0a0bacab751c"

store = Store()
create_project(store, project())
create_task(store, task(read_only=True), assign=False)
registry = MemoryClaimRegistry()

reserve_execution(store, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex", {"decision": "fresh"})
with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
    enter_running_gate(store, object(), None, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                       "read_only", task_claim_registry=registry)
exec_doc = store.get("executions", PROJECT_ID, EXECUTION_ID)
exec_doc["session_id"] = SESSION_ID
store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
with patch("manager.executions.read_drive_status", return_value=quota_document()):
    terminalize_execution(store, object(), None, registry, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                          "completed", 1, True, summary="Execution terminal completed")

# Corrupt to the exact live R17 partial-persistence shape: only 'execution'
# persisted, handoff missing, task rolled back to still-running, claim
# retained/absent from GCS -- mirrors
# TerminalPartialPersistenceRecoveryTests._corrupt_to_partial(["execution"]).
exec_doc = store.get("executions", PROJECT_ID, EXECUTION_ID)
exec_doc["cleanup_evidence"] = {
    "provider_outcome": "completed", "persistence": "partial", "persisted": ["execution"],
    "writer_release": "not_required", "task_claim_release": "retained",
    "errors": ["Drive verification failed: dispatch-cgate5-r17-20260831T111932Z-completed-command-"
              "dispatch-cgate5-r17-20260831T111932Z-0.json"],
}
validate("execution", exec_doc)
store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
for key in list(store.records):
    if key[0] == "handoffs" and key[1] == PROJECT_ID and key[2].startswith(TASK_ID):
        del store.records[key]
task_doc = store.get("tasks", PROJECT_ID, TASK_ID)
task_doc["status"] = "in_progress"
task_doc["blocked_reason"] = None
task_doc["current_progress"] = "still running (pre-terminal snapshot)"
task_doc["next_action"] = "Continue provider supervision"
task_doc.pop("completed_at", None)
task_doc["source_context"] = {"active_execution_id": EXECUTION_ID}
validate("task", task_doc)
store.put("tasks", PROJECT_ID, TASK_ID, task_doc)
registry.document = None

cmd = command(status="completed", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
             claimed_at=now_iso(), completed_at=now_iso(),
             result={"status": "completed", "session_id": SESSION_ID, "error_kind": None})

results = []
for _ in range(6):
    results.append(process_command(store, object(), cmd, claim_factory=lambda *_args: registry))

final_exec = store.get("executions", PROJECT_ID, EXECUTION_ID)
final_task = store.get("tasks", PROJECT_ID, TASK_ID)
print(json.dumps({
    "results": results,
    "final_execution_cleanup_evidence": final_exec["cleanup_evidence"],
    "final_task_status": final_task["status"],
}))
'''


def _base_worktree():
    """Create (once, memoized for this process) an isolated `git worktree`
    checkout of BASE_SHA outside this branch's own repo -- not a bare `git
    show`+exec of one file, so BASE's own manager.execution_lifecycle,
    manager.executions, manager.test_command_watcher, etc. are ALL the
    real base-SHA versions too, consistently, with zero risk of any of
    them resolving back to this candidate branch's modified copies."""
    if "path" in _BASE_WORKTREE:
        return _BASE_WORKTREE["path"]
    import atexit
    subprocess.run([GIT, "worktree", "prune"], cwd=REPO_ROOT, capture_output=True, text=True)
    worktree_dir = Path(tempfile.mkdtemp(prefix="adm-r17-base-worktree-"))
    subprocess.run(
        [GIT, "worktree", "add", "--detach", str(worktree_dir), BASE_SHA],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )

    def _cleanup():
        subprocess.run([GIT, "worktree", "remove", "--force", str(worktree_dir)],
                       cwd=REPO_ROOT, capture_output=True, text=True)

    atexit.register(_cleanup)
    _BASE_WORKTREE["path"] = worktree_dir
    return worktree_dir


def _run_fail_before_in_isolated_base_worktree():
    """Run the exact R17 durable-state FAIL-before scenario as a genuinely
    separate OS subprocess, cwd'd into an isolated `git worktree` checkout
    of BASE_SHA, with PYTHONPATH restricted to that worktree only -- so
    every `manager.*` import the base code performs, transitively, resolves
    to BASE_SHA's own source and NEVER to this candidate branch's modified
    manager/command_watcher.py or manager/execution_lifecycle.py. Returns
    the parsed JSON result dict the subprocess printed."""
    worktree_dir = _base_worktree()
    driver_path = worktree_dir / "_r17_fail_before_driver.py"
    driver_path.write_text(_FAIL_BEFORE_DRIVER, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    result = subprocess.run(
        [sys.executable, str(driver_path)],
        cwd=worktree_dir, env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"isolated base-worktree subprocess failed:\\n{result.stdout}\\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


_RESTART_PROCESS_1_DRIVER = '''
import json
import os
from unittest.mock import patch

from manager.command_watcher import process_command
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
from manager.executions import reserve_execution
from manager.tasks import TaskError, create_project, create_task, now_iso, validate
from manager.test_command_watcher import Store, command
from manager.test_execution_lifecycle import project, quota_document, task
from manager.test_task_claims import MemoryClaimRegistry

PROJECT_ID = "p1"
TASK_ID = "t1"
EXECUTION_ID = "command-cmd-1"
SESSION_ID = "codex:01a05796-1b5a-7fe2-bf89-0a0bacab751c"


class CrashyStore(Store):
    """process 1 never succeeds writing a Handoff -- the exact partial-
    persistence gap this restart proves survives the process boundary."""
    def put(self, area, project_id, name, document):
        if area == "handoffs":
            raise TaskError("simulated persistent Drive failure writing handoffs")
        return super().put(area, project_id, name, document)


# Build the clean terminal baseline with a NORMAL store first -- it
# genuinely needs one successful Handoff write -- then move the resulting
# durable records into the always-fails-on-handoffs CrashyStore for the
# actual (about to be interrupted) retry attempt below.
clean_store = Store()
create_project(clean_store, project())
create_task(clean_store, task(read_only=True), assign=False)
registry = MemoryClaimRegistry()

reserve_execution(clean_store, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex", {"decision": "fresh"})
with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
    enter_running_gate(clean_store, object(), None, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                       "read_only", task_claim_registry=registry)
exec_doc = clean_store.get("executions", PROJECT_ID, EXECUTION_ID)
exec_doc["session_id"] = SESSION_ID
clean_store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
with patch("manager.executions.read_drive_status", return_value=quota_document()):
    terminalize_execution(clean_store, object(), None, registry, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                          "completed", 1, True, summary="Execution terminal completed")

store = CrashyStore()
store.records = dict(clean_store.records)

exec_doc = store.get("executions", PROJECT_ID, EXECUTION_ID)
exec_doc["cleanup_evidence"] = {
    "provider_outcome": "completed", "persistence": "partial", "persisted": ["execution"],
    "writer_release": "not_required", "task_claim_release": "retained",
    "errors": ["Drive verification failed: simulated persistent Drive failure writing handoffs"],
}
validate("execution", exec_doc)
store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
for key in list(store.records):
    if key[0] == "handoffs" and key[1] == PROJECT_ID and key[2].startswith(TASK_ID):
        del store.records[key]
task_doc = store.get("tasks", PROJECT_ID, TASK_ID)
task_doc["status"] = "in_progress"
task_doc["blocked_reason"] = None
task_doc["current_progress"] = "still running (pre-terminal snapshot)"
task_doc["next_action"] = "Continue provider supervision"
task_doc.pop("completed_at", None)
task_doc["source_context"] = {"active_execution_id": EXECUTION_ID}
validate("task", task_doc)
store.put("tasks", PROJECT_ID, TASK_ID, task_doc)
registry.document = None

cmd = command(status="completed", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
             claimed_at=now_iso(), completed_at=now_iso(),
             result={"status": "completed", "session_id": SESSION_ID, "error_kind": None})
store.put("commands", PROJECT_ID, cmd["command_id"], cmd)

with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
    process_command(store, object(), store.get("commands", PROJECT_ID, cmd["command_id"]),
                    claim_factory=lambda *_args: registry)

after = store.get("executions", PROJECT_ID, EXECUTION_ID)
assert after["cleanup_evidence"]["persistence"] == "partial", after["cleanup_evidence"]

# "process 1" crashes here: everything from this point on is durable-file
# state only -- no Python object from this process is ever handed to
# "process 2", which runs as a genuinely separate OS process below.
fixture = {
    "records": {"|".join(key): value for key, value in store.records.items()},
    "claim_document": registry.document,
    "claim_generation": registry.generation,
    "command_id": cmd["command_id"],
}
with open(os.environ["ADM_FIXTURE_PATH"], "w", encoding="utf-8") as f:
    json.dump(fixture, f)
print(json.dumps({"process_1_persistence": after["cleanup_evidence"]["persistence"]}))
'''

_RESTART_PROCESS_2_DRIVER = '''
import json
import os
from unittest.mock import patch

from manager.command_watcher import process_command
from manager.test_command_watcher import Store
from manager.test_task_claims import MemoryClaimRegistry

PROJECT_ID = "p1"
TASK_ID = "t1"
EXECUTION_ID = "command-cmd-1"

with open(os.environ["ADM_FIXTURE_PATH"], "r", encoding="utf-8") as f:
    fixture = json.load(f)

store = Store()
for key, value in fixture["records"].items():
    area, project_id, name = key.split("|")
    store.records[(area, project_id, name)] = value

registry = MemoryClaimRegistry()
registry.document = fixture["claim_document"]
registry.generation = fixture["claim_generation"]

cmd = store.get("commands", PROJECT_ID, fixture["command_id"])

with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
    outcome = process_command(store, object(), cmd, claim_factory=lambda *_args: registry)

final_exec = store.get("executions", PROJECT_ID, EXECUTION_ID)
final_task = store.get("tasks", PROJECT_ID, TASK_ID)
final_cmd = store.get("commands", PROJECT_ID, fixture["command_id"])
print(json.dumps({
    "outcome": outcome,
    "final_execution_cleanup_evidence": final_exec["cleanup_evidence"],
    "final_task_status": final_task["status"],
    "final_command_status": final_cmd["status"],
}))
'''


def _run_restart_driver(source, fixture_path):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "ADM_FIXTURE_PATH": str(fixture_path)}
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"restart-boundary subprocess failed:\\n{result.stdout}\\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _claim_factory(registry):
    return lambda *_args: registry


class FlakyStore(Store):
    """Store double that can fail `put()` for a chosen area N times (then
    succeed) or forever, to model transient vs. persistent Drive failures
    on the specific write retry_incomplete_terminal_persistence() makes."""

    def __init__(self):
        super().__init__()
        self.fail_remaining = {}
        self.fail_forever = set()

    def put(self, area, project_id, name, document):
        if area in self.fail_forever:
            raise TaskError(f"simulated persistent Drive failure writing {area}")
        remaining = self.fail_remaining.get(area, 0)
        if remaining > 0:
            self.fail_remaining[area] = remaining - 1
            raise TaskError(f"simulated transient Drive failure writing {area}")
        return super().put(area, project_id, name, document)


class TerminalPartialPersistenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = FlakyStore()
        create_project(self.store, project())
        create_task(self.store, task(read_only=True), assign=False)
        self.registry = MemoryClaimRegistry()

    def _build_terminal_baseline(self, terminal_status="completed", store=None, registry=None):
        """Build a fully clean, correctly-terminalized execution/task/handoff
        baseline (persisted=[execution, handoff, task], persistence=complete,
        task_claim_release=released) -- the exact state
        terminalize_execution() reaches on a real success. Every partial
        shape in this file is then derived by corrupting this baseline the
        same way the live incident's real failure would have left it: some
        subset of [handoff, task] missing/stale, cleanup_evidence rolled
        back to 'partial', and the task claim NOT released."""
        store = store or self.store
        registry = registry or self.registry
        reserve_execution(store, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex", {"decision": "fresh"})
        with patch("manager.execution_lifecycle.read_drive_status", return_value=quota_document()):
            enter_running_gate(
                store, object(), None, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                "read_only", task_claim_registry=registry,
            )
        exec_doc = store.get("executions", PROJECT_ID, EXECUTION_ID)
        exec_doc["session_id"] = SESSION_ID
        store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
        with patch("manager.executions.read_drive_status", return_value=quota_document()):
            terminalize_execution(
                store, object(), None, registry, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex",
                terminal_status, 1, True, summary=f"Execution terminal {terminal_status}",
            )

    def _corrupt_to_partial(self, persisted, store=None, task_claim_release="retained",
                            claim_in_gcs=False, terminal_status="completed",
                            errors=None, registry=None, error_kind=None):
        """Roll a clean baseline back to the exact partial shape under test:
        keep only `persisted` records genuinely matching what
        _expected_terminal_task/_terminal_handoff would (re)compute, delete
        the rest, and rewrite cleanup_evidence to match the live incident's
        shape (persistence='partial', task_claim_release='retained',
        errors=['Drive verification failed: ...'])."""
        store = store or self.store
        registry = registry or self.registry
        if "handoff" not in persisted:
            handoff_keys = [k for k in list(store.records) if k[0] == "handoffs"
                            and k[1] == PROJECT_ID and k[2].startswith(TASK_ID)]
            for key in handoff_keys:
                del store.records[key]
        if "task" not in persisted:
            task_doc = store.get("tasks", PROJECT_ID, TASK_ID)
            task_doc["status"] = "in_progress"
            task_doc["blocked_reason"] = None
            task_doc["current_progress"] = "still running (pre-terminal snapshot)"
            task_doc["next_action"] = "Continue provider supervision"
            task_doc.pop("completed_at", None)
            task_doc["source_context"] = {"active_execution_id": EXECUTION_ID}
            validate("task", task_doc)
            store.put("tasks", PROJECT_ID, TASK_ID, task_doc)

        exec_doc = store.get("executions", PROJECT_ID, EXECUTION_ID)
        exec_doc["cleanup_evidence"] = {
            "provider_outcome": terminal_status, "persistence": "partial", "persisted": list(persisted),
            "writer_release": "not_required", "task_claim_release": task_claim_release,
            "errors": errors if errors is not None else [
                f"Drive verification failed: dispatch-cgate5-r17-20260831T111932Z-{terminal_status}-command-"
                f"dispatch-cgate5-r17-20260831T111932Z-0.json"
            ],
        }
        validate("execution", exec_doc)
        store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)

        if not claim_in_gcs:
            registry.document = None

        command_status = "completed" if terminal_status == "completed" else "failed"
        return command(status=command_status, execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                       claimed_at=now_iso(), completed_at=now_iso(),
                       result={"status": terminal_status, "session_id": SESSION_ID, "error_kind": error_kind})

    # ------------------------------------------------------------------
    # 1. The exact live r17 shape.
    # ------------------------------------------------------------------
    @unittest.skipUnless(GIT, "git required")
    def test_01_r17_exact_shape_fail_before_and_pass_after(self):
        # FAIL-BEFORE: run the REAL, unmodified manager/command_watcher.py
        # (and its real manager.execution_lifecycle, manager.executions,
        # etc. -- an entire pure BASE_SHA dependency graph, not just one
        # file) as a genuinely separate OS subprocess, cwd'd into an
        # isolated `git worktree` checkout of BASE_SHA with PYTHONPATH
        # restricted to that worktree only. No import in this subprocess
        # can resolve back to this candidate branch's modified
        # manager/command_watcher.py or manager/execution_lifecycle.py.
        fail_before = _run_fail_before_in_isolated_base_worktree()
        for outcome in fail_before["results"]:
            self.assertEqual({"status": "completed", "skipped": True}, outcome)
        self.assertEqual("partial", fail_before["final_execution_cleanup_evidence"]["persistence"])
        self.assertEqual(["execution"], fail_before["final_execution_cleanup_evidence"]["persisted"])
        self.assertEqual("retained", fail_before["final_execution_cleanup_evidence"]["task_claim_release"])
        self.assertEqual("in_progress", fail_before["final_task_status"])

        # PASS-AFTER: the real (fixed) candidate process_command(), in this
        # same process, on the exact same durable-state shape.
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        original_error = self.store.get("executions", PROJECT_ID, EXECUTION_ID)["cleanup_evidence"]["errors"]
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("completed", outcome["status"])
        self.assertTrue(outcome.get("reconciled"))

        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], final_exec["cleanup_evidence"]["persisted"])
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("completed", final_task["status"])

        stored_cmd = self.store.get("commands", PROJECT_ID, cmd["command_id"])
        self.assertEqual("completed", stored_cmd["status"])
        self.assertEqual(SESSION_ID, stored_cmd["result"]["session_id"])

        # Current cleanup_evidence.errors reflects the NOW-complete state
        # (empty), but the original "Drive verification failed" error is
        # never fabricated away -- it survives as durable historical audit
        # evidence in its own field (PHASE-3C-EXECUTION-LIFECYCLE.md #15).
        self.assertEqual([], final_exec["cleanup_evidence"]["errors"])
        self.assertEqual(original_error, final_exec["cleanup_evidence"]["historical_errors"])

    # ------------------------------------------------------------------
    # 2. persisted=['execution'] only.
    # ------------------------------------------------------------------
    def test_02_partial_execution_only_recovers(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", outcome["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual(["execution", "handoff", "task"], final_exec["cleanup_evidence"]["persisted"])

    # ------------------------------------------------------------------
    # 3. persisted=['execution', 'handoff']; only task missing.
    # ------------------------------------------------------------------
    def test_03_only_task_missing_recovers(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution", "handoff"])
        # Handoff must already be present and untouched.
        self.assertTrue(any(k[0] == "handoffs" for k in self.store.records))
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", outcome["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual(["execution", "handoff", "task"], final_exec["cleanup_evidence"]["persisted"])
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("completed", final_task["status"])

    # ------------------------------------------------------------------
    # 4. Transient handoff-write failure, succeeds on a later tick.
    # ------------------------------------------------------------------
    def test_04_transient_handoff_failure_converges_by_third_tick(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        self.store.fail_remaining["handoffs"] = 2

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            first = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
            exec_after_first = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
            # A single failed handoff-write attempt must never be reported
            # as convergence: the Authority Fence still surfaces the
            # already-durable 'completed' Command, but the underlying
            # persistence gap this test is about must remain exactly
            # 'partial' after this one attempt.
            self.assertEqual("partial", exec_after_first["cleanup_evidence"]["persistence"])
            second = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", first["status"])
        exec_after_two = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("partial", exec_after_two["cleanup_evidence"]["persistence"])

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            third = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", third["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution", "handoff", "task"], final_exec["cleanup_evidence"]["persisted"])

    # ------------------------------------------------------------------
    # 5. Transient task-write failure, succeeds on a later tick.
    # ------------------------------------------------------------------
    def test_05_transient_task_failure_converges_by_third_tick(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        self.store.fail_remaining["tasks"] = 2

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
            process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        exec_after_two = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("partial", exec_after_two["cleanup_evidence"]["persistence"])
        # Handoff is a pure function of execution/task and idempotently
        # re-derived/re-verified every tick -- the underlying record is
        # already durably written even though cleanup_evidence itself is
        # only updated atomically once ALL of execution/handoff/task are
        # confirmed together (retry_incomplete_terminal_persistence never
        # reports a partial success as if it were the final truth).
        handoff_keys = [k for k in self.store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual(1, len(handoff_keys))

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            third = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", third["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("completed", final_task["status"])

    # ------------------------------------------------------------------
    # 6. Persistent (non-transient) Drive failure -- must fail closed
    #    forever, never fake 'complete'/'released'.
    # ------------------------------------------------------------------
    def test_06_persistent_drive_failure_fails_closed_forever(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        self.store.put("commands", PROJECT_ID, cmd["command_id"], cmd)
        self.store.fail_forever.add("tasks")

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            for _ in range(10):
                outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
                # The outer Command was already durably 'completed' before
                # the persistence gap was even discovered (this is exactly
                # the live r17 shape) -- terminal monotonicity means it must
                # NEVER be downgraded to 'attention' on top of that. The
                # fail-closed correctness lives entirely in the Execution's
                # own cleanup_evidence, asserted below: it must never claim
                # 'complete'/'released' while the underlying Drive write
                # keeps failing, no matter how many natural ticks pass.
                self.assertEqual("completed", outcome.get("status"))

        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertNotEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertNotEqual(["execution", "handoff", "task"], final_exec["cleanup_evidence"]["persisted"])
        self.assertNotEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertNotEqual("completed", final_task["status"])

    # ------------------------------------------------------------------
    # 7. Restart durability: "process 2" is a genuinely separate OS
    #    subprocess resuming purely from a durable JSON fixture file --
    #    not a JSON round-trip of Python objects staying inside the same
    #    interpreter (that alone cannot prove no in-memory caches, module-
    #    level state, or import-time singletons survived a real restart).
    # ------------------------------------------------------------------
    def test_07_persistence_retry_durable_across_restart(self):
        fixture_path = Path(tempfile.mkdtemp(prefix="adm-r17-restart-fixture-")) / "fixture.json"
        try:
            process_1 = _run_restart_driver(_RESTART_PROCESS_1_DRIVER, fixture_path)
            self.assertEqual("partial", process_1["process_1_persistence"])
            self.assertTrue(fixture_path.exists())

            # "Process 1" has already exited by this point -- process_2 is
            # launched fresh below with nothing but the fixture file on
            # disk; no Python object, module cache, or interpreter state
            # from process 1 is reachable from it.
            process_2 = _run_restart_driver(_RESTART_PROCESS_2_DRIVER, fixture_path)
        finally:
            shutil.rmtree(fixture_path.parent, ignore_errors=True)

        self.assertEqual("completed", process_2["outcome"]["status"])
        self.assertEqual("complete", process_2["final_execution_cleanup_evidence"]["persistence"])
        self.assertEqual("released", process_2["final_execution_cleanup_evidence"]["task_claim_release"])
        self.assertEqual("completed", process_2["final_task_status"])
        self.assertEqual("completed", process_2["final_command_status"])

    # ------------------------------------------------------------------
    # 8. Concurrent reconcilers: no duplicate/conflicting terminal
    #    Handoff, cleanup evidence merges monotonically.
    # ------------------------------------------------------------------
    def test_08_concurrent_reconcilers_no_duplicate_handoff(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        # Instrumented store: (1) counts every write attempt per area so the
        # test can assert real attempt counts, not just the final tally, and
        # (2) forces each worker thread's FIRST read of the Task record to
        # rendezvous at a Barrier -- guaranteeing genuine overlap at the
        # exact point retry_incomplete_terminal_persistence() reads
        # authority, rather than hoping N independently-scheduled threads
        # happen to race.
        class InstrumentedStore(FlakyStore):
            def __init__(self):
                super().__init__()
                self.put_attempts = {}
                self.barrier = None
                self._local = threading.local()

            def put(self, area, project_id, name, document):
                self.put_attempts[area] = self.put_attempts.get(area, 0) + 1
                return super().put(area, project_id, name, document)

            def get(self, area, project_id, name):
                if area == "tasks" and self.barrier is not None:
                    reads = getattr(self._local, "task_reads", 0) + 1
                    self._local.task_reads = reads
                    if reads == 1:
                        # No except here: a genuine BrokenBarrierError (not
                        # every worker arriving in time) must propagate and
                        # fail this test loudly, never be silently absorbed
                        # into a degraded "raced by luck" run.
                        self.barrier.wait(timeout=5)
                return super().get(area, project_id, name)

        store = InstrumentedStore()
        store.records = deepcopy(self.store.records)
        store.put("commands", PROJECT_ID, cmd["command_id"], cmd)
        worker_count = 4
        store.barrier = threading.Barrier(worker_count, timeout=5)

        outcomes = []
        exceptions = []
        lock = threading.Lock()

        def run():
            try:
                result = process_command(store, object(), store.get("commands", PROJECT_ID, cmd["command_id"]),
                                         claim_factory=_claim_factory(self.registry))
                with lock:
                    outcomes.append(result)
            except Exception as exc:  # noqa: BLE001 -- must not be silently swallowed
                with lock:
                    exceptions.append(exc)

        # Patch ONCE for the whole test, outside the thread-spawning loop:
        # unittest.mock.patch mutates a shared module attribute with no
        # thread safety of its own, so N threads each independently
        # entering/exiting their own `with patch(...)` block on the SAME
        # target racily corrupts whichever value the last thread's teardown
        # restores -- observed live as global process_identity_state
        # corruption that broke unrelated tests in other files run
        # afterward in the same process. A single outer patch avoids the
        # hazard entirely while still exercising real concurrent
        # reconciliation.
        patcher = patch("manager.command_watcher.process_identity_state", return_value="stopped")
        patcher.start()
        try:
            threads = [threading.Thread(target=run) for _ in range(worker_count)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            patcher.stop()
            # BrokenBarrierError raised inside store.get() is caught by
            # retry_incomplete_terminal_persistence()'s own (legitimate,
            # real-Drive-failure-tolerant) `except Exception: return False`
            # -- so a broken barrier would NOT necessarily surface as a
            # raised exception in `exceptions` below. Capture the barrier's
            # own state directly, the one place this can be checked
            # independent of application code, before disarming it -- every
            # subsequent store.get("tasks", ...) below is this single
            # (main) thread doing final assertions, not another
            # rendezvousing worker, and leaving the barrier armed would make
            # that lone call block for the full timeout and then break too.
            barrier_broken = store.barrier.broken
            store.barrier = None

        self.assertFalse(barrier_broken, "workers did not genuinely rendezvous -- barrier timed out")

        # ALL_WORKERS_RETURNED / THREAD_EXCEPTIONS: every thread must have
        # actually finished (not hung on the barrier/join) and none may have
        # raised -- an exception silently swallowed by a bare `except: pass`
        # inside the reconciliation path would otherwise be invisible here.
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual([], exceptions)
        self.assertEqual(worker_count, len(outcomes))
        for outcome in outcomes:
            self.assertIn(outcome["status"], ("completed", "attention"))

        # HANDOFF_WRITE_ATTEMPTS: this proves the LOGICAL layer only -- N
        # concurrent attempts against the SAME in-memory dict key can only
        # ever converge to one entry by construction (a later put() to an
        # identical key always overwrites), so it cannot by itself prove
        # PHYSICAL_DRIVE_HANDOFF_DUPLICATE_SAFE (see
        # DriveRecordRaceTests.test_real_threaded_concurrent_terminal_
        # handoff_create_does_not_duplicate in manager/test_tasks.py for
        # that proof against the real DriveRecords create-then-verify
        # path). What this DOES prove: every concurrent attempt derived and
        # attempted to persist the exact same canonical content -- no
        # thread ever computed or wrote a conflicting Handoff.
        self.assertGreaterEqual(store.put_attempts.get("handoffs", 0), 2,
                                "expected more than one worker to attempt the handoff write (proves real overlap, not one winner short-circuiting the rest)")
        handoff_keys = [k for k in store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual(1, len(handoff_keys))
        canonical_handoff = store.records[handoff_keys[0]]
        self.assertEqual(TASK_ID, canonical_handoff["task_id"])
        self.assertEqual(SESSION_ID, canonical_handoff["from_session"])
        self.assertEqual("completed", canonical_handoff["current_state"])

        final_exec = store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])
        final_task = store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("completed", final_task["status"])

    # ------------------------------------------------------------------
    # 9. Newer execution protection: a newer execution already took over
    #    the Task -- the old retry must NOT overwrite newer Task state.
    # ------------------------------------------------------------------
    def test_09_newer_execution_protected_from_stale_retry(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        task_doc = self.store.get("tasks", PROJECT_ID, TASK_ID)
        task_doc["source_context"] = {"active_execution_id": "newer-execution-id"}
        task_doc["status"] = "in_progress"
        validate("task", task_doc)
        self.store.put("tasks", PROJECT_ID, TASK_ID, task_doc)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        # The outer Command was already durably 'completed' in Drive before
        # the newer execution took the Task over -- the Authority Fence
        # (_attention()) correctly refuses to downgrade that to
        # 'attention'; the real protection is that the stale retry must
        # never have clobbered the newer Task/Execution state, asserted
        # below.
        self.assertEqual("completed", outcome.get("status"))
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("newer-execution-id", (final_task.get("source_context") or {}).get("active_execution_id"))
        self.assertEqual("in_progress", final_task["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertNotEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertNotEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])
        # The stale retry must never even have written a Handoff on the
        # superseded execution's behalf -- it would be derived from the
        # already-invalid pre-race Task snapshot, and this execution's own
        # authority over the task is gone before any write is attempted.
        handoff_keys = [k for k in self.store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual([], handoff_keys)

    # ------------------------------------------------------------------
    # 10 & 11. Terminal monotonicity + session_id/result preservation.
    # ------------------------------------------------------------------
    def test_10_terminal_monotonicity_and_session_id_preserved(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("completed", outcome["status"])
        stored_cmd = self.store.get("commands", PROJECT_ID, cmd["command_id"])
        self.assertEqual("completed", stored_cmd["status"])
        self.assertIsNotNone(stored_cmd["result"])
        self.assertEqual(SESSION_ID, stored_cmd["result"]["session_id"])

        # A stale reconciler snapshot ("attention") must never be able to
        # downgrade the now-converged completed Command.
        from manager.command_watcher import _write
        stale = command(status="attention", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                        claimed_at=now_iso(), result=None)
        written = _write(self.store, stale)
        self.assertEqual("completed", written["status"])
        self.assertEqual(SESSION_ID, written["result"]["session_id"])

    # ------------------------------------------------------------------
    # 12. Round46 claim-absent / present / unknown compatibility.
    # ------------------------------------------------------------------
    def test_12a_claim_absent_after_recovery_converges_to_released(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"], claim_in_gcs=False)
        self.assertIsNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("completed", outcome["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

    def test_12b_claim_present_uses_cas_release(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"], claim_in_gcs=True)
        claim_task_execution(self.registry, PROJECT_ID, TASK_ID, EXECUTION_ID, "codex", now_iso())
        self.assertIsNotNone(self.registry.document)

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("completed", outcome["status"])
        self.assertIsNone(self.registry.document)  # CAS-released
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

    def test_12c_claim_unknown_fails_closed(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        with patch("manager.command_watcher.check_task_execution_claim", side_effect=TaskError("503 timeout")):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        # Command was already 'completed' in Drive -- Authority Fence keeps
        # it there (never downgrades to 'attention'); the fail-closed
        # behavior is that the claim is never declared released while its
        # true state is unknown.
        self.assertEqual("completed", outcome.get("status"))
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("retained", final_exec["cleanup_evidence"]["task_claim_release"])

    # ------------------------------------------------------------------
    # Extra: interrupted/failed terminal outcome maps Task -> 'blocked'
    # (item 12 of the brief: expected final Task status, via the existing
    # lifecycle helper _expected_terminal_task(), not a second state
    # machine).
    # ------------------------------------------------------------------
    def test_13_interrupted_outcome_recovers_task_to_blocked(self):
        self._build_terminal_baseline(terminal_status="interrupted")
        cmd = self._corrupt_to_partial(["execution"], terminal_status="interrupted")

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("failed", outcome["status"])
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("blocked", final_task["status"])

    # ------------------------------------------------------------------
    # Already-fully-converged completed Command stays a cheap no-op (no
    # behavior change / no regression for the common case).
    # ------------------------------------------------------------------
    def test_14_already_converged_completed_command_stays_skipped(self):
        self._build_terminal_baseline()
        exec_doc = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])
        cmd = command(status="completed", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                     claimed_at=now_iso(), completed_at=now_iso(),
                     result={"status": "completed", "session_id": SESSION_ID, "error_kind": None})
        self.store.put("commands", PROJECT_ID, cmd["command_id"], cmd)

        outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual({"status": "completed", "skipped": True}, outcome)

    # ------------------------------------------------------------------
    # 15. Stale-writer forced race: a newer execution takes REAL authority
    #     (both the GCS claim registry and the Task document) over the task
    #     DURING retry_incomplete_terminal_persistence()'s own execution,
    #     between its internal read and its internal write -- not merely
    #     staged before the call (that's test_09/test_19). The stale
    #     worker's first Task read must genuinely complete and capture the
    #     OLD snapshot BEFORE it pauses -- if the barrier sits before the
    #     real read, the "stale" worker would actually observe the NEW
    #     state once it resumes, and the race would test nothing.
    # ------------------------------------------------------------------
    def test_15_stale_writer_forced_race_does_not_overwrite_newer_task(self):
        self._build_terminal_baseline()
        self._corrupt_to_partial(["execution"])

        reader_arrived = threading.Event()
        newer_execution_done = threading.Event()
        original_get = self.store.get
        read_count = {"n": 0}
        lock = threading.Lock()

        def racy_get(area, project_id, name):
            if area != "tasks":
                return original_get(area, project_id, name)
            # Perform the REAL read first and capture its (still-old)
            # result before pausing -- the stale worker must walk away from
            # this call already holding the pre-takeover snapshot, exactly
            # as it would in a genuine OS-thread race where the read
            # instruction itself completed before the context switch.
            snapshot = original_get(area, project_id, name)
            with lock:
                read_count["n"] += 1
                first_read = read_count["n"] == 1
            if first_read:
                reader_arrived.set()
                if not newer_execution_done.wait(timeout=5):
                    raise AssertionError("newer execution never signalled completion")
            return snapshot

        self.store.get = racy_get

        exceptions = []

        def newer_execution_takes_over():
            try:
                if not reader_arrived.wait(timeout=5):
                    raise AssertionError("stale retry never reached its first task read")
                # Real authority transfer, not just a document edit: the
                # newer execution first wins the actual GCS generation-CAS
                # claim (exactly as enter_running_gate's normal launch path
                # would, once the old claim is genuinely gone) and only then
                # updates the Task document -- the same two-step sequence
                # production code follows.
                claim_task_execution(self.registry, PROJECT_ID, TASK_ID, "newer-execution-id", "codex", now_iso())
                task_doc = original_get("tasks", PROJECT_ID, TASK_ID)
                task_doc["source_context"] = {"active_execution_id": "newer-execution-id"}
                task_doc["status"] = "in_progress"
                task_doc["blocked_reason"] = None
                task_doc["current_progress"] = "newer execution now owns this task"
                task_doc["next_action"] = "Continue newer provider supervision"
                task_doc.pop("completed_at", None)
                validate("task", task_doc)
                self.store.put("tasks", PROJECT_ID, TASK_ID, task_doc)
            except Exception as exc:  # noqa: BLE001 -- captured, asserted below, never swallowed
                exceptions.append(exc)
            finally:
                newer_execution_done.set()

        result_holder = {}

        def run_stale_retry():
            try:
                result_holder["result"] = retry_incomplete_terminal_persistence(
                    self.store, PROJECT_ID, TASK_ID, EXECUTION_ID, self.registry)
            except Exception as exc:  # noqa: BLE001 -- captured, asserted below, never swallowed
                exceptions.append(exc)

        stale_retry = threading.Thread(target=run_stale_retry)
        newer = threading.Thread(target=newer_execution_takes_over)
        stale_retry.start()
        newer.start()
        stale_retry.join(timeout=10)
        newer.join(timeout=10)

        # ALL_WORKERS_RETURNED / THREAD_EXCEPTIONS_SILENTLY_SWALLOWED=NO
        self.assertFalse(stale_retry.is_alive())
        self.assertFalse(newer.is_alive())
        self.assertEqual([], exceptions)

        # FORCED_RACE_ACTUALLY_USES_STALE_SNAPSHOT: prove the stale worker's
        # captured read really was the pre-takeover snapshot, not the
        # post-takeover one -- otherwise this test would not be exercising
        # the race it claims to.
        self.assertEqual(1, read_count["n"])

        # STALE_WRITER_SAFE: the stale retry must report failure/abandon,
        # never a false "complete" -- both because the real claim registry
        # now shows a different owner (the CAS veto) and because the Task
        # document itself changed underneath it.
        self.assertFalse(result_holder.get("result"))

        # NEWER_EXECUTION_PROTECTED: the newer execution's real claim and
        # Task write must survive completely untouched by the stale retry.
        newer_claim = check_task_execution_claim(self.registry, PROJECT_ID, TASK_ID)
        self.assertEqual("newer-execution-id", newer_claim["execution_id"])
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("newer-execution-id", (final_task.get("source_context") or {}).get("active_execution_id"))
        self.assertEqual("in_progress", final_task["status"])
        self.assertEqual("newer execution now owns this task", final_task["current_progress"])

        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertNotEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        handoff_keys = [k for k in self.store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual([], handoff_keys)

    # ------------------------------------------------------------------
    # 16. error_kind and other terminal metadata survive recovery as
    #     monotonic enrichment, never a destructive overwrite.
    # ------------------------------------------------------------------
    def test_16_error_kind_and_terminal_metadata_preserved_through_recovery(self):
        self._build_terminal_baseline(terminal_status="failed")
        original_completed_at = self.store.get("executions", PROJECT_ID, EXECUTION_ID)["completed_at"]
        cmd = self._corrupt_to_partial(["execution"], terminal_status="failed", error_kind="provider_timeout")

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("failed", outcome["status"])
        stored_cmd = self.store.get("commands", PROJECT_ID, cmd["command_id"])
        # error_kind is monotonically preserved, not reset to null by
        # reconciliation re-deriving the Command's terminal result.
        self.assertEqual("provider_timeout", stored_cmd["result"]["error_kind"])
        self.assertEqual(SESSION_ID, stored_cmd["result"]["session_id"])

        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual(original_completed_at, final_exec["completed_at"])
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual([], final_exec["cleanup_evidence"]["errors"])
        self.assertEqual(
            [f"Drive verification failed: dispatch-cgate5-r17-20260831T111932Z-failed-command-"
             f"dispatch-cgate5-r17-20260831T111932Z-0.json"],
            final_exec["cleanup_evidence"]["historical_errors"],
        )

    # ------------------------------------------------------------------
    # 17. Guard boundary A: persistence already complete but the task claim
    #     is not yet in a released/terminal-settled state must still fall
    #     through to reconciliation.
    # ------------------------------------------------------------------
    def test_17_persistence_complete_but_claim_unsettled_triggers_reconciliation(self):
        self._build_terminal_baseline()
        exec_doc = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])
        exec_doc["cleanup_evidence"] = {**exec_doc["cleanup_evidence"], "task_claim_release": "retained"}
        validate("execution", exec_doc)
        self.store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
        cmd = command(status="completed", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                     claimed_at=now_iso(), completed_at=now_iso(),
                     result={"status": "completed", "session_id": SESSION_ID, "error_kind": None})

        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))

        self.assertEqual("completed", outcome["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

    # ------------------------------------------------------------------
    # 18. Guard boundary B: an unrelated, harmless cleanup_evidence anomaly
    #     (fully settled persistence + claim, but a provider_outcome
    #     mismatch that only the broader _terminal_cleanup_confirmed() --
    #     used elsewhere for a stricter purpose -- would flag) must NOT
    #     pull an otherwise-converged command into reconciliation.
    # ------------------------------------------------------------------
    def test_18_harmless_metadata_anomaly_stays_cheap_skip(self):
        self._build_terminal_baseline()
        exec_doc = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", exec_doc["cleanup_evidence"]["persistence"])
        self.assertEqual("released", exec_doc["cleanup_evidence"]["task_claim_release"])
        exec_doc["cleanup_evidence"] = {**exec_doc["cleanup_evidence"], "provider_outcome": "failed"}
        validate("execution", exec_doc)
        self.store.put("executions", PROJECT_ID, EXECUTION_ID, exec_doc)
        cmd = command(status="completed", execution_id=EXECUTION_ID, project_id=PROJECT_ID, task_id=TASK_ID,
                     claimed_at=now_iso(), completed_at=now_iso(),
                     result={"status": "completed", "session_id": SESSION_ID, "error_kind": None})

        outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        # Exact cheap no-op shape -- same as test_14 -- proving reconciliation
        # (which would have required a claim-registry round trip and Drive
        # writes) was never entered for this harmless anomaly.
        self.assertEqual({"status": "completed", "skipped": True}, outcome)
        untouched_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("failed", untouched_exec["cleanup_evidence"]["provider_outcome"])

    # ------------------------------------------------------------------
    # 19. TASK_AUTHORITY_CAS_USED: isolate the real GCS generation-CAS
    #     claim-registry veto from the pre-existing Task-document soft
    #     check -- the Task document still identifies THIS execution as
    #     active (the soft check alone would allow the write), so only the
    #     claim registry showing a genuinely different execution can be
    #     the reason this is refused.
    # ------------------------------------------------------------------
    def test_19_conflicting_real_claim_vetoes_write_even_when_task_document_unchanged(self):
        self._build_terminal_baseline()
        self._corrupt_to_partial(["execution"])
        claim_task_execution(self.registry, PROJECT_ID, TASK_ID, "other-execution-id", "codex", now_iso())
        before_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual(EXECUTION_ID, (before_task.get("source_context") or {}).get("active_execution_id"))

        result = retry_incomplete_terminal_persistence(self.store, PROJECT_ID, TASK_ID, EXECUTION_ID, self.registry)

        self.assertFalse(result)
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertNotEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        handoff_keys = [k for k in self.store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual([], handoff_keys)
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual(before_task, final_task)

    # ------------------------------------------------------------------
    # 20. An ABSENT claim (Round46 claim-absent convergence) must remain a
    #     permitted, non-conflicting state -- the real CAS veto only fires
    #     on a genuinely different owner, never merely because nothing is
    #     currently claimed.
    # ------------------------------------------------------------------
    def test_20_absent_claim_is_not_a_cas_conflict(self):
        self._build_terminal_baseline()
        self._corrupt_to_partial(["execution"], claim_in_gcs=False)
        self.assertIsNone(self.registry.document)

        result = retry_incomplete_terminal_persistence(self.store, PROJECT_ID, TASK_ID, EXECUTION_ID, self.registry)

        self.assertTrue(result)
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])

    # ------------------------------------------------------------------
    # 21. Monotonic cleanup_evidence merge: a concurrent process advances
    #     task_claim_release to 'released' WHILE this retry is still
    #     running (between its own initial read and its final write) --
    #     the retry's own write must never regress it back to 'retained'.
    # ------------------------------------------------------------------
    def test_21_cleanup_evidence_merge_never_regresses_concurrently_released_claim(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        original_put = self.store.put
        released_mid_call = threading.Event()

        def racy_put(area, project_id, name, document):
            result = original_put(area, project_id, name, document)
            if area == "handoffs" and not released_mid_call.is_set():
                # Simulate a concurrent process (recover_task_claim, via a
                # different reconciler) genuinely releasing the claim and
                # advancing cleanup_evidence.task_claim_release to
                # 'released' in the narrow window between this retry's own
                # early reads and its final cleanup_evidence write.
                concurrent_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
                concurrent_exec["cleanup_evidence"] = {
                    **(concurrent_exec.get("cleanup_evidence") or {}), "task_claim_release": "released",
                }
                original_put("executions", PROJECT_ID, EXECUTION_ID, concurrent_exec)
                released_mid_call.set()
            return result

        self.store.put = racy_put
        try:
            with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
                outcome = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        finally:
            self.store.put = original_put

        self.assertEqual("completed", outcome["status"])
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        # The concurrently-released claim must stay released -- this
        # retry's own (stale-at-the-time-of-its-first-read) view of
        # task_claim_release must never win and regress it back.
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])


if __name__ == "__main__":
    unittest.main()
