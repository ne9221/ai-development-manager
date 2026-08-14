# ChatGPT Runtime Integration Contract

`manager.runtime_bridge` is the single read/orchestration boundary for an
upper-level ChatGPT, connector, MCP adapter, local API, or plugin. It is a
Python API and clean JSON CLI today; it is not tied to any transport.

## Required client flow

For every development request, the upper-level client should:

1. Resolve or supply the project id/alias and call the runtime bridge once.
2. Use the returned Drive-backed quota freshness and recommendation instead of
   quota values from chat memory, README, or a Git snapshot.
3. Give the user the short human summary.
4. Put `generated_prompt` in a directly copyable code block.
5. Do not ask the user to repeat shared rules or manually provide quota.
6. For continuation, prefer `active_task`, `latest_handoff_summary`, and
   `next_action` over old conversation context.
7. For `multi_task`, present `execution_batches`; never auto-start providers.
8. If the bridge is unavailable, state that runtime data is unavailable and
   degrade explicitly. Never invent current quota or handoff state.

## Stable JSON boundary

For quota/status only, without project or task context:

```powershell
python -m manager.runtime_bridge status --json
```

This returns contract `1.0` with the fixed top-level keys
`contract_version`, `schema_version`, `generated_at`, and `providers`.
`providers` contains only `codex` and `claude`; each contains `status`, up to
eight normalized schema `windows`, `source`, `last_updated`, and `freshness`.
The derived status is `known`, `unknown`, `stale`, or `unavailable`. A stale
snapshot remains stale, a missing/malformed Drive status is unavailable, and
an unknown value is never converted to zero. Provider metadata and raw
responses are not returned. `known` specifically means a fresh numeric quota
from the verified Codex app-server or Claude statusline source; manual,
synthetic, inferred, and local-estimate values remain `unknown`. Future
timestamps beyond five minutes of clock skew are unavailable.

`source` is a fixed safe label (`codex_app_server`, `claude_statusline`, or
`unknown`), never upstream free text. Each window always contains a sanitized
`name`; `duration_minutes`, `used_percent`, `remaining_percent`, and
`resets_at` are optional. Duplicate normalized names keep the first window and
at most eight windows are returned.

`read_runtime_status()` is the self-contained Python boundary: it performs the
Drive read, bounded validation, reliability/freshness classification, safe
projection, and unavailable fallback, and fails closed to the same bounded
contract even if projection or final contract validation itself raises.
The local MCP stdio transport exposes this function as
`adm_runtime_quota_status` without reading or parsing quota itself. Start it
from the repository root with `python -m manager.mcp_adapter`; an MCP client
can then call quota status and read-only `adm_dispatch` without a Codex or
Claude conversation. See `docs/MCP-INTEGRATION.md` for the complete tool and
security contract.

This contract assumes write access to the Drive `AI-RESOURCE-STATUS/status.json`
SSOT is controlled: the file is downloaded and JSON-parsed before per-provider
and per-window caps apply, so the SSOT file itself must be a trusted source.

Successful JSON includes `contract_version`, compact project/task/handoff
fields, request type, recommendation, estimate, quota summary/freshness,
warnings, next action, generated prompt, and optional execution batches. It
never includes raw quota documents, execution history, repo dumps, OAuth data,
tokens, or credentials.

```powershell
python -m manager.runtime_bridge --project-id ai-development-manager `
  --request "Continue Phase 9" --json
```

Project aliases are stored in the project record. Multi-task requests reuse the
existing Scheduler; single-task/new/continuation requests reuse the Dispatcher.

## Rule priority and Ponytail

Prompts contain only relevant shared rules, not the full rules document. The
effective safety order is project business facts and acceptance criteria, then
AI Development Manager scope/do-not-touch/regression requirements, then the
optional Ponytail minimal-change preference. Ponytail is enabled only for an
opted-in coding/repo-edit task. If the local skill is unavailable, the prompt
uses the equivalent short text policy and continues.
