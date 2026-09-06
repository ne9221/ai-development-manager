"""Runtime integration: the controller over real, on-disk stores.

Every test here runs against a **temporary** AI_MANAGER_HOME created per test.
Phase B-2 builds runtime foundation and is explicitly not allowed to operate on
production, so the production refusals are asserted here rather than assumed --
and they are asserted against the same ``manager.production_guard`` primitive
the rest of ADM uses, not a private reimplementation of "what production means".

The round-trip assertions matter more than they look. A serialisation that
silently dropped a field would change the derivation, and nothing else in the
suite would notice, because every other test passes dataclasses straight to
``evaluate()`` without ever writing them down.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from manager.verification_loop import stores
from manager.verification_loop.bundle import canonical_json
from manager.verification_loop.controller import VerificationController, rehydrate
from manager.verification_loop.fixtures import fx_adm_false_dispatch, fx_ledger_freeze_pane
from manager.verification_loop.models import (
    FailureObservation,
    VerificationReportFixture,
    VerificationTicket,
)

LEDGER_GATES = ("V0", "V1", "V2", "V3")


class RoundTripFidelityTests(unittest.TestCase):
    """Records must come back out of the store exactly as they went in."""

    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.scenario = fx["scenario"](fx["bundle_with_clause"])

    def _round_trip(self, cls, obj):
        import json

        return rehydrate(cls, json.loads(canonical_json(obj)))

    def test_a_ticket_survives_serialisation_unchanged(self):
        ticket = self.scenario.ticket("V3")
        self.assertEqual(ticket, self._round_trip(VerificationTicket, ticket))

    def test_a_ticket_with_no_forbidden_identity_survives(self):
        ticket = self.scenario.ticket("V3", forbidden_identity=None)
        self.assertEqual(ticket, self._round_trip(VerificationTicket, ticket))

    def test_a_report_with_nested_failure_observations_survives(self):
        report = self.scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(
                FailureObservation(
                    signature="freeze_pane_mismatch",
                    transient_code="IO_TIMEOUT",
                    base_result="PASS",
                    repair_paths=("tests/golden/x.json",),
                ),
            ),
        )
        self.assertEqual(report, self._round_trip(VerificationReportFixture, report))

    def test_a_clean_pass_report_survives(self):
        report = self.scenario.report("V0")
        self.assertEqual(report, self._round_trip(VerificationReportFixture, report))


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        fx = fx_ledger_freeze_pane()
        self.fx = fx
        self.scenario = fx["scenario"](fx["bundle_with_clause"])
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "ephemeral-manager-home"
        self.home.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.controller = VerificationController(self.home)

    def _issue(self, gates=LEDGER_GATES, round_=1):
        return self.controller.issue_round(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            gate_ids=gates,
            round_=round_,
            issued_by=self.scenario.controller,
            expected_checker_identity=self.scenario.checker,
            candidate_head_at_issue=self.scenario.execution.candidate_sha,
            worktree_lock_id="repo-abc",
            worktree_generation=1,
            issued_at="2026-09-06T00:00:00Z",
        )

    def _derive(self, **kwargs):
        return self.controller.derive(
            task=self.scenario.task,
            execution=self.scenario.execution,
            bundle=self.scenario.bundle,
            preflight=self.scenario.preflight,
            **kwargs,
        )

    def test_a_full_honest_round_derives_accepted_from_disk(self):
        self._issue()
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        result = self._derive()
        self.assertEqual("ACCEPTED", result.acceptance_state, result.admitted_failure_classes)
        self.assertEqual((), result.invalidated_report_reasons)

    def test_reports_submitted_without_a_round_being_issued_do_not_count(self):
        # Ticket-before-report is the ordering the whole admission model rests
        # on, so it has to hold across the store boundary too.
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        result = self._derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertEqual(
            {"NO_MATCHING_TICKET"},
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_the_controller_binds_forbidden_identity_to_the_actual_executor(self):
        # Not a caller-supplied parameter: who executed is a fact about the
        # work, so an executor cannot nominate itself as an acceptable checker.
        tickets = self._issue()
        for ticket in tickets:
            self.assertEqual(self.scenario.executor, ticket.forbidden_identity)

    def test_an_executor_answering_its_own_ticket_is_rejected_end_to_end(self):
        self._issue()
        for gate in LEDGER_GATES:
            self.controller.submit(
                self.scenario.report(gate, producer_identity=self.scenario.executor)
            )
        result = self._derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)

    def test_a_failing_report_cannot_be_replaced_by_a_passing_one(self):
        # Content addressing means both survive, so the derivation still sees
        # the failure. Overwriting a verdict is not an operation on offer.
        self._issue()
        failing = self.scenario.report(
            "V3",
            result="FAIL",
            failure_observations=(FailureObservation(signature="freeze_pane_mismatch"),),
        )
        self.controller.submit(failing)
        for gate in ("V0", "V1", "V2"):
            self.controller.submit(self.scenario.report(gate))
        self.controller.submit(self.scenario.report("V3"))
        result = self._derive()
        self.assertNotEqual("ACCEPTED", result.acceptance_state)
        self.assertIn(
            "DUPLICATE_REPORT_FOR_TICKET",
            {reason for _key, reason in result.invalidated_report_reasons},
        )

    def test_reissuing_the_same_round_is_idempotent(self):
        first = self._issue()
        second = self._issue()
        self.assertEqual(first, second)
        self.assertEqual(len(LEDGER_GATES), len(self.controller.tickets.list_ids()))

    def test_a_conflicting_ticket_for_the_same_round_is_refused(self):
        self._issue()
        with self.assertRaises(stores.VerificationStoreError):
            self.controller.issue_round(
                task=self.scenario.task,
                execution=self.scenario.execution,
                bundle=self.scenario.bundle,
                gate_ids=("V0",),
                round_=1,
                issued_by=self.scenario.controller,
                expected_checker_identity=self.scenario.controller,  # different checker
                candidate_head_at_issue=self.scenario.execution.candidate_sha,
                worktree_lock_id="repo-abc",
                worktree_generation=1,
                issued_at="2026-09-06T00:00:00Z",
            )

    def test_reports_for_another_execution_are_not_collected(self):
        self._issue()
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        self.controller.submit(
            self.scenario.report("V0", execution_id="E-SOMEONE-ELSE")
        )
        collected = self.controller.stored_reports(self.scenario.execution.execution_id)
        self.assertEqual(len(LEDGER_GATES), len(collected))

    def test_derivation_is_stable_across_store_read_order(self):
        self._issue()
        for gate in LEDGER_GATES:
            self.controller.submit(self.scenario.report(gate))
        first = self._derive()
        second = self._derive()
        self.assertEqual(first, second)


class ProductionBoundaryIntegrationTests(unittest.TestCase):
    def test_a_controller_cannot_be_opened_on_a_marked_production_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "prod-home"
            home.mkdir()
            (home / ".adm-production-runtime.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(stores.ProductionStoreRefused):
                VerificationController(home)

    def test_a_controller_cannot_be_opened_on_the_canonical_manager_home(self):
        with tempfile.TemporaryDirectory() as root:
            canonical = Path(root) / ".ai-development-manager"
            canonical.mkdir()
            with mock.patch.object(
                stores.manager_home, "canonical_manager_home", return_value=canonical
            ):
                with self.assertRaises(stores.ProductionStoreRefused):
                    VerificationController(canonical)

    def test_refusal_happens_before_any_directory_is_created(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "prod-home"
            home.mkdir()
            (home / ".adm-production-runtime.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(stores.ProductionStoreRefused):
                VerificationController(home)
            self.assertFalse((home / "verification").exists())

    def test_a_production_risk_execution_never_reaches_accepted_through_the_controller(self):
        fx = fx_adm_false_dispatch()
        scenario = fx["production"]
        with tempfile.TemporaryDirectory() as root:
            controller = VerificationController(Path(root) / "home")
            controller.issue_round(
                task=scenario.task,
                execution=scenario.execution,
                bundle=scenario.bundle,
                gate_ids=("V0", "V1"),
                round_=1,
                issued_by=scenario.controller,
                expected_checker_identity=scenario.checker,
                candidate_head_at_issue=scenario.execution.candidate_sha,
                worktree_lock_id="repo-abc",
                worktree_generation=1,
                issued_at="2026-09-06T00:00:00Z",
            )
            for gate in ("V0", "V1"):
                controller.submit(scenario.report(gate))
            result = controller.derive(
                task=scenario.task,
                execution=scenario.execution,
                bundle=scenario.bundle,
                preflight=scenario.preflight,
            )
        self.assertEqual("ROUTE_TO_PFP", result.next_action)
        self.assertEqual("DEFERRED_TO_PFP", result.acceptance_state)
        # Single-factor: every report was admissible, so the routing is
        # production risk and not an incidental lineage rejection.
        self.assertEqual((), result.invalidated_report_reasons)


class NoSecondTruthSystemTests(unittest.TestCase):
    """Architectural guards, asserted rather than left to review."""

    def test_no_acceptance_state_is_writable_on_any_input_fixture(self):
        from manager.verification_loop import models

        for name in ("TaskFixture", "ExecutionFixture", "VerificationReportFixture",
                     "VerificationTicket", "AcceptanceBundleFixture"):
            fields = set(getattr(models, name).__dataclass_fields__)
            self.assertNotIn("acceptance_state", fields, name)
            self.assertNotIn("accepted", fields, name)

    def test_the_package_declares_no_loop_run_entity(self):
        from manager import verification_loop

        import pkgutil

        for module in pkgutil.iter_modules(verification_loop.__path__):
            self.assertNotIn("loop_run", module.name)

    def _imported_modules(self, path):
        """Every module name this file imports, from the AST rather than text.

        Parsing beats substring matching here: a docstring that *names*
        acceptance_gate to explain why it is not used would trip a text search,
        and a lazily imported module inside a function would evade a naive one.
        """
        import ast

        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = ("." * node.level) + (node.module or "")
                names.add(base)
                names.update(base + "." + alias.name for alias in node.names)
        return names

    def test_the_package_never_imports_the_unrelated_acceptance_gate(self):
        # CONTROLLED_ACCEPTANCE_GATE is a provider-unavailable reroute and has
        # nothing to do with this; Phase A v3 forbids reusing it.
        from manager import verification_loop

        root = Path(verification_loop.__path__[0])
        for path in root.glob("*.py"):
            for name in self._imported_modules(path):
                self.assertNotIn("acceptance_gate", name, f"{path.name} imports {name}")

    def test_the_package_reuses_adm_primitives_rather_than_reimplementing_them(self):
        # The production boundary must come from manager.production_guard and
        # manager.manager_home, not from a private idea of what production is.
        root = Path(__file__).parent / "verification_loop"
        imported = self._imported_modules(root / "stores.py")
        self.assertTrue(
            any("production_guard" in name for name in imported), imported
        )
        self.assertTrue(any("manager_home" in name for name in imported), imported)

    def test_only_the_store_layer_performs_io_or_reads_a_clock(self):
        """The derivation has to stay pure, or it stops being reproducible.

        Checked structurally rather than by convention: a clock or an
        environment read anywhere in the derivation path would make the same
        inputs able to produce two different acceptances.
        """
        import ast

        root = Path(__file__).parent / "verification_loop"
        allowed = {"stores.py", "controller.py"}
        forbidden_modules = {"os", "io", "pathlib", "time", "datetime", "random", "subprocess"}
        for path in sorted(root.glob("*.py")):
            if path.name in allowed:
                continue
            for name in self._imported_modules(path):
                self.assertNotIn(
                    name.split(".")[0], forbidden_modules, f"{path.name} imports {name}"
                )
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual("open", node.func.id, path.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
