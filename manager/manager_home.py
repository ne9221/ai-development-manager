"""Single canonical resolver for the ADM manager home.

Before this module every durable-runtime call site resolved the home
itself, and the two spellings had drifted apart:

* the safe spelling -- ``os.environ.get("AI_MANAGER_HOME", Path.home() /
  ".ai-development-manager")`` -- used by quota, refresh, config locks and
  the dashboard; and
* the unsafe spelling -- ``os.environ.get("AI_MANAGER_HOME", ".")`` --
  used by ``phase1_cursor`` and by all four Scheduled-Task ``main()``
  entrypoints, whose default is *the current working directory*.

The unsafe spelling caused a real production outage on 2026-09-02: a
process running with cwd set to the activated production checkout and no
``AI_MANAGER_HOME`` in its environment wrote ``runtime/phase1-cursor.json``
into that checkout.  ``manager.provenance.is_checkout_clean()`` counts
untracked files, so the tree went dirty, ``verify_running()`` fail-closed
per AI-DEVELOPMENT-RULES rule 18, and every Scheduled Task reported
``LastTaskResult=1`` for ~54 minutes until a human cleaned it up.

The contract here is deliberately narrow and fails closed:

1. an explicit path (argument, then ``AI_MANAGER_HOME``) is authoritative;
2. otherwise the canonical user-level home ``~/.ai-development-manager``
   is used, but only when it can be derived safely;
3. otherwise :class:`ManagerHomeError` -- never cwd, never the repo root.

Independently of which branch produced it, a home that resolves *inside a
git work tree* is rejected.  That is the invariant the outage was about:
durable runtime state must never land in a checkout.  Rejecting is loud,
so contamination stays detectable rather than being hidden -- which is
also why the fix is this resolver and not a ``runtime/`` .gitignore entry.

Tests must inject an explicit temporary home.  They must not rely on the
canonical fallback: that would make a test write the *real* production
rotation state in ``~/.ai-development-manager/runtime/``.
"""

import os
from pathlib import Path

ENV_VAR = "AI_MANAGER_HOME"
CANONICAL_HOME_DIRNAME = ".ai-development-manager"


class ManagerHomeError(RuntimeError):
    """Raised when no safe manager home can be established (fail closed)."""


def _enclosing_git_worktree(path):
    """Return the git work tree containing ``path``, or None.

    A pure-path ancestor walk on purpose: no subprocess, so this is cheap
    enough to run on every durable-state write, and it cannot be defeated
    by an unavailable/hung ``git`` binary.  ``.git`` is a directory in a
    normal clone and a file in a worktree/submodule; both count.
    """
    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def canonical_manager_home(environ=None):
    """The user-level ``~/.ai-development-manager``, or None if unsafe.

    Returns None -- rather than guessing -- when the user profile cannot
    be determined, so the caller fails closed instead of writing durable
    state somewhere arbitrary.
    """
    environ = os.environ if environ is None else environ
    for key in ("USERPROFILE", "HOME"):
        value = environ.get(key)
        if value and str(value).strip():
            return Path(value).expanduser() / CANONICAL_HOME_DIRNAME
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return None
    if str(home) in ("", ".", os.sep):
        return None
    return home / CANONICAL_HOME_DIRNAME


def resolve_manager_home(explicit=None, *, environ=None):
    """Resolve the ADM manager home, or raise :class:`ManagerHomeError`.

    ``explicit`` wins over ``AI_MANAGER_HOME``, which wins over the
    canonical user-level home.  There is deliberately no cwd, repo-root
    or ``"."`` fallback -- see the module docstring.
    """
    environ = os.environ if environ is None else environ

    source = "explicit argument"
    home = explicit
    if home is None or not str(home).strip():
        env_value = environ.get(ENV_VAR)
        if env_value is not None and str(env_value).strip():
            home, source = env_value, f"{ENV_VAR}"
        else:
            home, source = canonical_manager_home(environ), "canonical user-level home"

    if home is None or not str(home).strip():
        raise ManagerHomeError(
            "MANAGER_HOME_UNRESOLVED: no explicit manager home was supplied, "
            f"{ENV_VAR} is unset, and the canonical user-level home could not "
            "be derived safely; refusing to fall back to the working directory"
        )

    home = Path(home).expanduser()
    worktree = _enclosing_git_worktree(home)
    if worktree is not None:
        raise ManagerHomeError(
            f"MANAGER_HOME_IN_CHECKOUT: manager home {home} (from {source}) is "
            f"inside the git work tree {worktree}; durable runtime state must "
            "never be written into a checkout"
        )
    return home
