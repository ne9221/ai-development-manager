# MCP Read-only Integration

## Local Windows stdio transport

Start the existing adapter as a long-running local MCP process from the repository root:

`python -m manager.mcp_adapter`

Configure an MCP client with that command and this repository as its working directory. The process communicates only through MCP JSON-RPC on stdin/stdout and exposes the same four tools documented below. It does not start Codex, Claude, or any execution runner.

The stdio entrypoint uses the existing desktop OAuth service factory; the Cloud Run ASGI entrypoint continues to use application-default credentials with its read-only service account.

For B2, call `adm_runtime_quota_status` first, then call read-only `adm_dispatch` with `project_id`, `user_request`, and optional `task_id`. `adm_health` verifies that the local adapter process and runtime contract are available without exposing environment or Drive details.

Transport inputs are capped at 200 characters for identifiers and 4,000 characters for the request. Responses above 65,536 UTF-8 bytes fail closed. Backend exception text, credentials, OAuth data, raw provider responses, and Drive documents are never returned.

The existing `adm-runtime-bridge` Cloud Run service exposes a standard stateless Streamable HTTP MCP endpoint at:

`https://adm-runtime-bridge-551449082603.asia-east1.run.app/mcp/`

The adapter uses the official MCP Python SDK 2.0. Streamable HTTP is the production transport; no stdio-only deployment or second Cloud Run service is used.

## Tools

- `adm_dispatch`: accepts a project ID or alias, user request, and optional task/provider controls. It returns the existing sanitized runtime bridge contract `1.0`, including the existing generated prompt.
- `adm_status`: returns only active task, latest handoff summary, recommendation, compact quota summary/freshness, warnings, and next action.
- `adm_runtime_quota_status`: returns the existing bounded runtime quota contract `1.0` for Codex and Claude. Its only optional argument is `max_age_minutes` (an integer from 1 through 1440); the Drive file and raw source cannot be selected by callers.
- `adm_health`: returns service, MCP adapter, and runtime contract versions without reading Drive.

MCP SDK 2.0's generated function schema does not emit `additionalProperties: false`; unmodeled fields are ignored by the SDK and are not forwarded to the quota loader.

All tools declare MCP `readOnlyHint=true` and `destructiveHint=false`. Every bridge call uses `read_only=True`; there are no create, update, completion, handoff, execution, or AI-launch tools.

## Authentication

The MCP endpoint requires `Authorization: Bearer <ADM_API_KEY>`. The key remains in Google Secret Manager and is not embedded in code, tool schemas, documentation, manifests, Drive, or logs. Existing REST `/health` and `/dispatch` behavior is unchanged.

For a future ChatGPT or Apps SDK integration, configure the remote MCP URL and an approved authentication mechanism. Current OpenAI API clients can provide authorization or headers for remote MCP servers. Direct ChatGPT custom-MCP availability and setup depend on the workspace/product permissions available to the account; this repository does not hard-code plan names. Production user-facing distribution should replace the shared bearer secret with OAuth rather than exposing `ADM_API_KEY` to end users.

No UI component or ChatGPT App manifest is included in this phase.
