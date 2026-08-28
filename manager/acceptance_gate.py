#!/usr/bin/env python3
"""Local-only, fail-closed, non-persistent controlled acceptance gate.

Exists to answer one question with real, machine-verified evidence, without
ever touching the real quota SSOT or genuinely exhausting a real provider
account (PROJECT BLOCKER 2 "safe live reroute" / BLOCKER 3 "waiting_quota
restart durability", 2026-08-28): does the real production scheduler/
dispatcher path -- manager.dispatcher.dispatch(), manager.scheduler.schedule(),
manager.command_watcher's waiting_quota retry-sweep -- actually reroute to
an alternate provider (or correctly enter/promote out of waiting_quota) when
a specific provider is genuinely unavailable, using the exact same code path
real traffic uses?

Design constraints (all load-bearing, not incidental):

1. Never modifies the real quota SSOT. apply_controlled_unavailability()
   only ever returns a deep-copied, in-memory document; nothing here ever
   writes to Drive, and the on-disk local override file this module reads
   is never itself quota data -- just an activation switch.
2. A normal external dispatch request can never activate, scope, or extend
   this. The ONLY way an override is ever created is write_override(),
   called directly and locally (see its own docstring) -- never reachable
   from cloud.dispatch_ingress (Drive ingress, GitHub ingress) or
   manager.mcp_adapter (the MCP tools), none of which have local filesystem
   write access to an arbitrary manager_home path, and none of which pass
   caller-supplied data anywhere near this module.
3. Fails closed for ITSELF: read_active_override() treats every failure
   mode (file missing, malformed JSON, wrong tag, expired, negative/skewed
   age) as "no override" -- real quota is returned completely unmodified.
   This is deliberate: the override's own failure mode must only ever be
   "the simulation didn't activate," never "real production dispatch is
   blocked."
4. Every simulated-unavailable window is tagged in the audit log this
   module writes (acceptance-gate/applied-log.jsonl under manager_home) --
   never silently indistinguishable from real exhaustion in that log, even
   though the returned quota_document itself deliberately mirrors real
   exhaustion's exact shape (see point 5) so it flows through production
   code unmodified.
5. Routes through the exact same production path: this module never
   reimplements eligibility/reroute/waiting_quota logic. It is wired into
   manager.quota_reader.read_drive_status() -- the single function every
   quota-consuming call site (manager.dispatcher.dispatch(),
   manager.scheduler.schedule(), manager.command_watcher's admission-time
   read and its waiting_quota retry-sweep, and the Dashboard's own quota
   display) already calls -- so a simulated provider's windows are set to
   the exact values (remaining_percent=0.0, used_percent=100.0 on every
   window) manager.quota_reader.summarize()'s real, completely unmodified
   reliability/exhaustion computation already treats as genuine exhaustion.
   No parallel "test mode" branch exists anywhere in the dispatch/scheduler/
   watcher code; they cannot tell the difference, by design, other than by
   reading this module's own separate audit log.
6. Self-expiring: an override is only ever honored for at most
   MAX_OVERRIDE_AGE_SECONDS regardless of what `expires_at` the file itself
   claims, and clear_override() is the explicit, always-available way to
   remove one immediately. No test run of this mechanism is expected to
   need any manual cleanup beyond calling clear_override() once done, but
   even a forgotten override cannot outlive the hard cap.
7. No production backdoor: the override can only ever WIDEN which providers
   look unavailable (push windows toward more-exhausted), never grant a
   provider extra quota, bypass any other check, or select a specific
   provider/account itself -- real, unmodified scheduling logic still
   decides the actual outcome from there.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

OVERRIDE_TAG = "CONTROLLED_ACCEPTANCE_GATE"
# Hard upper bound on how old a written override is ever honored, regardless
# of the file's own `expires_at` -- see module docstring point 6. Kept short
# so a forgotten override self-heals fast rather than lingering.
MAX_OVERRIDE_AGE_SECONDS = 900

KNOWN_PROVIDERS = {"codex", "claude", "antigravity", "gemini_app"}


def _gate_dir(manager_home) -> Path:
    return Path(manager_home) / "acceptance-gate"


def _override_path(manager_home) -> Path:
    return _gate_dir(manager_home) / "override.json"


def _log_path(manager_home) -> Path:
    return _gate_dir(manager_home) / "applied-log.jsonl"


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_active_override(manager_home, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Return the active override dict, or None if no override should be
    applied right now. Never raises (see module docstring point 3) -- any
    problem reading, parsing, or validating the file is "no override"."""
    if not manager_home:
        return None
    now = now or datetime.now(timezone.utc)
    path = _override_path(manager_home)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("tag") != OVERRIDE_TAG:
        return None
    created_at = _parse_iso(data.get("created_at"))
    expires_at = _parse_iso(data.get("expires_at"))
    if created_at is None or expires_at is None:
        return None
    age_seconds = (now - created_at).total_seconds()
    if age_seconds < 0 or age_seconds > MAX_OVERRIDE_AGE_SECONDS or now > expires_at:
        return None
    providers = data.get("unavailable_providers")
    if (not isinstance(providers, list) or not providers
            or not all(isinstance(p, str) and p in KNOWN_PROVIDERS for p in providers)):
        return None
    return data


