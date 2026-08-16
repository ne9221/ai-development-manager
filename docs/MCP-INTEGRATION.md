# MCP Integration

## Local Windows stdio transport

Start the existing adapter as a long-running local MCP process from the repository root:

`python -m manager.mcp_adapter`

Configure an MCP client with that command and this repository as its working directory. The process communicates only through MCP JSON-RPC on stdin/stdout and exposes the six tools documented below. It does not start Codex, Claude, or any execution runner -- including `adm_create_task`, whose only side effect is creating a queued Task+Command through the existing Direct Dispatch ingress.

The stdio entrypoint uses the existing desktop OAuth service factory (already write-capable) for every tool, including `adm_create_task`. The Cloud Run ASGI entrypoint uses application-default credentials and keeps the existing split between a read-only service account (`adm_dispatch`/`adm_status`/`adm_runtime_quota_status`) and a write-scoped one (`adm_create_task`, mirroring `cloud/app.py`'s existing `default_service_factory`/`default_write_service_factory` split for the REST routes) plus the same GCS bucket the REST Direct Dispatch route needs for idempotency. As of this writing that write scope and bucket are not yet provisioned on the live `adm-runtime-bridge` service -- see the deployment plan for the exact infra delta.

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
- `adm_create_task` (write, idempotent): accepts exactly `project_id`, `title`, `goal`, `request_id` -- nothing else. It calls the same authenticated Direct Dispatch ingress (`cloud.dispatch_ingress.handle_dispatch`) the REST route uses, never a provider or execution runner directly. The created Task is always forced disposable/read-only under the v1 Safe Auto-Admission contract (see `manager/trusted_ingress.py`); there is no argument that requests a provider, account, execution policy, working directory, or any other execution control. Replaying the same `request_id` returns the identity already created, never a second Task/Command.
- `adm_task_status` (read-only): accepts `project_id` and at least one of `request_id` (resolved via the ingress's own `task_id = command_id = "dispatch-<request_id>"` convention), `task_id`, or `command_id`. Returns a bounded field set (status, provider/account, progress, latest update, result summary) for whichever of the Task/Command exist; raises if neither is found.

MCP SDK 2.0's generated function schema does not emit `additionalProperties: false`; unmodeled fields are ignored by the SDK and are not forwarded to the quota loader or `adm_create_task` -- a caller cannot smuggle `provider`, `account_id`, `executable`, `env`, or any other field through the tool call.

All tools except `adm_create_task` declare MCP `readOnlyHint=true`; `adm_create_task` declares `readOnlyHint=false, idempotentHint=true`. All tools declare `destructiveHint=false`. Read-only bridge calls all use `read_only=True`; there are no update, completion, handoff, or AI-launch tools -- `adm_create_task` is the sole create surface, and it only ever produces a `queued` Command for the existing, unmodified Command Watcher pipeline to pick up under its own Safe Auto-Admission and allowlist rules.

## Authentication

The MCP endpoint requires `Authorization: Bearer <ADM_API_KEY>`. The key remains in Google Secret Manager and is not embedded in code, tool schemas, documentation, manifests, Drive, or logs. Existing REST `/health` and `/dispatch` behavior is unchanged.

For a future ChatGPT or Apps SDK integration, configure the remote MCP URL and an approved authentication mechanism. Current OpenAI API clients can provide authorization or headers for remote MCP servers. Direct ChatGPT custom-MCP availability and setup depend on the workspace/product permissions available to the account; this repository does not hard-code plan names. Production user-facing distribution should replace the shared bearer secret with OAuth rather than exposing `ADM_API_KEY` to end users.

No UI component or ChatGPT App manifest is included in this phase.
