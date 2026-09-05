"""Fail-closed fence between test processes and the live Antigravity IDE.

The Antigravity adapter reaches the user's IDE through exactly four real-world
entry points: same-user process enumeration (``list_language_server_processes``),
port enumeration (``list_listening_ports``), the loopback Connect-RPC opener
(``_default_opener``) and the legacy ``tasklist`` probe
(``ag_ide_bridge._detect_live_processes``). Everything else is injectable and
tests inject it. Once the bridge became real (2026-09-05) a unit test that
built ``AgRunner(cli_runner=...)`` without a dead IDE bridge auto-discovered
the live IDE and dispatched real model turns. The root ``conftest.py`` fence
closed that for pytest only: ``python -m unittest``, a directly executed test
module or a subprocess test never load ``conftest.py`` and reached the real
cascade host again (independent review of f4cf5cb).

This module is the runner-independent layer. Each entry point above calls
``check()`` first. The fence is armed whenever a test framework (``unittest``
/ ``pytest``) is loaded in the process -- decided at call time, so import
order and the test runner are irrelevant -- unless the process explicitly
opts into live access:

* pytest: ``@pytest.mark.live_antigravity`` (``conftest.py`` suspends the
  fence for exactly that test);
* any other runner or script: ``ADM_ANTIGRAVITY_LIVE_OPT_IN=1`` in the
  environment. ``conftest.py`` refuses to run the suite with that variable
  set, so it can never silently widen an ordinary regression run.

Production processes (Command Watcher, refresh_status, the collectors, the
Layer-4 live smoke) load no test framework, so the fence is inert there;
``manager/test_ag_live_fence.py`` proves that from a fresh interpreter. A
fenced call fails exactly like an IDE that is not running
(``AgLsError("ide_not_running")``) so every caller degrades on its existing
path; the detail names the fence, and each refused attempt is recorded in
``attempts`` so a test that leaned on ambient discovery is reported loudly
instead of passing on a silent fallback.
"""

from __future__ import annotations

import os
import sys
import traceback
from contextlib import contextmanager

LIVE_OPT_IN_ENV = "ADM_ANTIGRAVITY_LIVE_OPT_IN"
TEST_FRAMEWORK_MODULES = ("unittest", "pytest", "_pytest")
FENCED_CLASSIFICATION = "ide_not_running"
FENCED_DETAIL = ("live Antigravity access is fenced off in a test process; inject a dead IDE bridge / fake "
                 "opener, or opt in explicitly (pytest: mark the test live_antigravity; other runners: "
                 f"{LIVE_OPT_IN_ENV}=1)")

# Refused attempts. ``attempts`` are unexpected (a test relied on ambient live
# discovery); ``expected_attempts`` were made inside ``expecting_refusal()``
# by a test that deliberately probes the fence.
attempts: list[dict] = []
expected_attempts: list[dict] = []
_suspend_depth = 0
_expect_depth = 0


def test_framework_loaded() -> bool:
    return any(name in sys.modules for name in TEST_FRAMEWORK_MODULES)


def live_opt_in() -> bool:
    return os.environ.get(LIVE_OPT_IN_ENV, "") == "1"


def is_armed() -> bool:
    return _suspend_depth == 0 and not live_opt_in() and test_framework_loaded()


def fenced(entry_point: str) -> bool:
    """True (and the attempt recorded) when ``entry_point`` must not reach the live IDE right now."""
    if not is_armed():
        return False
    record = {"entry_point": entry_point, "stack": "".join(traceback.format_stack(limit=10)[:-1])}
    (expected_attempts if _expect_depth > 0 else attempts).append(record)
    return True


def check(entry_point: str) -> None:
    """Raise ``AgLsError("ide_not_running")`` when ``entry_point`` is fenced."""
    if fenced(entry_point):
        from manager.ag_language_server import AgLsError  # lazy: ag_language_server imports this module
        raise AgLsError(FENCED_CLASSIFICATION, f"{entry_point}: {FENCED_DETAIL}")


@contextmanager
def suspended():
    """Allow live access inside the block (the pytest ``live_antigravity`` marker path)."""
    global _suspend_depth
    _suspend_depth += 1
    try:
        yield
    finally:
        _suspend_depth -= 1


@contextmanager
def expecting_refusal():
    """Attempts inside the block are deliberate fence probes, not defects."""
    global _expect_depth
    _expect_depth += 1
    try:
        yield
    finally:
        _expect_depth -= 1
