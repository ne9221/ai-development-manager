"""Static audit: nothing in this repository writes the Phase-1 cursor except its mutation path.

Prevention, not detection of a present defect: the repo audit found no
rogue writer, but the previous check (a grep for call sites) would have
missed a writer that spelled the path over two lines, used ``open(...,
"w")``, renamed through ``Path.rename``, lived in a nested module or a
root-level script, or built the path into a variable and wrote through
the alias later.

This is deliberately NOT a complete static analyzer. It recognises the
dangerous sink families listed in ``SINK_ATTRS`` / ``SINK_FUNCS`` and
associates them with the cursor by (a) the ``phase1-cursor`` literal
anywhere in the sink's arguments, (b) a name in the same scope assigned
from a path-shaped expression that mentions the literal (transitively),
or (c) a function parameter that such a name is passed into. The
negative controls below are the contract: each synthetic rogue writer
must be caught, and each innocent module must not be.
"""

import ast
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CURSOR_LITERAL = "phase1-cursor"
SANCTIONED = ("manager/phase1_cursor.py",)
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}

# Method-style sinks: <receiver>.<attr>(...)
SINK_ATTRS = {"write_text", "write_bytes", "rename", "replace", "unlink", "remove", "move", "link",
              "touch", "symlink_to", "hardlink_to"}
# Function-style sinks reached through a module: os.rename(...), shutil.move(...)
SINK_FUNCS = {("os", "rename"), ("os", "replace"), ("os", "remove"), ("os", "unlink"), ("os", "link"),
              ("shutil", "move"), ("shutil", "copyfile"), ("shutil", "copy"), ("shutil", "copy2")}
WRITE_MODE_CHARS = set("wax+")
PATH_SOURCE_FUNCS = {"_resolve_cursor_path", "bind_phase1_cursor_path"}
# Calls whose RESULT is path-shaped when an argument/receiver is.
PATH_BUILDERS = {"Path", "PurePath", "PureWindowsPath", "PurePosixPath", "WindowsPath", "PosixPath",
                 "str", "fspath", "join", "abspath", "realpath", "normpath", "expanduser", "format"}
PATH_METHODS = {"with_name", "with_suffix", "with_stem", "joinpath", "resolve", "absolute", "expanduser",
                "parent", "parents", "name", "format", "strip", "rstrip", "lstrip", "replace", "lower"}


def _mentions_literal(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and CURSOR_LITERAL in sub.value:
            return True
    return False


def _base_name(node):
    """The root Name of an attribute/subscript/call chain, or None."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return None


def _refers_to(node, aliases):
    """Does this expression mention the cursor literal, an alias, or a path-source call?"""
    if _mentions_literal(node):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in aliases:
            return True
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in PATH_SOURCE_FUNCS:
                return True
    return False


def _derives_path(node, aliases):
    """Does this expression EVALUATE TO something built from the cursor path?

    Narrower than :func:`_refers_to`: ``load_cursor(cursor_path)`` refers
    to the path but evaluates to a dict, and tainting that dict would
    flood the scope with false aliases. Only path-shaped constructions
    propagate: the literal, an alias, attribute/method chains rooted in
    one, ``/`` and ``+`` joins, f-strings, and path builders such as
    ``Path(...)``, ``os.path.join(...)`` and ``str(...)``.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and CURSOR_LITERAL in node.value
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _derives_path(node.value, aliases)
    if isinstance(node, ast.BinOp):
        return _derives_path(node.left, aliases) or _derives_path(node.right, aliases)
    if isinstance(node, ast.JoinedStr):
        return any(_derives_path(v.value, aliases) for v in node.values if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.IfExp):
        return _derives_path(node.body, aliases) or _derives_path(node.orelse, aliases)
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_derives_path(e, aliases) for e in node.elts)
    if isinstance(node, ast.NamedExpr):
        return _derives_path(node.value, aliases)
    if isinstance(node, ast.Call):
        func = node.func
        operands = list(node.args) + [k.value for k in node.keywords]
        if isinstance(func, ast.Name):
            if func.id in PATH_SOURCE_FUNCS:
                return True
            if func.id in PATH_BUILDERS:
                return any(_derives_path(a, aliases) for a in operands)
            return False
        if isinstance(func, ast.Attribute):
            if func.attr in PATH_SOURCE_FUNCS:
                return True
            if func.attr in PATH_BUILDERS or func.attr in PATH_METHODS:
                return _derives_path(func.value, aliases) or any(_derives_path(a, aliases) for a in operands)
        return False
    return False


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _scope_nodes(scope):
    """Every node in one scope body, not descending into nested scopes."""
    out = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _scopes(tree):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def _collect_aliases(tree, seed=()):
    """Per scope: names that (transitively) hold something built from the cursor path.

    Returns ``({scope: names}, {scope: nodes})``. An alias passed as an
    argument to a same-file function taints that function's parameter, so
    the fixpoint runs across scopes until nothing changes.
    """
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    scope_list = list(_scopes(tree))
    bodies = {scope: _scope_nodes(scope) for scope in scope_list}
    aliases = {scope: set(seed) for scope in scope_list}
    while True:
        changed = False
        for scope in scope_list:
            names = aliases[scope]
            for node in bodies[scope]:
                added = set()
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    if node.value is not None and _derives_path(node.value, names):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            added.update(_target_names(target))
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None and _derives_path(item.context_expr, names):
                            added.update(_target_names(item.optional_vars))
                elif isinstance(node, ast.For):
                    if _derives_path(node.iter, names):
                        added.update(_target_names(node.target))
                elif isinstance(node, ast.Call):
                    func_name = node.func.id if isinstance(node.func, ast.Name) else None
                    function = functions.get(func_name)
                    if function is not None:
                        params = [a.arg for a in function.args.posonlyargs + function.args.args]
                        tainted = set()
                        for index, arg in enumerate(node.args):
                            if index < len(params) and _derives_path(arg, names):
                                tainted.add(params[index])
                        for keyword in node.keywords:
                            if keyword.arg in params and _derives_path(keyword.value, names):
                                tainted.add(keyword.arg)
                        if tainted - aliases[function]:
                            aliases[function] |= tainted
                            changed = True
                if added - names:
                    names |= added
                    changed = True
        if not changed:
            return aliases, bodies


