# Cloud Runtime Bridge

## Decision

Use Google Cloud Run for the deployable boundary. It runs the existing Python `manager.runtime_bridge` unchanged and needs only one service, one service account, and one Secret Manager value. Google Apps Script was not selected because its V8/JavaScript runtime would require a second implementation of the Python bridge.

Cloud Run requires a Google Cloud project with Billing enabled. The service is therefore deploy-ready but is not deployed by this phase; no Billing account or paid service is enabled automatically. Cloud Run's free tier may cover this low-volume service, but it does not remove the Billing-account prerequisite.

## API

- `GET /health` is public and returns only `status`, `contract_version`, and `timestamp`.
- `POST /dispatch` accepts the Phase 9 bridge inputs and returns contract `1.0`.
- `/dispatch` requires `Authorization: Bearer <secret>`. The secret comes from `ADM_API_KEY`, should be mounted from Secret Manager, and is never accepted in a query string.
- Dispatch is read-only: new-task recommendations are transient and do not create or update Drive task records.

Errors use `{"error":{"code":"...","message":"...","request_id":"..."}}`. Logs contain only request ID, timestamp, HTTP status, latency, project ID, and error category.

## Drive permission model

Run the service as a dedicated service account. Share only the `AI Development Manager` Drive folder with that account as Viewer, and use the Drive `drive.readonly` OAuth scope. The handler reads only the existing folders needed by the bridge: `AI-RESOURCE-STATUS`, `PROJECTS`, `TASKS`, `HANDOFFS`, `TASK-HISTORY`, and `EXECUTIONS`. Do not upload a service-account key or desktop OAuth token.

## Manual deployment after Billing approval

1. Create/select a Google Cloud project and explicitly link an approved Billing account.
2. Enable Cloud Run, Cloud Build, Artifact Registry, Secret Manager, and Google Drive APIs.
3. Create a dedicated runtime service account and share the Drive runtime root with it as Viewer.
4. Store a random bearer secret in Secret Manager as `adm-api-key`; grant only that service account Secret Accessor.
5. From the repository root, deploy:

   `gcloud run deploy adm-runtime-bridge --source . --region asia-east1 --service-account SERVICE_ACCOUNT --set-secrets ADM_API_KEY=adm-api-key:latest --allow-unauthenticated`

   `--allow-unauthenticated` exposes the harmless health check; application authentication still protects `/dispatch`. It can later be replaced or layered with Cloud Run IAM/OAuth without changing the bridge contract.

6. Verify:

   `curl SERVICE_URL/health`

   `curl -X POST -H "Authorization: Bearer SECRET" -H "Content-Type: application/json" -d '{"project_id":"ai-development-manager","user_request":"Continue Phase 10"}' SERVICE_URL/dispatch`

The same JSON boundary can later sit behind MCP or a ChatGPT App connector; those integrations still need their protocol manifest/tool declaration and production OAuth/IAM policy.
