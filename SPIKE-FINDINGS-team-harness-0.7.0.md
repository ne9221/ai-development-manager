# Spike: team-harness 0.7.0 direct adoption (Windows) — findings

- Date: 2026-08-15
- Branch: `spike/team-harness-direct-adoption` (from `origin/main` @ `bbc9c24`)
- Session: team-harness-windows-adoption-spike

## Result: REJECT (for native Windows) — stopped at Adoption Decision per task guardrail

`team-harness==0.7.0` does not run at all on native Windows Python. The `th`
entry point crashes during import, before argument parsing, so `th --help`
and `th init` both fail with the same traceback:

```
File "team_harness\cli.py", line 38, in <module>
    from team_harness.tracking.reaper import DEFAULT_DRAIN_TIMEOUT_S
File "team_harness\tracking\reaper.py", line 38, in <module>
    import fcntl
ModuleNotFoundError: No module named 'fcntl'
```

`fcntl` is POSIX-only (advisory file locking). It is not an optional feature
behind a flag — it's imported unconditionally at module load time by
`cli.py`, so every `th` invocation fails identically. This cannot be worked
around via `.team-harness/config.toml` or a worker-command override: the
crash happens before any config is read.

POSIX process-group dependence is not limited to one file — confirmed in
three separate subsystems:

- `team_harness/tracking/reaper.py:38` — `import fcntl` (crash-durable
  advisory lock for the post-crash worker reaper)
- `team_harness/agents/process_identity.py:244` — `os.killpg(pgid, sig)`
- `team_harness/tools/shell_tools.py:81,93` — `os.killpg(proc.pid, ...)`
  (SIGTERM/SIGKILL on a process group)

Per the design doc referenced in `reaper.py`'s own docstring
(`design/designs/process-lifecycle-and-reaping.md`), the reaper's entire
crash-recovery model is built on POSIX pgid/starttime identity plus signals.
A Windows port would need Job Objects (or equivalent) and a different
locking primitive across all three subsystems — a real architectural
port, not a minimal patch or config toggle.

## What was and wasn't tested

- Confirmed present/working independently of team-harness:
  `claude.exe` (Claude Code 2.1.226) and `codex`/`codex.cmd`
  (codex-cli 0.147.0) both resolve and run `--version` fine on this
  machine — so the blocker is entirely in team-harness's Windows support,
  not in the underlying CLIs it would drive.
- Did NOT reach Phase B (Codex/Claude override config review) or Phase C
  (disposable E2E dispatch) — `th` cannot start, so there is nothing to
  configure or dispatch to. Attempting either would require first patching
  team-harness (i.e. forking), which the task explicitly said to stop
  before doing and instead report back.
- WSL (`wsl.exe`) exists on this machine but has no Linux distribution
  installed — untested as an alternative runtime path. Installing a distro
  was out of scope for this spike (new install, needs separate
  confirmation) and wasn't attempted.

## Adoption decision

**C. FORK REQUIRED** if Windows-native support is a hard requirement —
minimum scope would be: replace the `fcntl` advisory lock with a
cross-platform primitive (e.g. `msvcrt.locking` on Windows), and replace
the three `os.killpg` call sites with a Windows-compatible process-group
kill (e.g. spawn workers in a Job Object and terminate the job). This
touches core lifecycle/reaping code, not a thin wrapper — not something to
take on inside this 20-minute spike.

**D. REJECT for now** is the pragmatic default unless the user wants to
fund the fork above, OR unless running ADM's orchestration host inside WSL
(untested) turns out to be acceptable — that would need its own follow-up
spike (install a WSL distro, install Claude/Codex CLIs there, retest).

## Not stopped/blocked on

- Repo/HEAD verification, worktree creation, and package install all
  succeeded cleanly — see session report for exact commands and output.