def _open_mode(call):
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return "r"
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value
    return "?"  # dynamic mode: treat as writable


def _is_write_mode(mode):
    return mode == "?" or bool(WRITE_MODE_CHARS & set(mode))


def audit_source(source, label, aliases_seed=()):
    """Return the sorted list of (label, lineno, kind) cursor-writer findings in one module."""
    tree = ast.parse(source, filename=label)
    if not _mentions_literal(tree) and not any(
            isinstance(n, ast.Name) and n.id in PATH_SOURCE_FUNCS for n in ast.walk(tree)):
        return []
    aliases, bodies = _collect_aliases(tree, aliases_seed)
    findings = set()
    for scope, nodes in bodies.items():
        names = aliases[scope]
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            args = list(node.args) + [k.value for k in node.keywords]
            touches = any(_refers_to(a, names) for a in args)
            if isinstance(func, ast.Name):
                if func.id == "open" and touches and _is_write_mode(_open_mode(node)):
                    findings.add((label, node.lineno, "open(write-mode)"))
                elif func.id in {"rename", "replace", "remove", "unlink", "move"} and touches:
                    findings.add((label, node.lineno, f"{func.id}()"))
            elif isinstance(func, ast.Attribute):
                base = _base_name(func.value)
                receiver = _derives_path(func.value, names)
                if (base, func.attr) in SINK_FUNCS and touches:
                    findings.add((label, node.lineno, f"{base}.{func.attr}()"))
                elif func.attr == "open":
                    if (touches or receiver) and _is_write_mode(_open_mode(node)):
                        findings.add((label, node.lineno, ".open(write-mode)"))
                elif func.attr in SINK_ATTRS and (
                        receiver or (func.attr in {"rename", "replace", "move", "link"} and touches)):
                    findings.add((label, node.lineno, f".{func.attr}()"))
    return sorted(findings)


def audit_tree(root, sanctioned=SANCTIONED):
    root = Path(root)
    findings = []
    examined = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            rel = path.relative_to(root).as_posix()
            if filename.startswith("test_") or filename == "conftest.py" or rel in sanctioned:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                found = audit_source(source, rel)
            except SyntaxError:
                continue
            examined.append(rel)
            findings.extend(found)
    return findings, examined


