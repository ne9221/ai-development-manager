"""Recovery-protocol regression suite for the Phase-1 cursor custody transaction.

Round 3. An independent review of the previous round drove four
reproductions, each of which is re-run here through the same code paths
the reviewer used (the primitive, and the real ``poll_once`` tick):

* **Upgrade double failure.** A valid generation-2458 cursor with no
  initialization record; publication fails AND the restore fails. The
  next tick saw "no cursor, no marker" and started over at generation 1,
  and a later attempt reused the fixed claim name and overwrote the only
  surviving copy of the original.
* **First-boot interruption.** A genuine first boot that recorded its
  marker and died before creating the cursor wedged permanently: every
  later tick saw "marker present, cursor absent" and refused.
* **External insert after custody.** Generation 2500 installed at the
  canonical name after the last check but before ``os.replace`` was
  clobbered by 2459, because ``os.replace`` overwrites unconditionally.
* **Whole-Watcher path hijack.** The tick resolved the cursor path on
  load and again on save, so a relative home plus a cwd change in
  between read the original and advanced a decoy.

The numbers 2458 / 13 / 2500 are the reviewer's.
"""

import ast
import contextlib
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import manager.phase1_cursor as pc
from manager.phase1_cursor import (
    CLAIM_INFIX,
    CREATE_ONLY,
    INIT_COMMITTED,
    INIT_PREPARED,
    INIT_STATE_SUFFIX,
    CursorInitStateError,
    CursorReadError,
    CursorRecoveryRequiredError,
    CursorStateError,
    StaleCursorError,
    bind_phase1_cursor_path,
    load_phase1_cursor,
    phase1_cursor_init_state,
    require_phase1_cursor_first_boot,
    save_phase1_cursor,
)
from manager.test_phase1_cursor_integrity import fail_canonical_install
from manager.test_phase1_cursor_writer_audit import (
    _Context,
    _collect_aliases,
    _fold_string_constants,
    sink_module_aliases,
)

PROD_13_PROJECTS = {f"proj-{i:02d}": 100 + i for i in range(13)}


# ---------------------------------------------------------------------------
# Static guard: nothing in the mutation module lands on the canonical
# cursor name except by a primitive that refuses an occupied destination.
#
# This is the same lesson as Blocker 5, applied to the invariant Blocker 3
# established. Its predecessor was a line-oriented check that read only
# lines starting with ``os.replace(`` and accepted any that mentioned a
# claim or the record, so a trailing ``# retires the claim`` comment
# re-opened the hole; and it never looked at ``os.rename``, which is
# non-replacing on Windows but replaces silently on POSIX, so an
# unguarded one is a live lost-update defect anywhere else while the
# check stays green. Both were demonstrated against the real module.
#
# The guard is scope- and alias-aware, so a comment, a line break, an
# alias, a wrapper such as ``str()``, or a root-level script cannot hide
# a violation. It also covers the truncating writes the writer audit
# deliberately cannot see here -- ``manager/phase1_cursor.py`` is that
# audit's sanctioned path, so this file is the only place a truncating
# overwrite inside it would ever be caught.
# ---------------------------------------------------------------------------

#: Sinks that install over an occupied destination without complaint anywhere.
ALWAYS_REPLACING_FUNCS = {("os", "replace"), ("shutil", "move"), ("shutil", "copyfile"),
                          ("shutil", "copy"), ("shutil", "copy2")}
#: Replacing on POSIX, non-replacing on Windows: permitted only under an
#: explicit ``os.name == "nt"`` guard, which is what makes it safe.
POSIX_REPLACING_FUNCS = {("os", "rename")}
#: Truncating function-form sinks: they do not install a new file, they
#: destroy the bytes already at the destination. ``os.truncate`` on the
#: canonical name loses the durable cursor just as completely as an
#: overwrite does, and this module never truncates anything.
TRUNCATING_FUNCS = {("os", "truncate")}
#: DELIBERATELY NOT a sink here: ``os.remove`` / ``os.unlink``.
#:
#: ``_publish_exclusively`` rolls back a canonical file THIS CALL just
#: created exclusively -- ``_discard(path)`` on the failure branch, behind
#: an ``os.path.samestat`` identity check that proves the file being
#: removed is the one just created and not a competitor's. Interprocedural
#: taint reaches ``_discard``'s parameter from that call site, so listing
#: unlink here would report correct rollback code as a violation on every
#: run. Deciding that branch safe needs the samestat check, which is far
#: outside a bounded AST model -- so the exclusion is recorded here and
#: pinned by a test rather than left as a silent gap. Deletion of the
#: canonical is covered by the protocol's own claim/rollback tests.
NOT_GUARDED_FUNCS = {("os", "remove"), ("os", "unlink")}
#: The modules this guard's function-form tables name. Derived, so adding a
#: sink from a new module cannot leave the alias resolver behind.
GUARDED_SINK_MODULES = {module for module, _ in
                        (ALWAYS_REPLACING_FUNCS | POSIX_REPLACING_FUNCS
                         | TRUNCATING_FUNCS | {("os", "open")})}
ALWAYS_REPLACING_METHODS = {"replace"}
POSIX_REPLACING_METHODS = {"rename"}
#: Writes that truncate whatever the destination already holds.
TRUNCATING_METHODS = {"write_text", "write_bytes"}
WRITE_MODE_CHARS = set("wax+")
#: The module's name for the one bound canonical cursor path. A SEED for
#: the taint model below, no longer the whole of it: an independent review
#: defeated the name-matching predecessor with five one-line spellings that
#: simply called the destination something else.
CANONICAL_NAME = "path"
#: Calls that wrap a path without naming a DIFFERENT file.
PATH_WRAPPERS = {"str", "Path", "fspath", "PurePath", "WindowsPath", "PosixPath"}
#: Calls that hand back the canonical cursor path.
PATH_SOURCES = {"_resolve_cursor_path", "bind_phase1_cursor_path"}

_EMPTY_CTX = _Context()


def _names_canonical(node, aliases, ctx=_EMPTY_CTX):
    """Does this expression evaluate to the canonical cursor file itself?

    A bounded taint model, not a name match. Taint originates at the bound
    canonical path (:data:`CANONICAL_NAME`) and at the resolvers in
    :data:`PATH_SOURCES`, and :func:`_collect_aliases` propagates it
    through assignment, argument-to-parameter, helper return, containers
    and ``self.attr`` -- the five mechanisms the review used to walk past
    the old check.

    It stays deliberately NARROWER than the writer audit's
    :func:`~manager.test_phase1_cursor_writer_audit._derives_path`, and
    that difference is the whole point of not sharing the rule: this
    module legitimately installs files at ``path.with_name(...)`` names
    (the claim, the candidate, the record, the lock) many times over. A
    predicate that let taint through ``with_name``/``parent`` would call
    every one of those a violation, and a guard that cries wolf on its own
    module gets deleted. Only the bare name and transparent wrappers --
    ``str()``, ``Path()``, ``os.fspath()`` -- keep naming the same file.
    """
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        # ``self.dest = path`` then ``self.dest``: the attribute NAME
        # carries the taint. ``path.parent``/``path.name`` deliberately do
        # not -- they name a different file, which is why the receiver is
        # not consulted here.
        return node.attr in ctx.attrs
    if isinstance(node, ast.Subscript):
        return _names_canonical(node.value, aliases, ctx)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_names_canonical(e, aliases, ctx) for e in node.elts)
    if isinstance(node, ast.Dict):
        return any(_names_canonical(v, aliases, ctx) for v in node.values)
    if isinstance(node, ast.IfExp):
        return (_names_canonical(node.body, aliases, ctx)
                or _names_canonical(node.orelse, aliases, ctx))
    if isinstance(node, (ast.NamedExpr, ast.Await, ast.Starred)):
        return _names_canonical(node.value, aliases, ctx)
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in PATH_SOURCES or name in ctx.returns:
            return True
        if name in PATH_WRAPPERS:
            return any(_names_canonical(a, aliases, ctx) for a in node.args)
    return False


def _is_nt_guard(test):
    for sub in ast.walk(test):
        if (isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Attribute)
                and sub.left.attr == "name" and isinstance(sub.left.value, ast.Name)
                and sub.left.value.id == "os"):
            return any(isinstance(c, ast.Constant) and c.value == "nt" for c in sub.comparators)
    return False


