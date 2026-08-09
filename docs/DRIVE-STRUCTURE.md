# Google Drive Runtime Structure

Google Drive is the runtime SSOT. Project, task, handoff, and history records
are real JSON files; Git contains only their schemas, templates, and manager.

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

`SESSIONS/<project_id>/<session_id>.json` is the Session Registry. It contains
provider metadata and short indexing text only, never a full provider
conversation transcript. Sessions without a deterministic project match use
`SESSIONS/_unclassified/` and `classification_status: needs_review`.

`SESSION-REVIEWS/_review_queue/<session_id>.json` stores an explicit human
project assignment and its minimal reassignment audit. The review queue itself
is generated from read-only session discovery plus these records.
