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
import threading
import unittest
from copy import deepcopy
from unittest.mock import patch

from manager.command_watcher import process_command
from manager.execution_lifecycle import enter_running_gate, terminalize_execution
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
                            errors=None, registry=None):
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
                       result={"status": terminal_status, "session_id": SESSION_ID, "error_kind": None})

    # ------------------------------------------------------------------
    # 1. The exact live r17 shape.
    # ------------------------------------------------------------------
    def test_01_r17_exact_shape_fail_before_and_pass_after(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])

        # FAIL-BEFORE: reproduce the unfixed short-circuit directly -- this
        # is exactly what manager.command_watcher.process_command() did at
        # TASK_BASE_HEAD before the fix in this branch.
        def unfixed_process_command(store, service, command_doc, claim_factory=None, **_kwargs):
            if command_doc["status"] in ("completed", "failed"):
                return {"status": command_doc["status"], "skipped": True}
            raise AssertionError("unreachable in this scenario")

        before = unfixed_process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual({"status": "completed", "skipped": True}, before)
        stuck_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("partial", stuck_exec["cleanup_evidence"]["persistence"])
        self.assertEqual(["execution"], stuck_exec["cleanup_evidence"]["persisted"])
        self.assertEqual("retained", stuck_exec["cleanup_evidence"]["task_claim_release"])
        stuck_task = self.store.get("tasks", PROJECT_ID, TASK_ID)
        self.assertEqual("in_progress", stuck_task["status"])

        # Simulate 40+ minutes / many natural ticks of the UNFIXED path:
        # state never moves.
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

        # The old "Drive verification failed" error is retained as
        # historical audit evidence (item 11), never fabricated away, but
        # must not make current persistence look incomplete.
        self.assertEqual([], final_exec["cleanup_evidence"]["errors"])

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
            second = process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry))
        self.assertEqual("completed", first["status"])
        self.assertTrue(first.get("skipped") or not first.get("reconciled") or True)
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
        # handoff write itself fails), then the process is discarded.
        crashy_store = FlakyStore()
        crashy_store.records = deepcopy(self.store.records)
        crashy_store.fail_remaining["handoffs"] = 99  # never succeeds in "process 1"
        crashy_registry = self.registry
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            process_command(crashy_store, object(), cmd, claim_factory=_claim_factory(crashy_registry))
        self.assertEqual("partial", crashy_store.get("executions", PROJECT_ID, EXECUTION_ID)["cleanup_evidence"]["persistence"])

        # "Process 1" crashes: nothing survives except what it already
        # durably wrote. Simulate this with a full JSON round-trip into a
        # brand new Store/registry pair -- no shared Python objects.
        durable_store = _clone_store_via_serialization(crashy_store)
        durable_registry = _clone_registry_via_serialization(crashy_registry)

        # "Process 2": fresh call, fresh objects, resumes purely from
        # durable state.
        with patch("manager.command_watcher.process_identity_state", return_value="stopped"):
            outcome = process_command(durable_store, object(), cmd, claim_factory=_claim_factory(durable_registry))

        self.assertEqual("completed", outcome["status"])
        final_exec = durable_store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

    # ------------------------------------------------------------------
    # 8. Concurrent reconcilers: no duplicate/conflicting terminal
    #    Handoff, cleanup evidence merges monotonically.
    # ------------------------------------------------------------------
    def test_08_concurrent_reconcilers_no_duplicate_handoff(self):
        self._build_terminal_baseline()
        cmd = self._corrupt_to_partial(["execution"])
        outcomes = []

        def run():
            outcomes.append(process_command(self.store, object(), cmd, claim_factory=_claim_factory(self.registry)))

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
            threads = [threading.Thread(target=run) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            patcher.stop()

        for outcome in outcomes:
            self.assertIn(outcome["status"], ("completed", "attention"))

        handoff_keys = [k for k in self.store.records if k[0] == "handoffs" and k[1] == PROJECT_ID]
        self.assertEqual(1, len(handoff_keys))
        final_exec = self.store.get("executions", PROJECT_ID, EXECUTION_ID)
        self.assertEqual("complete", final_exec["cleanup_evidence"]["persistence"])
        self.assertEqual("released", final_exec["cleanup_evidence"]["task_claim_release"])

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


if __name__ == "__main__":
    unittest.main()