def imported_sink_funcs(tree):
    """``{local name: (module, function)}`` for ``from os import replace as swap``.

    The writer audit has always understood this spelling; this guard did
    not, even though the audit SKIPS ``manager/phase1_cursor.py`` as its
    sanctioned path and this is therefore the only check that can see a
    replacing install inside it. Two spellings walked straight past it.
    """
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in {"os", "shutil"}:
            continue
        for alias in node.names:
            out[alias.asname or alias.name] = (node.module, alias.name)
    return out


def _resolve_sink_call(call, module_aliases, imported):
    """The ``(module, function)`` a call names, through either alias spelling.

    One resolver for both, so a sink cannot be understood in the
    attribute form and missed in the imported form (or vice versa). The
    classification tables below are then keyed by the REAL module name,
    never by whatever the source chose to call it.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return imported.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        name = func.value.id
        # An alias wins; otherwise a receiver spelled with the module's own
        # name IS that module. The fallback matters because a fragment need
        # not carry its imports -- requiring one would have turned every
        # existing `os.replace(tmp, path)` control into a false negative.
        module = module_aliases.get(name)
        if module is None and name in GUARDED_SINK_MODULES:
            module = name
        if module is not None:
            return (module, func.attr)
    return None


def _replacing_sink(call, module_aliases=None, imported=None):
    """``(kind, destination, posix_only)`` for a replacing install, else None.

    A rename is not the only way to land on top of the canonical name.
    A truncating write gets there too, and inside this module the writer
    audit cannot see it -- ``manager/phase1_cursor.py`` is the sanctioned
    path there and is skipped by design. So the non-exclusive creates
    belong here: ``open(cursor, "w")``, ``cursor.write_text(...)``, and
    an ``os.open`` that omits ``O_EXCL``. The module's own POSIX
    publication fallback opens the canonical name WITH ``O_EXCL``, which
    is precisely what makes it a publication rather than an overwrite.
    """
    module_aliases = {} if module_aliases is None else module_aliases
    imported = {} if imported is None else imported
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open" and call.args:
        mode = call.args[1] if len(call.args) >= 2 else next(
            (k.value for k in call.keywords if k.arg == "mode"), None)
        text = mode.value if isinstance(mode, ast.Constant) else ""
        if isinstance(text, str) and set(text) & WRITE_MODE_CHARS:
            return ('open(%r)' % text, call.args[0], False)
        return None
    # Function-form sinks, resolved to their REAL module first so that
    # `import os as o` and `from os import replace as swap` reach the same
    # tables as the plain spelling.
    pair = _resolve_sink_call(call, module_aliases, imported)
    if pair is not None:
        if len(call.args) >= 2:
            if pair in ALWAYS_REPLACING_FUNCS:
                return ("%s.%s()" % pair, call.args[1], False)
            if pair in POSIX_REPLACING_FUNCS:
                return ("%s.%s()" % pair, call.args[1], True)
            if pair == ("os", "open") and not _mentions_o_excl(call.args[1]):
                return ("os.open() without O_EXCL", call.args[0], False)
        if call.args and pair in TRUNCATING_FUNCS:
            return ("%s.%s()" % pair, call.args[0], False)
    if isinstance(func, ast.Attribute):
        if len(call.args) == 1 and not call.keywords:
            # Method form: the RECEIVER is the source, the argument the
            # destination. ``Path.replace``/``rename`` take exactly one
            # argument, which is what separates them from the two-argument
            # ``str.replace`` that shares the name.
            if func.attr in ALWAYS_REPLACING_METHODS:
                return (".%s()" % func.attr, call.args[0], False)
            if func.attr in POSIX_REPLACING_METHODS:
                return (".%s()" % func.attr, call.args[0], True)
        if func.attr in TRUNCATING_METHODS:
            # Method form again, but here the RECEIVER is the destination.
            return (".%s()" % func.attr, func.value, False)
    return None


def _mentions_o_excl(flags):
    return any(isinstance(sub, ast.Attribute) and sub.attr == "O_EXCL" for sub in ast.walk(flags))


def _bare_name(node):
    """The Name an expression reduces to through transparent wrappers, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in PATH_WRAPPERS:
            for arg in node.args:
                found = _bare_name(arg)
                if found is not None:
                    return found
    return None


def _undecidable_params(tree):
    """{scope: parameter names no in-module call site ever binds}.

    Taint has to start somewhere, and a parameter of a function nothing in
    this source calls has no known origin: whether it is the canonical
    cursor depends entirely on a caller that is not here. Installing over
    such a destination with a replacing primitive is therefore not
    provably safe, and this guard exists to demand proof.

    Deliberately narrow -- ONLY parameters of functions with no in-module
    call site, and only when the destination reduces to that bare
    parameter. Every replacing install in ``manager/phase1_cursor.py``
    lands on a LOCAL whose origin is visible (``path.with_name(...)`` for
    the claim, the candidate, the record and the lock), so nothing in the
    real module depends on this branch.
    """
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    out = {}
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if scope.name in called:
            continue
        args = scope.args
        out[scope] = {a.arg for a in
                      args.posonlyargs + args.args + args.kwonlyargs}
    return out


def _scan_scope(node, aliases, ctx, unknown, nt_guarded, out, sinks=(None, None)):
    if isinstance(node, ast.If) and _is_nt_guard(node.test):
        for stmt in node.body:
            _scan_scope(stmt, aliases, ctx, unknown, True, out, sinks)
        for stmt in node.orelse:
            _scan_scope(stmt, aliases, ctx, unknown, nt_guarded, out, sinks)
        return
    if isinstance(node, ast.Call):
        sink = _replacing_sink(node, sinks[0], sinks[1])
        if sink is not None:
            kind, destination, posix_only = sink
            if not (posix_only and nt_guarded):
                if _names_canonical(destination, aliases, ctx):
                    out.append("%s over the canonical cursor at line %d" % (kind, node.lineno))
                elif _bare_name(destination) in unknown:
                    out.append("%s over an unproven destination (%s, a parameter no call site "
                               "binds) at line %d"
                               % (kind, _bare_name(destination), node.lineno))
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # its own scope, walked separately
        _scan_scope(child, aliases, ctx, unknown, nt_guarded, out, sinks)


def replacing_installs_over_the_cursor(source):
    """Every replacing install whose destination is TAINTED as the canonical cursor.

    The scope walk and the taint fixpoint are the writer audit's, reused
    rather than reimplemented; only the derivation rule
    (:func:`_names_canonical`) is this module's own. That is what makes
    ``_install(tmp, path)`` -- a destination that reaches its sink as a
    differently-named parameter of a helper -- a violation here.
    """
    tree = _fold_string_constants(ast.parse(textwrap.dedent(source)))
    aliases, _bodies, ctx = _collect_aliases(
        tree, seed={CANONICAL_NAME}, returns_seed=PATH_SOURCES, derives=_names_canonical)
    unknown_by_scope = _undecidable_params(tree)
    # Both alias spellings are resolved once for the whole module and
    # handed to every scope, so no scope can see a weaker set of sinks
    # than another.
    sinks = (sink_module_aliases(tree), imported_sink_funcs(tree))
    violations = []
    for scope, names in aliases.items():
        unknown = unknown_by_scope.get(scope, frozenset()) - names
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            _scan_scope(child, names, ctx, unknown, False, violations, sinks)
    return sorted(set(violations))


#: Legitimate code, shaped the way the real module shapes it: every
#: destination other than the canonical name is a LOCAL derived through
#: ``with_name``, so its origin is visible and provably a sibling. The
#: earlier version of this control passed those destinations in as
#: parameters, which no function in ``manager/phase1_cursor.py``
#: actually does -- and an unbound parameter is precisely the case the
#: guard can no longer wave through (reviewer control G5).
REPLACING_INSTALL_CONTROL = """
def publish(path, temp_name, txid, source):
    claim = path.with_name(path.name + ".claim-" + txid)
    record_path = path.with_name(path.name + ".init-state")
    os.replace(str(path), str(claim))
    os.replace(temp_name, str(record_path))
    stamp = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    label = str(path).replace(str(path), "<cursor>")
    if os.name == "nt":
        os.rename(str(source), str(path))
    return stamp, label

publish(_resolve_cursor_path(), "tmp", "tx", "src")
"""

