"""Layer-4 live Antigravity smoke: one real, minimal, independently verified dispatch.

Never part of pytest (the suite fences the live IDE off, see conftest.py).
Consumes ONE small model turn on the signed-in Antigravity account and leaves
one conversation in the IDE's history. Everything happens in a disposable git
repository the script creates itself; production data is never touched.

    python -m manager.ag_live_smoke [--model <catalog model id|label>] [--timeout 300]
                                    [--workspace DIR] [--evidence PATH]

What it proves, in order (each step is recorded in the evidence document):

1. READY handshake through the real language server (quota, account, model
   catalog, dispatch route) -- ``AgRunner.prepare``;
2. a NEW cascade bound to exactly the disposable workspace, the prompt
   delivered over the ``ide_bridge`` transport -- ``AgRunner.start``;
3. provider terminal truth observed through the trajectory RPCs, never an
   exit code -- ``AgRunner.wait``;
4. **independent** verification: ADM's own ``git status`` / file read of the
   workspace decide whether the task was done, not the agent's reply;
5. no lingering binding: the run state is terminal and ``close`` was proven.

Exit status 0 only when steps 1-5 all hold.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from manager.ag_language_server import redact
from manager.ag_run_state import TERMINAL_STATUSES, read_run_state
from manager.ag_runner import AgLaunchError, AgRunner, LaunchRequest

SMOKE_FILE = "adm-smoke.txt"
SMOKE_CONTENT = "ADM-LIVE-SMOKE-OK"
PROMPT = (
    f"In this workspace, create a new file named {SMOKE_FILE} whose entire content is the single line "
    f"{SMOKE_CONTENT}. Do not modify any other file. Do not run git commands. When done, reply DONE."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout


def make_disposable_repo(workspace: Path | None) -> Path:
    workspace = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="adm-ag-live-smoke-"))
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        _git(workspace, "init", "-q")
        _git(workspace, "config", "user.name", "adm-live-smoke")
        _git(workspace, "config", "user.email", "adm-live-smoke@localhost")
        (workspace / "README.md").write_text("disposable Antigravity live-smoke workspace\n", encoding="utf-8")
        _git(workspace, "add", "README.md")
        _git(workspace, "commit", "-q", "-m", "seed")
    if (workspace / SMOKE_FILE).exists():
        raise RuntimeError(f"{SMOKE_FILE} already exists in {workspace}; the smoke needs a clean workspace")
    return workspace.resolve()


def verify_workspace_independently(workspace: Path, head_before: str) -> dict:
    """ADM's own git evidence -- the agent's reply is not consulted here at all."""
    status = _git(workspace, "status", "--porcelain").splitlines()
    head_after = _git(workspace, "rev-parse", "HEAD").strip()
    target = workspace / SMOKE_FILE
    content = target.read_text(encoding="utf-8") if target.is_file() else None
    verdict = {
        "git_status_porcelain": status,
        "head_before": head_before, "head_after": head_after, "head_unchanged": head_before == head_after,
        "smoke_file_exists": target.is_file(),
        "smoke_file_content_ok": content is not None and content.strip() == SMOKE_CONTENT,
        "only_smoke_file_changed": status == [f"?? {SMOKE_FILE}"],
    }
    verdict["passed"] = all((verdict["head_unchanged"], verdict["smoke_file_exists"], verdict["smoke_file_content_ok"], verdict["only_smoke_file_changed"]))
    return verdict


def run_live_smoke(*, model: str | None, timeout: float, workspace: Path | None, manager_home: str | None = None) -> dict:
    evidence: dict = {"started_at": utc_now(), "transport_requested": "ide_bridge", "steps": {}}
    workspace = make_disposable_repo(workspace)
    evidence["workspace"] = str(workspace)
    head_before = _git(workspace, "rev-parse", "HEAD").strip()
    runner = AgRunner()
    request = LaunchRequest(working_directory=str(workspace), project_id="adm-live-smoke", model=model,
                            sandbox=None, approval_policy=None, timeout_seconds=15.0, turn_timeout_seconds=timeout)
    prepared = running = outcome = None
    try:
        try:
            prepared = runner.prepare(request)
            state = read_run_state(prepared.thread_id, manager_home) or {}
            evidence["steps"]["prepare"] = {
                "ok": True, "mode": prepared.mode, "thread_id": prepared.thread_id, "language_server_pid": prepared.pid,
                "process_creation_identity": prepared.process_creation_identity, "transport": state.get("transport"),
                "model_id": state.get("model_id"), "model_placeholder": state.get("model_placeholder"),
                "readiness": redact(state.get("readiness")),
            }
        except AgLaunchError as exc:
            evidence["steps"]["prepare"] = {"ok": False, "classification": exc.classification, "detail": exc.detail}
            evidence["passed"] = False
            return evidence
        try:
            running = runner.start(prepared, PROMPT)
            state = read_run_state(prepared.thread_id, manager_home) or {}
            evidence["steps"]["start"] = {
                "ok": True, "turn_id": running.turn_id, "started_at": running.started_at,
                "conversation_id": state.get("conversation_id"), "binding": state.get("binding"),
                "workspace_check": state.get("workspace_check"),
            }
        except AgLaunchError as exc:
            state = read_run_state(prepared.thread_id, manager_home) or {}
            evidence["steps"]["start"] = {"ok": False, "classification": exc.classification, "detail": exc.detail,
                                          "run_state_status": state.get("status"), "cancel_evidence": state.get("cancel_evidence")}
            evidence["passed"] = False
            return evidence
        outcome = runner.wait(running)
        evidence["steps"]["wait"] = {
            "status": outcome.status, "failure_classification": outcome.failure_classification,
            "failure_detail": outcome.failure_detail, "completed_at": outcome.completed_at,
            "response_text": (outcome.response_text or "")[:500], "stats": redact(outcome.stats),
        }
    finally:
        if prepared is not None:
            runner.close(running or prepared)
            state = read_run_state(prepared.thread_id, manager_home) or {}
            evidence["steps"]["close"] = {"run_state_status": state.get("status"), "terminal": state.get("status") in TERMINAL_STATUSES,
                                          "process_handle_stopped": getattr(prepared, "_process", None) is not None and prepared._process.poll() is not None}
    evidence["steps"]["independent_git_verification"] = verify_workspace_independently(workspace, head_before)
    evidence["passed"] = bool(outcome is not None and outcome.status == "completed"
                              and evidence["steps"]["independent_git_verification"]["passed"]
                              and evidence["steps"]["close"]["terminal"] and evidence["steps"]["close"]["process_handle_stopped"])
    evidence["finished_at"] = utc_now()
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live minimal Antigravity dispatch smoke (consumes one model turn).")
    parser.add_argument("--model", default=None, help="catalog model id/label; default = cheapest recommended Gemini Flash")
    parser.add_argument("--timeout", type=float, default=300.0, help="turn timeout in seconds")
    parser.add_argument("--workspace", default=None, help="existing disposable directory to use (default: a fresh temp dir)")
    parser.add_argument("--evidence", default=None, help="write the evidence JSON here as well as to stdout")
    args = parser.parse_args(argv)
    evidence = run_live_smoke(model=args.model, timeout=args.timeout, workspace=Path(args.workspace) if args.workspace else None)
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(text, encoding="utf-8")
    print(text)
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