class RepositoryAuditTests(unittest.TestCase):

    def test_no_module_or_script_writes_the_cursor_outside_the_mutation_path(self):
        findings, examined = audit_tree(REPO_ROOT)
        self.assertGreater(len(examined), 50, "the audit did not walk the repository")
        self.assertIn("manager/command_watcher.py", examined)
        self.assertEqual([], findings, f"rogue Phase-1 cursor writers: {findings}")

    def test_the_sanctioned_path_is_recognised_as_a_writer(self):
        """Non-vacuous: pointed at the real mutation module, the audit flags it."""
        findings, _ = audit_tree(REPO_ROOT, sanctioned=())
        kinds = {kind for label, _, kind in findings if label == "manager/phase1_cursor.py"}
        self.assertTrue(kinds, "the audit cannot even see the sanctioned writer")
        self.assertTrue({"os.replace()", "os.link()"} & kinds, kinds)

    def test_the_watcher_is_cursor_aware_but_only_mutates_through_the_api(self):
        source = (REPO_ROOT / "manager" / "command_watcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        aliases, _ = _collect_aliases(tree)
        self.assertTrue(any("phase1_cursor_path" in names for names in aliases.values()),
                        "the bound cursor path in poll_once must be tracked as an alias")
        self.assertEqual([], audit_source(source, "manager/command_watcher.py"))


ROGUES = {
    "nested/multiline_open_w.py": '''
        import os
        from pathlib import Path

        def reset(home):
            target = (
                Path(home)
                / "runtime"
                / "phase1-cursor.json"
            )
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{}")
    ''',
    "root_script.py": '''
        import os
        from pathlib import Path
        cursor = Path(os.environ["AI_MANAGER_HOME"]) / "runtime" / "phase1-cursor.json"
        cursor.write_text('{"generation": 0}', encoding="utf-8")
    ''',
    "nested/deeper/path_rename.py": '''
        from pathlib import Path

        def rotate(home):
            p = Path(home, "runtime", "phase1-cursor.json")
            p.rename(p.with_suffix(".bak"))
    ''',
    "nested/os_replace_alias.py": '''
        import os, json, tempfile
        from pathlib import Path

        def rewrite(home, data):
            cursor = Path(home) / "runtime" / "phase1-cursor.json"
            target = cursor
            fd, tmp = tempfile.mkstemp(dir=str(target.parent))
            os.close(fd)
            Path(tmp).write_text(json.dumps(data))
            os.replace(tmp, str(target))
    ''',
    "nested/param_flow.py": '''
        from pathlib import Path

        def dump(destination, body):
            destination.write_bytes(body)

        def clobber(home):
            cursor = Path(home) / "runtime" / "phase1-cursor.json"
            dump(cursor, b"{}")
    ''',
    "nested/shutil_move.py": '''
        import shutil

        def restore(home, backup):
            shutil.move(backup, f"{home}/runtime/phase1-cursor.json")
    ''',
    "nested/os_remove.py": '''
        import os

        def wipe(home):
            os.remove(os.path.join(home, "runtime", "phase1-cursor.json"))
    ''',
    "nested/unlink_alias.py": '''
        from pathlib import Path

        def wipe(home):
            p = Path(home) / "runtime"
            cursor = p / "phase1-cursor.json"
            cursor.unlink()
    ''',
    "nested/bound_path_writer.py": '''
        from manager.phase1_cursor import bind_phase1_cursor_path

        def clobber():
            bound = bind_phase1_cursor_path()
            bound.write_text("{}")
    ''',
}

INNOCENT = {
    "nested/reader.py": '''
        import json
        from pathlib import Path

        def read(home):
            p = Path(home) / "runtime" / "phase1-cursor.json"
            return json.loads(p.read_text(encoding="utf-8"))

        def read_other(home):
            with open(Path(home) / "runtime" / "phase1-cursor.json", "r") as handle:
                return handle.read()
    ''',
    "nested/unrelated_writer.py": '''
        from pathlib import Path

        def write_log(home):
            (Path(home) / "logs" / "watcher.log").write_text("ok")
    ''',
    "nested/dict_not_path.py": '''
        from pathlib import Path
        from manager.phase1_cursor import load_phase1_cursor

        def tick(home):
            cursor_path = Path(home) / "runtime" / "phase1-cursor.json"
            cursor = load_phase1_cursor(cursor_path=cursor_path)
            stamp = cursor["updated_at"].replace("Z", "+00:00")
            pending = [1, 2]
            pending.remove(1)
            return stamp, pending
    ''',
}


class NegativeControlTests(unittest.TestCase):
    """Each synthetic rogue writer MUST be caught; each innocent module must not be."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="adm-cursor-writer-audit-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def plant(self, rel, body):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def test_every_synthetic_rogue_writer_is_detected(self):
        for rel, body in ROGUES.items():
            with self.subTest(rogue=rel):
                self.plant(rel, body)
                findings, _ = audit_tree(self.root, sanctioned=())
                self.assertTrue(any(label == rel for label, _, _ in findings),
                                f"{rel} was not detected; findings={findings}")
                (self.root / rel).unlink()

    def test_expected_sink_kinds_are_named(self):
        for rel, body in ROGUES.items():
            self.plant(rel, body)
        findings, _ = audit_tree(self.root, sanctioned=())
        kinds = {(label, kind) for label, _, kind in findings}
        self.assertIn(("nested/multiline_open_w.py", "open(write-mode)"), kinds)
        self.assertIn(("root_script.py", ".write_text()"), kinds)
        self.assertIn(("nested/deeper/path_rename.py", ".rename()"), kinds)
        self.assertIn(("nested/os_replace_alias.py", "os.replace()"), kinds)
        self.assertIn(("nested/param_flow.py", ".write_bytes()"), kinds)
        self.assertIn(("nested/shutil_move.py", "shutil.move()"), kinds)
        self.assertIn(("nested/os_remove.py", "os.remove()"), kinds)
        self.assertIn(("nested/unlink_alias.py", ".unlink()"), kinds)
        self.assertIn(("nested/bound_path_writer.py", ".write_text()"), kinds)

    def test_innocent_modules_are_not_flagged(self):
        for rel, body in INNOCENT.items():
            self.plant(rel, body)
        findings, examined = audit_tree(self.root, sanctioned=())
        self.assertEqual(sorted(INNOCENT), sorted(examined))
        self.assertEqual([], findings)

    def test_test_files_and_sanctioned_paths_are_skipped(self):
        self.plant("test_rogue.py", ROGUES["root_script.py"])
        self.plant("manager/phase1_cursor.py", ROGUES["root_script.py"])
        self.plant("manager/other.py", ROGUES["root_script.py"])
        findings, _ = audit_tree(self.root)
        self.assertEqual(["manager/other.py"], sorted({label for label, _, _ in findings}))


if __name__ == "__main__":
    unittest.main()
