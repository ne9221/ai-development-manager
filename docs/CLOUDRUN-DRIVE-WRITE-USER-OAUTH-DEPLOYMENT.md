# Cloud Run Drive Write -- Dedicated User OAuth Credential -- Deployment Package

Status: **planning only, code-complete, not executed**. No Secret Manager
secret, IAM binding, Drive ACL, or Cloud Run deploy has been created or
changed by this session. This document is the delta for a human to review
and execute later.

## Why this exists

`cloud/app.py::default_write_service_factory` previously authenticated Drive
writes as the Cloud Run runtime service account via bare Application
Default Credentials. The target Drive folder (`AI Development Manager`
root, owned by the consumer Gmail account `neen971229@gmail.com`) is a
regular My Drive folder, not a Shared Drive, so the service account has
zero personal Drive storage quota of its own -- `files.create()` 403s with
`storageQuotaExceeded` regardless of folder Editor access (root cause
recorded separately, confirmed 2026-08-17). This was also masked for a
time by an unrelated idempotency-retry bug in
`cloud/dispatch_ingress.py::handle_dispatch()` (fixed on
`fix/dispatch-ingress-idempotency-retry-20260817` @ `cbfe8d2`, merged into
this branch) that reported fake `accepted: true` even though nothing was
ever written.

This branch (`cloud/drive_credentials.py`,
`cloud/app.py::default_write_service_factory`) fixes the root cause: the
write-required Drive path now runs under a real user OAuth identity
instead of the service account, reusing the exact same credential loader
(`collectors/publish_drive.py::credentials_with_source()`) the desktop
Command Watcher already uses -- no new OAuth implementation.

**This also lowers blast radius versus the prior deployment plan**
(`docs/DIRECT-DISPATCH-DEPLOYMENT-PLAN.md` item 4): that plan's highest-risk
step was raising the runtime service account's Drive share from Viewer to
Editor across the *entire* Drive tree. With this change, Drive writes run
as the folder's own owner (via OAuth), so **no service-account Drive ACL
change is needed at all** -- the runtime SA can stay Viewer-only, used
solely for the existing read-only `/dispatch` route.

## Secret Manager

| Secret name | Contents | Notes |
|---|---|---|
| `adm-cloudrun-drive-oauth-token` | The authorized-user OAuth token JSON (`refresh_token`, `client_id`, `client_secret`, `token_uri`, `scopes`) for a **dedicated Cloud Run identity**, distinct from the desktop production token at `~/.config/ai-development-manager/google-drive-token.json` | New secret. Never created by this session -- must be generated per "Generating the Cloud Run token" below and uploaded manually. |

No second secret is needed for OAuth client secrets at runtime: the token
JSON produced by `InstalledAppFlow` already embeds `client_id`/
`client_secret`, and `google.oauth2.credentials.Credentials.refresh()`
uses those embedded fields to refresh non-interactively (confirmed against
current `google-auth` docs) -- exactly what
`credentials_with_source()`'s `existing_token` / `refreshed_token` branches
already do. A client-secrets file (`GOOGLE_OAUTH_CLIENT_SECRETS`) is only
needed at token-minting time, on a human's workstation, never inside the
Cloud Run container.

The existing `ADM_API_KEY` secret (bearer auth for the HTTP boundary) is
unaffected.

## Cloud Run `--set-secrets` delta

Mount the token as a file (Cloud Run's file-mount form re-reads Secret
Manager on every file access, so a new secret version -- e.g. after
rotation -- takes effect without a redeploy):

```bash
gcloud run deploy adm-runtime-bridge \
  --region asia-east1 \
  --project ai-development-manager \
  --service-account ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com \
  --set-secrets ADM_API_KEY=ADM_API_KEY:latest,/secrets/gdrive-oauth/token.json=adm-cloudrun-drive-oauth-token:latest \
  --set-env-vars GOOGLE_DRIVE_TOKEN=/secrets/gdrive-oauth/token.json,ADM_LOCK_GCS_BUCKET=<per DIRECT-DISPATCH-DEPLOYMENT-PLAN.md>
```

