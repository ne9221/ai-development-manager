"""Static audit: nothing in this repository writes the Phase-1 cursor except its mutation path.

Prevention, not detection of a present defect: the repo audit finds no
rogue writer, but a line-oriented check would have missed a writer that
spelled the path over two lines, used ``open(..., "w")``, renamed
through ``Path.rename``, lived in a nested module or a root-level
script, or built the path into a variable and wrote through the alias
later.

This is deliberately NOT a complete static analyzer. It recognises the
sink families in ``SINK_ATTRS`` / ``SINK_FUNCS`` and associates them
with the cursor by tracking which expressions *evaluate to* the cursor
path. That tracking is lexically scoped, because Python is: a nested
function sees the names its enclosing function bound, and an earlier
version of this audit did not model that. A rogue write planted inside
``poll_once``'s own ``_persist_cursor`` closure -- the most natural
place in the entire repository for one to appear -- went undetected,
along with a module-level path constant, a ``self.cursor`` attribute, a
helper that returns the path, and a path held in a container. Each of
those is a negative control below.

Derivation is tracked separately from mere reference on purpose:
``load_phase1_cursor(cursor_path=p)`` *mentions* the cursor but returns
a dict, and treating that dict as a path floods the scope with false
positives (it made this audit fail on ``command_watcher.py``).
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
              "touch", "symlink_to", "hardlink_to", "truncate"}
# Function-style sinks reached through a module: os.rename(...), shutil.move(...)
SINK_FUNCS = {("os", "rename"), ("os", "replace"), ("os", "remove"), ("os", "unlink"), ("os", "link"),
              ("os", "truncate"), ("os", "mkdir"), ("os", "makedirs"),
              ("shutil", "move"), ("shutil", "copyfile"), ("shutil", "copy"), ("shutil", "copy2")}
WRITE_MODE_CHARS = set("wax+")
PATH_SOURCE_FUNCS = {"_resolve_cursor_path", "bind_phase1_cursor_path"}
# Calls whose RESULT is path-shaped when an argument or receiver is.
PATH_BUILDERS = {"Path", "PurePath", "PureWindowsPath", "PurePosixPath", "WindowsPath", "PosixPath",
                 "str", "fspath", "join", "abspath", "realpath", "normpath", "expanduser", "format"}
PATH_METHODS = {"with_name", "with_suffix", "with_stem", "joinpath", "resolve", "absolute", "expanduser",
                "parent", "parents", "name", "format", "strip", "rstrip", "lstrip", "replace", "lower"}


class _Context:
    """Whole-module derivation facts shared by every scope.

    ``returns`` -- functions whose return value is a cursor path, so
    ``cursor_for(home).write_text(...)`` is caught.
    ``attrs`` -- attribute names ever assigned a cursor path, so
    ``self.cursor = <path>`` in one method and ``self.cursor.unlink()``
    in another are connected.
    """

    __slots__ = ("returns", "attrs")

    def __init__(self):
        self.returns = set()
        self.attrs = set()


_EMPTY = _Context()


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


def _refers_to(node, aliases, ctx=_EMPTY):
    """Does this expression mention the cursor literal, an alias, or a path source?"""
    if _mentions_literal(node):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in aliases:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in ctx.attrs:
            return True
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in PATH_SOURCE_FUNCS or name in ctx.returns:
                return True
    return False


def _derives_path(node, aliases, ctx=_EMPTY):
    """Does this expression EVALUATE TO something built from the cursor path?

    Narrower than :func:`_refers_to` on purpose -- see the module
    docstring on why a loader's return value must not propagate.
    """
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and CURSOR_LITERAL in node.value
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in ctx.attrs or _derives_path(node.value, aliases, ctx)
    if isinstance(node, ast.Subscript):
        return _derives_path(node.value, aliases, ctx)
    if isinstance(node, ast.BinOp):
        return (_derives_path(node.left, aliases, ctx)
                or _derives_path(node.right, aliases, ctx))
    if isinstance(node, ast.JoinedStr):
        return any(_derives_path(v.value, aliases, ctx)
                   for v in node.values if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.IfExp):
        return (_derives_path(node.body, aliases, ctx)
                or _derives_path(node.orelse, aliases, ctx))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_derives_path(e, aliases, ctx) for e in node.elts)
    if isinstance(node, ast.Dict):
        return any(_derives_path(v, aliases, ctx) for v in node.values)
    if isinstance(node, ast.NamedExpr):
        return _derives_path(node.value, aliases, ctx)
    if isinstance(node, ast.Await):
        return _derives_path(node.value, aliases, ctx)
    if isinstance(node, ast.Call):
        func = node.func
        operands = list(node.args) + [k.value for k in node.keywords]
        if isinstance(func, ast.Name):
            if func.id in PATH_SOURCE_FUNCS or func.id in ctx.returns:
                return True
            if func.id in PATH_BUILDERS:
                return any(_derives_path(a, aliases, ctx) for a in operands)
            return False
        if isinstance(func, ast.Attribute):
            if func.attr in PATH_SOURCE_FUNCS or func.attr in ctx.returns:
                return True
            if func.attr in PATH_BUILDERS or func.attr in PATH_METHODS:
                return (_derives_path(func.value, aliases, ctx)
                        or any(_derives_path(a, aliases, ctx) for a in operands))
        return False
    return False


def _split_targets(target):
    """(plain names, attribute names) bound by one assignment target."""
    names, attrs = [], []
    stack = [target]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.append(node.attr)
        elif isinstance(node, (ast.Tuple, ast.List)):
            stack.extend(node.elts)
        elif isinstance(node, ast.Starred):
            stack.append(node.value)
    return names, attrs


def _target_names(target):
    names, attrs = _split_targets(target)
    return names + attrs


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


def _child_scopes(scope):
    """The scopes defined directly inside ``scope``, at any statement depth.

    Does not descend into them, so what comes back are immediate
    children -- a function nested three ``if`` blocks deep still belongs
    to this scope, but a function inside one of those functions does not.
    """
    out = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _scope_parents(tree):
    """{scope: immediately enclosing scope}, so aliases inherit lexically."""
    parents = {tree: None}
    stack = [tree]
    while stack:
        scope = stack.pop()
        for child in _child_scopes(scope):
            parents[child] = scope
            stack.append(child)
    return parents


def _collect_aliases(tree, seed=()):
    """Per scope: names that (transitively) hold something built from the cursor path.

    Returns ``({scope: names}, {scope: nodes})``. The name sets are
    *effective*: each already includes everything its enclosing scopes
    bound, because a nested function reads those names too. An alias
    passed as an argument to a same-file function taints that function's
    parameter, and a function that returns a cursor path makes its own
    call sites derive one, so the fixpoint runs until nothing changes.
    """
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    parents = _scope_parents(tree)
    scope_list = list(parents)
    bodies = {scope: _scope_nodes(scope) for scope in scope_list}
    own = {scope: set(seed) for scope in scope_list}
    ctx = _Context()

    def effective(scope):
        names = set()
        cursor = scope
        while cursor is not None:
            names |= own[cursor]
            cursor = parents.get(cursor)
        return names

    while True:
        changed = False
        for scope in scope_list:
            names = effective(scope)
            added, added_attrs = set(), set()
            for node in bodies[scope]:
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    if _derives_path(node.value, names, ctx):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            plain, attrs = _split_targets(target)
                            added.update(plain)
                            added_attrs.update(attrs)
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None and _derives_path(item.context_expr, names, ctx):
                            plain, attrs = _split_targets(item.optional_vars)
                            added.update(plain)
                            added_attrs.update(attrs)
                elif isinstance(node, ast.For):
                    if _derives_path(node.iter, names, ctx):
                        plain, attrs = _split_targets(node.target)
                        added.update(plain)
                        added_attrs.update(attrs)
                elif isinstance(node, ast.Return):
                    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                            _derives_path(node.value, names, ctx) and scope.name not in ctx.returns:
                        ctx.returns.add(scope.name)
                        changed = True
                elif isinstance(node, ast.Call):
                    func_name = node.func.id if isinstance(node.func, ast.Name) else None
                    function = functions.get(func_name)
                    if function is not None:
                        params = [a.arg for a in function.args.posonlyargs + function.args.args]
                        tainted = set()
                        for index, arg in enumerate(node.args):
                            if index < len(params) and _derives_path(arg, names, ctx):
                                tainted.add(params[index])
                        for keyword in node.keywords:
                            if keyword.arg in params and _derives_path(keyword.value, names, ctx):
                                tainted.add(keyword.arg)
                        if tainted - own[function]:
                            own[function] |= tainted
                            changed = True
            if added - own[scope]:
                own[scope] |= added
                changed = True
            if added_attrs - ctx.attrs:
                ctx.attrs |= added_attrs
                changed = True
        if not changed:
            return {scope: effective(scope) for scope in scope_list}, bodies, ctx


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


def _os_open_is_writable(call):
    """os.open(path, flags): writable unless the flags are provably read-only."""
    flags = call.args[1] if len(call.args) >= 2 else None
    for keyword in call.keywords:
        if keyword.arg == "flags":
            flags = keyword.value
    if flags is None:
        return True
    names = {sub.attr for sub in ast.walk(flags) if isinstance(sub, ast.Attribute)}
    if not names:
        return True
    return not names <= {"O_RDONLY"}


def audit_source(source, label, aliases_seed=()):
    """Return the sorted list of (label, lineno, kind) cursor-writer findings in one module."""
    tree = ast.parse(source, filename=label)
    if not _mentions_literal(tree) and not any(
            isinstance(n, ast.Name) and n.id in PATH_SOURCE_FUNCS for n in ast.walk(tree)):
        return []
    aliases, bodies, ctx = _collect_aliases(tree, aliases_seed)
    findings = set()
    for scope, nodes in bodies.items():
        names = aliases[scope]
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            args = list(node.args) + [k.value for k in node.keywords]
            touches = any(_refers_to(a, names, ctx) for a in args)
            if isinstance(func, ast.Name):
                if func.id == "open" and touches and _is_write_mode(_open_mode(node)):
                    findings.add((label, node.lineno, "open(write-mode)"))
                elif func.id in {"rename", "replace", "remove", "unlink", "move", "truncate"} and touches:
                    findings.add((label, node.lineno, f"{func.id}()"))
            elif isinstance(func, ast.Attribute):
                base = _base_name(func.value)
                receiver = _derives_path(func.value, names, ctx)
                if (base, func.attr) == ("os", "open"):
                    if touches and _os_open_is_writable(node):
                        findings.add((label, node.lineno, "os.open(write-flags)"))
                elif (base, func.attr) in SINK_FUNCS and touches:
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

    def test_the_watchers_bound_cursor_path_is_tracked_into_its_persist_closure(self):
        """The regression that motivated lexical scoping.

        ``_persist_cursor`` is a closure that uses ``phase1_cursor_path``
        from ``poll_once``. If the audit cannot see that name inside the
        closure, the single most likely place in the repo for a rogue
        cursor write is invisible to it.
        """
        source = (REPO_ROOT / "manager" / "command_watcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        aliases, _, _ = _collect_aliases(tree)
        closures = [s for s in aliases if isinstance(s, ast.FunctionDef) and s.name == "_persist_cursor"]
        self.assertTrue(closures, "_persist_cursor closure not found")
        for closure in closures:
            self.assertIn("phase1_cursor_path", aliases[closure],
                          "the bound cursor path is invisible inside the persist closure")
        self.assertEqual([], audit_source(source, "manager/command_watcher.py"))

    def test_a_rogue_write_inside_that_closure_is_detected(self):
        """Live negative control against the real production file."""
        source = (REPO_ROOT / "manager" / "command_watcher.py").read_text(encoding="utf-8")
        anchor = '        cursor["per_project_attention_visits"] = attention_visits\n'
        self.assertIn(anchor, source)
        rogue = source.replace(
            anchor, anchor + '        phase1_cursor_path.write_text("{}", encoding="utf-8")\n', 1)
        findings = audit_source(rogue, "manager/command_watcher.py")
        self.assertTrue(findings, "a rogue write inside _persist_cursor was not detected")
        self.assertIn(".write_text()", {kind for _, _, kind in findings})


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
    # --- the six bypasses an adversarial re-review of round 3 found ---
    "nested/closure_writer.py": '''
        from pathlib import Path

        def outer(home):
            cursor = Path(home) / "runtime" / "phase1-cursor.json"

            def inner():
                cursor.write_text("{}")
            inner()
    ''',
    "nested/module_constant.py": '''
        import os
        from pathlib import Path

        CURSOR = Path(os.environ["AI_MANAGER_HOME"]) / "runtime" / "phase1-cursor.json"

        def wipe():
            CURSOR.unlink()
    ''',
    "nested/class_attribute.py": '''
        from pathlib import Path

        class Rotator:
            def __init__(self, home):
                self.cursor = Path(home) / "runtime" / "phase1-cursor.json"

            def reset(self):
                self.cursor.write_text("{}")
    ''',
    "nested/helper_return.py": '''
        from pathlib import Path

        def cursor_for(home):
            return Path(home) / "runtime" / "phase1-cursor.json"

        def reset(home):
            cursor_for(home).write_text("{}")
    ''',
    "nested/container_indirection.py": '''
        from pathlib import Path

        def reset(home):
            paths = {"cursor": Path(home) / "runtime" / "phase1-cursor.json"}
            paths["cursor"].write_text("{}")
    ''',
    "nested/os_truncate.py": '''
        import os

        def zap(home):
            os.truncate(os.path.join(home, "runtime", "phase1-cursor.json"), 0)
    ''',
    "nested/os_open_write.py": '''
        import os

        def reset(home):
            target = os.path.join(home, "runtime", "phase1-cursor.json")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            os.write(fd, b'{"generation": 0}')
            os.close(fd)
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
    "nested/readonly_os_open.py": '''
        import os

        def read(home):
            fd = os.open(os.path.join(home, "runtime", "phase1-cursor.json"), os.O_RDONLY)
            try:
                return os.read(fd, 4096)
            finally:
                os.close(fd)
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
        for expected in [
            ("nested/multiline_open_w.py", "open(write-mode)"),
            ("root_script.py", ".write_text()"),
            ("nested/deeper/path_rename.py", ".rename()"),
            ("nested/os_replace_alias.py", "os.replace()"),
            ("nested/param_flow.py", ".write_bytes()"),
            ("nested/shutil_move.py", "shutil.move()"),
            ("nested/os_remove.py", "os.remove()"),
            ("nested/unlink_alias.py", ".unlink()"),
            ("nested/bound_path_writer.py", ".write_text()"),
            ("nested/closure_writer.py", ".write_text()"),
            ("nested/module_constant.py", ".unlink()"),
            ("nested/class_attribute.py", ".write_text()"),
            ("nested/helper_return.py", ".write_text()"),
            ("nested/container_indirection.py", ".write_text()"),
            ("nested/os_truncate.py", "os.truncate()"),
            ("nested/os_open_write.py", "os.open(write-flags)"),
        ]:
            self.assertIn(expected, kinds)

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
