# Collectors (planned, not implemented in v0.1)

No collector code exists yet. This just records, per provider, what the
Phase-0 PoCs found so the next implementation round doesn't have to
re-derive it.

| Provider | Planned primary source | Automatic? | PoC evidence |
|---|---|---|---|
| Codex | Official `codex app-server`, JSON-RPC `account/rateLimits/read` over stdio | Yes | Verified: real response had one `primary` 7-day window (`usedPercent`, `windowDurationMins`, `resetsAt`), `secondary: null`, `planType`, `credits`. Not blocked by the local machine's TLS interception issue (stdio, not HTTPS). |
| Claude Code | Official statusline `rate_limits` (JSON piped to a configured `statusLine` command, populated after the first API response in a live interactive session) | Yes, technically | Not yet collectible in this environment: no reachable `claude` CLI binary, no `settings.json`/statusLine configured. Fallback used: `claude-monitor` reading `~/.claude/projects/*.jsonl` -> `source_type: local_estimate`, only `five_hour` populated, `seven_day` null. |
| Antigravity | Unknown | No (manual in v0.1) | Filesystem scan (Preferences, Local State, logs, Local Storage leveldb) found zero quota-related keys. `DevToolsActivePort` exists but the port it names refused connections in this PoC (curl exit 7) - inconclusive, not confirmed impossible. |
| Gemini App / Google AI Pro | Unknown | No (manual in v0.1) | No `gemini`/`gcloud` CLI, no OAuth credential store found on the test machine. Do not confuse with Gemini API developer quota. |

See `../docs/QUOTA-SOURCES.md` for the full PoC narrative.
