# Google Drive Runtime Structure

Google Drive is the runtime SSOT. Project, task, handoff, and history records
are real JSON files; Git contains only their schemas, templates, and manager.

Development Overviews are stored as `OVERVIEWS/<project_id>/overview.json`.
They are compact management summaries with stable item IDs, not replacements
for detailed TASKS or HANDOFFS.

```
AI Development Manager/
├─ AI-RESOURCE-STATUS/status.json
├─ PROJECTS/<project_id>/<project_id>.json
├─ TASKS/<project_id>/<task_id>.json
├─ HANDOFFS/<project_id>/<handoff_id>.json
├─ EXECUTIONS/<project_id>/<execution_id>.json
└─ TASK-HISTORY/<project_id>/<task_id>-<completion-date>.json
```

The manager creates only missing per-project folders and updates the same
record file by id/name. Duplicate records are rejected, and writes are read
back byte-for-byte before success is reported. Completion keeps the live task,
adds a final handoff, and copies the completed task into `TASK-HISTORY`.

`SESSIONS/<project_id>/<manager_session_key>.json` is the Session Registry. The
key is the reversible provider-aware identity `provider:provider_session_id`
(with percent-encoding), while `provider_session_id` remains the raw
provider-owned value. Existing Codex files named with the raw session ID remain
readable. The registry contains provider metadata and short indexing text only,
never a full provider conversation transcript. Sessions without a deterministic project match use
`SESSIONS/_unclassified/` and `classification_status: needs_review`.

`SESSION-REVIEWS/_review_queue/<manager_session_key>.json` stores an explicit
human project assignment and its minimal reassignment audit. Legacy Codex raw
session-ID review records remain readable. The review queue itself
is generated from read-only session discovery plus these records.

Working-tree lock authority is not stored in the Drive tree above. Its
authoritative registry is one explicitly configured Google Cloud Storage
object. Drive remains the SSOT for existing task/session metadata only.
Canonical repository hashes key its `locks` object,
and every creation/update uses GCS `ifGenerationMatch=0/N`. Lease owner tokens
are returned only by acquire; the registry stores their hashes. Released and
expired generations remain auditable but do not block a new owner.
`COMMANDS/<project_id>/<command_id>.json` is the bounded ChatGPT-to-Windows command queue. A ChatGPT client writes a schema-valid `queued` command that references an existing Drive `TASKS` record. The Windows watcher moves it through `claimed`, healthy `running`, nonterminal `attention`, and terminal `completed`/`failed`, writing only compact lifecycle, execution/session, and error-category evidence. `attention` never grants retry or releases authority. It never stores raw prompts, transcripts, credentials, OAuth data, or provider responses.

`DISPATCH-REQUESTS` is a separate private folder in the configured owner's My
Drive root, not below the service-account-shared ADM folder. ChatGPT may upload
only strict `schema/dispatch_request.schema.json` records named
`<request_id>.json`. The Windows watcher admits a record only when its local
OAuth identity, folder, file parent, sole owner, sole permission, MIME type,
size, timestamp, filename, and schema all verify. It then calls the existing
trusted Direct Dispatch ingress, which generates governance metadata, claims
`request_id` through the existing GCS CAS registry, writes the normal governed
Task and queued Command, and leaves launch authority with the Watcher.

Configure the scheduled watcher with `-IngressFolderId` and `-IngressOwner`.
The runner exposes these only as `ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID` and
`ADM_DRIVE_DISPATCH_INGRESS_OWNER`; omitting either disables/fails the ingress
without changing the original COMMANDS path.
