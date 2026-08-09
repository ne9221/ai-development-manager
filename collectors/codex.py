#!/usr/bin/env python3
"""Collect Codex quota from the official app-server JSON-RPC interface."""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


class CollectorError(RuntimeError):
    pass


def iso_time(epoch=None):
    value = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class AppServer:
    def __init__(self, timeout):
        executable = os.environ.get("CODEX_BIN")
        if not executable:
            executable = (shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")) if os.name == "nt" else shutil.which("codex")
        if not executable:
            raise CollectorError("codex CLI not found in PATH")
        command = [executable, "app-server"]
        if executable.lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", f'""{executable}" app-server"']
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            raise CollectorError(f"failed to start codex app-server: {exc}") from exc
        self.timeout = timeout
        self.messages = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message):
        if self.process.poll() is not None:
            detail = self.process.stderr.read().strip()
            raise CollectorError(f"codex app-server exited during startup: {detail or self.process.returncode}")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, request_id, method, params=None):
        message = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            try:
                reply = self.messages.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise CollectorError(f"{method} did not respond within {self.timeout:g}s") from exc
            if reply is None:
                detail = self.process.stderr.read().strip()
                raise CollectorError(f"codex app-server closed stdout: {detail or 'no error detail'}")
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise CollectorError(f"JSON-RPC error from {method}: {json.dumps(reply['error'])}")
            if "result" not in reply:
                raise CollectorError(f"invalid JSON-RPC response from {method}")
            return reply

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def normalize(reply):
    result = reply["result"]
    limits = result.get("rateLimits") or result
    captured = iso_time()
    windows = []
    raw_resets = {}
    for name, window in limits.items():
        if not isinstance(window, dict) or "usedPercent" not in window:
            continue
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        if reset is not None:
            raw_resets[name] = reset
        windows.append({
            "name": name,
            "duration_minutes": window.get("windowDurationMins"),
            "used_percent": used,
            "remaining_percent": None if used is None else max(0, min(100, 100 - used)),
            "resets_at": None if reset is None else iso_time(reset),
        })
    metadata = {
        "method": "account/rateLimits/read",
        "limit_id": limits.get("limitId"),
        "limit_name": limits.get("limitName"),
        "plan_type": limits.get("planType", result.get("planType")),
        "credits": limits.get("credits", result.get("credits")),
        "rate_limit_reached_type": limits.get("rateLimitReachedType"),
        "raw_resets_at": raw_resets,
    }
    return {
        "schema_version": "0.1.0",
        "generated_at": captured,
        "providers": [{
            "provider": "codex", "display_name": "Codex",
            "collection_mode": "automatic", "source": "codex_app_server",
            "source_type": "official", "confidence": "official",
            "last_updated": captured, "status": "ok", "windows": windows,
            "metadata": {key: value for key, value in metadata.items() if value is not None},
        }],
    }


def validate(document, schema_path):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise CollectorError("schema validation unavailable; install collectors/requirements.txt") from exc
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise CollectorError(f"schema validation failed: {exc}") from exc


def collect(timeout):
    server = AppServer(timeout)
    try:
        try:
            server.request(1, "initialize", {"clientInfo": {
                "name": "ai_development_manager", "title": "AI Development Manager", "version": "0.1.0"
            }})
        except CollectorError as exc:
            raise CollectorError(f"initialize failed: {exc}") from exc
        server.send({"method": "initialized", "params": {}})
        raw = server.request(2, "account/rateLimits/read")
        return raw, normalize(raw)
    finally:
        server.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("codex.status.json"))
    parser.add_argument("--schema", type=Path, default=Path(__file__).parents[1] / "schema" / "status.schema.json")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--show-raw", action="store_true")
    args = parser.parse_args()
    try:
        raw, document = collect(args.timeout)
        validate(document, args.schema)
        args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        if args.show_raw:
            print("RAW " + json.dumps(raw, separators=(",", ":")))
        print(json.dumps(document, indent=2))
        print(f"VALID {args.schema}", file=sys.stderr)
    except (CollectorError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
