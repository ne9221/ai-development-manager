"""Session-wide test isolation for the ADM manager home.

Two failure modes this file exists to make impossible, both of which have
happened for real:

1. **Checkout contamination.**  ``manager.phase1_cursor`` and the four
   Scheduled-Task ``main()`` entrypoints used to default the manager home
   to ``"."``.  Running pytest with cwd set to the activated production
   checkout wrote ``runtime/phase1-cursor.json`` into it, the tree went
   dirty, and rule 18 fail-closed every Scheduled Task for ~54 minutes
   (2026-09-02).  ``manager.manager_home`` now fails closed instead of
   falling back to cwd.

2. **Canonical production state corruption.**  Failing closed alone is not
   enough for the test suite.  With no ``AI_MANAGER_HOME`` set, the
   resolver legitimately falls back to the real user-level
   ``~/.ai-development-manager``, so the 70-odd ``poll_once()`` call sites
   that do not pass an explicit ``cursor_path`` would read and advance the
   *live* production rotation cursor (generation 2342 across 13 projects).
   That is strictly worse than dirtying a checkout.

So the suite pins ``AI_MANAGER_HOME`` to a throwaway directory for the
whole session.  This is a floor, not a licence: a test that cares about
the home should still inject its own, and per-test ``patch.dict`` of
``AI_MANAGER_HOME`` keeps working and still wins.

An ambient ``AI_MANAGER_HOME`` is honoured (so a deliberate harness can
still point the suite somewhere) but is rejected if it lands inside a git
work tree -- otherwise this file would reintroduce exactly the
contamination it exists to prevent.
"""

import os
import tempfile
from pathlib import Path

_ENV_VAR = "AI_MANAGER_HOME"


def _enclosing_git_worktree(path):
    candidate = Path(path).expanduser().resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _install_isolated_manager_home():
    ambient = os.environ.get(_ENV_VAR)
    if ambient and ambient.strip():
        worktree = _enclosing_git_worktree(ambient)
        if worktree is not None:
            raise RuntimeError(
                f"refusing to run the suite with {_ENV_VAR}={ambient}: it is inside "
                f"the git work tree {worktree}, and durable runtime state written "
                "there would dirty the checkout (see AI-DEVELOPMENT-RULES rule 18)"
            )
        return ambient
    # Deliberately not tmp_path: this must be in place before collection,
    # long before any fixture runs. Left on disk for post-mortem; the OS
    # temp directory is the right owner of its lifetime.
    isolated = tempfile.mkdtemp(prefix="adm-test-manager-home-")
    os.environ[_ENV_VAR] = isolated
    return isolated


ISOLATED_MANAGER_HOME = _install_isolated_manager_home()
