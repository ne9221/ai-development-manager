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
├─ ADM-RESULTS/<project_id>/<result_id>.json   # Slice B: create-only immutable adm-result/v1
└─ TASK-HISTORY/<project_id>/<task_id>-<completion-date>.json
```