REPLACING_INSTALL_MUTANTS = {
    "trailing comment naming a claim": """
def publish(path, temp_name):
    os.replace(str(temp_name), str(path))  # retires the claim
""",
    "os.rename without the nt guard": """
def publish(path, temp_name):
    os.rename(str(temp_name), str(path))
""",
    "destination reached through an alias": """
def publish(path, temp_name):
    destination = path
    os.replace(str(temp_name), str(destination))
""",
    "spelled over several lines": """
def publish(path, temp_name):
    os.replace(
        str(temp_name),
        str(path))
""",
    "Path.replace method form": """
def publish(path, temp_name):
    Path(temp_name).replace(path)
""",
    "shutil.move": """
def publish(path, temp_name):
    shutil.move(temp_name, str(path))
""",
    "destination from the bound-path resolver": """
def publish(temp_name):
    os.replace(temp_name, str(bind_phase1_cursor_path()))
""",
    "in a root-level script with no function at all": """
cursor = _resolve_cursor_path()
os.replace("staged.json", str(cursor))
""",
    "truncating open over the canonical name": """
def publish(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
""",
    "Path.write_text over the canonical name": """
def publish(path, payload):
    path.write_text(payload, encoding="utf-8")
""",
    "os.open create without O_EXCL": """
def publish(path):
    return os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
""",
    # --- the five taint mechanisms an independent review used to walk
    # past the name-matching predecessor. Each reaches the SAME sink by a
    # different route, so one broken propagation rule fails exactly one.
    "G1 destination is a differently-named parameter of a helper": """
def _install(source, destination):
    os.replace(source, destination)

def publish(path, temp_name):
    _install(temp_name, path)
""",
    "G2 destination held in a container": """
def publish(path, temp_name):
    slots = {"dest": path}
    os.replace(temp_name, str(slots["dest"]))
""",
    "G3 destination reached through an instance attribute": """
class Writer:
    def bind(self, path):
        self.dest = path

    def publish(self, temp_name):
        os.replace(temp_name, str(self.dest))
""",
    "G4 destination returned by a local helper": """
def _dest(x):
    return x

def publish(path, temp_name):
    os.replace(temp_name, str(_dest(path)))
""",
    "G5 destination bound to a name other than 'path'": """
def publish(path, temp_name):
    cursor_file = path
    os.replace(temp_name, str(cursor_file))
""",
}

#: The six controls the Final Adversarial Review ran against the
#: predecessor of this guard: 5 MISSED, 1 CAUGHT. Named here so the count
#: is asserted rather than inferred from the mutant table's length. G6 --
#: the plain ``os.replace(str(temp_name), str(path))`` the old guard did
#: catch -- is the "trailing comment" entry above, which is that exact
#: call plus the comment that used to defeat the line-oriented check.
REVIEWER_STATIC_GUARD_CONTROLS = (
    "G1 destination is a differently-named parameter of a helper",
    "G2 destination held in a container",
    "G3 destination reached through an instance attribute",
    "G4 destination returned by a local helper",
    "G5 destination bound to a name other than 'path'",
    "trailing comment naming a claim",
)


def _tasks(pid):
    from manager.trusted_ingress import TRUSTED_INGRESS_ORIGIN
    return [{"project_id": pid, "task_id": "%s-t%d" % (pid, i), "title": "T",
             "status": "queued", "recommended_provider": None,
             "quota_evidence": {"codex": {}},
             "source_context": {"origin": TRUSTED_INGRESS_ORIGIN}} for i in range(6)]


def watcher_tick(cursor_path, midway=None):
    """One real ``poll_once`` tick over two in-memory projects.

    ``midway`` runs once, after the tick has loaded the cursor and before
    it saves -- the window the path-hijack reproduction targets.
    """
    from manager.command_watcher import poll_once
    import manager.command_watcher as cw
    from manager.test_phase1_fair_scheduling import MemoryDiscoveryStore

    store = MemoryDiscoveryStore({p: _tasks(p) for p in ("p0", "p1")})
    real_enumerate = cw._enumerate_recent_commands
    fired = []

    def enumerate_hook(*args, **kwargs):
        if midway is not None and not fired:
            fired.append(1)
            midway()
        return real_enumerate(*args, **kwargs)

    with patch("manager.command_watcher.read_drive_status", return_value={"codex": {"status": "available"}}), \
            patch.object(cw, "_enumerate_recent_commands", side_effect=enumerate_hook):
        return poll_once(store, None, discovery_store=store, cursor_path=cursor_path, allowlist=frozenset())


class RecoveryTestCase(unittest.TestCase):
    """A throwaway manager home per test. Never the production one."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="adm-cursor-recovery-"))
        self.runtime = self.home / "runtime"
        self.runtime.mkdir(parents=True)
        self.cursor_path = self.runtime / "phase1-cursor.json"
        self.state_path = self.runtime / ("phase1-cursor.json" + INIT_STATE_SUFFIX)
        self.addCleanup(shutil.rmtree, self.home, True)

    def seed(self, generation=2458, records=None, path=None):
        path = path or self.cursor_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "project_cursor": 0,
            "per_project_record_cursor": dict(PROD_13_PROJECTS) if records is None else records,
            "per_project_attention_visits": {},
            "generation": generation,
            "updated_at": "2026-09-02T23:31:20Z",
        }), encoding="utf-8")

    def durable(self, path=None):
        return json.loads((path or self.cursor_path).read_text(encoding="utf-8"))

    def save(self, cursor_data, **kwargs):
        return save_phase1_cursor(cursor_data, cursor_path=self.cursor_path, **kwargs)

    def claims(self):
        return sorted(p for p in self.runtime.iterdir() if CLAIM_INFIX in p.name)

    def state(self):
        return phase1_cursor_init_state(cursor_path=self.cursor_path)

    def write_state(self, state, txid="t-1", **extra):
        body = {"schema": 2, "cursor": self.cursor_path.name, "state": state, "txid": txid,
                "recorded_at": "2026-09-03T00:00:00Z"}
        body.update(extra)
        self.state_path.write_text(json.dumps(body), encoding="utf-8")

    def fail_double(self):
        """Publication AND restore fail: the reviewer's 'swap fails, restore fails'."""
        return fail_canonical_install(self.cursor_path)


# ---------------------------------------------------------------------------
# Blocker 1 -- upgrade double failure
# ---------------------------------------------------------------------------


