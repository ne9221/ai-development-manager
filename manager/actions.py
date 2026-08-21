"""Action Center domain model, automatic action derivation, and Cloud-first Drive SSOT store."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from manager.dashboard_core import parse_time, is_execution_stale


TYPE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
TYPE_ACTION_NEEDED = "ACTION_NEEDED"
TYPE_BLOCKED = "BLOCKED"
TYPE_MILESTONE_REACHED = "MILESTONE_REACHED"
TYPE_INFO = "INFO"

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"
STATUS_CONFLICTED = "conflicted"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

ROOT_FOLDER_ID = "1pXvl8BglU05ZrXMHIVIDyK-lOWNShXSO"
MIME_JSON = "application/json"
MIME_FOLDER = "application/vnd.google-apps.folder"


@dataclass
class ActionItem:
    action_id: str
    title: str
    type: str = TYPE_ACTION_NEEDED
    severity: str = SEVERITY_MEDIUM
    project_id: str = "Unassigned"
    task_id: Optional[str] = None
    milestone_id: Optional[str] = None
    created_at: str = ""
    waiting_since: str = ""
    reason: str = ""
    impact: str = ""
    recommended_next_step: str = ""
    need_user_action: bool = True
    status: str = STATUS_OPEN
    source: str = "System Detector"
    linked_entity_ids: List[str] = field(default_factory=list)
    # Stable detector identity; action_id identifies one occurrence of it.
    incident_key: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None
    dismissed_at: Optional[str] = None
    resolution_note: Optional[str] = None
    is_conflicted: bool = False
    conflict_details: Optional[List[Dict[str, Any]]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("is_conflicted", None)
        d.pop("conflict_details", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActionItem:
        return cls(
            action_id=str(data.get("action_id", "")),
            title=str(data.get("title", "")),
            type=str(data.get("type", TYPE_ACTION_NEEDED)),
            severity=str(data.get("severity", SEVERITY_MEDIUM)),
            project_id=str(data.get("project_id") or "Unassigned"),
            task_id=data.get("task_id"),
            milestone_id=data.get("milestone_id"),
            created_at=str(data.get("created_at", "")),
            waiting_since=str(data.get("waiting_since", "")),
            reason=str(data.get("reason", "")),
            impact=str(data.get("impact", "")),
            recommended_next_step=str(data.get("recommended_next_step", "")),
            need_user_action=bool(data.get("need_user_action", True)),
            status=str(data.get("status", STATUS_OPEN)),
            source=str(data.get("source", "System Detector")),
            linked_entity_ids=list(data.get("linked_entity_ids", [])),
            incident_key=data.get("incident_key"),
            acknowledged_at=data.get("acknowledged_at"),
            resolved_at=data.get("resolved_at"),
            dismissed_at=data.get("dismissed_at"),
            resolution_note=data.get("resolution_note"),
        )


def format_waiting_duration(waiting_since_val: Any, now: Optional[datetime] = None) -> str:
    """Derive live waiting duration from waiting_since timestamp. Returns 'Unknown' if missing/invalid."""
    if not waiting_since_val:
        return "Unknown"
    dt = parse_time(waiting_since_val) if isinstance(waiting_since_val, str) else waiting_since_val
    if not dt:
        return "Unknown"
    now_dt = now or datetime.now(timezone.utc)
    seconds = max(0.0, (now_dt - dt).total_seconds())
    minutes = int(seconds // 60)
    hours = minutes // 60
    rem_mins = minutes % 60
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}h {rem_mins:02d}m"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class ActionsStore:
    """Cloud-first Action Center store with Drive SSOT and local cache fallback."""

    def __init__(self, drive_service: Any = None, local_file_path: Optional[str] = None):
        self.drive_service = drive_service
        if local_file_path:
            self.local_file_path = Path(local_file_path)
        else:
            home = os.environ.get("AI_MANAGER_HOME") or os.path.expanduser("~/.ai-development-manager")
            self.local_file_path = Path(home) / "actions.json"
        self._actions: List[ActionItem] = []
        self.is_degraded: bool = drive_service is None
        self.last_error: Optional[str] = None
        self.conflicted_action_ids: Set[str] = set()
        self.load()

    def _get_drive_folder(self, parent_id: str, name: str, create: bool = True) -> Optional[str]:
        if not self.drive_service:
            return None
        files_client = self.drive_service.files()
        query = f"'{parent_id}' in parents and name='{name}' and mimeType='{MIME_FOLDER}' and trashed=false"
        res = files_client.list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        if not create:
            return None
        created = files_client.create(
            body={"name": name, "parents": [parent_id], "mimeType": MIME_FOLDER},
            fields="id"
        ).execute()
        return created.get("id")

    def _find_scoped_action_files(self, action_id: str) -> List[Dict[str, Any]]:
        if not self.drive_service:
            return []
        files_client = self.drive_service.files()
        actions_root_id = self._get_drive_folder(ROOT_FOLDER_ID, "ACTIONS", create=False)
        if not actions_root_id:
            return []

        q_folders = f"'{actions_root_id}' in parents and mimeType='{MIME_FOLDER}' and trashed=false"
        res_folders = files_client.list(q=q_folders, spaces="drive", fields="files(id, name)").execute()
        proj_folders = res_folders.get("files", [])

        filename = f"{action_id}.json"
        matching = []
        for pf in proj_folders:
            pf_id = pf["id"]
            pf_name = pf.get("name", "Unassigned")
            q_file = f"'{pf_id}' in parents and name='{filename}' and trashed=false"
            res_file = files_client.list(q=q_file, spaces="drive", fields="files(id, name, parents)").execute()
            for f in res_file.get("files", []):
                matching.append({
                    "id": f["id"],
                    "name": f.get("name"),
                    "parents": f.get("parents", [pf_id]),
                    "folder_id": pf_id,
                    "folder_name": pf_name,
                })
        return matching

    def load(self) -> List[ActionItem]:
        self._actions = []
        self.last_error = None
        self.conflicted_action_ids = set()

        if self.drive_service:
            try:
                actions_root_id = self._get_drive_folder(ROOT_FOLDER_ID, "ACTIONS", create=False)
                if not actions_root_id:
                    self._actions = []
                    self.is_degraded = False
                    self._save_local_cache()
                    return self._actions

                files_client = self.drive_service.files()
                q_folders = f"'{actions_root_id}' in parents and mimeType='{MIME_FOLDER}' and trashed=false"
                res_folders = files_client.list(q=q_folders, spaces="drive", fields="files(id, name)").execute()
                proj_folders = res_folders.get("files", [])

                raw_records_by_id: Dict[str, List[Tuple[ActionItem, Dict[str, Any]]]] = {}

                for pf in proj_folders:
                    pf_id = pf["id"]
                    pf_name = pf.get("name", "Unassigned")
                    q_files = f"'{pf_id}' in parents and mimeType='{MIME_JSON}' and trashed=false"
                    res_files = files_client.list(q=q_files, spaces="drive", fields="files(id, name)").execute()
                    for f in res_files.get("files", []):
                        raw = files_client.get_media(fileId=f["id"]).execute()
                        doc = json.loads(raw.decode("utf-8"))
                        item = ActionItem.from_dict(doc)
                        file_meta = {"file_id": f["id"], "folder_id": pf_id, "folder_name": pf_name}
                        raw_records_by_id.setdefault(item.action_id, []).append((item, file_meta))

                final_actions = []
                conflict_messages = []

                for act_id, records in raw_records_by_id.items():
                    if len(records) > 1:
                        self.conflicted_action_ids.add(act_id)
                        conflict_info = [r[1] for r in records]
                        conflict_desc = ", ".join([f"[Folder '{ci['folder_name']}', FileId '{ci['file_id']}']" for ci in conflict_info])
                        conflict_messages.append(f"Action '{act_id}' ({conflict_desc})")

                        conflicted_item = ActionItem(
                            action_id=act_id,
                            title=f"[CONFLICTED] {records[0][0].title}",
                            type=TYPE_BLOCKED,
                            severity=SEVERITY_HIGH,
                            project_id="Conflict-Locked",
                            reason=f"SSOT Conflict: {len(records)} copies exist in Drive across {conflict_desc}.",
                            status=STATUS_CONFLICTED,
                            is_conflicted=True,
                            conflict_details=conflict_info,
                        )
                        final_actions.append(conflicted_item)
                    else:
                        final_actions.append(records[0][0])

                if conflict_messages:
                    self.last_error = f"SSOT Consistency Error: Conflicting duplicate action records detected for: {'; '.join(conflict_messages)}."

                self._actions = final_actions
                self.is_degraded = False
                self._save_local_cache()
                return self._actions
            except Exception as exc:
                self.last_error = f"Failed to load Actions from Drive SSOT: {exc}"
                self.is_degraded = True
                self._load_local_cache()
                return self._actions
        else:
            self.is_degraded = True
            self._load_local_cache()
            return self._actions

    def _load_local_cache(self) -> None:
        if self.local_file_path.exists():
            try:
                raw = json.loads(self.local_file_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._actions = [ActionItem.from_dict(d) for d in raw]
                    return
            except Exception as exc:
                self.last_error = f"Corrupt local actions cache: {exc}"
        self._actions = []

    def _save_local_cache(self) -> None:
        try:
            self.local_file_path.parent.mkdir(parents=True, exist_ok=True)
            raw = [a.to_dict() for a in self._actions if not a.is_conflicted]
            self.local_file_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.last_error = f"Local actions cache write failed: {exc}"
            raise RuntimeError(f"Failed to write local actions cache: {exc}") from exc

    def _write_to_drive(self, action: ActionItem) -> None:
        if not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")

        if action.action_id in self.conflicted_action_ids or action.is_conflicted:
            raise RuntimeError(f"Action '{action.action_id}' is in a conflicted state. Mutations are blocked.")

        files_client = self.drive_service.files()
        actions_root_id = self._get_drive_folder(ROOT_FOLDER_ID, "ACTIONS", create=True)
        target_proj = action.project_id or "Unassigned"
        target_folder_id = self._get_drive_folder(actions_root_id, target_proj, create=True)

        matching_files = self._find_scoped_action_files(action.action_id)
        body_content = json.dumps(action.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")

        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(body_content, mimetype=MIME_JSON, resumable=False)

        filename = f"{action.action_id}.json"

        if matching_files:
            primary = matching_files[0]
            file_id = primary["id"]
            parents = primary.get("parents", [])

            if target_folder_id not in parents:
                remove_parents = ",".join(parents) if parents else None
                update_kwargs = {
                    "fileId": file_id,
                    "media_body": media,
                    "addParents": target_folder_id,
                }
                if remove_parents:
                    update_kwargs["removeParents"] = remove_parents
                files_client.update(**update_kwargs).execute()
            else:
                files_client.update(fileId=file_id, media_body=media).execute()

            for extra in matching_files[1:]:
                try:
                    files_client.delete(fileId=extra["id"]).execute()
                except Exception as del_exc:
                    self.last_error = f"SSOT Cleanup Error: Failed to delete duplicate action file '{extra['id']}': {del_exc}"
                    raise RuntimeError(f"Failed to clean up duplicate action file '{extra['id']}': {del_exc}") from del_exc
        else:
            files_client.create(
                body={"name": filename, "parents": [target_folder_id], "mimeType": MIME_JSON},
                media_body=media,
                fields="id"
            ).execute()

    def list_actions(self) -> List[ActionItem]:
        return list(self._actions)

    def get_by_id(self, action_id: str) -> Optional[ActionItem]:
        for a in self._actions:
            if a.action_id == action_id:
                return a
        return None

    def reconcile_automatic_actions(self, derived_candidates: List[ActionItem]) -> List[ActionItem]:
        """Reconcile auto-derived actions into canonical Action SSOT store.

        - Creates and persists newly detected actions.
        - Closes detector-owned occurrences when their canonical evidence clears.
        - Preserves acknowledged/dismissed lifecycle state while the condition persists.
        - Keeps initial waiting_since and created_at timestamps stable across Dashboard reruns.
        """
        reconciled: List[ActionItem] = []
        candidate_incidents = {c.incident_key or c.action_id for c in derived_candidates}

        def incident_of(action: ActionItem) -> Optional[str]:
            return action.incident_key

        def persist(action: ActionItem) -> bool:
            if self.is_degraded or not self.drive_service:
                return False
            try:
                self._write_to_drive(action)
                return True
            except Exception as exc:
                self.last_error = f"Failed to persist auto-derived action to Drive: {exc}"
                self.is_degraded = True
                return False

        for cand in derived_candidates:
            incident_key = cand.incident_key or cand.action_id
            matching = [a for a in self._actions if incident_of(a) == incident_key]
            existing = next((a for a in reversed(matching)
                             if a.status in (STATUS_OPEN, STATUS_ACKNOWLEDGED)), None)
            if existing:
                # Update descriptive text while strictly preserving user lifecycle & stable timestamps
                updated = replace(existing, reason=cand.reason, impact=cand.impact,
                                  recommended_next_step=cand.recommended_next_step,
                                  linked_entity_ids=cand.linked_entity_ids)
                if not updated.waiting_since and cand.waiting_since:
                    updated.waiting_since = cand.waiting_since
                if persist(updated):
                    self._actions[self._actions.index(existing)] = updated
                    existing = updated
                reconciled.append(existing)
            else:
                prior = next((a for a in reversed(matching) if a.status in (STATUS_RESOLVED, STATUS_DISMISSED)), None)
                # A user-dismissed/resolved incident remains closed while its evidence persists.
                if prior and not (prior.resolution_note or "").startswith("Automatically resolved: canonical recovery"):
                    reconciled.append(prior)
                    continue
                occurrence = cand
                if matching:
                    occurrence = replace(cand, action_id=f"{cand.action_id}-OCC-{len(matching) + 1}")
                if persist(occurrence):
                    self._actions.append(occurrence)
                    reconciled.append(occurrence)

        # Canonical recovery closes only new detector-owned, non-conflicted occurrences.
        for existing in list(self._actions):
            incident_key = incident_of(existing)
            if (not incident_key or incident_key in candidate_incidents or existing.is_conflicted
                    or existing.status not in (STATUS_OPEN, STATUS_ACKNOWLEDGED)):
                continue
            closed = replace(existing, status=STATUS_RESOLVED,
                             resolved_at=datetime.now(timezone.utc).isoformat(),
                             resolution_note="Automatically resolved: canonical recovery / condition cleared")
            if persist(closed):
                self._actions[self._actions.index(existing)] = closed

        # Update local cache with reconciled items
        try:
            self._save_local_cache()
        except Exception:
            pass

        # Include any manually persisted actions not present in current derived candidates
        cand_ids = {a.action_id for a in reconciled}
        for p_act in self._actions:
            if p_act.action_id not in cand_ids and p_act not in reconciled:
                reconciled.append(p_act)

        return reconciled

    def add_action(self, action: ActionItem) -> None:
        self.last_error = None
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")
        if not action.title.strip():
            raise ValueError("Action title cannot be empty.")

        try:
            self._write_to_drive(action)
            self.is_degraded = False
        except Exception as exc:
            self.last_error = f"Drive SSOT write failed: {exc}"
            raise RuntimeError(f"Failed to persist Action to Drive SSOT: {exc}") from exc

        self._actions.append(action)
        self._save_local_cache()

    def update_action(self, action: ActionItem) -> None:
        self.last_error = None
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")

        if action.action_id in self.conflicted_action_ids or action.is_conflicted:
            raise RuntimeError(f"Action '{action.action_id}' is in a conflicted state. Mutations blocked.")

        try:
            self._write_to_drive(action)
            self.is_degraded = False
        except Exception as exc:
            self.last_error = f"Drive SSOT write failed: {exc}"
            raise RuntimeError(f"Failed to update Action in Drive SSOT: {exc}") from exc

        for idx, existing in enumerate(self._actions):
            if existing.action_id == action.action_id:
                self._actions[idx] = action
                self._save_local_cache()
                return
        self._actions.append(action)
        self._save_local_cache()

    def acknowledge_action(self, action_id: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")
        act = self.get_by_id(action_id)
        if not act:
            raise KeyError(f"Action not found: {action_id}")
        act.status = STATUS_ACKNOWLEDGED
        act.acknowledged_at = datetime.now(timezone.utc).isoformat()
        if note:
            act.resolution_note = note
        self.update_action(act)

    def resolve_action(self, action_id: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")
        act = self.get_by_id(action_id)
        if not act:
            raise KeyError(f"Action not found: {action_id}")
        act.status = STATUS_RESOLVED
        act.resolved_at = datetime.now(timezone.utc).isoformat()
        if note:
            act.resolution_note = note
        self.update_action(act)

    def dismiss_action(self, action_id: str, note: str = "") -> None:
        if self.is_degraded or not self.drive_service:
            raise RuntimeError("Drive SSOT unavailable — Action Center is read-only until cloud connection is restored.")
        act = self.get_by_id(action_id)
        if not act:
            raise KeyError(f"Action not found: {action_id}")
        act.status = STATUS_DISMISSED
        act.dismissed_at = datetime.now(timezone.utc).isoformat()
        if note:
            act.resolution_note = note
        self.update_action(act)


def get_default_actions_store(drive_service: Any = None) -> ActionsStore:
    return ActionsStore(drive_service=drive_service)


def derive_automatic_actions(
    all_tasks: List[Dict[str, Any]],
    active_executions: List[Dict[str, Any]],
    ideas_conflicted: List[Any],
    infra_health_list: Optional[List[Any]] = None,
    persisted_actions: Optional[List[ActionItem]] = None,
    commands: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> List[ActionItem]:
    """Derive live Action Item candidates from current SSOT state."""
    now_dt = now or datetime.now(timezone.utc)
    derived: List[ActionItem] = []

    # 1. Blocked Tasks
    for t in all_tasks:
        t_id = t.get("task_id", "")
        p_id = t.get("project_id", "Unassigned")
        st = t.get("status", "")
        if st == "blocked":
            act_id = f"ACT-TASK-BLOCKED-{p_id}-{t_id}"
            initial_waiting = t.get("updated_at") or t.get("created_at") or now_dt.isoformat()
            derived.append(ActionItem(
                    action_id=act_id,
                    title=f"Task Blocked: {t.get('title', t_id)}",
                    type=TYPE_BLOCKED,
                    severity=SEVERITY_HIGH,
                    project_id=p_id,
                    task_id=t_id,
                    created_at=t.get("created_at") or now_dt.isoformat(),
                    waiting_since=initial_waiting,
                    reason=f"Task marked as '{st}'. Next Action: {t.get('next_action', 'None')}",
                    impact=f"Project roadmap for '{p_id}' cannot progress.",
                    recommended_next_step="Inspect task logs, resolve blocker dependencies, or update status.",
                    need_user_action=True,
                    source="Task State Monitor",
                    linked_entity_ids=[t_id],
                    incident_key=act_id,
                ))

        # 3. Manual AG Dispatch Needed
        elif st == "ready" and (t.get("assigned_provider") == "antigravity" or t.get("recommended_provider") == "antigravity"):
            act_id = f"ACT-AG-DISPATCH-{p_id}-{t_id}"
            initial_waiting = t.get("created_at") or now_dt.isoformat()
            derived.append(ActionItem(
                    action_id=act_id,
                    title=f"Manual AG Dispatch Required: {t.get('title', t_id)}",
                    type=TYPE_ACTION_NEEDED,
                    severity=SEVERITY_HIGH,
                    project_id=p_id,
                    task_id=t_id,
                    created_at=t.get("created_at") or now_dt.isoformat(),
                    waiting_since=initial_waiting,
                    reason="Task prepared for Antigravity (AG); AG has no direct background connector and requires manual prompt injection.",
                    impact="Automatic autonomous execution cannot proceed until dispatched to AG.",
                    recommended_next_step="Copy task prompt to Antigravity IDE and start execution session.",
                    need_user_action=True,
                    source="Provider Dispatch Matrix",
                    linked_entity_ids=[t_id],
                    incident_key=act_id,
                ))

    # 4. Stale Executions
    from manager.runtime_visibility import determine_ai_runtime_activity
    for exe in active_executions:
        ai_state = determine_ai_runtime_activity(exe, now_dt)[0]
        status = exe.get("status", "")
        failed = status in ("failed", "interrupted", "cancelled")
        if is_execution_stale(exe, now_dt) or ai_state in ["POSSIBLY STALLED", "STALE"] or failed:
            e_id = exe.get("execution_id", "unknown")
            p_id = exe.get("project_id", "Unassigned")
            t_id = exe.get("task_id", "")
            prov = exe.get("provider", "AI")
            task_block = next((a for a in derived if a.project_id == p_id and a.task_id == t_id and a.type == TYPE_BLOCKED), None)
            act_id = task_block.action_id if task_block else f"ACT-EXEC-ISSUE-{p_id}-{t_id}-{e_id}"
            initial_waiting = exe.get("stale_at") or exe.get("heartbeat_at") or exe.get("started_at") or now_dt.isoformat()
            result = exe.get("result") or {}
            detail = exe.get("error_kind") or result.get("error_kind") or exe.get("recovery_reason") or status or "activity timeout"
            candidate = ActionItem(
                    action_id=act_id,
                    title=f"Execution Needs Attention: {prov} on {t_id}",
                    type=TYPE_BLOCKED,
                    severity=SEVERITY_HIGH,
                    project_id=p_id,
                    task_id=t_id,
                    created_at=exe.get("started_at") or now_dt.isoformat(),
                    waiting_since=initial_waiting,
                    reason=f"Execution {status or 'liveness'} evidence on {prov}: {detail}.",
                    impact="Provider worker may have crashed or hung.",
                    recommended_next_step="Restart provider session or trigger rollback.",
                    need_user_action=True,
                    source="Execution Liveness Watcher",
                    linked_entity_ids=[e_id, t_id],
                    incident_key=act_id,
                )
            if task_block:
                task_block.reason = candidate.reason
                task_block.linked_entity_ids = list(dict.fromkeys(task_block.linked_entity_ids + candidate.linked_entity_ids))
            else:
                derived.append(candidate)

    # Command status is canonical evidence; Task status is deliberately not overloaded.
    for command in commands or []:
        status = command.get("status", "")
        if status not in ("attention", "failed"):
            continue
        p_id, t_id = command.get("project_id", "Unassigned"), command.get("task_id", "")
        c_id = command.get("command_id", "unknown")
        act_id = f"ACT-COMMAND-{status.upper()}-{p_id}-{t_id}-{c_id}"
        result = command.get("result") or {}
        detail = command.get("recovery_reason") or result.get("error_kind") or command.get("error_kind") or status
        derived.append(ActionItem(action_id=act_id, incident_key=act_id,
            title=f"Command {status.title()}: {c_id}", type=TYPE_REVIEW_REQUIRED if status == "attention" else TYPE_BLOCKED,
            severity=SEVERITY_HIGH, project_id=p_id, task_id=t_id,
            created_at=command.get("created_at") or now_dt.isoformat(),
            waiting_since=command.get("stale_at") or command.get("created_at") or now_dt.isoformat(),
            reason=f"Command {status} evidence: {detail}.", impact="Execution requires review or recovery.",
            recommended_next_step="Inspect command and execution evidence before retrying.", source="Command State Monitor",
            linked_entity_ids=[c_id, t_id]))

    # 5. Conflicted SSOT records
    for conf in ideas_conflicted:
        idea_id = getattr(conf, "idea_id", str(conf))
        act_id = f"ACT-SSOT-CONFLICT-{idea_id}"
        derived.append(ActionItem(
                action_id=act_id,
                title=f"SSOT Record Conflict: Idea {idea_id}",
                type=TYPE_BLOCKED,
                severity=SEVERITY_HIGH,
                project_id="System",
                created_at=now_dt.isoformat(),
                waiting_since=now_dt.isoformat(),
                reason="Multiple conflicting records exist with the same ID across Drive folders.",
                impact="Mutations on this entity are locked to prevent split-brain data corruption.",
                recommended_next_step="Inspect Google Drive folder and remove duplicate orphaned files.",
                need_user_action=True,
                source="SSOT Truth Guard",
                linked_entity_ids=[idea_id],
                incident_key=act_id,
            ))

    return derived


def get_actions_summary(actions: List[ActionItem]) -> Dict[str, int]:
    open_items = [a for a in actions if a.status in [STATUS_OPEN, STATUS_ACKNOWLEDGED]]
    return {
        "total": len(actions),
        "open": len([a for a in actions if a.status == STATUS_OPEN]),
        "acknowledged": len([a for a in actions if a.status == STATUS_ACKNOWLEDGED]),
        "history": len([a for a in actions if a.status in [STATUS_RESOLVED, STATUS_DISMISSED]]),
        "need_user_action": len([a for a in open_items if a.need_user_action]),
        "review_required": len([a for a in open_items if a.type == TYPE_REVIEW_REQUIRED]),
        "blocked": len([a for a in open_items if a.type == TYPE_BLOCKED]),
        "high_severity": len([a for a in open_items if a.severity == SEVERITY_HIGH]),
    }
