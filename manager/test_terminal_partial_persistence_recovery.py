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

import importlib.util
import json
import shutil
import subprocess
import sys
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


def _load_base_process_command():
    """Load the literal manager/command_watcher.py source blob as it existed
    at BASE_SHA (`git show <sha>:<path>`, the same primitive
    manager.provenance/worktree_materializer already use to read historical
    source) and exec it as a standalone module, returning its real
    process_command. Its internal `from manager.xxx import ...` statements
    resolve against the currently-installed manager package -- correct here
    because every one of those dependency modules is unchanged between
    BASE_SHA and this branch (only manager/command_watcher.py and
    manager/execution_lifecycle.py differ), so this is the real base
    process_command running with its real collaborators, not a mock."""
    module_name = "_base_r17_command_watcher"
    if module_name in sys.modules:
        return sys.modules[module_name].process_command
    source = subprocess.run(
        [GIT, "show", f"{BASE_SHA}:manager/command_watcher.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    base_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = base_module
    try:
        exec(compile(source, f"{BASE_SHA}:manager/command_watcher.py", "exec"), base_module.__dict__)
    except Exception:
        del sys.modules[module_name]
        raise
    return base_module.process_command


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


def _clone_store_via_serialization(store):
    """Simulate a fresh watcher process that has crashed and restarted:
    round-trips the store's records through JSON so nothing but plain,
    durable data survives -- no shared Python objects, no in-memory
    caches, no closures from the process that wrote them."""
    plain = {f"{area}|{project_id}|{name}": value
             for (area, project_id, name), value in store.records.items()}
    restored = json.loads(json.dumps(plain))
    fresh = Store()
    for key, value in restored.items():
        area, project_id, name = key.split("|")
        fresh.records[(area, project_id, name)] = value
    return fresh


def _clone_registry_via_serialization(registry):
    fresh = MemoryClaimRegistry()
    if registry.document is not None:
        fresh.document = json.loads(json.dumps(registry.document))
        fresh.generation = registry.generation
    return fresh


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
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        original_error = self.store.get("executions", PROJECT_ID, EXECUTION_ID)["cleanup_evidence"]["errors"]

        # FAIL-BEFORE: run the REAL, unmodified manager/command_watcher.py
        # source blob as it existed at BASE_SHA (loaded via `git show`, not
        # hand-written) against the exact same durable state. This is
        # literally the base-SHA code, not a guess at what it used to do.
        unfixed_process_command = _load_base_process_command()

        before = unfixed_process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual({"status": "completed", "skipped": True}, before)
        stuck_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("partial", stuck_exec["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution"], stuck_exec["cleanup_evidence"]["persisted"])
        self.assertEqual("retained", stuck_exec["cleanup_evidence"]["task_claim_release"])
        stuck_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("in_progress", stuck_task["status"])

        # Simulate 40+ minutes / many natural ticks of the real base-SHA
        # code: state never moves, because it never even reads
        # cleanup_evidence before short-circuiting.
        for _ in range(5):
            unfixed_process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        still_stuck = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("partial", still_stuck["cleanup_evidence"]["persistence"])

        # PASS-AFTER: the real (fixed) process_command() on the exact same
        # durable state.
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
    # 7. Restart durability: a fresh "process 2" resumes from durable
    #    state only -- no in-memory carryover from "process 1".
    # ------------------------------------------------------------------
    def test_07_persistence_retry_durable_across_restart(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        # "Process 1": one flaky attempt that fails partway through (the
        # handoff write itself fails), then the process is discarded. The
        # Command itself must be a durable record too -- persist it into the
        # store like every other area, instead of only ever existing as a
        # Python dict handed directly to process_command().
        crashy_store = FlakyStore()
        crashy_store.records = deepcopy(self.store.records)
        crashy_store.put("commands", PROJECT_ID, cmd["command_id"], cmd)
        crashy_store.fail_remaining["handoffs"] = 99  # never succeeds in "process 1"
        crashy_registry = self.registry
        process_1_cmd = crashy_store.get("commands", PROJECT_ID, cmd["command_id"])
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            process_command(crashy_store, object(), process_1_cmd, claim_factory=_claim_factory(crashy_registry))
        self.assertEqual("partial", crashy_store.get("executions", PROJECT_ID, EXECUTION_ID)["cleanup_evidence"]["persistence"])

        # "Process 1" crashes: nothing survives except what it already
        # durably wrote. Simulate this with a full JSON round-trip into a
        # brand new Store/registry pair -- no shared Python objects, no
        # in-memory caches, no closures from "process 1" survive.
        durable_store = _clone_store_via_serialization(crashy_store)
        durable_registry = _clone_registry_via_serialization(crashy_registry)

        # "Process 2": fresh call, fresh objects. The Command itself is also
        # re-read from the fresh durable store rather than reusing "process
        # 1"'s original `cmd`/`process_1_cmd` Python object -- proving
        # recovery resumes purely from durable records, including the
        # Command, not from anything still alive in the old process.
        self.assertIsNot(process_1_cmd, durable_store.get("commands", PROJECT_ID, cmd["command_id"]))
        process_2_cmd = durable_store.get("commands", PROJECT_ID, cmd["command_id"])
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(durable_store, object(), process_2_cmd, claim_factory=_claim_factory(durable_registry))

        self.assertEqual("completed", outcome["status"])
        final_exec = durable_store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])
        final_task = durable_store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("completed", final_task["status"])
        final_cmd = durable_store.get("commands", PROJECT_ID, cmd["command_id"])
        self.assertEqual("completed", final_cmd["status"])

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
                        try:
                            self.barrier.wait(timeout=5)
                        except threading.BrokenBarrierError:
                            pass
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

        # ALL_WORKERS_RETURNED / THREAD_EXCEPTIONS: every thread must have
        # actually finished (not hung on the barrier/join) and none may have
        # raised -- an exception silently swallowed by a bare `except: pass`
        # inside the reconciliation path would otherwise be invisible here.
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual([], exceptions)
        self.assertEqual(worker_count, len(outcomes))
        for outcome in outcomes:
            self.assertIn(outcome["status"], ("completed", "attention"))

        # HANDOFF_WRITE_ATTEMPTS: multiple concurrent attempts are expected
        # (that's the whole point of the race), but exactly one durable
        # Handoff record must exist -- no duplicate/conflicting write won.
        self.assertGreaterEqual(store.put_attempts.get("handoffs", 0), 1)
        handoff_keys = [k for k in store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual(1, len(handoff_keys))

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
    # 15. Stale-writer forced race: a newer execution takes authority over
    #     the task DURING retry_incomplete_terminal_persistence()'s own
    #     execution, between its internal read and its internal write --
    #     not merely staged before the call (that's test_09). A
    #     deterministic Event-based barrier forces the interleaving instead
    #     of hoping two threads race by luck.
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
            if area == "tasks":
                with lock:
                    read_count["n"] += 1
                    first_read = read_count["n"] == 1
                if first_read:
                    reader_arrived.set()
                    if not newer_execution_done.wait(timeout=5):
                        raise AssertionError("newer execution never signalled completion")
            return original_get(area, project_id, name)

        self.store.get = racy_get

        exceptions = []

        def newer_execution_takes_over():
            try:
                if not reader_arrived.wait(timeout=5):
                    raise AssertionError("stale retry never reached its first task read")
                task_doc = original_get("tasks", PROJECT_ID, TASK_ID)
                task_doc["source_context"] = {"active_execution_id": "newer-execution-id"}
                task_doc["status"] = "in_progress"
                task_doc["blocked_reason"] = None
                task_doc["current_progress"] = "newer execution now owns this task"
                task_doc["next_action"] = "Continue newer provider supervision"
                task_doc.pop("completed_at", None)
                validate("task", task_doc)
                self.store.put("tasks", PROJECT_ID, TASK_ID, task_doc)
            except Exception as exc:  # noqa: BLE001
                exceptions.append(exc)
            finally:
                newer_execution_done.set()

        result_holder = {}

        def run_stale_retry():
            try:
                result_holder["result"] = retry_incomplete_terminal_persistence(
                    self.store, PROJECT_ID, TASK_ID, EXECUTION_ID)
            except Exception as exc:  # noqa: BLE001
                exceptions.append(exc)

        stale_retry = threading.Thread(target=run_stale_retry)
        newer = threading.Thread(target=newer_execution_takes_over)
        stale_retry.start()
        newer.start()
        stale_retry.join(timeout=10)
        newer.join(timeout=10)

        # ALL_WORKERS_RETURNED / THREAD_EXCEPTIONS
        self.assertFalse(stale_retry.is_alive())
        self.assertFalse(newer.is_alive())
        self.assertEqual([], exceptions)

        # STALE_WRITER_SAFE: the stale retry must report failure/abandon,
        # never a false "complete".
        self.assertFalse(result_holder.get("result"))

        # NEWER_EXECUTION_PROTECTED: the newer execution's Task write must
        # survive completely untouched by the stale retry.
        final_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("newer-execution-id", (final_task.get("source_context") or {}).get("active_execution_id"))
        self.assertEqual("in_progress", final_task["status"])
        self.assertEqual("newer execution now owns this task", final_task["current_progress"])

        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertNotEqual("complete", final_exec["cleanup_evidence"]["persistence"])

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


if __name__ == "__main__":
    unittest.main()