`GOOGLE_DRIVE_TOKEN` is the same env var `collectors/publish_drive.py`
already honors (`token_path()`); pointing it at the mounted secret path is
the entire wiring change on the Cloud Run side. `GOOGLE_OAUTH_CLIENT_SECRETS`
is intentionally **not** set in Cloud Run -- `default_write_service_factory`
always calls with `allow_interactive=False`, so the interactive-flow branch
that needs it can never be reached there (enforced by
`cloud/drive_credentials.py`'s `user_oauth_write_credentials`, and covered
by `cloud/test_drive_credentials.py::test_never_calls_interactive_flow`).

## IAM delta

| Grant | Target | Change |
|---|---|---|
| `roles/secretmanager.secretAccessor` on `adm-cloudrun-drive-oauth-token` | `ai-development-manager-runtime@ai-development-manager.iam.gserviceaccount.com` | New -- scoped to this one secret only |
| Drive folder ACL on `AI Development Manager` root | Runtime service account | **No change** -- stays Viewer, used only by the existing read-only `/dispatch` route. The write identity is now the human OAuth principal, who already owns the folder. |
| Drive folder ACL | The Cloud-Run-dedicated OAuth principal (see below) | **No change if it is the folder owner's own account** (`neen971229@gmail.com` authorizing itself); if a *different* Google identity is deliberately chosen as the dedicated Cloud Run principal, that identity needs Editor sharing on the folder -- a Drive ACL change, out of scope for this session either way. |

No project-level IAM change, no new Cloud Run ingress setting, no change to
`--allow-unauthenticated` (still gated by the existing `ADM_API_KEY`
bearer check at the application layer).

## Generating the Cloud-Run-dedicated OAuth token

Run on a trusted workstation (never inside Cloud Run, never committed to
the repo) -- reuses the existing `manager.drive_auth authorize` command
unchanged, just pointed at a fresh token path so it never touches or
overwrites the desktop production token:

```bash
export GOOGLE_DRIVE_TOKEN=./cloudrun-drive-oauth-token.json
export GOOGLE_OAUTH_CLIENT_SECRETS=/path/to/oauth-client-secrets.json
python -m manager.drive_auth authorize
```

This opens the standard Google consent screen; complete it as the Drive
folder's owning account (or another account already granted Editor on the
folder, if intentionally using a separate principal). The resulting
`cloudrun-drive-oauth-token.json` contains the refresh token this session
must never print, log, or commit. Upload its contents as the Secret
Manager secret version:

```bash
gcloud secrets create adm-cloudrun-drive-oauth-token --project ai-development-manager --replication-policy automatic
gcloud secrets versions add adm-cloudrun-drive-oauth-token --project ai-development-manager --data-file=cloudrun-drive-oauth-token.json
shred -u cloudrun-drive-oauth-token.json   # or securely delete by other means
```

Then delete the local file. Verify without printing secret content:

```bash
gcloud secrets versions access latest --secret adm-cloudrun-drive-oauth-token --project ai-development-manager | python -c "import json,sys; d=json.load(sys.stdin); print(sorted(d.keys()))"
```

## Credential rotation

Uploading a new secret version (`gcloud secrets versions add ...`) is
sufficient -- the file-mount form of `--set-secrets` re-resolves on every
read, so a running revision picks up the new token without a redeploy.
Recommended cadence: rotate opportunistically (e.g. alongside a Drive
security review), not on a fixed schedule, since `credentials_with_source()`
already self-refreshes the access token from the same refresh token on
every expiry.

## Revocation

