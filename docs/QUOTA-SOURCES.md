# Quota Sources - PoC Findings

Summary of the Phase-0 PoC rounds. Each round tested one provider on one
real Windows machine; nothing here is from vendor documentation alone
unless explicitly marked.

## Codex - verified automatic, official

- `codex-cli 0.146.0` has an `app-server` subcommand (stdio JSON-RPC,
  `[experimental]`).
- Real request/response captured:
  `account/rateLimits/read` -> `{"primary":{"usedPercent":99,
  "windowDurationMins":10080,"resetsAt":<epoch>},"secondary":null,
  "credits":{...},"planType":"plus",...}`.
- This account has exactly **one** window (7 days / 10080 minutes), not the
  commonly-assumed 5h+weekly pair. Do not hardcode window names/counts.
- Not affected by this machine's separate TLS-interception problem (that
  only broke the *other*, now-abandoned approach: `codex_usage.py`'s HTTPS
  calls to `/wham/usage` etc, which is not part of this design).

## Claude Code - technically automatic, not yet collectible here

- Official mechanism: Claude Code pipes a JSON blob (including
  `rate_limits.five_hour` / `rate_limits.seven_day`, each with
  `used_percentage` + `resets_at`) to a configured `statusLine` command's
  stdin. Only populated for Pro/Max after the first API response in a live
  interactive session. No standalone `claude usage` command exists.
- On the test machine: no `claude` CLI binary reachable from the automated
  shell, no `~/.claude/settings.json` (so no statusLine configured).
  `~/.claude/projects` does have real session JSONL.
- Fallback tested and working: `claude-monitor --once --output json`
  (pip package) parses that JSONL. Real output had `five_hour.used_percentage:
  31.3`, `resets_at`, `confidence: "local_estimate"`; `seven_day` fields were
  all `null`.

### P0.0 root-cause update (2026-08-15) - why capture went stale for 6 days

Real, reproducible evidence that the `statusLine` channel is structurally
unreachable from either of the two ways Claude is actually run today, not a
collector/refresh code defect:

- `collectors/claude.py::normalize()` and `manager/refresh_status.py` were
  re-read line by line; both already handle a rate-limits-less payload
  correctly (empty `windows`, `confidence: "unknown"`, no fabricated
  percentage, old official snapshot preserved if one exists). All 12
  `collectors` tests and 52 `manager` tests (including
  `test_refresh_status.py`'s `test_unavailable_provider_preserves_old_value`
  and `test_empty_claude_capture_preserves_official_snapshot`) already cover
  this and pass. No code bug found.
- `~/.claude/statusline-payload.json` was last written 2026-08-09 (by
  whatever the last real *interactive terminal* session was) and even that
  snapshot has no `rate_limits` key at all - it was captured before the
  session's first API response (`cost.total_api_duration_ms: 0`).
- `manager/claude_launcher.py::_build_argv()` (the real, already-shipped
  `ClaudeLauncher` - not re-implemented this round) always invokes
  `claude -p --session-id ... --input-format stream-json --output-format
  stream-json --verbose ...`. Reproduced that exact invocation shape
  directly against the real, currently-logged-in Pro account:
  `claude -p "Reply with exactly: OK"` returned a real `OK` (a genuine API
  turn happened) but `statusline-payload.json`'s mtime was byte-for-byte
  unchanged before and after (`1786248538` both times). `-p`/headless mode
  does not invoke the `statusLine` hook at all - confirmed empirically, not
  assumed from docs.
- Checked for an alternative official headless source: `claude --help` has
  no `usage`/`quota`/`rate-limit` subcommand; `claude auth status --json`
  (real, logged-in) returns only `{loggedIn, authMethod, apiProvider, email,
  orgId, orgName, subscriptionType}` - no rate-limit fields anywhere.
- Conclusion: the *only* official source of `rate_limits.five_hour` /
  `seven_day` is the interactive-terminal `statusLine` hook, which fires on
  the ink-based TUI's render loop. Neither of Claude's two real usage
  patterns on this machine - (a) `ClaudeLauncher`'s headless `-p` dispatch,
  (b) this Desktop-app chat session itself (its own `statusline-payload.json`
  mtime never moved across an entire long multi-turn real session) - drives
  that render loop. This is a platform constraint external to this repo, not
  a defect in the collection/refresh pipeline, which is why fixing it
  requires either an unattended tool like `claude-monitor`
  (non-official, `confidence: local_estimate`, needs a new pip dependency -
  deliberately deferred to a separate task rather than added under this P0.0
  fix) or a periodic human-attended interactive session - not a change to
  `collectors/claude.py` or `manager/refresh_status.py`.

## Antigravity - verified automatic, official (2026-09-02)

Supersedes the v0.1 "manual / CDP inconclusive" finding below, which is kept
for history.

