# M0 Gate 0 — Thin Windows Session Center

Status: Gate 0-A PASS; Gate 0-B PASS.

The Windows PoC deliberately bypasses AASC runtime dependencies. It is one
Python standard-library HTTP server bound to `127.0.0.1`. The browser polls the
local endpoint; no scheduled watcher or automatic task dispatch is installed.

## Verified command

```powershell
python -m manager.session_center `
  --provider-session-id 019fff9d-05b6-7221-ad04-dc208c78f69c `
  --project-id ai-development-manager `
  --task-id m0-gate0-session-center-poc `
  --branch codex/m0-gate0-session-center-poc
```

Open `http://127.0.0.1:8765`.

The UI showed the real active Codex Desktop session with provider-native ID,
cwd, branch, provider `started_at`, live state, and latest activity. It reads
only the first `session_meta` line and the JSONL file size; transcript content
is never read.

## Deterministic ADM correlation

Pass a read-only export of an existing ADM Execution:

```powershell
python -m manager.session_center `
  --provider-session-id <native-thread-id> `
  --execution-file <execution.json>
```

The server marks the session `CORRELATED` only after exact
`provider_session_id` equality and normalized cwd equality with
`task_snapshot.working_directory`. It then displays the Execution's provider,
Project, Task ID, Execution ID, cwd, and branch. Without that record it displays
`UNLINKED`; it never guesses from timestamps or cwd.

The external Codex Desktop session remains intentionally unlinked because it
has no authoritative ADM Execution. Gate 0-B was instead proven by resuming an
existing read-only Execution using the same provider-native ID.

## Visible auto-launch

Session Center can wait directly on the Drive Execution SSOT before the runner
starts:

```powershell
python -m manager.session_center `
  --execution-project-id ai-development-manager `
  --execution-id <execution-id> `
  --wait-seconds 180
```

The page starts only after ADM persists the provider-native session link. The
runner also terminalizes the Session Registry record after the Task and
Execution terminal state is durable.

### Verified Phase 3 run

- Task: `m0-gate0-visible-auto-launch-20260814-1926-c2`
- Execution: `m0-gate0-visible-auto-launch-20260814-1926-c2-run-2`
- Provider session: `01a0001b-4d1d-74d1-b87d-4db6b5c355e0`
- Result: Task, Execution, and Session Registry all reached `completed`; the
  Session Center card was automatically `CORRELATED` from the Drive Execution.
- Access: disposable read-only; writer lease not required; task claim released.
- No scheduled watcher or hands-off dispatch was enabled.

The default CLI history load was blocked by a newer recovery Execution whose
fields are not yet in this branch's schema. The spike reused the existing
`launch_task(..., executions=[])` entry point to skip optional historical
estimation without changing the recovery branch or bypassing lifecycle gates.