1. Revoke the OAuth grant from the account's
   [Google Account permissions page](https://myaccount.google.com/permissions)
   (search "third-party apps" -- revoke the ai-development-manager OAuth
   client's Cloud-Run-scoped grant specifically, not the desktop grant,
   since they are separate authorizations even though they may share an
   OAuth client ID).
2. Delete the Secret Manager secret version (or the whole secret):
   `gcloud secrets delete adm-cloudrun-drive-oauth-token --project ai-development-manager`.
3. Confirm fail-closed behavior: with the secret gone,
   `default_write_service_factory` raises (missing token file, ADC
   rejected by `user_oauth_write_credentials`'s allowlist) and
   `cloud/app.py`'s existing `except Exception: 503 drive_unavailable`
   handler returns a generic, secret-free error -- already covered by
   `cloud/test_drive_credentials.py::test_missing_token_fails_closed`.

## Rollback

Two independent, composable options:

```bash
# Full traffic rollback to the previous revision (reverts this whole change):
gcloud run services update-traffic adm-runtime-bridge \
  --region asia-east1 --project ai-development-manager \
  --to-revisions <previous-revision>=100

# Or: keep this revision's code but remove only the OAuth wiring, which
# makes default_write_service_factory fail closed immediately (missing
# token -> PublisherError -> 503 drive_unavailable) rather than silently
# falling back to the SA/ADC path that caused storageQuotaExceeded:
gcloud run services update adm-runtime-bridge --region asia-east1 --project ai-development-manager \
  --remove-env-vars GOOGLE_DRIVE_TOKEN
```

Because `user_oauth_write_credentials` never accepts `application_default`
as a substitute, there is no rollback state in which this code silently
reverts to the pre-fix SA-ADC behavior -- it either has a valid user OAuth
token mounted, or it fails closed. This is the deliberate design point of
Phase A's contract.

## Fail-safe behavior when token refresh fails

All failure modes converge on the same outcome, already proven by
`cloud/test_drive_credentials.py` and unchanged
`cloud/test_dispatch_route.py::test_write_drive_unavailable_maps_to_503`:

| Failure | Result |
|---|---|
| Token file missing/unreadable | `credentials_with_source` raises `PublisherError` (`Google OAuth reauthorization required`) |
| Token file malformed JSON | Same -- category `malformed_token`, message never includes file content |
| Refresh token invalid/revoked | `RefreshError` caught, re-raised as `PublisherError` with category `invalid_refresh_token` only -- no exception text from the underlying refresh call is included |
| Only ADC/service-account credentials available | `credentials_with_source` succeeds with `source="application_default"`, but `user_oauth_write_credentials` explicitly rejects it -> `DriveWriteCredentialError` |
| Any of the above | `cloud/app.py`'s existing `try: write_service = write_service_factory() except Exception: 503 drive_unavailable` converts it to a generic, secret-free HTTP response; the request never reaches `files.create()` |

No case silently degrades to a service-account write attempt. This closes
the original gap: previously, an SA-only environment would not fail until
`files.create()` itself returned `403 storageQuotaExceeded`, deep inside
the dispatch ingress call; now it fails at credential-construction time,
before any Drive API call is attempted.

## Golden E2E prerequisites (checklist, not executed this session)

- [ ] `adm-cloudrun-drive-oauth-token` secret exists in Secret Manager and
      contains a token JSON authorized against an account with at least
      Editor access on the `AI Development Manager` Drive root.
- [ ] Runtime service account granted `secretmanager.secretAccessor` on
      that secret only.
- [ ] Cloud Run revision deployed with `GOOGLE_DRIVE_TOKEN` pointed at the
      mounted secret path.
- [ ] `ADM_LOCK_GCS_BUCKET` set per `docs/DIRECT-DISPATCH-DEPLOYMENT-PLAN.md`
      (idempotency backend prerequisite, independent of this change).
- [ ] The idempotency-retry fix (`fix/dispatch-ingress-idempotency-retry-20260817`
      @ `cbfe8d2`) is merged -- already included in this branch's history,
      but confirm it is also present on whatever branch is actually
      deployed.
- [ ] A **disposable, read-only** test dispatch (per
      `docs/DIRECT-DISPATCH-GOLDEN-E2E-TEST-PLAN.md`) confirms: (a) the
      Cloud Run logs show `"event":"drive_write_credential","source":"existing_token"`
      or `"refreshed_token"` (never `application_default`), (b) the created
      Task/Command is independently visible via a direct Drive read, (c)
      no refresh token or client secret appears anywhere in Cloud Run logs,
      the dispatch response body, or the created Task/Command/Execution
      records.
- [ ] Only after all of the above: consider re-attempting the full hands-off
      activation previously rolled back (see project memory
      `project-direct-dispatch-production-activation`).

## Not done by this session (explicitly out of scope)

- No Secret Manager secret was created.
- No IAM binding was changed.
- No Drive ACL was changed.
- No Cloud Run deploy was executed.
- No real refresh token was generated, printed, or stored anywhere in this
  repository, branch, or conversation.
- The Scheduled Task running the desktop Command Watcher was not touched.
- The live Golden E2E was not re-run.
