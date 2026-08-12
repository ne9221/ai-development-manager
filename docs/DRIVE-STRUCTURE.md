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
├─ WORKTREE-LOCKS/_global/registry.json
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

`WORKTREE-LOCKS/_global/registry.json` is a separately provisioned CAS registry
for coarse repository writer leases. Its Drive file ID is configuration; lock
operations never discover authority by filename. Canonical repository hashes
key the `locks` object, and every mutation sends the prior ETag in `If-Match`,
so concurrent writers have one winner. Lease owner tokens are returned only by
acquire; Drive stores their hashes. Released and expired generations remain
auditable but do not block a new owner.
