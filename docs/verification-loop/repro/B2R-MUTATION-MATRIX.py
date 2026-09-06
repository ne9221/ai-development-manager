"""Phase B-2R mutation matrix M41-M51.

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
  aborts the run rather than letting later mutants measure a dirty tree.
"""

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
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
        '''        if ticket.consumed_report_digest != report_digest(report):
            return "REPORT_DIGEST_NOT_TICKET_CONSUMED"''',
        '''        if False:
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
        "M53", V + "admission.py",
        '''        return "WORKTREE_LEASE_NOT_REPORTED"''',
        '''        pass''',
        "test_lease_5_an_unreported_lease_is_not_a_matching_lease",
        "an unreported lease reads as a matching lease",
    ),
]


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def run_suite():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:randomly", *SUITES],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    failures = set()
    for line in proc.stdout.splitlines():
        if line.startswith(("FAILED ", "ERROR ", "SUBFAILED")):
            failures.add(line.split(" ", 1)[1].split(" ")[0])
    return proc.returncode, failures, proc.stdout


print("Baseline: the unmutated tree must be green, or nothing below means anything.")
code, failures, out = run_suite()
if code != 0:
    print("BASELINE NOT GREEN -- aborting")
    print(out[-3000:])
    sys.exit(2)
print("  baseline green:", out.strip().splitlines()[-1])
print("")

summary = []
for mid, relpath, find, repl, target, what in MUTANTS:
    path = ROOT / relpath
    original = path.read_text(encoding="utf-8")
    before = sha(path)
    if find not in original:
        summary.append((mid, "NOT_APPLIED", what, ""))
        print("%-5s NOT_APPLIED  %s" % (mid, what))
        continue
    path.write_text(original.replace(find, repl, 1), encoding="utf-8", newline="")
    try:
        code, failures, out = run_suite()
    finally:
        path.write_text(original, encoding="utf-8", newline="")
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
print("=" * 100)
sys.exit(0 if killed == len(summary) else 1)
