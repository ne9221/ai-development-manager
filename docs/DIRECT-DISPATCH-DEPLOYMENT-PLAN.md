# Direct Dispatch Production Activation -- Deployment Plan

Status: **planning only, code-complete, not executed**. Everything below was
prepared read-only against the live `adm-runtime-bridge` Cloud Run service on
project `ai-development-manager` (region `asia-east1`) on 2026-08-17. No IAM,
GCS, Cloud Run, or OAuth-scope change has been made. Execution requires a
one-time human approval -- see the end of this document.

## Current live state (read-only inspection, 2026-08-17)

- Service: `adm-runtime-bridge`, region `asia-east1`, `maxScale=1`,
  `containerConcurrency=20`, `--allow-unauthenticated` at the Cloud Run
  ingress layer (app-level `ADM_API_KEY` Bearer auth is the real gate for
  every non-`/health` route, REST and MCP alike).
- Live revision: `adm-runtime-bridge-00006-xjg`, image digest
  `asia-east1-docker.pkg.dev/ai-development-manager/cloud-run-source-deploy/adm-runtime-bridge@sha256:ef862f89c50115450a7d22bd1470ca4baa83ce33508edcd71c40b0b3a247b7bc`.
- Service account: `ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com`.
- Current env vars on the live revision: `MCP_ALLOWED_HOST=adm-runtime-bridge-551449082603.asia-east1.run.app`,
  `ADM_API_KEY` (from Secret Manager `ADM_API_KEY:latest`). **No
  `ADM_LOCK_GCS_BUCKET` is set** -- `POST /api/v1/tasks/dispatch` and
  `adm_create_task` both fail closed (`idempotency_backend_unavailable`)
  against this revision today, by design (matches
  `manager/dispatch_requests.py`'s fail-closed contract).
- Drive permission model (per `docs/CLOUD-RUNTIME-BRIDGE.md`, not
  independently re-verified against live Drive ACLs this session): the
  runtime service account is shared on the `AI Development Manager` Drive
  root as **Viewer**, using the `drive.readonly` scope in code
  (`cloud/app.py:default_service_factory`). `cloud/app.py` already defines a
  separate `default_write_service_factory` (`drive` full scope) for the
  existing `/api/v1/tasks/dispatch` route and now also for `adm_create_task`
  -- but the OAuth *scope string* requested in code is not the actual
  enforcement boundary for Drive API access from a GCP service account; the
  Drive-side sharing permission level (Viewer vs Editor/Content Manager) on
  that email address is. **The write scope in code has been ready since the
  original Direct Dispatch ingress PR; the Drive-side permission has not
  been raised to match it**, so this is the one item this plan cannot fully
  verify or execute without either Drive Admin console access or running
  authenticated code against Drive -- flagged explicitly below.
- GCS buckets already in the project: `adm-lock-smoke-551449082603-20260813-0147`
  (created 2026-08-13, likely a prior session's smoke-test artifact --
  `git log`/memory does not show it wired into any deployed service) and
  `run-sources-ai-development-manager-asia-east1` (Cloud Run's own build
  source bucket, not for application use). The smoke bucket **already
  grants `roles/storage.objectUser` to the runtime service account** --
  zero new IAM would be needed to reuse it for the lock registry, though its
  name signals "throwaway", not "production".
- Secret Manager: only `ADM_API_KEY` exists, already scoped to the runtime
  service account (`roles/secretmanager.secretAccessor`). No new secret is
  needed -- the same bearer key continues to gate both the REST dispatch
  route and the MCP mount.
- Rollback candidates (all `Ready=True`): `adm-runtime-bridge-00006-xjg`
  (current), `-00005-9j7`, `-00004-f74`, `-00003-rb7`, `-00002-57s`,
  `-00001-qbz`.

## Infra delta required (minimal set, least privilege)

| # | Item | Change | New or already present |
|---|------|--------|------------------------|
| 1 | Cloud Run source deploy | Redeploy from `integration/p0-direct-dispatch-activation-20260817` (commit `0778689` as of this writing) | New revision |
| 2 | `ADM_LOCK_GCS_BUCKET` env var | Set on the new revision | New |
| 3 | GCS lock bucket | Reuse `adm-lock-smoke-551449082603-20260813-0147` (recommended -- IAM already granted) **or** create a purpose-named bucket + grant `roles/storage.objectUser` to the runtime SA | Reuse: no new IAM. Fresh: 1 bucket + 1 IAM binding |
| 4 | Drive folder permission | Raise the runtime SA's share level on the `AI Development Manager` Drive root from Viewer to Editor (Content Manager) | New -- highest blast-radius item, see below |
| 5 | Secret Manager | None | Unchanged |
| 6 | Public endpoint exposure | None -- same `--allow-unauthenticated` ingress + app-level Bearer gate as today | Unchanged |
| 7 | Desktop Command Watcher env | Human must confirm `ADM_LOCK_GCS_BUCKET` on the machine(s) running `manager/command_watcher.py` points at the **same** bucket as item 3, or the watcher's `verify_trusted_ingress_admission()` cross-check can never find the idempotency record and every trusted-ingress command silently falls back to `not_allowlisted` (fails closed, not unsafe, but hands-off dispatch would just never actually launch) | Human-side config, not executable by this session |

## Exact commands (NOT executed -- for the one-time approval this plan is asking for)

```bash
# 1) Redeploy from the integration branch (from a fresh clone checked out at
#    integration/p0-direct-dispatch-activation-20260817)
gcloud run deploy adm-runtime-bridge \
  --source . \
  --region asia-east1 \
  --project ai-development-manager \
  --service-account ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com \
  --set-secrets ADM_API_KEY=ADM_API_KEY:latest \
  --set-env-vars MCP_ALLOWED_HOST=adm-runtime-bridge-551449082603.asia-east1.run.app,ADM_LOCK_GCS_BUCKET=adm-lock-smoke-551449082603-20260813-0147 \
  --allow-unauthenticated

# 2a) If reusing the existing smoke bucket: no IAM command needed (already granted).
# 2b) If instead creating a fresh bucket:
gcloud storage buckets create gs://adm-lock-registry-ai-development-manager-asia-east1 \
  --project ai-development-manager --location asia-east1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://adm-lock-registry-ai-development-manager-asia-east1 \
  --member "serviceAccount:ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com" \
  --role roles/storage.objectUser
# then use that bucket name in --set-env-vars above instead.

# 3) Drive folder permission (Viewer -> Editor): this is a Drive ACL change,
#    not a gcloud/IAM command -- it must be done from the Drive UI or Drive
#    API by whichever human/account owns the "AI Development Manager" Drive
#    folder, changing ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com's
#    role on that folder from Viewer to Editor (Content Manager). No command
#    to hand over here; this plan flags it as the action item.

# Verify after deploy:
curl https://adm-runtime-bridge-551449082603.asia-east1.run.app/health
curl -X POST -H "Authorization: Bearer $ADM_API_KEY" -H "Content-Type: application/json" \
  -d '{"request_id":"verify-1","project_id":"ai-development-manager","title":"verify","goal":"Reply OK. Read only. Touch nothing."}' \
  https://adm-runtime-bridge-551449082603.asia-east1.run.app/api/v1/tasks/dispatch
```

## Blast radius

- **Cloud Run redeploy (#1)**: new revision serving 100% of traffic
  immediately (no canary step in the existing single-revision deploy
  pattern this repo already uses). Affects every existing caller of
  `/health`, `/dispatch`, and `/mcp` -- all of which have their own full
  regression suite (810+ tests) passing on this branch, and none of whose
  existing behavior this branch changes except the two items below.
- **`ADM_LOCK_GCS_BUCKET` (#2)**: unblocks `POST /api/v1/tasks/dispatch`
  and `adm_create_task`, which were previously hard-failing closed
  (`idempotency_backend_unavailable`). No effect on any other route.
- **GCS bucket (#3)**: reusing the smoke bucket adds no new IAM surface;
  creating a fresh bucket adds one bucket + one `storage.objectUser`
  binding scoped to that single bucket only (not project-wide storage
  access).
- **Drive folder permission (#4)**: the single highest-blast-radius item.
  Raising the runtime SA from Viewer to Editor grants it write access to
  the **entire** `AI Development Manager` Drive tree -- every project's
  Tasks, Commands, Executions, Handoffs, not just Direct-Dispatch-created
  ones. This is unavoidable with Drive's folder-level (not
  record-level) ACL model. It does **not** by itself grant launch
  authority: `manager/trusted_ingress.py`'s Safe Auto-Admission gate and
  the existing `ADM_WATCHER_ALLOWLIST_PATH` allowlist still separately
  govern what the Command Watcher will ever actually launch, and that
  gate runs on the desktop watcher process, entirely outside this Cloud
  Run service's authority. The practical new capability this grants the
  Cloud Run service is: create/update Task and Command Drive records
  (already true for the pre-existing `/dispatch`-adjacent write path this
  repo ships), never spawn a provider process directly.
- **No change**: public exposure model, secrets, OAuth client
  configuration, or any other service's IAM.

## Rollback

```bash
# Revert traffic to the previously live revision (zero data-plane impact --
# the new revision's code changes are additive/backward-compatible, so no
# Drive/GCS schema rollback is implied):
gcloud run services update-traffic adm-runtime-bridge \
  --region asia-east1 --project ai-development-manager \
  --to-revisions adm-runtime-bridge-00006-xjg=100

# If a fresh GCS bucket was created and needs undoing:
gcloud storage buckets delete gs://adm-lock-registry-ai-development-manager-asia-east1 --project ai-development-manager

# If the Drive folder permission was raised and needs reverting: change the
# runtime SA's role on the Drive folder back to Viewer from the Drive UI/API.
# (No gcloud command; same manual-action caveat as the forward change.)

# Unsetting ADM_LOCK_GCS_BUCKET alone (without a full traffic rollback) is
# also sufficient to return dispatch ingress to its current fail-closed
# state without touching anything else:
gcloud run services update adm-runtime-bridge --region asia-east1 --project ai-development-manager \
  --remove-env-vars ADM_LOCK_GCS_BUCKET
```

## Sequencing recommendation

1. Approve and raise the Drive folder permission first (Editor), since
   nothing else is useful without it and it is the only step this session
   cannot execute itself regardless of approval.
2. Approve the GCS bucket decision (reuse smoke bucket vs. create fresh).
3. Approve the Cloud Run redeploy + env var in one step (they are one
   `gcloud run deploy` invocation above).
4. Run the golden E2E disposable read-only test (Phase 5) against the live
   service before considering this hands-off.

---

**AWAITING ONE-TIME INFRA APPROVAL** -- no IAM, GCS, Cloud Run, or Drive
permission change has been made. Reply with which of the numbered items
above to proceed with (all, or a subset) and, for item 3, whether to reuse
`adm-lock-smoke-551449082603-20260813-0147` or create a fresh bucket.
