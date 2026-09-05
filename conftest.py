"""Session-wide test isolation for the ADM manager home.

Three failure modes this file exists to make impossible. The first two have
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
   *live* production rotation cursor (generation 2458 across 13 projects).
   That is strictly worse than dirtying a checkout.

3. **An ambient home that already points at production.**  Pinning only
   when the variable is unset would still let an inherited
   ``AI_MANAGER_HOME=~/.ai-development-manager`` drive the whole suite
   straight at live state.  That case is refused outright rather than
   silently overridden, because silently ignoring an explicit operator
   setting is its own surprise.

So the suite pins ``AI_MANAGER_HOME`` to a throwaway directory for the
whole session.  This is a floor, not a licence: a test that cares about
the home should still inject its own, and per-test ``patch.dict`` of
``AI_MANAGER_HOME`` keeps working and still wins.

An ambient ``AI_MANAGER_HOME`` is otherwise honoured (so a deliberate
harness can point the suite somewhere of its choosing) but is rejected if
it lands inside a git work tree -- otherwise this file would reintroduce
exactly the contamination it exists to prevent.

Every check here is a pure path comparison.  Nothing in this file reads,
writes, stats or creates anything under the production home.
"""

import os
import tempfile
from pathlib import Path

import pytest

_ENV_VAR = "AI_MANAGER_HOME"
CANONICAL_HOME_DIRNAME = ".ai-development-manager"


def _same_path(left, right):
    """Compare two paths without touching the filesystem.

    ``normcase`` is what makes this correct on Windows, where the
    production home differs from an inherited value only by case.
    """
    return (os.path.normcase(os.path.abspath(os.path.expanduser(str(left))))
            == os.path.normcase(os.path.abspath(os.path.expanduser(str(right)))))


def _enclosing_git_worktree(path):
    candidate = Path(path).expanduser().resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _canonical_production_home():
    """``~/.ai-development-manager`` as a string, or None if underivable."""
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key)
        if value and value.strip():
            return os.path.join(value, CANONICAL_HOME_DIRNAME)
    try:
        return os.path.join(str(Path.home()), CANONICAL_HOME_DIRNAME)
    except (OSError, RuntimeError):
        return None


def _install_isolated_manager_home():
    ambient = os.environ.get(_ENV_VAR)
    if ambient and ambient.strip():
        production = _canonical_production_home()
        if production is not None and _same_path(ambient, production):
            raise RuntimeError(
                f"refusing to run the suite with {_ENV_VAR}={ambient}: that is the "
                "live production manager home. The suite writes durable runtime "
                "state (the Phase-1 rotation cursor among others), so running it "
                "here would mutate real production state. Unset the variable to "
                "get an isolated temporary home, or point it somewhere disposable."
            )
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


# ---------------------------------------------------------------------------
# Live Antigravity fence (Layer 1/2 must never consume AG quota)
#
# The Antigravity adapter discovers the IDE's language server by enumerating
# live processes and talks to it over loopback. Once that bridge became real
# (2026-09-05) three ordinary unit tests that built an ``AgRunner`` without
# injecting a dead IDE bridge dispatched three REAL model turns into the
# user's IDE. The fence itself now lives in ``manager/ag_live_fence.py`` and
# is consulted by the real process/loopback entry points at call time, so it
# holds under ``python -m unittest``, a directly executed test module and a
# subprocess -- none of which load this file (independent review of f4cf5cb
# proved the pytest-only fence reached the live cascade host that way).
#
# This file adds the pytest-specific parts: the ``live_antigravity`` marker
# (Layer 3/4 smokes, never part of the default regression run) suspends the
# fence for exactly that test, and every unmarked test FAILS if it reached a
# fenced entry point -- a test that leans on ambient live discovery must not
# pass on a silent "IDE not running" fallback (that is how the last hole
# stayed hidden). Tests that deliberately probe the fence wrap the probe in
# ``ag_live_fence.expecting_refusal()``. The environment opt-in that other
# runners use is refused outright here, mirroring the manager-home refusal
# above: an inherited variable must never widen an ordinary suite run.
# ---------------------------------------------------------------------------

LIVE_ANTIGRAVITY_MARKER = "live_antigravity"


def _refuse_ambient_live_opt_in():
    from manager import ag_live_fence

    if ag_live_fence.live_opt_in():
        raise RuntimeError(
            f"refusing to run the suite with {ag_live_fence.LIVE_OPT_IN_ENV}=1: that opts every test into the "
            "live Antigravity IDE (real model turns, real quota). Unset it; a test that genuinely needs the "
            f"live IDE opts in individually with @pytest.mark.{LIVE_ANTIGRAVITY_MARKER}."
        )


_refuse_ambient_live_opt_in()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{LIVE_ANTIGRAVITY_MARKER}: opt a test into the real Antigravity IDE (consumes AG quota; never in ordinary regression)")


@pytest.fixture(autouse=True)
def _fence_live_antigravity(request):
    from manager import ag_live_fence

    if request.node.get_closest_marker(LIVE_ANTIGRAVITY_MARKER):
        with ag_live_fence.suspended():
            yield
        return
    assert ag_live_fence.is_armed(), "the live Antigravity fence must be armed for every unmarked test"
    before = len(ag_live_fence.attempts)
    yield
    reached = ag_live_fence.attempts[before:]
    if reached:
        entry_points = sorted({item["entry_point"] for item in reached})
        pytest.fail(
            f"test reached the live Antigravity entry point(s) {entry_points} and only the fence stopped it: "
            "inject a dead IDE bridge / fake discover or opener (or wrap a deliberate probe in "
            f"ag_live_fence.expecting_refusal(), or mark the test {LIVE_ANTIGRAVITY_MARKER}).\n"
            + reached[0]["stack"],
            pytrace=False,
        )