class TestUpgradeDoubleFailure(RecoveryTestCase):

    def test_reviewer_reproduction_through_two_watcher_ticks(self):
        """2458/13, no record; publish and restore fail; two ticks; nothing resets, nothing is lost."""
        self.seed(2458)
        self.assertIsNone(self.state())
        with self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        claims = self.claims()
        self.assertEqual(1, len(claims))
        self.assertEqual(2458, self.durable(claims[0])["generation"])
        self.assertFalse(self.cursor_path.exists())
        # The fence was established BEFORE custody, so the next tick knows.
        self.assertEqual(INIT_COMMITTED, self.state())

        watcher_tick(str(self.cursor_path))
        watcher_tick(str(self.cursor_path))

        self.assertFalse(self.cursor_path.exists(), "a tick reinitialized a cursor whose last copy is in a claim")
        self.assertEqual(claims, self.claims(), "a tick touched the custody claim")
        self.assertEqual(2458, self.durable(claims[0])["generation"])
        self.assertEqual(13, len(self.durable(claims[0])["per_project_record_cursor"]))

    def test_the_double_failure_reproduction_holds_on_every_platform(self):
        """The injection must defeat EVERY install route, not the convenient ones.

        ``_publish_exclusively`` tries a hard link, then Windows' native
        non-replacing rename, then an exclusive create. An injection that
        blocks only link/rename/replace is defeated by the third route:
        on Windows the second is reached and the scenario happens to
        reproduce, but anywhere else publication would quietly succeed
        and this whole class would be asserting against a failure the
        code can route around. Forcing each platform branch proves the
        reproduction is real rather than incidental.
        """
        candidate = self.runtime / ("phase1-cursor.json" + ".candidate-probe")
        for platform in ("nt", "posix"):
            with self.subTest(os_name=platform):
                candidate.write_text('{"generation": 2459}', encoding="utf-8")
                # ``os.name`` is patched around the publication primitive
                # only: pathlib dispatches its concrete class on it, so a
                # wider patch would fail constructing paths rather than
                # exercising the branch.
                with self.fail_double(), patch.object(pc.os, "name", platform):
                    with self.assertRaises(OSError):
                        pc._publish_exclusively(candidate, self.cursor_path)
                self.assertFalse(self.cursor_path.exists(),
                                 f"the {platform} install route was NOT blocked by the injection")
                self.assertTrue(candidate.exists(), "a refused publish consumed its source")
                candidate.unlink()

    def test_leftover_claim_prevents_first_init_even_without_a_record(self):
        """ANY existing claim means NOT first boot -- with no record at all."""
        self.seed(2458, path=self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "deadbeef"))
        self.assertIsNone(self.state())
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(1, len(self.claims()))
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(1, len(self.claims()))

    def test_claim_names_are_unique_and_never_reused(self):
        """Two interrupted transactions in one process leave two distinct claims."""
        self.seed(2458)
        with self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        first = self.claims()
        self.assertEqual(1, len(first))
        # A human puts the cursor back (the documented recovery)...
        os.rename(str(first[0]), str(self.cursor_path))
        self.assertEqual(2459, self.save({"project_cursor": 1}, expected_generation=2458)["generation"])
        # ...and the next transaction fails the same way. Every rename must
        # land on a name that did not exist the instant before.
        real_replace = os.replace
        destinations = []

        def never_over_existing(src, dst):
            destinations.append(str(dst))
            self.assertFalse(os.path.lexists(str(dst)), f"rename over an existing file: {dst}")
            return real_replace(src, dst)

        with patch.object(pc.os, "replace", side_effect=never_over_existing), self.fail_double():
            with self.assertRaises(OSError):
                self.save({"project_cursor": 2}, expected_generation=2459)
        second = self.claims()
        self.assertEqual(1, len(second))
        self.assertNotEqual(first[0].name, second[0].name)
        self.assertEqual(2459, self.durable(second[0])["generation"])
        self.assertTrue(any(CLAIM_INFIX in d for d in destinations))
        # And two ids from the generator never collide.
        self.assertNotEqual(pc._claim_path_for(self.cursor_path, pc._new_txid()),
                            pc._claim_path_for(self.cursor_path, pc._new_txid()))

    def test_fence_is_durable_before_custody(self):
        """No instant exists where the historical cursor has left its name with no record."""
        self.seed(2458)
        real_replace = os.replace
        seen = {}

        def custody(src, dst):
            if CLAIM_INFIX in str(dst):
                seen["state_at_custody"] = self.state()
                raise OSError("injected: custody refused")
            return real_replace(src, dst)

        with patch.object(pc.os, "replace", side_effect=custody):
            with self.assertRaises(CursorStateError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertEqual(INIT_COMMITTED, seen["state_at_custody"])
        self.assertEqual(2458, self.durable()["generation"])
        self.assertEqual([], self.claims())

    def test_upgrade_preserves_generation_and_coverage(self):
        self.seed(2458)
        saved = self.save({"project_cursor": 3, "per_project_record_cursor": {"proj-00": 999}},
                          expected_generation=2458)
        self.assertEqual(2459, saved["generation"])
        self.assertEqual(13, len(saved["per_project_record_cursor"]))
        self.assertEqual(999, saved["per_project_record_cursor"]["proj-00"])
        self.assertEqual(INIT_COMMITTED, self.state())


# ---------------------------------------------------------------------------
# Blocker 2 -- first-boot transaction
# ---------------------------------------------------------------------------


class TestFirstBootTransaction(RecoveryTestCase):

    def test_crash_before_prepare_retries_first_boot(self):
        self.assertIsNone(self.state())
        self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual(1, self.durable()["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_crash_after_prepare_before_create_resumes(self):
        """The reviewer's wedge: prepared record, no cursor. The next tick must recover."""
        with fail_canonical_install(self.cursor_path):
            with self.assertRaises(OSError):
                self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))

        watcher_tick(str(self.cursor_path))
        self.assertEqual(1, self.durable()["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())
        watcher_tick(str(self.cursor_path))
        self.assertEqual(2, self.durable()["generation"])

    def test_a_commit_failure_rolls_the_creation_back(self):
        """PREPARED + absent must mean "never created", not "created and lost".

        Without the rollback, a create that succeeded and a commit that
        failed left a cursor whose later loss looked exactly like a
        pre-create crash, authorising a second generation-1 creation.
        """
        real = pc._write_init_state
        calls = []

        def fail_the_commit(path, state, txid):
            calls.append(state)
            if state == INIT_COMMITTED:
                raise CursorReadError("injected: cannot record the commit")
            return real(path, state, txid)

        with patch.object(pc, "_write_init_state", side_effect=fail_the_commit):
            with self.assertRaises(CursorReadError):
                self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertIn(INIT_PREPARED, calls)
        self.assertIn(INIT_COMMITTED, calls)
        self.assertFalse(self.cursor_path.exists(),
                         "a cursor no durable record vouches for was left behind")
        self.assertEqual(INIT_PREPARED, self.state())
        # And the retry is a clean, single first boot.
        saved = self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual(1, saved["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def _fail_commit_after(self, tamper):
        """Run a CREATE_ONLY save whose commit fails, tampering first."""
        real = pc._write_init_state

        def fail_the_commit(path, state, txid):
            if state == INIT_COMMITTED:
                if tamper is not None:
                    tamper(Path(str(path)))
                raise CursorReadError("injected: cannot record the commit")
            return real(path, state, txid)

        with patch.object(pc, "_write_init_state", side_effect=fail_the_commit):
            with self.assertRaises(CursorReadError):
                self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)

    def test_the_rollback_never_deletes_a_cursor_it_did_not_create(self):
        """The rollback deletes by IDENTITY, never by name.

        An independent review drove this exact shape: between
        ``_create_exclusively`` returning and the commit failing, another
        writer put a real cursor at the canonical name, and the rollback
        deleted it -- a generation-2458 file with 13 projects, destroyed
        by code whose comment reasons about the generation-1 file it had
        created. The reasoning was sound; it just did not describe the
        file being unlinked.

        Both tampering shapes matter, and an identity check alone only
        catches the first: a competitor that PUBLISHES gets a new inode,
        while one that opens the name and rewrites it keeps the inode and
        walks past ``samestat`` untouched.
        """
        def replaced(path):
            other = path.with_name(path.name + ".other")
            other.write_text(json.dumps({
                "project_cursor": 0,
                "per_project_record_cursor": dict(PROD_13_PROJECTS),
                "per_project_attention_visits": {},
                "generation": 2458, "updated_at": None}), encoding="utf-8")
            os.replace(str(other), str(path))

        def truncated_in_place(path):
            path.write_text(json.dumps({
                "project_cursor": 0,
                "per_project_record_cursor": dict(PROD_13_PROJECTS),
                "per_project_attention_visits": {},
                "generation": 2458, "updated_at": None}), encoding="utf-8")

        def appended_in_place(path):
            with open(str(path), "a", encoding="utf-8") as handle:
                handle.write("\n")

        def replaced_with_identical_bytes(path):
            """A DIFFERENT file that happens to hold the same bytes.

            The content comparison alone cannot tell this from our own
            file; only the identity half can. It is the competitor's
            published cursor, so it is not ours to remove.
            """
            same = path.read_bytes()
            other = path.with_name(path.name + ".other")
            other.write_bytes(same)
            os.replace(str(other), str(path))

        def same_size_rewrite(path):
            """Same inode AND same length -- only the bytes differ.

            A length check masquerading as a content check would pass this.
            """
            original = path.read_bytes()
            path.write_bytes(b"X" * len(original))

        for label, tamper, survives_as in [
            ("competitor replaced the file (new inode)", replaced, 2458),
            ("competitor truncated in place (same inode)", truncated_in_place, 2458),
            ("competitor rewrote in place (same inode)", appended_in_place, 1),
            ("competitor replaced with byte-identical content", replaced_with_identical_bytes, 1),
            ("competitor rewrote same-size, different bytes", same_size_rewrite, None),
        ]:
            with self.subTest(shape=label):
                self.setUp()
                self._fail_commit_after(tamper)
                self.assertTrue(self.cursor_path.exists(),
                                f"{label}: the rollback destroyed a file it did not create")
                if survives_as is not None:
                    self.assertEqual(survives_as, self.durable()["generation"], label)

    def test_a_writer_that_wins_the_name_before_the_token_is_taken_is_not_adopted(self):
        """The ownership token must describe what publication INSTALLED.

        A review defeated the first version of this check by winning the
        canonical name in the gap between ``_publish_exclusively``
        returning and the token being read back off that name: the
        external file's own stat and bytes became the token, so the
        rollback "proved" it owned a generation-3000 cursor it had never
        created, and deleted it. Nothing may consult the canonical name
        to establish ownership.
        """
        real_publish = pc._publish_exclusively

        def publish_then_lose_the_race(source, dest):
            installed = real_publish(source, dest)
            target = Path(str(dest))
            usurper = target.with_name(target.name + ".usurper")
            usurper.write_text(json.dumps({
                "project_cursor": 0,
                "per_project_record_cursor": dict(PROD_13_PROJECTS),
                "per_project_attention_visits": {},
                "generation": 3000, "updated_at": None}), encoding="utf-8")
            os.replace(str(usurper), str(target))
            return installed

        with patch.object(pc, "_publish_exclusively",
                          side_effect=publish_then_lose_the_race):
            self._fail_commit_after(None)
        self.assertTrue(self.cursor_path.exists(),
                        "the rollback deleted a cursor that won the name before the token")
        self.assertEqual(3000, self.durable()["generation"])

    def test_publication_reports_the_identity_of_what_it_installed(self):
        """Every publication primitive must answer for its own install.

        ``os.link`` and ``os.rename`` preserve identity, so the source's
        pre-publication stat IS the canonical's; the exclusive-create
        fallback makes a NEW file and must report the descriptor it
        created instead. Getting this wrong in either direction either
        adopts a stranger's file or silently disables the rollback.
        """
        for label, break_link, platform in [
            ("os.link", False, os.name),
            ("Windows os.rename fallback", True, "nt"),
            ("POSIX exclusive-create fallback", True, "posix"),
        ]:
            with self.subTest(primitive=label):
                source = self.runtime / f"candidate-{label.split()[0]}.tmp"
                source.write_bytes(b'{"generation": 1}\n')
                dest = self.runtime / f"published-{abs(hash(label))}.json"
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(pc.os, "name", platform))
                    if break_link:
                        stack.enter_context(patch.object(
                            pc.os, "link", side_effect=NotImplementedError("no hard links")))
                    installed = pc._publish_exclusively(source, dest)
                self.assertIsNotNone(installed, f"{label}: no identity reported")
                self.assertTrue(dest.exists(), f"{label}: nothing was published")
                self.assertTrue(os.path.samestat(os.stat(str(dest)), installed),
                                f"{label}: reported identity is not the published file")
                # ...and a stranger at the same name must NOT match it.
                stranger = dest.with_name(dest.name + ".stranger")
                stranger.write_bytes(b'{"generation": 3000}\n')
                os.replace(str(stranger), str(dest))
                self.assertFalse(os.path.samestat(os.stat(str(dest)), installed),
                                 f"{label}: a replacement still matched the token")

    def test_the_rollback_still_removes_the_file_it_did_create(self):
        """The identity check must not turn the rollback off.

        Leaving an uncommitted generation-1 cursor behind is the failure
        the rollback exists to prevent: a later loss would read as
        PREPARED + absent, indistinguishable from "never created", and
        authorise a second first boot.
        """
        self._fail_commit_after(None)
        self.assertFalse(self.cursor_path.exists(),
                         "a cursor no durable record vouches for was left behind")
        self.assertEqual(INIT_PREPARED, self.state())
        saved = self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertEqual(1, saved["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_an_unestablished_creation_token_never_deletes(self):
        """No token means no proof of ownership, so nothing is removed."""
        self.seed(generation=2458)
        pc._discard_if_same(self.cursor_path, None)
        self.assertTrue(self.cursor_path.exists())
        self.assertEqual(2458, self.durable()["generation"])

    def test_crash_after_create_before_commit_adopts_generation_1(self):
        self.write_state(INIT_PREPARED)
        self.seed(1, records={"only": 1})
        saved = self.save({"project_cursor": 1}, expected_generation=1)
        self.assertEqual(2, saved["generation"])
        self.assertEqual({"only": 1}, saved["per_project_record_cursor"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_committed_record_without_cursor_is_recovery_required(self):
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=5)
        self.assertFalse(self.cursor_path.exists())

    def test_prepared_and_committed_are_distinguished(self):
        self.write_state(INIT_PREPARED)
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)


class TestLegacyMarkerMigration(RecoveryTestCase):
    """Renaming the artifact must not amount to forgetting what it recorded.

    Round 2 wrote an existence-only ``phase1-cursor.json.initialized``.
    Round 3 renamed it. A deployment that ran round 2 and then lost its
    cursor would show "no record, no cursor" under the new name and be
    reinitialized from zero -- reintroducing the exact P0 both rounds
    exist to close.
    """

    def legacy(self):
        return self.runtime / ("phase1-cursor.json" + pc.LEGACY_INIT_MARKER_SUFFIX)

    def write_legacy(self, body=None):
        self.legacy().write_text(
            json.dumps({"schema": 1, "initialized_at": "2026-09-03T00:00:00Z"} if body is None else body),
            encoding="utf-8")

    def test_legacy_marker_alone_reads_as_committed(self):
        self.write_legacy()
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_lost_cursor_with_only_a_legacy_marker_is_not_reinitialized(self):
        self.write_legacy()
        with self.assertRaises(CursorRecoveryRequiredError):
            require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        self.assertFalse(self.cursor_path.exists(),
                         "a lost cursor was rebuilt from zero because the marker had been renamed")

    def test_watcher_tick_does_not_rebuild_behind_a_legacy_marker(self):
        self.write_legacy()
        watcher_tick(str(self.cursor_path))
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists())

    def test_legacy_marker_contents_are_never_parsed(self):
        """Only its presence is evidence; that marker had no trustworthy schema."""
        for body in (b"", b"   ", b"not json at all", b"[]", b'{"schema": 99}'):
            with self.subTest(body=body):
                self.legacy().write_bytes(body)
                self.assertEqual(INIT_COMMITTED, self.state())

    def test_a_live_cursor_beside_a_legacy_marker_still_amends_normally(self):
        self.write_legacy()
        self.seed(2458)
        saved = self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertEqual(2459, saved["generation"])
        self.assertEqual(13, len(saved["per_project_record_cursor"]))
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_the_new_record_wins_over_a_legacy_marker(self):
        self.write_legacy()
        self.write_state(INIT_PREPARED)
        self.assertEqual(INIT_PREPARED, self.state())
        self.assertTrue(require_phase1_cursor_first_boot(cursor_path=self.cursor_path))

    def test_the_legacy_marker_is_never_written_or_removed(self):
        self.write_legacy()
        before = self.legacy().read_bytes()
        self.seed(10)
        self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(self.legacy().exists())
        self.assertEqual(before, self.legacy().read_bytes())
        source = Path(pc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_write_init_state(_legacy", source)
        writes = [line for line in source.splitlines()
                  if "_legacy_marker_path_for" in line and ("unlink" in line or "write" in line)]
        self.assertEqual([], writes, f"the legacy marker must be read-only: {writes}")


class TestInitStateValidation(RecoveryTestCase):

    BAD = {
        "zero_byte": b"",
        "whitespace": b"  \n",
        "truncated": b'{"schema": 2, "cursor": "phase1-cursor.json", "state": "comm',
        "not_an_object": b"[]",
        "wrong_schema": json.dumps({"schema": 1, "initialized_at": "x"}).encode(),
        "wrong_cursor": json.dumps({"schema": 2, "cursor": "other.json", "state": "committed",
                                    "txid": "t"}).encode(),
        "unknown_state": json.dumps({"schema": 2, "cursor": "phase1-cursor.json", "state": "done",
                                     "txid": "t"}).encode(),
        "no_txid": json.dumps({"schema": 2, "cursor": "phase1-cursor.json", "state": "committed"}).encode(),
    }

    def test_invalid_records_fail_closed_everywhere(self):
        for name, raw in self.BAD.items():
            with self.subTest(record=name):
                self.state_path.write_bytes(raw)
                with self.assertRaises(CursorInitStateError):
                    phase1_cursor_init_state(cursor_path=self.cursor_path)
                with self.assertRaises(CursorInitStateError):
                    require_phase1_cursor_first_boot(cursor_path=self.cursor_path)
                with self.assertRaises(CursorInitStateError):
                    self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
                self.assertFalse(self.cursor_path.exists(), name)
                self.seed(2458)
                before = self.cursor_path.read_bytes()
                with self.assertRaises(CursorInitStateError):
                    self.save({"project_cursor": 1}, expected_generation=2458)
                self.assertEqual(before, self.cursor_path.read_bytes(), name)
                self.assertEqual(raw, self.state_path.read_bytes(), "the bad record was rewritten")
                self.cursor_path.unlink()

    def test_invalid_record_is_a_state_error_not_absence_or_recovery(self):
        self.assertTrue(issubclass(CursorInitStateError, CursorStateError))
        self.assertFalse(issubclass(CursorInitStateError, CursorRecoveryRequiredError))

    def test_watcher_does_not_persist_over_a_corrupt_record(self):
        self.seed(2458)
        self.state_path.write_bytes(b"")
        watcher_tick(str(self.cursor_path))
        self.assertEqual(2458, self.durable()["generation"])
        self.assertEqual(b"", self.state_path.read_bytes())
        self.cursor_path.unlink()
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists(), "a corrupt record was treated as 'never initialized'")


# ---------------------------------------------------------------------------
# Blocker 3 -- no-overwrite publication
# ---------------------------------------------------------------------------


class TestNoOverwritePublication(RecoveryTestCase):

    def install_at_publish(self, generation=2500):
        """Fire at the publish instruction: destination is the canonical name and it is vacant."""
        real = {"replace": os.replace, "rename": os.rename, "link": os.link}
        canonical = os.path.normcase(str(self.cursor_path))
        fired = []

        def hook(name):
            def wrapped(src, dst, *args, **kwargs):
                if not fired and os.path.normcase(str(dst)) == canonical and not os.path.lexists(str(self.cursor_path)):
                    fired.append(1)
                    self.seed(generation, records={"external": 1})
                return real[name](src, dst, *args, **kwargs)
            return wrapped

        return patch.multiple(pc.os, replace=hook("replace"), rename=hook("rename"), link=hook("link")), fired

    def test_external_insert_after_the_last_check_survives(self):
        self.seed(2458)
        ctx, fired = self.install_at_publish()
        with ctx:
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertTrue(fired)
        self.assertEqual(2500, self.durable()["generation"], "the external winner was clobbered")
        claims = self.claims()
        self.assertEqual(1, len(claims), "the claim must be kept for adjudication, not deleted")
        self.assertEqual(2458, self.durable(claims[0])["generation"])

    def test_the_richer_claim_left_by_an_external_winner_is_never_retired(self):
        """The aftermath of the race above must not become a second data loss.

        An earlier version of this test asserted the opposite -- that the
        next mutation deletes the claim because its generation is lower.
        An independent review pointed out that this blesses the defect:
        the claim held 2458 with 13 projects and the external winner held
        2500 with one, so "older" and "poorer" coincided and retiring the
        claim erased the only copy of the other twelve projects.
        """
        self.seed(2458)
        ctx, _ = self.install_at_publish()
        with ctx:
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        claims = self.claims()
        self.assertEqual(1, len(claims))
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=2500)
        self.assertEqual(claims, self.claims(), "the richer claim was deleted")
        self.assertEqual(13, len(self.durable(claims[0])["per_project_record_cursor"]))
        self.assertEqual(2500, self.durable()["generation"], "the external winner was disturbed")

    def test_a_provably_redundant_claim_is_retired(self):
        """Retirement needs proof, and a claim our own successor covers has it."""
        self.seed(2458)
        self.save({"project_cursor": 1}, expected_generation=2458)
        claim = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "older")
        self.seed(2458, path=claim)
        saved = self.save({"project_cursor": 1}, expected_generation=2459)
        self.assertEqual(2460, saved["generation"])
        self.assertEqual([], self.claims())
        self.assertFalse(claim.exists())

    def test_no_unconditional_replace_over_the_canonical_name(self):
        """Static: nothing installs over the canonical name with a replacing primitive."""
        violations = replacing_installs_over_the_cursor(Path(pc.__file__).read_text(encoding="utf-8"))
        self.assertEqual([], violations, f"replacing install over the canonical cursor: {violations}")

    def test_the_static_guard_catches_every_shape_of_replacing_install(self):
        """Non-vacuous: the guard is AST-level, so no spelling of the defect slips past.

        Its predecessor was a line-oriented check -- the very weakness
        Blocker 5 condemned in the writer audit. It read only lines
        beginning ``os.replace(`` and accepted any that mentioned a claim,
        so a trailing ``# retires the claim`` comment re-opened the hole;
        ``os.rename``, which replaces silently on POSIX, was never
        examined at all; and neither was a truncating write.
        """
        for label, body in REPLACING_INSTALL_MUTANTS.items():
            with self.subTest(shape=label):
                self.assertNotEqual([], replacing_installs_over_the_cursor(body),
                                    f"{label} was not detected")
        self.assertEqual([], replacing_installs_over_the_cursor(REPLACING_INSTALL_CONTROL))

    def test_all_six_reviewer_static_guard_controls_are_detected(self):
        """The Final Adversarial Review's own six controls: 5 MISSED, now 6/6.

        Name-matching was the defect. Each control below reaches the same
        ``os.replace`` by a different route -- a helper parameter, a dict
        entry, an instance attribute, a helper's return value, a plain
        rebinding -- and the old guard, which only recognised a variable
        literally called ``path``, saw none of the first five. This matters
        more than its size suggests: the writer audit skips
        ``manager/phase1_cursor.py`` as its sanctioned path, so this guard
        is the ONLY check that can ever see a replacing install inside the
        mutation module.
        """
        detected = 0
        for label in REVIEWER_STATIC_GUARD_CONTROLS:
            with self.subTest(control=label):
                self.assertIn(label, REPLACING_INSTALL_MUTANTS)
                found = replacing_installs_over_the_cursor(REPLACING_INSTALL_MUTANTS[label])
                self.assertNotEqual([], found, f"reviewer control {label} was not detected")
                detected += 1
        self.assertEqual(6, detected, "all six reviewer controls must be exercised")

    def test_sink_aliases_cannot_hide_a_replacing_install(self):
        """P1-B. This guard understood one spelling of a sink; the audit knew two.

        That asymmetry mattered more than it looks: the writer audit
        SKIPS ``manager/phase1_cursor.py`` as its sanctioned path, so
        this guard is the only check that can ever see a replacing
        install inside the publishing module -- and it was the weaker of
        the two. ``import os as o`` and ``from os import replace as
        swap`` both walked past it.
        """
        for label, source in [
            ("module alias, os.replace",
             "import os as o\n\ndef publish(path, tmp):\n    o.replace(tmp, path)\n"),
            ("module alias, os.rename",
             "import os as o\n\ndef publish(path, tmp):\n    o.rename(tmp, path)\n"),
            ("module alias, shutil.copyfile",
             "import shutil as sh\n\ndef publish(path, tmp):\n    sh.copyfile(tmp, path)\n"),
            ("arbitrary module alias",
             "import shutil as files\n\ndef publish(path, tmp):\n    files.move(tmp, path)\n"),
            ("from-import alias, replace",
             "from os import replace as swap\n\ndef publish(path, tmp):\n    swap(tmp, path)\n"),
            ("from-import alias, rename",
             "from os import rename as move\n\ndef publish(path, tmp):\n    move(tmp, path)\n"),
            ("from-import alias, copyfile",
             "from shutil import copyfile as cp\n\ndef publish(path, tmp):\n    cp(tmp, path)\n"),
            ("truncating the canonical outright",
             "from os import truncate as cut\n\ndef publish(path):\n    cut(path, 0)\n"),
            ("module-aliased truncate",
             "import os as o\n\ndef publish(path):\n    o.truncate(path, 0)\n"),
        ]:
            with self.subTest(spelling=label):
                self.assertNotEqual([], replacing_installs_over_the_cursor(source),
                                    f"{label} was not detected")

    def test_aliased_sinks_over_siblings_are_still_permitted(self):
        """Widening the sink set must not start flagging the module's own work."""
        for label, source in [
            ("aliased replace onto a sibling",
             "import os as o\n\ndef publish(path, tmp):\n"
             "    o.replace(tmp, str(path.with_name(path.name + '.claim')))\n"),
            ("aliased copyfile onto a candidate",
             "import shutil as sh\n\ndef publish(path, tmp):\n"
             "    sh.copyfile(tmp, str(path.with_name('candidate.tmp')))\n"),
            ("from-import alias onto a sibling",
             "from os import replace as swap\n\ndef publish(path, tmp):\n"
             "    swap(tmp, str(path.with_name('.init-state')))\n"),
        ]:
            with self.subTest(spelling=label):
                self.assertEqual([], replacing_installs_over_the_cursor(source), label)

    def test_deletion_is_deliberately_outside_this_guard(self):
        """``os.remove``/``os.unlink`` are excluded ON PURPOSE, and it is pinned.

        ``_publish_exclusively`` rolls back a canonical file it just
        created exclusively, via ``_discard(path)``, behind an
        ``os.path.samestat`` identity check. Interprocedural taint
        reaches ``_discard``'s parameter from that call site, so treating
        unlink as a sink here would report correct rollback code as a
        violation on every run -- and a guard that cries wolf on its own
        module gets deleted. Recorded as an exclusion rather than left as
        a silent gap; the writer audit DOES cover these two everywhere
        else in the tree.
        """
        self.assertEqual({("os", "remove"), ("os", "unlink")}, NOT_GUARDED_FUNCS)
        self.assertFalse(NOT_GUARDED_FUNCS & (ALWAYS_REPLACING_FUNCS | POSIX_REPLACING_FUNCS
                                              | TRUNCATING_FUNCS))
        source = "import os as o\n\ndef publish(path):\n    o.unlink(path)\n"
        self.assertEqual([], replacing_installs_over_the_cursor(source))
        # ...and the real module's rollback stays clean, which is the point.
        module = (Path(pc.__file__)).read_text(encoding="utf-8")
        self.assertEqual([], replacing_installs_over_the_cursor(module))

    def test_an_unprovable_destination_is_reported_and_a_provable_one_is_not(self):
        """Taint has to start somewhere; a caller-supplied destination has no origin.

        The review's own G5 spelling installs over a bare parameter that
        no call site in the source binds. It is not provably the canonical
        cursor -- and it is not provably anything else either, which is
        the point: the guard demands proof rather than assuming safety.
        Bind the same parameter from a call site and it becomes decidable,
        and legitimate, again.
        """
        unproven = """
def publish(cursor_file, temp_name):
    os.replace(temp_name, str(cursor_file))
"""
        self.assertNotEqual([], replacing_installs_over_the_cursor(unproven))
        proven_sibling = """
def publish(cursor_file, temp_name):
    os.replace(temp_name, str(cursor_file))

def caller(path, temp_name):
    publish(path.with_name(path.name + ".init-state"), temp_name)
"""
        self.assertEqual([], replacing_installs_over_the_cursor(proven_sibling),
                         "a destination whose origin IS visible must not be reported")
        proven_canonical = """
def publish(cursor_file, temp_name):
    os.replace(temp_name, str(cursor_file))

def caller(path, temp_name):
    publish(path, temp_name)
"""
        self.assertNotEqual([], replacing_installs_over_the_cursor(proven_canonical))

    def test_the_taint_model_does_not_leak_through_sibling_paths(self):
        """The other half of correctness: this module's own installs are NOT violations.

        ``path.with_name(...)`` names the claim, the candidate, the record
        and the lock. A taint rule that propagated through it would call
        every legitimate install in the module a replacing overwrite, and
        a guard that fails on correct code gets switched off.
        """
        for label, body in {
            "claim install": "def swap(path, txid):\n"
                             "    claim = path.with_name(path.name + '.claim-' + txid)\n"
                             "    os.replace(str(path), str(claim))\n",
            "record install": "def record(path, temp_name):\n"
                              "    record_path = path.with_name(path.name + '.init-state')\n"
                              "    os.replace(temp_name, str(record_path))\n",
            "lock open without O_EXCL": "def lock(path):\n"
                                        "    lock_path = path.with_name(path.name + '.lock')\n"
                                        "    return os.open(str(lock_path), os.O_RDWR | os.O_CREAT)\n",
            "parent directory": "def prepare(path):\n    path.parent.mkdir(parents=True)\n",
        }.items():
            with self.subTest(shape=label):
                self.assertEqual([], replacing_installs_over_the_cursor(body),
                                 f"false positive on a legitimate sibling install: {label}")

    def test_publish_primitive_refuses_an_occupied_destination(self):
        source = self.runtime / "candidate"
        source.write_text("new", encoding="utf-8")
        self.cursor_path.write_text("occupied", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            pc._publish_exclusively(source, self.cursor_path)
        self.assertEqual("occupied", self.cursor_path.read_text(encoding="utf-8"))
        self.assertTrue(source.exists(), "the source must survive a refused publish")
        self.cursor_path.unlink()
        pc._publish_exclusively(source, self.cursor_path)
        self.assertEqual("new", self.cursor_path.read_text(encoding="utf-8"))
        self.assertFalse(source.exists())


# ---------------------------------------------------------------------------
# Claim lifecycle / recovery matrix
# ---------------------------------------------------------------------------


class TestCompareToCustodyRace(RecoveryTestCase):
    """The P0 an independent review found in the previous round.

    The pre-custody compare parsed the file, and the fingerprint that
    bound it read the file a second time. An external writer landing
    between those two reads made the fingerprint describe ITS file:
    custody then verified that file against itself and published a
    successor computed from the stale parse. Generation 2500 became
    2459. Both the generation check and the merge base now come from a
    re-parse taken after custody, on a file no one else can reach.
    """

    def install_between_parse_and_fingerprint(self, generation=2500, records=None):
        real = pc._fingerprint
        fired = []

        def hook(path):
            if not fired and path == self.cursor_path:
                fired.append(1)
                self.seed(generation, records=records)
            return real(path)

        return patch.object(pc, "_fingerprint", side_effect=hook), fired

    def test_external_write_between_the_parse_and_the_fingerprint_is_not_rolled_back(self):
        self.seed(2458)
        ctx, fired = self.install_between_parse_and_fingerprint(records={"external": 1})
        with ctx:
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertTrue(fired)
        self.assertEqual(2500, self.durable()["generation"],
                         "generation 2500 was rolled back to 2459")
        self.assertEqual({"external": 1}, self.durable()["per_project_record_cursor"])

    def test_a_same_generation_external_rewrite_does_not_lose_its_projects(self):
        """Equal generations make the fingerprint check pass; the re-parse still saves us."""
        self.seed(2458, records={"a": 1})
        ctx, fired = self.install_between_parse_and_fingerprint(
            generation=2458, records={"a": 1, "added-by-external": 5})
        with ctx:
            saved = self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertTrue(fired)
        self.assertEqual(2459, saved["generation"])
        self.assertIn("added-by-external", self.durable()["per_project_record_cursor"],
                      "the merge base was the stale pre-custody read")

    def test_the_merge_base_is_the_state_under_custody(self):
        recorded = {}
        real = pc._load_at

        def watch(path, missing_ok):
            state = real(path, missing_ok)
            if CLAIM_INFIX in path.name:
                recorded["claim_generation"] = state["generation"]
            return state

        self.seed(2458)
        with patch.object(pc, "_load_at", side_effect=watch):
            saved = self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertEqual(2458, recorded.get("claim_generation"),
                         "the successor was not derived from a re-parse under custody")
        self.assertEqual(2459, saved["generation"])


class TestRecoveryMatrix(RecoveryTestCase):

    def claim(self, generation, name="a"):
        path = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + name)
        self.seed(generation, path=path)
        return path

    def test_cursor_present_no_claim_committed_is_normal(self):
        self.seed(10)
        self.write_state(INIT_COMMITTED)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])

    def test_cursor_present_with_older_claim_retires_it(self):
        self.seed(10)
        self.claim(9)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertEqual([], self.claims())

    def test_cursor_present_with_newer_claim_fails_closed_and_deletes_nothing(self):
        self.seed(10)
        claim = self.claim(2458)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertEqual(10, self.durable()["generation"])
        self.assertEqual(2458, self.durable(claim)["generation"])

    def test_cursor_present_with_equal_but_different_claim_fails_closed(self):
        self.seed(10)
        claim = self.claim(10)
        claim.write_text(claim.read_text(encoding="utf-8").replace('"project_cursor": 0', '"project_cursor": 7'),
                         encoding="utf-8")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(claim.exists())

    def test_cursor_present_with_identical_claim_is_a_duplicate_and_retires(self):
        self.seed(10)
        claim = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "dup")
        shutil.copyfile(self.cursor_path, claim)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertEqual([], self.claims())

    def test_cursor_present_with_unreadable_claim_fails_closed(self):
        self.seed(10)
        claim = self.runtime / ("phase1-cursor.json" + CLAIM_INFIX + "bad")
        claim.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 1}, expected_generation=10)
        self.assertTrue(claim.exists())

    def test_cursor_absent_one_claim_is_recovery_required(self):
        claim = self.claim(2458)
        for token in (CREATE_ONLY, 2458, 0):
            with self.subTest(token=token):
                with self.assertRaises(CursorRecoveryRequiredError):
                    self.save({"project_cursor": 0}, expected_generation=token)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(2458, self.durable(claim)["generation"])

    def test_cursor_absent_multiple_claims_fails_closed(self):
        a, b = self.claim(2458, "a"), self.claim(2459, "b")
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=2459)
        self.assertTrue(a.exists() and b.exists())
        self.assertFalse(self.cursor_path.exists())

    def test_cursor_absent_no_claim_committed_fails_closed(self):
        self.write_state(INIT_COMMITTED)
        with self.assertRaises(CursorRecoveryRequiredError):
            self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)

    def test_cursor_absent_no_claim_prepared_resumes_first_boot(self):
        self.write_state(INIT_PREPARED)
        self.assertEqual(1, self.save({"project_cursor": 0}, expected_generation=CREATE_ONLY)["generation"])
        self.assertEqual(INIT_COMMITTED, self.state())

    def test_cursor_corrupt_with_a_prior_init_record_fails_closed(self):
        """Matrix row 9: corruption beside a record of a committed cursor never resolves itself.

        Neither token may turn this into a write: an amendment cannot
        read the generation it is amending, and CREATE_ONLY cannot claim
        a first boot over a file that is sitting right there. The corrupt
        file is left untouched as evidence.
        """
        self.cursor_path.write_text('{"generation": 24', encoding="utf-8")
        before = self.cursor_path.read_bytes()
        self.write_state(INIT_COMMITTED)
        for token in (2458, 0, CREATE_ONLY):
            with self.subTest(token=token):
                with self.assertRaises((CursorStateError, StaleCursorError,
                                        CursorRecoveryRequiredError)):
                    self.save({"project_cursor": 1}, expected_generation=token)
                self.assertEqual(before, self.cursor_path.read_bytes())
                self.assertEqual([], self.claims())
        watcher_tick(str(self.cursor_path))
        self.assertEqual(before, self.cursor_path.read_bytes(),
                         "a tick wrote over a corrupt cursor instead of leaving the evidence")

    def test_claim_destroyed_during_custody_aborts_without_recreating(self):
        self.seed(2458)
        real = pc._fingerprint

        def destroy(path):
            if CLAIM_INFIX in path.name and path.exists():
                path.unlink()
            return real(path)

        with patch.object(pc, "_fingerprint", side_effect=destroy):
            with self.assertRaises(StaleCursorError):
                self.save({"project_cursor": 1}, expected_generation=2458)
        self.assertFalse(self.cursor_path.exists())
        self.assertEqual(INIT_COMMITTED, self.state(), "the fence must survive the abort")
        self.assertEqual([], [p.name for p in self.runtime.iterdir() if ".candidate-" in p.name])
        watcher_tick(str(self.cursor_path))
        self.assertFalse(self.cursor_path.exists(), "a low-generation cursor was created after the abort")

    def test_stale_candidates_are_debris_and_never_truth(self):
        self.seed(10)
        stale = self.runtime / "phase1-cursor.json.candidate-old"
        self.seed(9999, path=stale)
        self.assertEqual(11, self.save({"project_cursor": 1}, expected_generation=10)["generation"])
        self.assertFalse(stale.exists())


