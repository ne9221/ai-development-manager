"""Static guard: every ``AgRunner(...)`` built in a test injects an IDE bridge, or opts into the live IDE.

Prevention, not detection of a present defect. The runtime fence
(``manager/ag_live_fence.py``) makes a bare ``AgRunner(cli_runner=...)``
harmless, but harmless-by-fence is still a defect: the test silently
exercises the ``live_ide_not_found`` fallback instead of the path it names,
and under pytest it now fails loudly at teardown. This guard catches the
construction itself, before the test is ever run, in every ``test_*.py`` of
the repository, so a future test cannot repeat the f4cf5cb hole under any
runner.

Rules, applied to every call whose callee resolves to ``manager.ag_runner
.AgRunner`` (direct import, ``import ... as`` alias, module-qualified
attribute):

* ``ide_bridge=<expr>`` must be present and must not be the literal
  ``None``;
* ``**kwargs`` splats are rejected -- the guard cannot prove what they carry;
* a call inside a function or class decorated ``pytest.mark.live_antigravity``
  is exempt (explicit live opt-in).

This is deliberately not a whole-program analysis: a runner obtained
through ``PROVIDER_RUNTIMES[...]["launcher_factory"]()`` or any other
indirection is the runtime fence's job. Sources are decoded as
``utf-8-sig`` and an unreadable or unparseable test module FAILS the guard
instead of vanishing from it. The negative controls below prove each rule
bites.
"""

import ast
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
RUNNER_MODULE = "manager.ag_runner"
RUNNER_CLASS = "AgRunner"
LIVE_MARKER = "live_antigravity"


def _decorated_live(node):
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == LIVE_MARKER:
            return True
    return False


class _Analyzer(ast.NodeVisitor):
    def __init__(self):
        self.runner_names = set()      # bare names bound to AgRunner
        self.module_aliases = set()    # names bound to the manager.ag_runner module
        self.findings = []
        self._live_depth = 0

    # -- bindings --------------------------------------------------------
    def visit_ImportFrom(self, node):
        if node.module == RUNNER_MODULE:
            for alias in node.names:
                if alias.name == RUNNER_CLASS:
                    self.runner_names.add(alias.asname or alias.name)
        elif node.module == RUNNER_MODULE.rsplit(".", 1)[0]:
            for alias in node.names:
                if alias.name == RUNNER_MODULE.rsplit(".", 1)[1]:
                    self.module_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name == RUNNER_MODULE:
                self.module_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    # -- scopes ----------------------------------------------------------
    def _scoped(self, node):
        live = _decorated_live(node)
        self._live_depth += live
        self.generic_visit(node)
        self._live_depth -= live

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    # -- calls -----------------------------------------------------------
    def _is_runner(self, func):
        if isinstance(func, ast.Name):
            return func.id in self.runner_names
        if isinstance(func, ast.Attribute) and func.attr == RUNNER_CLASS:
            value = func.value
            if isinstance(value, ast.Name):
                return value.id in self.module_aliases
            # manager.ag_runner.AgRunner spelled out in full
            return ast.unparse(value) == RUNNER_MODULE
        return False

    def visit_Call(self, node):
        if self._is_runner(node.func) and self._live_depth == 0:
            problem = None
            if any(keyword.arg is None for keyword in node.keywords):
                problem = "**kwargs splat: the guard cannot prove an ide_bridge is injected"
            else:
                bridge = next((keyword for keyword in node.keywords if keyword.arg == "ide_bridge"), None)
                if bridge is None:
                    problem = "no ide_bridge= keyword: the runner would auto-discover the live IDE"
                elif isinstance(bridge.value, ast.Constant) and bridge.value.value is None:
                    problem = "ide_bridge=None is the same as omitting it"
            if problem:
                self.findings.append((node.lineno, problem))
        self.generic_visit(node)


def analyze_source(source, label="<snippet>"):
    tree = ast.parse(source, filename=label)
    analyzer = _Analyzer()
    analyzer.visit(tree)
    return analyzer.findings


def iter_test_modules():
    for path in sorted(REPO_ROOT.rglob("test_*.py")):
        if SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        yield path


class ConstructionGuardTests(unittest.TestCase):
    def test_every_agrunner_built_in_a_test_injects_an_ide_bridge_or_opts_into_live(self):
        examined, violations = [], []
        for path in iter_test_modules():
            relative = path.relative_to(REPO_ROOT).as_posix()
            try:
                source = path.read_text(encoding="utf-8-sig")
                findings = analyze_source(source, relative)
            except (OSError, SyntaxError, ValueError) as exc:
                violations.append(f"{relative}: could not be audited ({exc})")
                continue
            examined.append(relative)
            violations.extend(f"{relative}:{line}: AgRunner(...) {problem}" for line, problem in findings)
        self.assertIn("manager/test_ag_execution.py", examined)
        self.assertIn("manager/test_ag_runner.py", examined)
        self.assertIn(Path(__file__).relative_to(REPO_ROOT).as_posix(), examined)
        self.assertGreater(len(examined), 90, examined)
        self.assertEqual([], violations, "\n".join(violations))


class NegativeControlTests(unittest.TestCase):
    """Each rule must bite, or the guard above is decorative."""

    def findings(self, body):
        return analyze_source(textwrap.dedent(body))

    def test_bare_construction_is_a_violation(self):
        self.assertEqual(1, len(self.findings("""
            from manager.ag_runner import AgRunner
            def test_x():
                launcher = AgRunner(cli_runner=object())
        """)))

    def test_none_bridge_is_a_violation(self):
        self.assertIn("ide_bridge=None", self.findings("""
            from manager.ag_runner import AgRunner
            launcher = AgRunner(ide_bridge=None, cli_runner=object())
        """)[0][1])

    def test_kwargs_splat_is_a_violation(self):
        self.assertIn("splat", self.findings("""
            from manager.ag_runner import AgRunner
            launcher = AgRunner(**dict(cli_runner=object()))
        """)[0][1])

    def test_import_alias_and_module_attribute_forms_are_seen(self):
        self.assertEqual(3, len(self.findings("""
            from manager.ag_runner import AgRunner as Runner
            import manager.ag_runner as ar
            from manager import ag_runner
            a = Runner()
            b = ar.AgRunner()
            c = ag_runner.AgRunner(cli_runner=1)
        """)))

    def test_injected_bridge_is_accepted(self):
        self.assertEqual([], self.findings("""
            from unittest.mock import MagicMock
            from manager.ag_runner import AgRunner
            a = AgRunner(ide_bridge=MagicMock())
            dead = object()
            b = AgRunner(ide_bridge=dead, cli_runner=object())
        """))

    def test_live_marked_function_and_class_are_exempt(self):
        self.assertEqual([], self.findings("""
            import pytest
            from manager.ag_runner import AgRunner
            @pytest.mark.live_antigravity
            def test_live():
                AgRunner()
            @pytest.mark.live_antigravity
            class LiveTests:
                def test_it(self):
                    AgRunner(cli_runner=None)
        """))

    def test_other_markers_do_not_exempt(self):
        self.assertEqual(1, len(self.findings("""
            import pytest
            from manager.ag_runner import AgRunner
            @pytest.mark.slow
            def test_live():
                AgRunner()
        """)))

    def test_unrelated_calls_named_agrunner_elsewhere_are_ignored(self):
        self.assertEqual([], self.findings("""
            from somewhere import AgRunner
            AgRunner()
        """))


if __name__ == "__main__":
    unittest.main()
