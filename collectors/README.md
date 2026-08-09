# Collectors

## Codex

`codex.py` starts the official `codex app-server` over stdio, performs the
required initialize handshake, reads `account/rateLimits/read`, normalizes all
non-null quota windows, and validates the result against
`../schema/status.schema.json`.

```powershell
python -m pip install -r collectors/requirements.txt
python collectors/codex.py --show-raw
python -m unittest collectors/test_codex.py
```

The default output is `collectors/codex.status.json`. Failures exit non-zero
without writing fabricated quota values. Set `CODEX_BIN` only when `codex` is
not available on `PATH`.

## Google Drive publisher

`publish_drive.py` validates `codex.status.json`, then creates or updates the
single raw `application/json` file named `status.json` in the configured Drive
folder and verifies its metadata and bytes after upload.

Authentication uses Google Application Default Credentials, an existing token
at `GOOGLE_DRIVE_TOKEN`, or an official Desktop OAuth client JSON selected with
`GOOGLE_OAUTH_CLIENT_SECRETS`. Credentials remain outside the repository.

```powershell
python collectors/publish_drive.py
python -m unittest collectors/test_codex.py collectors/test_publish_drive.py
```

## Planned providers

| Provider | Planned primary source | Automatic? | PoC evidence |
|---|---|---|---|
| Codex | Official `codex app-server`, JSON-RPC `account/rateLimits/read` over stdio | Yes | Verified: real response had one `primary` 7-day window (`usedPercent`, `windowDurationMins`, `resetsAt`), `secondary: null`, `planType`, `credits`. Not blocked by the local machine's TLS interception issue (stdio, not HTTPS). |
| Claude Code | Official statusline `rate_limits` (JSON piped to a configured `statusLine` command, populated after the first API response in a live interactive session) | Yes, technically | Not yet collectible in this environment: no reachable `claude` CLI binary, no `settings.json`/statusLine configured. Fallback used: `claude-monitor` reading `~/.claude/projects/*.jsonl` -> `source_type: local_estimate`, only `five_hour` populated, `seven_day` null. |
| Antigravity | Unknown | No (manual in v0.1) | Filesystem scan (Preferences, Local State, logs, Local Storage leveldb) found zero quota-related keys. `DevToolsActivePort` exists but the port it names refused connections in this PoC (curl exit 7) - inconclusive, not confirmed impossible. |
| Gemini App / Google AI Pro | Unknown | No (manual in v0.1) | No `gemini`/`gcloud` CLI, no OAuth credential store found on the test machine. Do not confuse with Gemini API developer quota. |

See `../docs/QUOTA-SOURCES.md` for the full PoC narrative.