# ---------------------------------------------------------------------------
# Blocker 4 -- the whole Watcher tick binds its path once
# ---------------------------------------------------------------------------


class TestWatcherPathBinding(RecoveryTestCase):

    def setUp(self):
        super().setUp()
        self.original_cwd = os.getcwd()
        self.original_env = dict(os.environ)
        self.a = self.home / "a"
        self.b = self.home / "b"
        self.pa = self.a / "runtime" / "phase1-cursor.json"
        self.pb = self.b / "runtime" / "phase1-cursor.json"
        self.seed(2458, path=self.pa)
        self.seed(2458, path=self.pb)

    def tearDown(self):
        os.chdir(self.original_cwd)
        os.environ.clear()
        os.environ.update(self.original_env)

    def hijack(self, relative_home):
        os.chdir(str(self.b))
        os.environ.update(AI_MANAGER_HOME="." if relative_home else str(self.b),
                          USERPROFILE=str(self.b), HOME=str(self.b))

    def test_relative_cursor_path_survives_a_midway_hijack(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = str(self.a)
        watcher_tick("runtime/phase1-cursor.json", midway=lambda: self.hijack(False))
        self.assertEqual(2459, self.durable(self.pa)["generation"])
        self.assertEqual(2458, self.durable(self.pb)["generation"], "the decoy was advanced")

    def test_relative_manager_home_survives_a_midway_hijack(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        watcher_tick(None, midway=lambda: self.hijack(True))
        self.assertEqual(2459, self.durable(self.pa)["generation"])
        self.assertEqual(2458, self.durable(self.pb)["generation"], "the decoy was advanced")

    def test_load_and_save_receive_the_identical_bound_path(self):
        import manager.command_watcher as cw
        received = []
        real_load, real_save = pc.load_phase1_cursor, pc.save_phase1_cursor

        def load(**kwargs):
            received.append(("load", kwargs.get("cursor_path")))
            return real_load(**kwargs)

        def save(data, **kwargs):
            received.append(("save", kwargs.get("cursor_path")))
            return real_save(data, **kwargs)

        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        with patch.object(pc, "load_phase1_cursor", side_effect=load), \
                patch.object(pc, "save_phase1_cursor", side_effect=save):
            watcher_tick(None, midway=lambda: self.hijack(True))
        kinds = [k for k, _ in received]
        self.assertEqual(["load", "save"], kinds, received)
        (_, loaded), (_, saved) = received
        self.assertIs(loaded, saved, "load and save must use the same bound object")
        self.assertTrue(loaded.is_absolute())
        self.assertEqual(self.pa.resolve(), loaded)
        self.assertEqual(2459, self.durable(self.pa)["generation"])

    def test_bind_is_stable_under_environment_change(self):
        os.chdir(str(self.a))
        os.environ["AI_MANAGER_HOME"] = "."
        bound = bind_phase1_cursor_path()
        self.hijack(True)
        self.assertEqual(bound, bind_phase1_cursor_path(cursor_path=bound))
        self.assertEqual(self.pa.resolve(), bound)
        self.assertEqual(2458, load_phase1_cursor(cursor_path=bound)["generation"])


if __name__ == "__main__":
    unittest.main()
