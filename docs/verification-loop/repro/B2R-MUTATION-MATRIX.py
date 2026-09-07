"""Phase B-2R mutation matrix: fourteen mutants, M41-M54.

    PYTHONPATH=. python docs/verification-loop/repro/B2R-MUTATION-MATRIX.py

Run from a scratch clone with a temporary AI_MANAGER_HOME. ROOT is the
repository root -- this file lives three directories below it -- and the
script refuses to run if that directory does not carry the repository markers
it mutates (Phase B3-2, residual NB-C: the first cut resolved ROOT to this
``repro/`` directory, so pytest found nothing and the matrix exited 2 with
"BASELINE NOT GREEN" -- fail-closed, but never measuring anything).

Each mutant neutralises exactly one guard the repair added, then the whole
verification-loop suite runs. A mutant is KILLED only if it fails, and only if
the named target test is among the failures -- a mutant killed by some unrelated
test elsewhere tells you nothing about whether the guard is under test, which is
the vacuity trap both earlier reviews found.

Oracle traps guarded explicitly, all of which have produced false results in
this repository before:

* a mutation whose search text is absent reports NOT_APPLIED, never a silent
  SURVIVED;
* a crash counts as KILLED, not as an error to be retried;
* every mutated file is sha256-verified restored afterwards, and a mismatch
  aborts the run rather than letting later mutants measure a dirty tree;
* ``git status`` of the mutated package is compared before and after the whole
  run, so the matrix leaves the tree exactly as it found it (it may run on an
  uncommitted fix, so it demands "unchanged", not "clean").
"""

import hashlib
import pathlib
import subprocess
import sys

# Paths that must exist under ROOT for this script to be pointing at a
# repository checkout: the package it mutates and the directory it lives in.
ROOT_MARKERS = ("manager/verification_loop/stores.py", "docs/verification-loop/repro")


def resolve_root(script=None):
    """The repository root for this script, verified against ROOT_MARKERS.

    The script lives at ``docs/verification-loop/repro/``, three directories
    below the root, so ``parents[3]`` is the candidate. A moved script (or a
    copy run from somewhere else) makes the candidate miss a marker, and the
    run stops with a message naming the wrong directory instead of a baseline
    that is "not green" because pytest was handed no tests.
    """
    here = pathlib.Path(script or __file__).resolve()
    candidate = here.parents[3] if len(here.parents) > 3 else here.parent
    missing = [marker for marker in ROOT_MARKERS if not (candidate / marker).exists()]
    if missing:
        raise SystemExit(
            "ROOT %s is not the repository root (missing %s); this script must live at "
            "docs/verification-loop/repro/ inside a checkout" % (candidate, ", ".join(missing))
        )
    return candidate


ROOT = resolve_root()
SUITES = [
    "manager/test_verification_loop_evaluator.py",
    "manager/test_verification_loop_b2_hostile.py",
    "manager/test_verification_loop_b2_primitives.py",
    "manager/test_verification_loop_b2_runtime.py",
    "manager/test_verification_loop_b2r_integrity.py",
]

V = "manager/verification_loop/"