- Antigravity IDE 1.107.0 on this machine ships **no standalone `agy` binary**
  (whole-machine search). Its real machine surface is the bundled language
  server `resources/app/extensions/antigravity/bin/language_server_windows_x64.exe`
  (build label `agy_ls_release_branch/2.7`), which the IDE starts with a
  per-run `--csrf_token <uuid>` argument and which listens on two random
  loopback ports (HTTPS/gRPC and plain HTTP). `~/.gemini/antigravity-ide/bin/
  agentapi.bat` is only a thin wrapper around `language_server ... agentapi`.
- Real request/response captured against the live server (Connect-RPC JSON,
  header `x-codeium-csrf-token`, **no API key needed**, no model turn consumed):
  `POST http://127.0.0.1:<http_port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`
  -> `{"response":{"groups":[{"displayName":"Gemini Models","buckets":[
  {"bucketId":"gemini-weekly","window":"weekly","remainingFraction":0.675,
  "resetTime":"2026-09-04T08:20:30Z"},{"bucketId":"gemini-5h","window":"5h",
  "remainingFraction":1,"resetTime":...}]},{"displayName":"Claude and GPT
  models","buckets":[{"bucketId":"3p-weekly",...},{"bucketId":"3p-5h",...}]}]}}`.
  `GetUserStatus` adds `userStatus.email/name`, `planStatus.planInfo.planName`
  (Pro), prompt/flow credits and per-model `quotaInfo.remainingFraction/resetTime`.
- protobuf JSON omits zero-valued scalars: a bucket with no `remainingFraction`
  but a `resetTime` is an exhausted bucket (0), not an unknown one.
- Discovery: `manager/ag_language_server.py` enumerates the same-user
  `language_server*` process (Win32_Process argv -> CSRF nonce, `--app_data_dir
  antigravity-ide`, executable under a real Antigravity install), lists its
  listening ports (`netstat -ano`) and probes `GetStatus` to tell the HTTP port
  from the HTTPS one. The IDE must be running (or the language server left
  alive via the IDE setting `antigravity.persistentLanguageServer`); otherwise
  the collector fails closed with `ide_not_running` and the last-good entry is
  kept with `metadata.refresh` explaining why.
- Collector: `collectors/antigravity.py` -> `source =
  antigravity_language_server_quota_summary`, `source_type = official`,
  `confidence = official`, four windows named by bucket id (`gemini-5h`,
  `gemini-weekly`, `3p-5h`, `3p-weekly`; 300 / 10080 minutes). The CSRF nonce
  is never logged or persisted; the Google OAuth token is never read (it stays
  in the IDE, which performs every authenticated call).

## Antigravity - manual in v0.1, CDP path inconclusive (historical)

- Filesystem scan of `AppData\Roaming\Antigravity\` (app_storage.json,
  Preferences, Local State, logs\*.log, Local Storage\leveldb) found zero
  quota/usage/rateLimit-related strings.
- `DevToolsActivePort` file exists and is readable (`<port>\n
  /devtools/browser/<uuid>`), suggesting a Chrome DevTools Protocol port is
  opened at startup - a real lead, not yet working.
- Round 2: with Antigravity confirmed running and Settings > Models open,
  `curl http://127.0.0.1:<port>/json/version` and `/json/list` both failed
  with **connection refused** (curl exit 7). The port value read from
  `DevToolsActivePort` matched the previous (pre-restart) run exactly,
  suggesting it may be a stale/leftover value rather than the live port, or
  that CDP isn't exposed externally by default.
- Verdict: `CDP reachable, quota source not located` was the target
  classification; actual result did not even reach "reachable" -
  connectivity itself is unconfirmed. Marked inconclusive, not "impossible".

## Gemini App / Google AI Pro - manual in v0.1

- No `gemini` or `gcloud` CLI executable found in PATH.
- `~/.gemini` exists but its contents (`antigravity/`, `antigravity-ide/`,
  `config/config.json` with only `remoteControlHostname`/`userSettings`
  keys) belong to Antigravity's own internal storage, not a genuine Google
  Gemini CLI OAuth credential store.
- `caut`'s own Gemini provider is documented as OAuth-based
  (`"Session, weekly"` windows) but its README does not state whether it
  measures Gemini API developer quota or Google AI Pro App subscription
  usage, and separately notes browser-cookie-based "web sources" are
  macOS-only - so even a working `caut` build may not cover this on
  Windows.
- Explicitly not tested: Gemini API developer quota (out of scope - would
  measure a different product).

## Cross-cutting takeaway that shaped `status.schema.json` v0.1

No two providers returned the same window shape. Codex: one window, no
name assumption should be "five_hour"/"weekly". Claude Code: two named
windows, either independently nullable. Antigravity/Gemini: zero windows,
a manual status enum instead. The schema's `windows[]` is an open list for
this reason.
