"""The Layer-4 live smoke's controlled write does not depend on repo-write ADMISSION.

``repo_write_capable=False`` on the Antigravity registry entry (independent
review of f4cf5cb, P1-2) governs the dispatcher / Command Watcher admission of
ordinary repo-write Tasks. The live smoke (``python -m manager.ag_live_smoke``)
is the explicitly controlled write path: it builds its own disposable git
repository and drives ``AgRunner`` directly, never through ``dispatch()`` or
the capability registry. This test pins that separation hermetically -- the
runner is replaced by a fake that performs the file write the real IDE would
-- so that keeping the smoke green can never become an argument for widening
the registry again. (The real smoke consumes a model turn and is never part
of the suite.)
"""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import ag_live_smoke
from manager.ag_runner import LaunchOutcome, PreparedLaunch, RunningLaunch
from manager.assignment import CAPABILITIES


class _FakeRunner:
    """Stands in for AgRunner(): writes the smoke file exactly as the IDE would, then reports terminal truth."""

    def __init__(self):
        self.calls = []

    def prepare(self, request):
        self.calls.append("prepare")
        prepared = PreparedLaunch(thread_id="ag-live-fake", session_path=None, pid=1, process_creation_identity="x",
                                  prepared_at="2026-09-05T00:00:00Z", mode="live_ide", _target={}, _request=request)
        prepared._process = type("P", (), {"poll": lambda self: 0})()
        return prepared

    def start(self, prepared, prompt):
        self.calls.append("start")
        (Path(prepared._request.working_directory) / ag_live_smoke.SMOKE_FILE).write_text(ag_live_smoke.SMOKE_CONTENT + "\n", encoding="utf-8")
        return RunningLaunch(prepared=prepared, turn_id="turn-1", started_at="2026-09-05T00:00:01Z")

    def wait(self, running):
        self.calls.append("wait")
        return LaunchOutcome(status="completed", thread_id="ag-live-fake", turn_id="turn-1", completed_at="2026-09-05T00:00:02Z", response_text="DONE")

    def close(self, target):
        self.calls.append("close")


class LiveSmokePathTests(unittest.TestCase):
    def test_smoke_write_path_bypasses_the_capability_registry(self):
        self.assertFalse(CAPABILITIES["antigravity"]["repo_write_capable"])
        fake = _FakeRunner()
        with tempfile.TemporaryDirectory() as workspace, \
             patch.object(ag_live_smoke, "AgRunner", lambda: fake), \
             patch.object(ag_live_smoke, "read_run_state", lambda *_a, **_k: {"status": "completed"}):
            evidence = ag_live_smoke.run_live_smoke(model=None, timeout=5.0, workspace=Path(workspace))
        self.assertEqual(["prepare", "start", "wait", "close"], fake.calls)
        self.assertTrue(evidence["passed"], evidence)
        self.assertTrue(evidence["steps"]["independent_git_verification"]["passed"])
        # The smoke never consults the dispatcher/registry: it is a controlled
        # Layer-4 write, not a production repo-write admission.
        source = inspect.getsource(ag_live_smoke)
        for forbidden in ("manager.dispatcher", "manager.assignment", "CAPABILITIES", "repo_write_capable"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
