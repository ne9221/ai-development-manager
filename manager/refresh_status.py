#!/usr/bin/env python3
"""Refresh automatic quota providers and publish one validated Drive SSOT."""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from collectors.claude import normalize as normalize_claude
from collectors.codex import collect as collect_codex
from collectors.publish_drive import build_service, sync_drive
from manager.quota_reader import read_drive_status, validate_status


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "schema" / "status.schema.json"


class RefreshError(RuntimeError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_line(path, message):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{now_iso()} {message}\n")


@contextmanager
def runtime_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RefreshError("another refresh is already running") from exc
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def replace_provider(document, provider):
    provider_id = provider["provider"]
    document["providers"] = [item for item in document["providers"] if item.get("provider") != provider_id]
    document["providers"].append(provider)


def claude_snapshot(path):
    if not path.is_file():
        return None
    captured = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return normalize_claude(json.loads(path.read_text(encoding="utf-8-sig")), captured_at=captured)


def write_atomic(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def refresh(*, service, runtime_path, log_path, lock_path, claude_path,
            reader=read_drive_status, codex_collector=collect_codex,
            publisher=sync_drive, validator=validate_status):
    with runtime_lock(lock_path):
        log_line(log_path, "refresh start")
        try:
            document = reader(service=service)
        except Exception as exc:
            log_line(log_path, f"Drive read failure: {type(exc).__name__}")
            raise RefreshError("could not read Drive runtime SSOT") from exc

        outcomes = {}
        try:
            _, codex_document = codex_collector(timeout=20)
            codex = next(item for item in codex_document["providers"] if item.get("provider") == "codex")
            replace_provider(document, codex)
            outcomes["codex"] = "success"
            log_line(log_path, "provider codex success")
        except Exception as exc:
            outcomes["codex"] = "unavailable"
            log_line(log_path, f"provider codex unavailable: {type(exc).__name__}")

        try:
            claude = claude_snapshot(claude_path)
            existing = next((item for item in document["providers"] if item.get("provider") == "claude"), None)
            if claude and claude["windows"]:
                if not existing or claude["last_updated"] > existing.get("last_updated", ""):
                    replace_provider(document, claude)
                    outcomes["claude"] = "success"
                else:
                    outcomes["claude"] = "unchanged"
            else:
                outcomes["claude"] = "unavailable"
            log_line(log_path, f"provider claude {outcomes['claude']}")
        except Exception as exc:
            outcomes["claude"] = "unavailable"
            log_line(log_path, f"provider claude unavailable: {type(exc).__name__}")

        document["generated_at"] = now_iso()
        try:
            validator(document, SCHEMA)
            log_line(log_path, "schema validation success")
        except Exception as exc:
            log_line(log_path, f"schema validation failure: {type(exc).__name__}")
            raise RefreshError("schema validation failed; Drive was not published") from exc

        write_atomic(runtime_path, document)
        try:
            result = publisher(service, runtime_path)
            log_line(log_path, f"Drive publish success: {result['action']}")
        except Exception as exc:
            log_line(log_path, f"Drive publish failure: {type(exc).__name__}")
            raise RefreshError("Drive publish failed") from exc
        log_line(log_path, "refresh end success")
        return {"providers": outcomes, "publish": result, "document": document}


def main():
    home = Path(os.environ.get("AI_MANAGER_HOME", Path.home() / ".ai-development-manager"))
    try:
        result = refresh(
            service=build_service(),
            runtime_path=home / "runtime" / "status.json",
            log_path=home / "logs" / "refresh.log",
            lock_path=home / "refresh.lock",
            claude_path=Path(os.environ.get("CLAUDE_STATUSLINE_PAYLOAD", Path.home() / ".claude" / "statusline-payload.json")),
        )
        print(f"REFRESHED Drive status.json ({result['publish']['action']})")
        return 0
    except RefreshError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: refresh initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
