# M0 Gate 0 — Thin Windows Session Center

Status: Gate 0-A PASS; Gate 0-B awaiting an authoritative ADM Execution record.

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

Gate 0-B is not claimed for the current external Codex Desktop session because
an authoritative Execution record was not available locally and Google Drive
SSOT access was not approved. The shortest completion path is an ADM-launched
disposable read-only Codex execution, whose existing runner persists the native
thread ID as `provider_session_id` before the turn starts.