# (id, file, find, replace, target test substring, what it neutralises)
MUTANTS = [
    (
        "M41", V + "stores.py",
        '''    actual = record_digest(record)
    if actual != expected_digest:''',
        '''    actual = record_digest(record)
    if False and actual != expected_digest:''',
        "test_rpt_2_a_stored_fail_edited_into_a_pass_is_refused",
        "read-time content-digest verification (both ledgers)",
    ),
    (
        "M42", V + "stores.py",
        '''            _verify_record(path, VerificationTicket, path.stem, "TICKET")
            for path in sorted(self.directory.glob("*.json"))''',
        '''            rehydrate(VerificationTicket, __import__("json").loads(_read_exact(path)))
            for path in sorted(self.directory.glob("*.json"))''',
        "test_tkt_1_rewriting_the_expected_checker_does_not_authorise_a_rogue",
        "ticket read-time verification only (reports still verified)",
    ),
    (
        "M43", V + "stores.py",
        '''    canonical = canonical_json(record)
    if canonical != raw:''',
        '''    canonical = canonical_json(record)
    if False and canonical != raw:''',
        "test_rpt_6_the_persisted_bytes_must_be_the_canonical_form",
        "canonical-bytes check: a record may be stored in any serialisation",
    ),
    (
        "M44", V + "admission.py",
        '''    if ticket.consumed_report_digest != report_digest(report):
        return "REPORT_DIGEST_NOT_TICKET_CONSUMED"''',
        '''    if False:
        return "REPORT_DIGEST_NOT_TICKET_CONSUMED"''',
        "test_csm_8_predicate_11_is_enforced_in_the_pure_evaluator_too",
        "predicate 11: the consumed digest is no longer compared",
    ),
    (
        "M45", V + "stores.py",
        '''    if len(consumed) > 1:
        return replace(issued[0], status="invalidated")''',
        '''    if len(consumed) > 1:
        return consumed[0]''',
        "test_csm_10_two_conflicting_consumptions_invalidate_the_ticket",
        "two conflicting consumptions: first writer wins instead of invalidating",
    ),
    (
        "M46", V + "classification.py",
        '''    claimed = observation.base_result if observation.base_result in ("PASS", "FAIL") else None
    if claimed is None:
        return attested, reason''',
        '''    claimed = observation.base_result if observation.base_result in ("PASS", "FAIL") else None
    if claimed is not None:
        return claimed, "MEASURED_THIS_ROUND"
    if claimed is None:
        return attested, reason''',
        "test_base_1_attestation_says_pass_and_the_report_claims_fail",
        "baseline precedence: the self-declared base_result is read first again",
    ),
    (
        "M47", V + "classification.py",
        '''    if claimed != attested:
        return UNKNOWN, "BASE_EVIDENCE_CONTRADICTION"''',
        '''    if False:
        return UNKNOWN, "BASE_EVIDENCE_CONTRADICTION"''',
        "test_base_1_attestation_says_pass_and_the_report_claims_fail",
        "contradiction detection only (precedence otherwise intact)",
    ),
    (
        "M48", V + "admission.py",
        '''    if report.task_id != execution.task_id:
        return "REPORT_TASK_ID_MISMATCH"''',
        '''    if False:
        return "REPORT_TASK_ID_MISMATCH"''',
        "test_task_2_task_as_evidence_cannot_be_spent_on_task_b",
        "the only cross-Task barrier (reviewer finding N-1, mutant R3)",
    ),
    (
        "M49", V + "admission.py",
        '''    if ticket.worktree_generation != preflight.worktree_generation:
        return "WORKTREE_GENERATION_CHANGED_DURING_ROUND"''',
        '''    if False:
        return "WORKTREE_GENERATION_CHANGED_DURING_ROUND"''',
        "test_lease_1_a_generation_change_at_identical_head_invalidates_the_round",
        "worktree lease generation comparison",
    ),
    (
        "M50", V + "admission.py",
        '''    if ticket.worktree_lock_id != preflight.worktree_lock_id:
        return "WORKTREE_LOCK_ID_CHANGED_DURING_ROUND"''',
        '''    if False:
        return "WORKTREE_LOCK_ID_CHANGED_DURING_ROUND"''',
        "test_lease_2_a_lock_id_change_at_identical_head_invalidates_the_round",
        "worktree lease identity comparison",
    ),
    (
        "M51", V + "admission.py",
        '''    if report.result == "PASS" and report.failure_observations:''',
        '''    if False and report.result == "PASS" and report.failure_observations:''',
        "test_sc_1_a_pass_carrying_failures_is_inadmissible",
        "PASS-with-failures self-contradiction check",
    ),
    (
        "M52", V + "tickets.py",
        '''        if not isinstance(digest, str) or not digest.strip():
            return "TICKET_CONSUMED_WITHOUT_DIGEST"''',
        '''        if False:
            return "TICKET_CONSUMED_WITHOUT_DIGEST"''',
        "test_csm_9_a_ticket_claiming_consumption_without_a_digest_is_self_refuting",
        "a ticket may claim consumption while naming no report",
    ),
    (
        "M54", V + "admission.py",
        '''    if ticket.status == "issued":''',
        '''    if False and ticket.status == "issued":''',
        "test_csm_11_a_persisted_report_whose_ticket_was_never_consumed_is_refused",
        "predicate 11 as a positive requirement: the crash window between the "
        "store's two writes stops failing closed",
    ),
    (
        "M53", V + "admission.py",
        '''        return "WORKTREE_LEASE_NOT_REPORTED"''',
        '''        pass''',
        "test_lease_5_an_unreported_lease_is_not_a_matching_lease",
        "an unreported lease reads as a matching lease",
    ),
]


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def read_source(path):
    """Newline-normalised source for matching, plus the exact bytes for restore.

    Reading with ``read_text`` and writing back with ``newline=""`` silently
    rewrites CRLF into LF. On a Windows checkout -- which is how this repository
    is normally cloned -- that made every restore fail the sha256 assertion
    below even though the content was identical, and rewrote the line endings of
    files the run never meant to touch.

    Both halves are needed and they are not the same string. Mutations are
    written against LF source, so matching has to happen on normalised text or
    every mutant silently reports NOT_APPLIED on a CRLF checkout; the restore
    has to happen from the original bytes, or the file comes back subtly
    different from the one that was read.
    """
    raw = pathlib.Path(path).read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n"), raw


