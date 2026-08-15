#!/usr/bin/env python3
"""Local read-only telemetry adapter for Codex, Claude, and Antigravity."""

import os
import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# JWT pattern matcher to redact sensitive tokens/credentials
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+=]*")

def format_timestamp(ts):
    """Normalize timestamp to ISO Z format."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # If timestamp is millisecond-based epoch
        if ts > 1e11:
            ts = ts / 1000.0
        try:
            dt = datetime.fromtimestamp(ts, timezone.utc)
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        except Exception:
            return None
    if isinstance(ts, str):
        if ts.isdigit():
            try:
                val = float(ts)
                if val > 1e11:
                    val = val / 1000.0
                dt = datetime.fromtimestamp(val, timezone.utc)
                return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
            except Exception:
                return None
        # Handle formats like "2026-08-15T11:00:00.000Z"
        if ts.endswith("Z") or "+00:00" in ts:
            return ts
        try:
            # Try to parse string to verify, otherwise just return it
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        except Exception:
            return ts
    return None

def map_cwd_to_project(cwd, projects):
    """Normalize and match working directories to project IDs."""
    if not cwd or not projects:
        return None
    try:
        cwd_norm = os.path.normcase(os.path.abspath(cwd))
        for proj in projects:
            proj_cwd = proj.get("working_directory")
            if proj_cwd:
                proj_cwd_norm = os.path.normcase(os.path.abspath(proj_cwd))
                if cwd_norm == proj_cwd_norm or cwd_norm.startswith(proj_cwd_norm + os.sep) or proj_cwd_norm.startswith(cwd_norm + os.sep):
                    return proj.get("project_id")
    except Exception:
        pass
    return None

def sanitize_value(val):
    """Filter out JWTs, credentials, and potential secrets from output."""
    if not isinstance(val, str):
        return val
    val = JWT_PATTERN.sub("[REDACTED JWT]", val)
    # Redact common credential patterns or keywords in telemetry logs
    if any(secret_kw in val.lower() for secret_kw in ["bearer ", "api_key", "password", "client_secret"]):
        return "[REDACTED CREDENTIAL]"
    return val

def sanitize_record(record):
    """Sanitize all fields in a telemetry record."""
    return {k: sanitize_value(v) for k, v in record.items()}

def collect_codex_telemetry(codex_home=None, store=None):
    """Query local Codex threads database in read-only mode."""
    if not codex_home:
        codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    db_path = Path(codex_home) / "state_5.sqlite"
    
    if not db_path.exists():
        return []
        
    records = []
    conn = None
    try:
        # Connect strictly in read-only mode
        db_uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        cursor = conn.cursor()
        
        # Dynamically inspect column availability to support older/newer schemas
        cursor.execute("PRAGMA table_info(threads)")
        columns = {row[1] for row in cursor.fetchall()}
        
        supported = ["id", "created_at", "updated_at", "model", "model_provider", "reasoning_effort", "tokens_used", "cwd", "title", "source", "cli_version", "archived"]
        active_cols = [col for col in supported if col in columns]
        
        if "id" not in columns:
            return []
            
        query = f"SELECT {', '.join(active_cols)} FROM threads"
        cursor.execute(query)
        rows = cursor.fetchall()
        
        projects = []
        if store:
            try:
                projects = store.list_projects()
            except Exception:
                pass
                
        for row in rows:
            row_dict = dict(zip(active_cols, row))
            
            thread_id = row_dict.get("id")
            if not thread_id:
                continue
                
            started_at = format_timestamp(row_dict.get("created_at"))
            updated_at = format_timestamp(row_dict.get("updated_at"))
            
            cwd = row_dict.get("cwd")
            project_id = map_cwd_to_project(cwd, projects) or "_unclassified"
            
            tokens = row_dict.get("tokens_used")
            if tokens is not None:
                try:
                    tokens = int(tokens)
                except (ValueError, TypeError):
                    tokens = 0
            else:
                tokens = 0
                
            reasoning_effort = row_dict.get("reasoning_effort")
            title = row_dict.get("title")
            activity = f"Thread: {title}" if title else "Active turn"
            
            rec = {
                "provider": "codex",
                "account_id": "codex-account",
                "session_id": str(thread_id),
                "project": project_id,
                "model": row_dict.get("model") or "unknown",
                "reasoning_effort": reasoning_effort,
                "activity": activity,
                "started_at": started_at,
                "updated_at": updated_at,
                "tokens": tokens,
                "source": str(db_path),
                "confidence": "official"
            }
            records.append(sanitize_record(rec))
    except Exception as e:
        # Let callers/tests handle locked/unreadable DB failures specifically
        raise e
    finally:
        if conn:
            conn.close()
            
    return records

def parse_claude_jsonl(file_path, account_name, projects):
    """Parse a single Claude session JSONL file with event aggregation."""
    file_path = Path(file_path)
    session_id = file_path.stem
    
    first_timestamp = None
    last_timestamp = None
    total_tokens = 0
    cwd = None
    last_event_type = None
    last_text_snippet = None
    model = "claude-3-5-sonnet"
    
    # Read line-by-line for isolation and memory efficiency
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                # Isolate malformed JSON lines
                continue
                
            ts = data.get("timestamp")
            if ts:
                if first_timestamp is None:
                    first_timestamp = ts
                last_timestamp = ts
                
            s_id = data.get("sessionId")
            if s_id:
                session_id = s_id
                
            c = data.get("cwd")
            if c:
                cwd = c
                
            m = data.get("model")
            if m:
                model = m
                
            msg = data.get("message")
            if isinstance(msg, dict):
                if "model" in msg:
                    model = msg["model"]
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    in_tok = usage.get("input_tokens") or usage.get("inputTokens") or 0
                    out_tok = usage.get("output_tokens") or usage.get("outputTokens") or 0
                    try:
                        total_tokens += int(in_tok) + int(out_tok)
                    except (ValueError, TypeError):
                        pass
                        
            ev_type = data.get("type")
            if ev_type:
                last_event_type = ev_type
                
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list) and content:
                    first_block = content[0]
                    if isinstance(first_block, dict) and first_block.get("type") == "text":
                        text = first_block.get("text")
                        if text:
                            last_text_snippet = text[:50]
                elif isinstance(content, str):
                    last_text_snippet = content[:50]

    if not first_timestamp:
        return None
        
    project_id = map_cwd_to_project(cwd, projects) or "_unclassified"
    
    # Derived activity label
    activity = f"Event: {last_event_type}" if last_event_type else "Active session"
    if last_text_snippet:
        activity = f"{activity} ({last_text_snippet}...)"
        
    started_at = format_timestamp(first_timestamp)
    updated_at = format_timestamp(last_timestamp)
    
    rec = {
        "provider": "claude",
        "account_id": account_name,
        "session_id": str(session_id),
        "project": project_id,
        "model": model,
        "reasoning_effort": None,
        "activity": activity,
        "started_at": started_at,
        "updated_at": updated_at,
        "tokens": total_tokens,
        "source": str(file_path),
        "confidence": "derived"
    }
    return sanitize_record(rec)

def collect_claude_telemetry_for_root(root_path, account_name, store=None):
    """Scans all session JSONL files under a project root directory."""
    root_path = Path(root_path)
    if not root_path.exists():
        return []
        
    records = []
    projects_dir = root_path / "projects"
    if not projects_dir.exists():
        return []
        
    projects = []
    if store:
        try:
            projects = store.list_projects()
        except Exception:
            pass
            
    for jsonl_file in projects_dir.rglob("*.jsonl"):
        try:
            record = parse_claude_jsonl(jsonl_file, account_name, projects)
            if record:
                records.append(record)
        except Exception:
            # Isolate single file parsing failures
            pass
            
    return records

def collect_antigravity_telemetry():
    """Telemetry is unavailable for Antigravity; returns status unknown/unavailable."""
    rec = {
        "provider": "antigravity",
        "account_id": "unknown",
        "session_id": "unknown",
        "project": "_unclassified",
        "model": "unknown",
        "reasoning_effort": None,
        "activity": "Telemetry unavailable",
        "started_at": None,
        "updated_at": None,
        "tokens": 0,
        "source": "unavailable",
        "confidence": "unavailable"
    }
    return [sanitize_record(rec)]

def collect_local_telemetry(codex_home=None, claude_roots=None, store=None):
    """Master local read-only telemetry adapter dispatcher."""
    results = []
    
    # 1. Codex
    try:
        results.extend(collect_codex_telemetry(codex_home=codex_home, store=store))
    except Exception:
        pass
        
    # 2. Claude
    if not claude_roots:
        claude_roots = {
            "account-a": os.path.expanduser("~/.claude"),
            "account-b": os.path.expanduser("~/.claude-b")
        }
        
    for account_name, root_path in claude_roots.items():
        try:
            results.extend(collect_claude_telemetry_for_root(root_path, account_name, store=store))
        except Exception:
            pass
            
    # 3. Antigravity
    results.extend(collect_antigravity_telemetry())
    
    return results