def _append_log(manager_home, event: Dict[str, Any]) -> None:
    try:
        path = _log_path(manager_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception:
        # Audit logging is best-effort: a logging failure must never itself
        # block or corrupt the real quota_document this wraps.
        pass


def apply_controlled_unavailability(document, manager_home, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return `document` completely unmodified unless a fresh, valid local
    override is active (see read_active_override), in which case return a
    deep-copied document with the named providers' quota windows forced to
    the same shape real exhaustion produces -- see module docstring point 5
    for why that specific shape, not some separate "unavailable" flag, is
    what gets set. Every application is appended to the local audit log
    (module docstring point 4) with the real providers affected and the
    override's own request_id, before the simulated document is returned."""
    override = read_active_override(manager_home, now=now)
    if override is None:
        return document
    targets = set(override["unavailable_providers"])
    simulated = copy.deepcopy(document)
    affected = []
    for item in simulated.get("providers", []):
        if item.get("provider") in targets:
            for window in item.get("windows", []) or []:
                window["remaining_percent"] = 0.0
                window["used_percent"] = 100.0
            affected.append({"provider": item.get("provider"), "account_id": item.get("account_id")})
    _append_log(manager_home, {
        "tag": OVERRIDE_TAG, "applied_at": (now or datetime.now(timezone.utc)).isoformat(),
        "request_id": override.get("request_id"), "unavailable_providers": sorted(targets),
        "affected_records": affected,
    })
    return simulated


def write_override(manager_home, unavailable_providers: Iterable[str], ttl_seconds: int = 300,
                   request_id: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The only way to create/refresh an override. Intended to be called
    directly and locally by whoever is running a controlled acceptance-gate
    test on this machine -- never by any code reachable from an external
    ingress (see module docstring point 2). Writes atomically (a temp file
    then an OS-level rename) so a reader can never observe a half-written
    file. `ttl_seconds` is clamped to MAX_OVERRIDE_AGE_SECONDS regardless of
    what is requested."""
    providers = sorted(set(unavailable_providers))
    if not providers or not all(p in KNOWN_PROVIDERS for p in providers):
        raise ValueError(f"unavailable_providers must be a non-empty subset of {sorted(KNOWN_PROVIDERS)}")
    now = now or datetime.now(timezone.utc)
    ttl_seconds = max(1, min(int(ttl_seconds), MAX_OVERRIDE_AGE_SECONDS))
    data = {
        "tag": OVERRIDE_TAG,
        "created_at": now.isoformat(),
        "expires_at": (now.timestamp() + ttl_seconds),
        "unavailable_providers": providers,
        "request_id": request_id,
    }
    # expires_at must be an ISO string like created_at, not the raw
    # timestamp float computed above -- fixed before ever touching disk.
    data["expires_at"] = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).isoformat()
    gate_dir = _gate_dir(manager_home)
    gate_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = _override_path(manager_home).with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.replace(_override_path(manager_home))
    return data


def clear_override(manager_home) -> bool:
    """Explicit, always-available immediate removal. Returns whether a file
    actually existed to remove. Idempotent: removing an already-absent
    override is not an error."""
    path = _override_path(manager_home)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