def apply_mutation(path, find, repl):
    """Write the mutant over ``path``; return the original bytes for restore.

    Returns None without touching the file when the search text is absent, so
    the caller reports NOT_APPLIED instead of a silent SURVIVED. Line endings
    of the file are preserved: matching is done on LF-normalised text, and a
    CRLF file gets its mutant written back as CRLF.
    """
    original, original_bytes = read_source(path)
    if find not in original:
        return None
    mutated = original.replace(find, repl, 1)
    if b"\r\n" in original_bytes:
        mutated = mutated.replace("\n", "\r\n")
    pathlib.Path(path).write_bytes(mutated.encode("utf-8"))
    return original_bytes


def restore(path, original_bytes):
    """Put the original bytes back and prove it, or abort the whole run."""
    pathlib.Path(path).write_bytes(original_bytes)
    expected = hashlib.sha256(original_bytes).hexdigest()
    if sha(path) != expected:
        raise AssertionError("%s not restored byte-for-byte" % path)


def tree_status(root):
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "manager/verification_loop"],
        capture_output=True, text=True, cwd=str(root),
    )
    return proc.stdout.strip()


def run_suite(root):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:randomly", *SUITES],
        capture_output=True, text=True, cwd=str(root),
    )
    failures = set()
    for line in proc.stdout.splitlines():
        if line.startswith(("FAILED ", "ERROR ", "SUBFAILED")):
            failures.add(line.split(" ", 1)[1].split(" ")[0])
    return proc.returncode, failures, proc.stdout


def main(root=ROOT):
    print("ROOT: %s" % root)
    status_before = tree_status(root)
    print("git status before (manager/verification_loop): %s" % (status_before or "clean"))
    print("Baseline: the unmutated tree must be green, or nothing below means anything.")
    code, failures, out = run_suite(root)
    if code != 0:
        print("BASELINE NOT GREEN -- aborting")
        print(out[-3000:])
        return 2
    print("  baseline green:", out.strip().splitlines()[-1])
    print("")

    summary = []
    for mid, relpath, find, repl, target, what in MUTANTS:
        path = root / relpath
        before = sha(path)
        original_bytes = apply_mutation(path, find, repl)
        if original_bytes is None:
            summary.append((mid, "NOT_APPLIED", what, ""))
            print("%-5s NOT_APPLIED  %s" % (mid, what))
            continue
        try:
            code, failures, out = run_suite(root)
        finally:
            restore(path, original_bytes)
            assert sha(path) == before, "%s: %s not restored byte-for-byte" % (mid, relpath)

        hit = any(target in name for name in failures)
        if code == 0:
            verdict = "SURVIVED"
        elif hit:
            verdict = "KILLED"
        else:
            verdict = "KILLED_WRONG_REASON"
        detail = "%d failing; target %s" % (len(failures), "HIT" if hit else "MISSED")
        summary.append((mid, verdict, what, detail))
        print("%-5s %-19s %-62s %s" % (mid, verdict, what, detail))

    print("")
    killed = sum(1 for _m, v, _w, _d in summary if v == "KILLED")
    print("=" * 100)
    print("%d/%d KILLED by their named target test; %d survived, %d not applied, %d killed for the wrong reason"
          % (killed, len(summary),
             sum(1 for _m, v, _w, _d in summary if v == "SURVIVED"),
             sum(1 for _m, v, _w, _d in summary if v == "NOT_APPLIED"),
             sum(1 for _m, v, _w, _d in summary if v == "KILLED_WRONG_REASON")))
    status_after = tree_status(root)
    print("git status after  (manager/verification_loop): %s" % (status_after or "clean"))
    unchanged = status_after == status_before
    print("tree unchanged by matrix: %s" % unchanged)
    print("=" * 100)
    return 0 if killed == len(summary) and unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
