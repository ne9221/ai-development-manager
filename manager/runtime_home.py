"""Canonical, cwd-independent resolution of the ADM runtime home.

LIVE INCIDENT (2026-09-02, ~54 minutes of full runtime unavailability):
`manager.phase1_cursor` resolved its durable cursor path with
`os.environ.get("AI_MANAGER_HOME", ".")`. Any process that reached that
code WITHOUT an explicit AI_MANAGER_HOME while its cwd happened to be the
production checkout therefore wrote a real, durable
`<production checkout>/runtime/phase1-cursor.json`.

That is a governance-visible contamination, not a cosmetic one: the
untracked `runtime/` made the production checkout dirty, so
`manager.provenance.is_checkout_clean()` correctly returned False and the
Rule 18 runtime guard correctly fail-closed EVERY component (Command
Watcher, Drive/GitHub Dispatch Ingress, Quota Refresh all returned
RESULT=1) until the stray artifact was removed. The guard behaved exactly
as designed; the defect was that a runtime process could silently write
durable state into the checkout at all.

The fix is deliberately NOT `.gitignore runtime/`: hiding the
contamination from git would blind the cleanliness guard and weaken Rule
18 drift detection. Instead the runtime home is resolved to a stable
location OUTSIDE any checkout, and an unresolvable home fails closed
rather than silently degrading to cwd.

Resolution order (never cwd, at any step):

1. An explicit `manager_home` argument -- the contract every production
   wrapper and every test already uses (`run_command_watcher.ps1` et al
   take a MANDATORY -ManagerHome and export AI_MANAGER_HOME from it).
2. A non-empty AI_MANAGER_HOME environment variable.
3. The canonical user-level home, `~/.ai-development-manager`. This is
   NOT a new rule invented here -- it is the convention this repo already
   encodes in `manager/quota_reader.py`, `manager/quota_history.py`,
   `manager/refresh_status.py`, `manager/claude_config_locks.py`,
   `dashboard.py` and `desktop/AdmCommon.ps1`, and it is the real
   production home (the user-level ".ai-development-manager" directory). This module
   centralizes that existing contract; it does not add a second one.
4. Otherwise raise RuntimeHomeError -- fail closed, write nothing.
"""

import os
from pathlib import Path

CANONICAL_HOME_DIRNAME = ".ai-development-manager"


class RuntimeHomeError(Exception):
    """Raised when no safe runtime home can be determined.

    Callers must let this propagate rather than falling back to a
    cwd-relative path: writing durable runtime state into whatever
    directory a process happens to be started from is the exact failure
    this module exists to make impossible.
    """


def _clean(value):
    """A set-but-empty/whitespace env var is treated as NOT set.

    `AI_MANAGER_HOME=` in a wrapper is a misconfiguration, not a request
    to write to the filesystem root or to cwd, so it degrades to the
    canonical home rather than to something surprising.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_ai_manager_home(manager_home=None, environ=None):
    """Return the runtime home as a Path, or raise RuntimeHomeError.

    `manager_home` is the explicit caller-supplied home (a wrapper's
    -ManagerHome, or a test's temp directory) and always wins. `environ`
    defaults to os.environ and exists so tests can drive resolution
    without mutating real process state.
    """
    explicit = _clean(manager_home)
    if explicit is not None:
        return Path(explicit)

    env = os.environ if environ is None else environ
    from_env = _clean(env.get("AI_MANAGER_HOME"))
    if from_env is not None:
        return Path(from_env)

    # Canonical user-level home. Path.home() raises RuntimeError when no
    # home directory is resolvable (service accounts, stripped
    # environments); that must fail closed, never degrade to cwd.
    try:
        home = Path.home()
    except (RuntimeError, OSError) as exc:
        raise RuntimeHomeError(
            "RUNTIME_HOME_UNRESOLVABLE: AI_MANAGER_HOME is not set and no user home "
            "directory could be resolved; refusing to fall back to the current working "
            "directory (see manager/runtime_home.py for the 2026-09-02 incident)"
        ) from exc

    if not str(home).strip():
        raise RuntimeHomeError(
            "RUNTIME_HOME_UNRESOLVABLE: resolved user home is empty; refusing to fall "
            "back to the current working directory"
        )
    return home / CANONICAL_HOME_DIRNAME
