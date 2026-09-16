"""Verification: REPORTED claims become VERIFIED or CONTRADICTED, or stay put.

A structured result is still only what the agent says. Probes look at the
world and return *observations*; ``verify`` folds them into the result under
fixed rules:

- a fact becomes VERIFIED only when a probe observed a value and that value is
  equivalent to what was claimed (or nothing was claimed); the verified fact
  always holds the **observed** value;
- an observed value that differs from the claim makes the fact CONTRADICTED,
  keeping the claim and recording what was observed;
- a probe that cannot decide changes nothing -- the fact stays REPORTED,
  DERIVED or UNKNOWN. Nothing is inferred from a probe being unavailable;
- two probes that observe different values for the same fact make it UNKNOWN
  and signal ``verify.sources_disagree`` (a human decides which source is
  right).

Probes are read-only. ``GitProbe`` only ever runs whitelisted read commands
(``--no-optional-locks`` so ``git status`` does not even refresh the index),
and ``ExecutionRecordProbe`` reuses the facts ADM already verified itself on
the repo-write path (manager.repo_write_enforcement / remote_readback)
instead of re-deriving them.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import subprocess

from manager.nextplan import contracts
from manager.nextplan import result as r
from manager.nextplan import vocabulary as v

# Facts whose contradiction means the agent claimed success that is not true.
SUCCESS_CRITICAL = frozenset({
    "status", "tests_run", "tests_passed", "tests_failed", "tests_skipped", "commit_sha", "commits", "head_sha",
    "remote_sha", "push_status", "git_status", "review_verdict", "reviewed_sha", "ssot_sync_github",
    "ssot_sync_drive",
})
# Facts whose contradiction has a more specific meaning than "false pass".
FIELD_SIGNALS = {
    "worktree": "verify.worktree.path_mismatch",
    "repo": "verify.repo.identity_mismatch",
    "branch": "verify.branch.mismatch",
    "task_id": "result.identity_mismatch",
    "session_id": "result.identity_mismatch",
}

READ_ONLY_GIT = frozenset({"rev-parse", "config", "symbolic-ref", "status", "cat-file", "ls-remote", "merge-base",
                           "diff", "rev-list"})
_UNMERGED = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}


class ProbeUnavailable(RuntimeError):
    """The probe could not look at all. Its facts stay exactly as they were."""


def observation(field, outcome, observed, probe, detail=None):
    if field not in r.FIELD_KINDS:
        raise ValueError(f"unknown fact field {field!r}")
    if outcome not in (v.VERIFIED, v.CONTRADICTED, v.UNKNOWN):
        raise ValueError(f"invalid observation outcome {outcome!r}")
    return {"field": field, "outcome": outcome, "observed": observed, "probe": probe, "detail": detail}


# -- normalization -----------------------------------------------------------------

def normalize_path(path):
    return os.path.normcase(os.path.abspath(str(path))).replace("\\", "/").rstrip("/")


def normalize_repo(url):
    url = str(url).strip()
    match = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)github\.com[/:]([^/]+)/(.+?)(?:\.git)?/?$", url, re.I)
    if match:
        return f"github:{match.group(1).lower()}/{match.group(2).lower()}"
    if re.match(r"^(github|path):", url):
        return url.lower()
    return "path:" + normalize_path(url)


def _norm_file(path):
    path = str(path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def in_scope(path, allowed_paths):
    """Prefix semantics: an allowed entry covers itself and everything below it."""
    if allowed_paths is None:
        return True
    path = _norm_file(path)
    for allowed in allowed_paths:
        allowed = _norm_file(allowed).rstrip("/")
        if path == allowed or path.startswith(allowed + "/"):
            return True
    return False


# What the world can actually distinguish. git sees whether the remote has the
# commit; it cannot tell a push that failed from a push that never happened, so
# claiming "failed" while the world says "not_pushed" is agreement, not a lie.
EQUIVALENT_VALUES = {"push_status": ({"not_pushed", "failed"},)}


def equivalent(field, claimed, observed):
    kind = r.FIELD_KINDS[field]
    if claimed is None or observed is None:
        return claimed is observed
    for group in EQUIVALENT_VALUES.get(field, ()):
        if claimed in group and observed in group:
            return True
    if kind == "sha":
        claimed, observed = claimed.lower(), observed.lower()
        return claimed.startswith(observed) or observed.startswith(claimed)
    if kind == "shalist":
        return len(claimed) == len(observed) and all(equivalent("head_sha", a, b) for a, b in zip(claimed, observed))
    if kind == "strlist":
        return sorted({_norm_file(p) for p in claimed}) == sorted({_norm_file(p) for p in observed})
    if field == "worktree":
        return normalize_path(claimed) == normalize_path(observed)
    if field == "repo":
        return normalize_repo(claimed) == normalize_repo(observed)
    return claimed == observed


# -- probes ---------------------------------------------------------------------------

def subprocess_git(args, cwd, timeout=30):
    completed = subprocess.run(
        ["git", "--no-optional-locks", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return completed.returncode, completed.stdout, completed.stderr


class GitProbe:
    """Read-only facts about the task worktree, its branch and its remote."""

    name = "git"

    def __init__(self, runner=subprocess_git, network=True):
        self._runner = runner
        self._network = network

    def _git(self, cwd, *args):
        if args[0] not in READ_ONLY_GIT:
            raise ValueError(f"GitProbe refuses non-read-only git command {args[0]!r}")
        try:
            return self._runner(list(args), cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            return 128, "", str(exc)

    def run(self, result, context):
        worktree = context.get("worktree")
        if not worktree or not os.path.isdir(worktree):
            raise ProbeUnavailable("no readable worktree")
        code, top, _ = self._git(worktree, "rev-parse", "--show-toplevel")
        if code != 0:
            raise ProbeUnavailable("worktree is not a git work tree")
        obs, signals = [], set()
        add = lambda *a, **k: obs.append(observation(*a, probe=self.name, **k))  # noqa: E731

        observed_worktree = normalize_path(top.strip())
        add("worktree", v.VERIFIED, observed_worktree)
        if normalize_path(worktree) != observed_worktree:
            signals.add("verify.worktree.path_mismatch")

        code, url, _ = self._git(worktree, "config", "--get", "remote.origin.url")
        if code == 0 and url.strip():
            observed_repo = normalize_repo(url)
            add("repo", v.VERIFIED, observed_repo)
            if context.get("repo") and normalize_repo(context["repo"]) != observed_repo:
                signals.add("verify.repo.identity_mismatch")

        code, head, _ = self._git(worktree, "rev-parse", "HEAD")
        head = head.strip() if code == 0 else None
        if head:
            add("head_sha", v.VERIFIED, head)

        code, branch, _ = self._git(worktree, "symbolic-ref", "--short", "-q", "HEAD")
        branch = branch.strip() if code == 0 and branch.strip() else None
        if branch is None:
            signals.add("verify.head.detached")
        else:
            add("branch", v.VERIFIED, branch)
            if context.get("branch") and context["branch"] != branch:
                signals.add("verify.branch.mismatch")

        code, porcelain, _ = self._git(worktree, "status", "--porcelain=v1", "-uall")
        if code == 0:
            dirty = []
            for line in porcelain.splitlines():
                if len(line) < 4:
                    continue
                if line[:2] in _UNMERGED:
                    signals.add("verify.git.unmerged_paths")
                dirty.append(_norm_file(line[3:].split(" -> ")[-1].strip('"')))
            dirty = sorted(set(dirty))
            add("git_status", v.VERIFIED, "dirty" if dirty else "clean")
            add("dirty_paths", v.VERIFIED, dirty)
            allowed = context.get("allowed_paths")
            if any(not in_scope(p, allowed) for p in dirty):
                signals.add("verify.git_status.dirty_out_of_scope")
            if any(in_scope(p, allowed) for p in dirty):
                signals.add("verify.git_status.dirty_in_scope")

        claimed_commit = r.value(result, "commit_sha")
        if claimed_commit and head:
            code, _, _ = self._git(worktree, "cat-file", "-e", f"{claimed_commit}^{{commit}}")
            if code != 0:
                add("commit_sha", v.CONTRADICTED, None, detail="claimed commit does not exist in the task worktree")
            else:
                add("commit_sha", v.VERIFIED, head, detail="final commit must be the worktree HEAD")

        base = context.get("base_sha")
        base_ok = False
        if base and head:
            code, _, _ = self._git(worktree, "merge-base", "--is-ancestor", base, head)
            if code == 0:
                base_ok = True
                code, full_base, _ = self._git(worktree, "rev-parse", base)
                add("base_sha", v.VERIFIED, full_base.strip() if code == 0 else base)
            elif code == 1:
                signals.add("verify.base.behind_required_base")

        if base_ok:
            code, names, _ = self._git(worktree, "diff", "--name-only", base, head)
            if code == 0:
                files = sorted({_norm_file(n) for n in names.splitlines() if n.strip()})
                add("files_changed", v.VERIFIED, files)
                if any(not in_scope(p, context.get("allowed_paths")) for p in files):
                    signals.add("verify.diff.out_of_allowed_paths")
            claimed_commits = r.value(result, "commits")
            if claimed_commits:
                code, listing, _ = self._git(worktree, "rev-list", "--reverse", f"{base}..{head}")
                if code == 0:
                    history = listing.split()
                    expanded = []
                    for claimed in claimed_commits:
                        match = next((sha for sha in history if sha.startswith(claimed.lower())), None)
                        expanded.append(match)
                    if all(expanded):
                        add("commits", v.VERIFIED, expanded)
                    else:
                        add("commits", v.CONTRADICTED, None, detail="a claimed commit is not in base..HEAD")

        target_branch = context.get("branch") or branch
        if self._network and target_branch and head:
            self._remote(worktree, target_branch, head, result, add, signals)
        return obs, signals

    def _remote(self, worktree, branch, head, result, add, signals):
        code, out, _ = self._git(worktree, "ls-remote", "origin", f"refs/heads/{branch}")
        if code != 0:
            signals.add("verify.github.unreachable")
            return
        parts = out.split()
        remote = parts[0] if parts else None
        claimed_push = r.value(result, "push_status") == "pushed" or r.value(result, "remote_sha") is not None
        if remote is None:
            if claimed_push:
                signals.add("verify.remote.branch_absent")
            add("push_status", v.VERIFIED, "not_pushed", detail="remote branch does not exist")
            if r.value(result, "remote_sha") is not None:
                add("remote_sha", v.CONTRADICTED, None, detail="remote branch does not exist")
            return
        add("remote_sha", v.VERIFIED, remote)
        if remote == head:
            add("push_status", v.VERIFIED, "pushed")
            return
        code, _, _ = self._git(worktree, "merge-base", "--is-ancestor", remote, head)
        if code == 0:
            signals.add("verify.push.local_ahead_of_remote")
            add("push_status", v.VERIFIED, "not_pushed", detail="local HEAD is ahead of the remote branch")
        else:
            signals.add("verify.remote.not_ancestor_of_head")


class ExecutionRecordProbe:
    """Facts ADM itself already verified on the bounded repo-write path."""

    name = "adm_execution_record"

    def run(self, result, context):
        evidence = (context.get("execution_record") or {}).get("repo_write_evidence")
        if not evidence:
            raise ProbeUnavailable("no ADM repo_write_evidence")
        obs = []
        if evidence.get("push_status") == "verified":
            final = evidence["final_commit_sha"]
            for field, value in (("remote_sha", evidence["remote_sha"]), ("head_sha", final), ("commit_sha", final),
                                 ("push_status", "pushed"), ("branch", evidence["branch"]),
                                 ("files_changed", sorted(evidence["files_changed"])), ("commits", evidence["commits"])):
                obs.append(observation(field, v.VERIFIED, value, self.name))
        elif evidence.get("push_status") == "not_applicable":
            obs.append(observation("push_status", v.VERIFIED, "not_applicable", self.name))
        return obs, set()


# Commands ADM may treat as an actual test run. The string comes from the Task's
# own declared `validation_command`, which repo_write_enforcement re-runs itself
# in the isolated worktree -- it is not agent free text. Even so it is only ever
# a fallback: an explicit `kind` on the step always wins.
_TEST_RUNNERS = (
    "pytest", "py.test", "unittest", "tox", "nox", "trial", "green",
    "jest", "vitest", "mocha", "ava", "karma",
    "rspec", "minitest", "phpunit", "pester", "invoke-pester",
)
_TEST_SUBCOMMANDS = {"go": {"test"}, "cargo": {"test"}, "dotnet": {"test"}, "mvn": {"test"},
                     "gradle": {"test"}, "npm": {"test"}, "yarn": {"test"}, "pnpm": {"test"},
                     "bun": {"test"}, "swift": {"test"}, "make": {"test", "check"}}
_SEGMENT = re.compile(r"&&|\|\||;|\||\n")
_INTERPRETER = re.compile(r"^(?:python[\d.]*|py|pypy[\d.]*|node|deno|bunx?|npx|pnpx|uv|uvx|poetry|pipenv|"
                          r"hatch|rye|pdm|conda|micromamba|powershell|pwsh|cmd|sh|bash|zsh|env)$")


def is_test_command(command):
    """Does this shell string *look like* a test-runner invocation? Diagnostic only.

    DEMOTED in Round 5. This used to decide whether a recorded run's output
    could become VERIFIED test counts, and Codex broke it with a real
    subprocess::

        echo documentation; pytest & echo ===== 12 passed in 3.10s =====

    _SEGMENT split on ``;`` but not on ``&``, so the printed word ``pytest`` was
    read as an executed program. Adding ``&`` would close that instance and
    leave the shape untouched: the designation is decided for the *command
    string* while the counts are taken from the *whole output*, so nothing ties
    a number to a process. ``pytest && echo "===== 999 passed ====="`` defeats
    any lexical repair of this function.

    It survives as an annotation -- useful for explaining a legacy record to a
    human -- and no longer gates anything. Counts now come only from
    manager.nextplan.runner, which spawns an argv list and reads the runner's
    own structured report.
    """
    if not command or not isinstance(command, str):
        return False
    for segment in _SEGMENT.split(command):
        words = [w for w in re.split(r"\s+", segment.strip()) if w and "=" not in w.split("/")[-1][:1]]
        index = 0
        while index < len(words):
            program = os.path.basename(words[index].strip("'\"")).lower()
            program = re.sub(r"\.(exe|cmd|bat|ps1)$", "", program)
            if program in _TEST_RUNNERS:
                return True
            following = [w.lower() for w in words[index + 1:] if not w.startswith("-")]
            if program in _TEST_SUBCOMMANDS and following and following[0] in _TEST_SUBCOMMANDS[program]:
                return True
            if _INTERPRETER.match(program):
                index += 1
                while index < len(words):
                    token = words[index].lower()
                    if token == "-c":  # inline code is not a runner invocation
                        return False
                    if token in ("-m", "-X", "run", "exec", "--") or token.startswith("-"):
                        index += 1
                        continue
                    break
                continue
            break
    return False


def is_test_step(test):
    """Was this recorded validation step designated a test run? Diagnostic only.

    Demoted with is_test_command: a designation is not evidence that tests ran.
    """
    kind = (test or {}).get("kind")
    if kind is not None:
        return str(kind).lower() in ("test", "tests")
    return is_test_command((test or {}).get("command"))


def adm_test_evidence(execution):
    """Adapt ADM's own recorded validation runs to test evidence.

    Two kinds of record can appear, and only one of them can produce counts.

    ``validation_results`` holds ``adm-validation-result/v1`` objects, each
    written by a runner adapter that spawned an argv list and read the runner's
    own structured report (manager.nextplan.runner). Its counts belong to that
    ``execution_id``, so they are admissible.

    ``tests`` holds the legacy shell-string records: a command line, an exit
    code, and whatever the command printed. **These can never yield counts.**
    Not because the parser is weak, but because the record itself cannot
    distinguish output a test runner produced from output a command echoed --
    the evidence needed to tell those apart was never captured. The exit code is
    still carried, so a failing run is not lost; it simply cannot say how many
    tests ran, and ``tests_run`` stays UNKNOWN.

    An unproven run therefore fails the completion proof instead of satisfying
    it. That is the point: ``true`` exits 0, and so does ``echo``.
    """
    evidence = (execution or {}).get("repo_write_evidence") or {}
    runs = []
    for block in evidence.get("validation_results", []):
        problems = contracts.validation_problems(block)
        run = {"execution_id": block.get("execution_id") if isinstance(block, dict) else None,
               "argv": block.get("argv") if isinstance(block, dict) else None,
               "runner": block.get("runner") if isinstance(block, dict) else None,
               "exit_code": block.get("exit_code") if isinstance(block, dict) else None,
               "timed_out": bool(block.get("timed_out")) if isinstance(block, dict) else True,
               "structured": not problems, "problems": problems}
        counts = contracts.validation_counts(block) if not problems else None
        if counts:
            run.update(passed=counts["passed"], failed=counts["failed"], skipped=counts["skipped"])
        runs.append(run)
    for test in evidence.get("tests", []):
        # Legacy record: exit status only, by construction. `test_step` is kept
        # for human explanation and is deliberately not consulted for counts.
        runs.append({"command": test.get("command"), "exit_code": test.get("exit_code"),
                     "timed_out": bool(test.get("timed_out")), "test_step": is_test_step(test),
                     "structured": False,
                     "problems": ["legacy shell-string record: output cannot be bound to a test process"]})
    return {"source": "adm_run", "runs": runs} if runs else None


class TestEvidenceProbe:
    """Compare test claims with test runs ADM (or a trusted artifact) recorded."""

    name = "test_evidence"
    __test__ = False  # not a pytest test class

    def run(self, result, context):
        evidence = context.get("test_evidence")
        if not evidence or not evidence.get("runs"):
            raise ProbeUnavailable("no test evidence")
        runs = evidence["runs"]
        failed_run = any(run.get("timed_out") or run.get("exit_code") != 0 for run in runs)
        obs, signals = [], set()
        add = lambda *a, **k: obs.append(observation(*a, probe=self.name, **k))  # noqa: E731
        # Every run must carry counts AND the identity of the process they came
        # from: an execution_id and the argv that was spawned. Those two fields
        # are what a legacy shell-string record structurally cannot supply, and
        # they are what ties a number to a run rather than to a string that
        # happened to be printed. One unbound record in the set withholds all
        # counts -- a total assembled partly from unbound output is not a
        # measurement of anything.
        counted = all(run.get("execution_id") and run.get("argv")
                      and isinstance(run.get("passed"), int) and isinstance(run.get("failed"), int)
                      for run in runs)
        if counted:
            observed = {
                "tests_passed": sum(run["passed"] for run in runs),
                "tests_failed": sum(run["failed"] for run in runs),
                "tests_skipped": sum(int(run.get("skipped") or 0) for run in runs),
            }
            observed["tests_run"] = observed["tests_passed"] + observed["tests_failed"]
            for field, value in observed.items():
                add(field, v.VERIFIED, value)
                claimed = r.get(result, field)
                if claimed["source"].startswith("agent:") and claimed["value"] is not None and claimed["value"] != value:
                    signals.add("verify.tests.count_mismatch")
        elif not failed_run:
            add("tests_failed", v.VERIFIED, 0, detail="every recorded run exited 0")
        elif r.value(result, "tests_failed") == 0:
            add("tests_failed", v.CONTRADICTED, None, detail="a recorded run failed")
        if failed_run:
            signals.add("verify.tests.failed")
            if r.value(result, "status") == "PASS":
                add("status", v.CONTRADICTED, None, detail="required tests failed")
        return obs, signals


class DriveReadbackProbe:
    """Read evidence back from Drive; an upload response is never proof."""

    name = "drive_readback"

    def __init__(self, reader):
        self._reader = reader

    def run(self, result, context):
        target = context.get("drive_evidence")
        if not target:
            raise ProbeUnavailable("no Drive evidence target")
        try:
            data = self._reader(target["file_id"])
        except Exception as exc:  # noqa: BLE001 - any read failure is "could not look"
            return [observation("ssot_sync_drive", v.UNKNOWN, None, self.name,
                                f"read-back failed: {type(exc).__name__}")], {"verify.drive.unreachable"}
        if hashlib.sha256(data).hexdigest() == target["expected_sha256"]:
            return [observation("ssot_sync_drive", v.VERIFIED, "synced", self.name)], set()
        return ([observation("ssot_sync_drive", v.VERIFIED, "failed", self.name, "read-back digest mismatch")],
                {"verify.drive.readback_mismatch"})


class DispatchRecordProbe:
    """Identity ADM itself assigned at dispatch."""

    name = "dispatch_record"

    def run(self, result, context):
        record = context.get("dispatch")
        if not record:
            raise ProbeUnavailable("no dispatch record")
        return [observation(field, v.VERIFIED, record[field], self.name)
                for field in ("task_id", "session_id", "agent", "model") if record.get(field)], set()


# -- folding --------------------------------------------------------------------------

def _apply(result, field, observations, signals):
    current = result["facts"][field]
    decisive = [o for o in observations if o["outcome"] != v.UNKNOWN]
    if not decisive:
        return current
    verified_values = [o["observed"] for o in decisive if o["outcome"] == v.VERIFIED]
    distinct = []
    for value in verified_values:
        if not any(equivalent(field, value, seen) for seen in distinct):
            distinct.append(value)
    if len(distinct) > 1:
        signals.add("verify.sources_disagree")
        probes = ", ".join(sorted({o["probe"] for o in decisive}))
        return r.fact(None, v.UNKNOWN, "probe:conflict", f"probes disagree ({probes}): {distinct!r}"[:300])

    contradiction = next((o for o in decisive if o["outcome"] == v.CONTRADICTED), None)
    claimed = current["value"]
    if contradiction is not None:
        if claimed is None:
            return current  # nothing was claimed, so nothing is contradicted
        updated = r.fact(claimed, v.CONTRADICTED, f"probe:{contradiction['probe']}", contradiction["detail"])
    else:
        observed = decisive[0]
        if claimed is None or equivalent(field, claimed, observed["observed"]):
            updated = r.fact(observed["observed"], v.VERIFIED, f"probe:{observed['probe']}", observed["detail"])
        else:
            detail = f"observed {observed['observed']!r}" + (f"; {observed['detail']}" if observed["detail"] else "")
            updated = r.fact(claimed, v.CONTRADICTED, f"probe:{observed['probe']}", detail[:300])
    if updated["level"] == v.CONTRADICTED and current["source"].startswith("agent:"):
        signals.add(FIELD_SIGNALS.get(field) or ("verify.claim_contradicted" if field in SUCCESS_CRITICAL else None))
        signals.discard(None)
    return updated


def verify(result, context, probes):
    """Return ``(verified_result, report)``. Never mutates ``result``."""
    observations, signals, ran, unavailable = [], set(), [], []
    for index, probe in enumerate(probes):
        try:
            found, probe_signals = probe.run(result, context)
        except ProbeUnavailable as exc:
            unavailable.append({"probe": probe.name, "reason": str(exc)})
            continue
        ran.append(probe.name)
        observations.extend((index, o) for o in found)
        signals |= set(probe_signals)

    verified = copy.deepcopy(result)
    by_field = {}
    for index, item in sorted(observations, key=lambda pair: (pair[1]["field"], pair[0])):
        by_field.setdefault(item["field"], []).append(item)
    for field, items in by_field.items():
        verified["facts"][field] = _apply(verified, field, items, signals)
    r.validate_result(verified)
    report = {
        "observations": [item for _, item in sorted(observations, key=lambda pair: (pair[1]["field"], pair[0]))],
        "signals": sorted(signals),
        "probes_run": ran,
        "probes_unavailable": unavailable,
    }
    return verified, report
