"""Durable, crash-safe Phase-1 cursor primitive for actual-invocation fair scheduling."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class StaleCursorError(Exception):
    """Raised when a concurrent/stale writer attempts to overwrite a newer cursor generation."""
    pass


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_cursor_path(manager_home=None, cursor_path=None):
    if cursor_path is not None:
        return Path(cursor_path)
    home = manager_home if manager_home is not None else os.environ.get("AI_MANAGER_HOME", ".")
    return Path(home) / "runtime" / "phase1-cursor.json"


def _default_cursor():
    return {
        "project_cursor": 0,
        "per_project_record_cursor": {},
        "per_project_attention_visits": {},
        "generation": 0,
        "updated_at": None,
    }


def load_phase1_cursor(manager_home=None, cursor_path=None):
    """Load the Phase-1 cursor state with fail-safe recovery on missing or corrupted state."""
    path = _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)
    try:
        if not path.exists():
            return _default_cursor()
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return _default_cursor()
        project_cursor = data.get("project_cursor")
        if not isinstance(project_cursor, int) or project_cursor < 0:
            project_cursor = 0
        record_cursors = data.get("per_project_record_cursor")
        if not isinstance(record_cursors, dict):
            record_cursors = {}
        else:
            clean_records = {}
            for k, v in record_cursors.items():
                if isinstance(k, str) and isinstance(v, int) and v >= 0:
                    clean_records[k] = v
            record_cursors = clean_records
        attention_visits = data.get("per_project_attention_visits")
        if not isinstance(attention_visits, dict):
            attention_visits = {}
        else:
            attention_visits = {k: v for k, v in attention_visits.items()
                                if isinstance(k, str) and isinstance(v, int) and v >= 0}
        generation = data.get("generation")
        if not isinstance(generation, int) or generation < 0:
            generation = 0
        updated_at = data.get("updated_at")
        if not isinstance(updated_at, str):
            updated_at = None
        return {
            "project_cursor": project_cursor,
            "per_project_record_cursor": record_cursors,
            "per_project_attention_visits": attention_visits,
            "generation": generation,
            "updated_at": updated_at,
        }
    except Exception:
        return _default_cursor()


def save_phase1_cursor(cursor_data, manager_home=None, cursor_path=None, expected_generation=None):
    """Atomically persist Phase-1 cursor with optional CAS generation verification.

    ``generation`` is a strictly increasing write serial (every successful
    save advances it by one), so a delayed writer can never roll the file
    back to an older generation; ``expected_generation`` is the CAS token
    a writer must present. One invocation may legitimately save twice
    (Phase-1 cursor advance, then its attention-visit advances).
    """
    path = _resolve_cursor_path(manager_home=manager_home, cursor_path=cursor_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if expected_generation is not None:
        current = load_phase1_cursor(manager_home=manager_home, cursor_path=cursor_path)
        if current.get("generation", 0) != expected_generation:
            raise StaleCursorError(
                f"Cursor generation mismatch: expected {expected_generation}, found {current.get('generation')}"
            )

    new_generation = (cursor_data.get("generation") or 0) + 1
    project_cursor = int(cursor_data.get("project_cursor", 0))
    if project_cursor < 0:
        project_cursor = 0

    record_cursors = cursor_data.get("per_project_record_cursor", {})
    if not isinstance(record_cursors, dict):
        record_cursors = {}
    clean_records = {str(k): int(v) for k, v in record_cursors.items() if isinstance(v, (int, float)) and v >= 0}
    attention_visits = cursor_data.get("per_project_attention_visits", {})
    if not isinstance(attention_visits, dict):
        attention_visits = {}
    clean_visits = {str(k): int(v) for k, v in attention_visits.items() if isinstance(v, (int, float)) and v >= 0}

    payload = {
        "project_cursor": project_cursor,
        "per_project_record_cursor": clean_records,
        "per_project_attention_visits": clean_visits,
        "generation": new_generation,
        "updated_at": now_iso(),
    }

    dir_path = path.parent
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dir_path, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name

    try:
        os.replace(temp_name, str(path))
    except Exception:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise

    return payload
