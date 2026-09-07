"""Phase B3-2: the committed Phase B-2R reproducers must reproduce.

The B-2 final independent re-review (PHASE_B2_ACCEPTED) carried three
documentation/reproducibility residuals, none of which touch production code:

* NB-B -- ``repro/B2R-ADVERSARIAL-AUDIT.py`` claimed to run "at either SHA"
  but imported a head-only fixture helper, so at the base ``794db70`` it died
  with ImportError before scoring a single attack.
* NB-C -- ``repro/B2R-MUTATION-MATRIX.py`` resolved ROOT to its own
  ``repro/`` directory, handed pytest no tests, and exited 2 ("BASELINE NOT
  GREEN") on every checkout -- fail-closed, but it never measured anything.
* NB-D -- the README said thirteen mutants; the matrix defines fourteen.

These tests lock the repaired contract: the audit runs at the base SHA and at
this head with a stable output shape and pinned verdict sets; the matrix
resolves ROOT to the repository root from any cwd, refuses a moved copy,
finds every mutant's search text, restores byte-for-byte on LF and CRLF
files; and the README's numbers are the numbers the scripts produce.

The base-SHA test extracts ``manager/`` from ``794db70`` with ``git archive``
into a temporary directory -- it registers no worktree and never touches the
checkout's index -- and skips only when that commit is absent from the local
object store, saying so.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPRO = REPO_ROOT / "docs" / "verification-loop" / "repro"
AUDIT = REPRO / "B2R-ADVERSARIAL-AUDIT.py"
MATRIX = REPRO / "B2R-MUTATION-MATRIX.py"
README = REPO_ROOT / "docs" / "verification-loop" / "README.md"

BASE_SHA = "794db7011de4561b186a690e49f363e5120dc4cb"
ATTACK_IDS = ["A%d" % n for n in range(1, 12)]
CONTROL_IDS = ["C1", "C2", "C3"]

# Verdicts the audit produced when it was repaired (Phase B3-2, 2026-09-07).
# Changing these is changing the README's headline; both must move together.
BASE_BYPASSED = {"A1", "A2", "A3", "A5", "A8", "A10", "A11"}
BASE_BLOCKED = {"A4", "A7", "A9"}
BASE_NOT_APPLICABLE = {"A6"}

SUMMARY = re.compile(
    r"^(\d+)/(\d+) attacks blocked, (\d+) bypassed, (\d+) not applicable at this version; "
    r"(\d+)/(\d+) controls still healthy$"
)


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_audit(stdout: str):
    """Rows keyed by id -> (verdict, detail); plus the parsed summary tuple."""
    rows = {}
    summary = None
    profile = None
    for line in stdout.splitlines():
        if line.startswith("VERSION PROFILE: "):
            profile = line[len("VERSION PROFILE: "):]
        if line.startswith("  ") and len(line) > 12:
            verdict = line[2:10].strip()
            name = line[11:69].strip()
            detail = line[70:].strip()
            if verdict in ("BLOCKED", "BYPASSED", "N/A", "OK", "BROKEN") and name:
                rows[name.split()[0]] = (verdict, detail)
        match = SUMMARY.match(line.strip())
        if match:
            summary = tuple(int(g) for g in match.groups())
    return rows, summary, profile


def _run_audit(pythonpath: pathlib.Path, cwd: pathlib.Path, scratch: pathlib.Path):
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(pythonpath)
    env["AI_MANAGER_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(AUDIT), str(scratch / "store")],
        capture_output=True, text=True, cwd=str(cwd), env=env,
    )
    return proc


def _short_tmp() -> pathlib.Path:
    # The ledger files records flat with 64-hex names; keep the root short so
    # the test measures the loop, not the Windows path-length cliff.
    return pathlib.Path(tempfile.mkdtemp(prefix="b32-"))


class AuditAtHeadTests(unittest.TestCase):
    def test_audit_runs_from_a_foreign_cwd_and_blocks_every_attack(self):
        scratch = _short_tmp()
        proc = _run_audit(REPO_ROOT, scratch, scratch)
        rows, summary, profile = _parse_audit(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertIn("consumption-bound tickets", profile or "")
        self.assertEqual(sorted(k for k in rows if k.startswith("A")), sorted(ATTACK_IDS))
        self.assertEqual(sorted(k for k in rows if k.startswith("C")), CONTROL_IDS)
        self.assertEqual({rows[a][0] for a in ATTACK_IDS}, {"BLOCKED"})
        self.assertEqual({rows[c][0] for c in CONTROL_IDS}, {"OK"})
        self.assertEqual(summary, (11, 11, 0, 0, 3, 3))

    def test_audit_never_writes_into_the_checkout(self):
        scratch = _short_tmp()
        before = subprocess.run(
            ["git", "status", "--porcelain", "--ignored=no"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        ).stdout
        _run_audit(REPO_ROOT, scratch, scratch)
        after = subprocess.run(
            ["git", "status", "--porcelain", "--ignored=no"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        ).stdout
        self.assertEqual(before, after)
        self.assertFalse((REPO_ROOT / "b2r-adversarial").exists())


class AuditAtBaseTests(unittest.TestCase):
    def _extract_base(self) -> pathlib.Path:
        probe = subprocess.run(
            ["git", "cat-file", "-e", BASE_SHA + "^{commit}"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        if probe.returncode != 0:
            self.skipTest(
                "base commit %s is not in this clone's object store; fetch "
                "origin/fix/verification-loop-phase-b2r-evidence-integrity-20260906 first" % BASE_SHA[:7]
            )
        scratch = _short_tmp()
        archive = scratch / "base.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", "-o", str(archive), BASE_SHA, "manager"],
            check=True, capture_output=True, cwd=str(REPO_ROOT),
        )
        tree = scratch / "tree"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tree)
        self.assertTrue((tree / "manager" / "verification_loop" / "fixtures.py").exists())
        fixtures = (tree / "manager" / "verification_loop" / "fixtures.py").read_text(encoding="utf-8")
        self.assertNotIn("def consumed_against", fixtures, "this is not the base contract")
        return scratch, tree

    def test_audit_runs_at_base_and_reports_the_historical_bypasses(self):
        scratch, tree = self._extract_base()
        proc = _run_audit(tree, tree, scratch)
        rows, summary, profile = _parse_audit(proc.stdout)
        self.assertNotIn("ImportError", proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertIn("issued-ticket admission", profile or "")
        self.assertEqual({rows[c][0] for c in CONTROL_IDS}, {"OK"},
                         "the adapter's binding must admit the honest round at base")
        by_verdict = {}
        for attack in ATTACK_IDS:
            by_verdict.setdefault(rows[attack][0], set()).add(attack)
        self.assertEqual(by_verdict.get("BYPASSED"), BASE_BYPASSED)
        self.assertEqual(by_verdict.get("BLOCKED"), BASE_BLOCKED)
        self.assertEqual(by_verdict.get("N/A"), BASE_NOT_APPLICABLE)
        self.assertEqual(summary, (3, 11, 7, 1, 3, 3))
        self.assertIn("consumed_report_digest", rows["A6"][1])


class MatrixRootTests(unittest.TestCase):
    def test_root_resolves_to_the_repository_root_from_any_cwd(self):
        module = _load(MATRIX)
        self.assertEqual(module.ROOT, REPO_ROOT)
        previous = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            self.assertEqual(module.resolve_root(), REPO_ROOT)
        finally:
            os.chdir(previous)
        for suite in module.SUITES:
            self.assertTrue((REPO_ROOT / suite).is_file(), suite)

    def test_a_moved_script_is_refused_by_name(self):
        module = _load(MATRIX)
        moved = pathlib.Path(tempfile.mkdtemp()) / "a" / "b" / "c" / "B2R-MUTATION-MATRIX.py"
        moved.parent.mkdir(parents=True)
        moved.write_bytes(b"")
        with self.assertRaises(SystemExit) as caught:
            module.resolve_root(moved)
        self.assertIn("not the repository root", str(caught.exception))
        self.assertIn("manager/verification_loop/stores.py", str(caught.exception))
        # Also one level too shallow: parents[3] of docs/verification-loop/X.py
        # is above the checkout and carries no markers either.
        shallow = REPO_ROOT / "docs" / "verification-loop" / "B2R-MUTATION-MATRIX.py"
        with self.assertRaises(SystemExit):
            module.resolve_root(shallow)


class MatrixMutantTests(unittest.TestCase):
    def test_every_mutant_search_text_is_present_exactly_where_it_says(self):
        module = _load(MATRIX)
        ids = [m[0] for m in module.MUTANTS]
        self.assertEqual(len(ids), len(set(ids)), "duplicate mutant id")
        self.assertEqual(sorted(ids), ["M%d" % n for n in range(41, 55)])
        for mid, relpath, find, _repl, target, _what in module.MUTANTS:
            source, _raw = module.read_source(module.ROOT / relpath)
            self.assertIn(find, source, "%s would report NOT_APPLIED" % mid)
            self.assertTrue(target, "%s names no target test" % mid)

    def test_every_named_target_test_exists_in_the_suites(self):
        module = _load(MATRIX)
        corpus = "\n".join((REPO_ROOT / s).read_text(encoding="utf-8") for s in module.SUITES)
        for mid, _relpath, _find, _repl, target, _what in module.MUTANTS:
            self.assertIn("def " + target, corpus, "%s target %s not defined" % (mid, target))

    def test_apply_and_restore_are_byte_exact_on_crlf_and_lf(self):
        module = _load(MATRIX)
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                path = pathlib.Path(tempfile.mkdtemp()) / "victim.py"
                original = newline.join([b"a = 1", b"if x:", b"    y()", b""])
                path.write_bytes(original)
                before = hashlib.sha256(original).hexdigest()
                kept = module.apply_mutation(path, "if x:\n", "if False and x:\n")
                self.assertEqual(kept, original)
                mutated = path.read_bytes()
                self.assertIn(b"if False and x:", mutated)
                self.assertEqual(mutated.count(b"\r\n"), original.count(b"\r\n"))
                module.restore(path, kept)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_absent_search_text_leaves_the_file_untouched(self):
        module = _load(MATRIX)
        path = pathlib.Path(tempfile.mkdtemp()) / "victim.py"
        path.write_bytes(b"nothing here\n")
        self.assertIsNone(module.apply_mutation(path, "absent", "x"))
        self.assertEqual(path.read_bytes(), b"nothing here\n")


class ReadmeClaimsTests(unittest.TestCase):
    def test_readme_mutant_count_is_the_matrix_count(self):
        module = _load(MATRIX)
        text = README.read_text(encoding="utf-8")
        match = re.search(r"`repro/B2R-MUTATION-MATRIX\.py` is the Phase B-2R mutation matrix: (\d+) mutants \(M41.M54\)", text)
        self.assertIsNotNone(match, "README no longer states the B2R mutant count in the pinned form")
        self.assertEqual(int(match.group(1)), len(module.MUTANTS))
        # Anchor after the B2R paragraph: the B3-1 matrix paragraph above it
        # legitimately says 3/3.
        killed = re.search(r"\*\*(\d+)/(\d+) killed by their named target", text[match.start():])
        self.assertIsNotNone(killed)
        self.assertEqual(killed.groups(), (str(len(module.MUTANTS)),) * 2)
        # The README may quote the old wrong word when it explains NB-D; the
        # sentence that states the count must not.
        self.assertNotIn("thirteen", match.group(0).lower())
        self.assertNotIn("thirteen", text[match.start():match.start() + 200].lower())

    def test_readme_audit_numbers_are_the_pinned_measurements(self):
        text = README.read_text(encoding="utf-8")
        base = re.search(r"Measured at `794db70`: \*\*(\d+) bypassed, (\d+) blocked, (\d+) not applicable \((A\d+)\)\*\*", text)
        self.assertIsNotNone(base, "README no longer states the base audit measurement in the pinned form")
        self.assertEqual(base.groups(), (str(len(BASE_BYPASSED)), str(len(BASE_BLOCKED)),
                                         str(len(BASE_NOT_APPLICABLE)), "A6"))
        self.assertIsNotNone(re.search(r"\*\*11/11 blocked, 0 bypassed", text))
        self.assertIn("3/3 controls", text)


if __name__ == "__main__":
    unittest.main()
