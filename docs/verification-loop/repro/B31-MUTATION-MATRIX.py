"""Phase B3-1 mutation matrix: neutralise the consumption binding, watch it die.

Each mutant removes or bypasses exactly the guard B3-1 added -- never a
syntax error, never a sibling guard -- and names the test that must be among
the failures. A mutant killed only by some other test is scored
KILLED_WRONG_REASON, not as a kill, because that would mean the named test
does not actually depend on the guard (the vacuity trap all three prior
reviews found). The NB-A attack is re-run under each mutant as well, so the
matrix shows the attack *reopening*, not merely a test going red.

    PYTHONPATH=. python docs/verification-loop/repro/B31-MUTATION-MATRIX.py [<scratch-dir>]

Run from a scratch clone with a temporary AI_MANAGER_HOME. ROOT is the
repository root (this file lives three directories below it). Every mutated
file is sha256-verified restored byte-for-byte before the next mutant runs,
and the script exits non-zero unless every mutant is KILLED by its own target.
"""

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRATCH = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "./b31-mutants").resolve()
HARNESS = ROOT / "docs" / "verification-loop" / "repro" / "B31-NBA-FORGED-CONSUMPTION.py"
TEST_FILE = "manager/test_verification_loop_b3_1_consumption_binding.py"
TICKETS = ROOT / "manager" / "verification_loop" / "tickets.py"
STORES = ROOT / "manager" / "verification_loop" / "stores.py"

MUTANTS = [
    {
        "id": "M-B31-1",
        "guard": "consumption_divergence_field always reports agreement",
        "file": TICKETS,
        "search": (
            "    for name in issue_frozen_fields():\n"
            "        if getattr(issued, name) != getattr(consumption, name):\n"
            "            return name\n"
            "    return None\n"
        ),
        "replace": (
            "    for name in issue_frozen_fields():\n"
            "        if getattr(issued, name) != getattr(consumption, name):\n"
            "            return None\n"
            "    return None\n"
        ),
        "target": TEST_FILE + "::ForgedConsumptionTests::test_nba_1_a_consumption_naming_a_rogue_expected_checker_is_refused",
    },
    {
        "id": "M-B31-2",
        "guard": "_fold_ticket never consults the binding (faithful revert to b38ceb2 fold)",
        "file": STORES,
        "search": (
            "    for record in consumed:\n"
            "        divergent = consumption_divergence_field(issued[0], record)\n"
            "        if divergent is not None:\n"
        ),
        "replace": (
            "    for record in ():\n"
            "        divergent = consumption_divergence_field(issued[0], record)\n"
            "        if divergent is not None:\n"
        ),
        "target": TEST_FILE + "::ForgedConsumptionTests::test_nba_4_forged_consumption_plus_forged_pass_never_accepts",
    },
    {
        "id": "M-B31-3",
        "guard": "expected_checker_identity declared mutable by consumption",
        "file": TICKETS,
        "search": '    {"status", "consumed_report_digest", "consumed_at", "ticket_seq"}\n',
        "replace": '    {"status", "consumed_report_digest", "consumed_at", "ticket_seq", "expected_checker_identity"}\n',
        "target": TEST_FILE + "::ConsumptionDivergenceFieldTests::test_pin_1_exactly_four_fields_are_mutable_by_consumption",
    },
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, **kw):
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, **kw)


def pytest_failures(target_file):
    proc = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--no-header", "-rfE", target_file])
    failed = set()
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failed.add(line.split(" ", 1)[1].split(" - ")[0].strip())
    return proc.returncode, failed, proc.stdout[-1500:]


def attack_outcome(tag):
    proc = run([sys.executable, str(HARNESS), str(SCRATCH / tag)])
    a1 = next((l.strip() for l in proc.stdout.splitlines() if l.strip().startswith("A1 ")), "A1 ?")
    summary = next((l for l in proc.stdout.splitlines() if l.startswith("false_accepts=")), "")
    return proc.returncode, a1, summary


def tree_status():
    return run(["git", "status", "--porcelain", "--", "manager/verification_loop"]).stdout.strip()


print("ROOT = %s" % ROOT)
status_before = tree_status()
print("git status before (manager/verification_loop): %s" % (status_before or "clean"))
print("baseline ...")
code, failed, _tail = pytest_failures(TEST_FILE)
if code != 0 or failed:
    print("BASELINE NOT GREEN -- aborting: %s" % sorted(failed))
    sys.exit(2)
code, a1, summary = attack_outcome("baseline")
print("  baseline attack: %s | %s | harness exit %d" % (a1, summary, code))
if code != 0:
    print("BASELINE ATTACK NOT CLOSED -- aborting")
    sys.exit(2)

verdicts = {}
for mutant in MUTANTS:
    path = mutant["file"]
    original = path.read_bytes()
    original_sha = sha(path)
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    search = mutant["search"].replace("\n", newline)
    replacement = mutant["replace"].replace("\n", newline)
    if search not in text:
        verdicts[mutant["id"]] = "NOT_APPLIED"
        print("%s NOT_APPLIED (search text absent)" % mutant["id"])
        continue
    path.write_bytes(text.replace(search, replacement, 1).encode("utf-8"))
    try:
        code, a1, summary = attack_outcome(mutant["id"])
        tcode, failed, tail = pytest_failures(TEST_FILE)
        target_hit = mutant["target"] in failed
        if target_hit:
            verdict = "KILLED"
        elif failed or tcode != 0:
            verdict = "KILLED_WRONG_REASON"
        else:
            verdict = "SURVIVED"
        verdicts[mutant["id"]] = verdict
        print("%s %s" % (mutant["id"], verdict))
        print("    guard   : %s" % mutant["guard"])
        print("    attack  : %s | %s | harness exit %d" % (a1, summary, code))
        print("    failures: %s" % (sorted(failed) or "none"))
        print("    target  : %s" % mutant["target"].split("::", 1)[1])
    finally:
        path.write_bytes(original)
        restored = sha(path)
        print("    restored: %s" % ("byte-exact" if restored == original_sha else "MISMATCH " + restored))
        if restored != original_sha:
            sys.exit(3)

status_after = tree_status()
print("")
print("git status after  (manager/verification_loop): %s" % (status_after or "clean"))
# The matrix must leave the tree exactly as it found it. It is not entitled
# to demand a clean tree -- it may run before the fix is committed -- but it
# is entitled to demand that it changed nothing.
unchanged = status_after == status_before
print("tree unchanged by matrix: %s" % unchanged)
killed = sum(1 for v in verdicts.values() if v == "KILLED")
print("KILLED %d/%d  %s" % (killed, len(MUTANTS), verdicts))
sys.exit(0 if killed == len(MUTANTS) and unchanged else 1)
