# Spike: OpenHarness (HKUDS/OpenHarness, PyPI `openharness-ai==0.1.9`) direct adoption (Windows) — findings

- Date: 2026-08-15
- Branch: `spike/openharness-direct-adoption` (from `origin/main` @ `bbc9c24`)
- Session: openharness-windows-adoption-spike

## Existence verification (raw HTTP, not WebFetch summary)

- `https://api.github.com/repos/HKUDS/OpenHarness` → HTTP 200. Real, public,
  not archived. 15,369 stars. Default branch `main`. Created 2026-04-01,
  last push 2026-06-04 (≈2.5 months stale as of this spike).
- `https://pypi.org/pypi/openharness-ai/json` → HTTP 200. Latest `0.1.9`
  (2026-05-07). Summary: "Open-source Python port of Claude Code."
- A first WebFetch-tool summary of the GitHub page included several very
  specific quotes; these were cross-checked against the raw
  `raw.githubusercontent.com/.../README.md` and confirmed accurate (not
  hallucinated) — but this cross-check is why Phase A took longer than
  planned. Treat single WebFetch summaries of unfamiliar repos as
  unverified until spot-checked against a raw source in future spikes.

## Phase A — Windows install: PASS

- No Python 3.12 available on this machine (only 3.14) — did not install a
  new runtime; used 3.14 in an isolated venv per task's own fallback
  instruction ("如果仅安装标准 Python 版本即可继续... 不得改系统级全局配置").
- `pip install openharness-ai==0.1.9` in `C:\th-spike\.venv-openh` — succeeded.
- `openh --version` → `openharness 0.1.9`
- `openh --help` → real, rich CLI (session/model/output/permission/system
  flags, `setup`/`auth`/`provider`/`mcp`/`plugin`/`cron`/`autopilot`
  subcommands). Confirms `--dangerously-skip-permissions` exists and was
  **not** used, per task constraint.
- `where.exe claude` → `C:\Users\EE\.local\bin\claude.exe` (2.1.226)
- `where.exe codex` → `C:\Users\EE\AppData\Roaming\npm\codex(.cmd)` (0.147.0)
  Both present and untouched — no logout/reconfiguration performed.

## Phase B — Auth/subscription-reuse gate: verified from source, not README

Read actual source from `main` branch (raw.githubusercontent.com), not
just README claims:

- `src/openharness/auth/external.py` — `default_binding_for_provider()`
  points `CODEX_PROVIDER` at `$CODEX_HOME/auth.json` (default
  `~/.codex/auth.json`) and `CLAUDE_PROVIDER` at
  `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude/.credentials.json`,
  or macOS Keychain). Both files exist on this machine (existence checked
  only, contents never read/printed).
- `src/openharness/api/codex_client.py` — does **not** shell out to the
  `codex` binary. It extracts the `chatgpt_account_id` from the JWT in
  `auth.json` and makes its own `httpx` requests directly to
  `https://chatgpt.com/backend-api/codex/responses` — ChatGPT's internal,
  undocumented backend, not OpenAI's public API — with header
  `originator: openharness` (at least it identifies itself, doesn't spoof
  as `codex-cli`).
- `external.py` also hardcodes Claude Code's own OAuth `client_id`
  (`9d1c250a-e61b-44d9-88ed-5944d1962f5e`) and beta headers
  (`claude-code-20250219`, `oauth-2025-04-20`) to refresh/use the Claude
  Code OAuth token directly against `platform.claude.com` /
  `console.anthropic.com` — i.e. it presents itself to Anthropic's API as
  the official Claude Code client using the harvested token.

**Conclusion: this is option A (reuses existing subscription, no separate
API key) but via extracted-OAuth-token + private/undocumented-endpoint
calls, not via subprocess-invoking the official `claude`/`codex`
binaries.** This is a materially different, higher-risk mechanism than
"launch the already-logged-in CLI as a child process": no official-CLI
safety/sandboxing layer is involved, and it depends on an internal API
surface that can change or flag anomalous clients without notice. Flagged
this explicitly to the user before proceeding — see chat log for the
approval and stated constraints (no login-state mutation, no token
plaintext output, read-only, no auto-fallback to API keys on failure).

`openh auth --help` confirms the mechanism at the UX level too:
`codex-login` = "Bind OpenHarness to a local Codex CLI **subscription
session**"; `claude-login` = same for Claude — "bind", not "launch".

`openh auth status` (before any binding) — output is state-only, no
secrets:

```
Codex subscription       missing        missing
Claude subscription      missing        missing
```

## Phase C — Real disposable E2E: BLOCKED, not completed

- `openh --dry-run -p "Read README.md and output only its first line"` run
  inside the spike worktree — PASS as a dry run: confirms `read_file` tool
  exists, default profile is `claude-api` (Anthropic API-key path, not yet
  the subscription bridge), `permission_mode: default`, and correctly
  reports `auth: missing` / readiness `blocked` since nothing was bound
  yet. No live call made.
- Attempted `openh auth codex-login` to bind the real local Codex
  subscription (with explicit user approval to proceed, and explicit
  constraints: no login-state mutation, no token plaintext output,
  read-only only, no silent fallback to API keys) — **blocked by Claude
  Code's own auto-mode permission classifier**, independent of
  OpenHarness or the Codex/Claude CLIs themselves:
  > "Permission for this action was denied by the Claude Code auto mode
  > classifier... STOP and explain to the user what you were trying to do
  > and why you need this permission."
- Did not attempt to route around this (e.g. hand-writing a binding config
  to fake the effect) — that would defeat the purpose of the classifier
  and wasn't part of what the user approved.
- **Net result: `claude-login`/`codex-login` binding, and therefore any
  live disposable E2E call through the subscription bridge, was not
  executed. No worker/session/run.json/logs exist to report — none were
  produced.** Do not treat the Phase B source-code findings as a
  substitute for an actual executed E2E; they establish *how* the bridge
  would work, not that a live call was verified end-to-end.

## Phase D — overlap with ADM (partial, since E2E didn't run)

From `openh --help` / `--dry-run` output alone, OpenHarness already ships:
provider abstraction (11+ backends), session resume (`--continue`/
`--resume`), permission modes, MCP client, skills, cron scheduler,
`autopilot`, JSON/stream-json output. This overlaps with a large chunk of
what a hand-rolled ADM runtime layer would otherwise need to build — but
this spike could not verify any of it actually *executes* correctly for
Codex/Claude subscription workflows on Windows, because Phase C didn't run.

## Adoption decision — FINAL (2026-08-15)

**E. REJECT for current P0.**

Rationale (user-confirmed, closing this spike without completing Phase C):

- Windows CLI install/runtime works natively — this part is not the
  blocker.
- Claude/Codex "subscription reuse" is **not** through the official local
  CLI (`claude.exe` / `codex`/`codex.cmd`). It works by reading the CLIs'
  stored OAuth credentials (`~/.claude/.credentials.json`,
  `~/.codex/auth.json`) and calling undocumented private backends directly
  (`chatgpt.com/backend-api/codex/responses`; Anthropic's OAuth/inference
  endpoints using Claude Code's own `client_id` and beta headers) — see
  Phase B above for the exact source evidence.
- Current P0 requires official-CLI-based access. This mechanism does not
  meet that bar, independent of whether it would technically work — so
  Phase C was intentionally not completed and no further OpenHarness
  investigation is planned.

This branch (`spike/openharness-direct-adoption`) is kept as a pushed,
disposable record only. No further Phase C/E2E, no auth binding, and no
OpenHarness source changes were made or are planned. Harness research for
this P0 stops here — no new candidate is queued as a follow-up from this
task.
