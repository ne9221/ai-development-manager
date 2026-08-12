# MCP Read-only Integration

The existing `adm-runtime-bridge` Cloud Run service exposes a standard stateless Streamable HTTP MCP endpoint at:

`https://adm-runtime-bridge-551449082603.asia-east1.run.app/mcp/`

The adapter uses the official MCP Python SDK 2.0. Streamable HTTP is the production transport; no stdio-only deployment or second Cloud Run service is used.

## Tools

- `adm_dispatch`: accepts a project ID or alias, user request, and optional task/provider controls. It returns the existing sanitized runtime bridge contract `1.0`, including the existing generated prompt.
- `adm_status`: returns only active task, latest handoff summary, recommendation, compact quota summary/freshness, warnings, and next action.
- `adm_runtime_quota_status`: returns the existing bounded runtime quota contract `1.0` for Codex and Claude. Its only optional argument is `max_age_minutes` (an integer from 1 through 1440); the Drive file and raw source cannot be selected by callers.
- `adm_health`: returns service, MCP adapter, and runtime contract versions without reading Drive.

All tools declare MCP `readOnlyHint=true` and `destructiveHint=false`. Every bridge call uses `read_only=True`; there are no create, update, completion, handoff, execution, or AI-launch tools.

## Authentication

The MCP endpoint requires `Authorization: Bearer <ADM_API_KEY>`. The key remains in Google Secret Manager and is not embedded in code, tool schemas, documentation, manifests, Drive, or logs. Existing REST `/health` and `/dispatch` behavior is unchanged.

For a future ChatGPT or Apps SDK integration, configure the remote MCP URL and an approved authentication mechanism. Current OpenAI API clients can provide authorization or headers for remote MCP servers. Direct ChatGPT custom-MCP availability and setup depend on the workspace/product permissions available to the account; this repository does not hard-code plan names. Production user-facing distribution should replace the shared bearer secret with OAuth rather than exposing `ADM_API_KEY` to end users.

No UI component or ChatGPT App manifest is included in this phase.
