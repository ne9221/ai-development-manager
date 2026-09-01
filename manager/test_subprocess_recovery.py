"""Gap 2 architecture proof: TRUE fresh-OS-process recovery, not a
same-process fresh-object simulation.

Every action in these tests runs as `python -m manager.subprocess_recovery_cli
<verb> ...` -- a genuinely separate OS process (fresh interpreter, fresh
module state) via subprocess.run(). No Python object, Store instance,
claim registry instance, in-memory proposal, or worker state is ever
shared between two invocations; the only channel is the durable JSON file
state on disk (manager.subprocess_recovery_support.FileStore/
FileClaimRegistry).

SUBPROCESS_DURABLE_PROOF=YES: every test here crosses a real OS process
boundary over durable file-backed state.
REAL_GCS_DRIVE_PROOF=NO: no real GCS/Drive credentials are available in
this sandboxed environment, so the durable backend is a file, not an
actual GCS object + Drive API round trip. The two are deliberately not
conflated -- see manager/subprocess_recovery_support.py's module
docstring.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(verb, tmpdir, *extra_args):
    # cmd_partial's signature is (stage, store, claim, project, task,
    # execution) -- the stage argument goes BEFORE the common positional
    # args, unlike every other verb, which takes exactly the common five.
    common_args = [os.path.join(tmpdir, "store.json"), os.path.join(tmpdir, "claim.json"), "p1", "t1", "exec-a"]
    args = [*extra_args, *common_args] if verb == "partial" else common_args
    result = subprocess.run(
        [sys.executable, "-m", "manager.subprocess_recovery_cli", verb, *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"subprocess `{verb}` failed (exit {result.returncode}):\n"
                             f"stdout={result.stdout}\nstderr={result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def _seed(tmpdir):
    return _run("seed", tmpdir)


def _partial(tmpdir, stage):
    return _run("partial", tmpdir, stage)


def _recover(tmpdir):
    return _run("recover", tmpdir)


def _verify(tmpdir):
    return _run("verify", tmpdir)


class SubprocessRecoveryCrashMatrixTests(unittest.TestCase):
    """Each test seeds durable state in one subprocess, crashes at a named
    point in a second subprocess, and resumes/verifies in one or two more
    -- each subprocess a real OS process with nothing shared but the file
    on disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="adm-subprocess-recovery-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_CP1_bind_persisted_fresh_process_resumes_materialization(self):
        """CP1: Execution terminal proposal persisted (the seeded starting
        state); process exits right after the terminal bind CAS lands;
        a fresh process resumes and completes Handoff/Task materialization."""
        _seed(self.tmpdir)
        crash = _partial(self.tmpdir, "bind")
        self.assertEqual("crashed_after_bind", crash["status"])
        mid = _verify(self.tmpdir)
        self.assertIsNotNone(mid["task_root_document"]["terminal"])
        self.assertEqual("partial", mid["execution_cleanup_evidence"]["persistence"])

        recovered = _recover(self.tmpdir)
        self.assertTrue(recovered["result"])
        final = _verify(self.tmpdir)
        self.assertEqual("complete", final["execution_cleanup_evidence"]["persistence"])
        self.assertEqual("completed", final["task_status"])
        self.assertEqual("verified", final["task_root_document"]["materialization"]["task"]["status"])
        self.assertEqual("verified", final["task_root_document"]["materialization"]["handoff"]["status"])

    def test_CP2_bind_durable_process_exits_before_handoff_fresh_process_materializes(self):
        """CP2: GCS terminal bind durable; process exits before Handoff is
        even attempted; a fresh process materializes both Handoff and
        Task from the same durable bind."""
        _seed(self.tmpdir)
        crash = _partial(self.tmpdir, "bind")
        self.assertEqual("crashed_after_bind", crash["status"])
        bind_before = _verify(self.tmpdir)["task_root_document"]["terminal"]

        recovered = _recover(self.tmpdir)
        self.assertTrue(recovered["result"])
        final = _verify(self.tmpdir)
        self.assertEqual(bind_before["proposal_hash"], final["task_root_document"]["terminal"]["proposal_hash"])
        self.assertGreaterEqual(len(final["handoff_ids"]), 1)

    def test_CP3_handoff_durable_process_exits_before_task_fresh_process_reuses_same_id(self):
        """CP3: fixed-ID Handoff durable; process exits before the Task
        projection write; a fresh process completes the Task write and
        reuses the SAME fixed Handoff ID -- never generates a second one.
        (This environment has no real Drive credentials, so
        task_drive_id_factory/handoff_drive_id_factory are None throughout
        -- the assertion that matters here is architectural: the bind's
        own handoff_drive_file_id field, whatever it is, never changes
        across the crash.)"""
        _seed(self.tmpdir)
        crash = _partial(self.tmpdir, "handoff")
        self.assertEqual("crashed_after_handoff", crash["status"])
        before = _verify(self.tmpdir)
        handoff_id_before = before["task_root_document"]["terminal"]["handoff_drive_file_id"]
        handoff_ids_before = set(before["handoff_ids"])

        recovered = _recover(self.tmpdir)
        self.assertTrue(recovered["result"])
        after = _verify(self.tmpdir)
        self.assertEqual(handoff_id_before, after["task_root_document"]["terminal"]["handoff_drive_file_id"])
        self.assertEqual(handoff_ids_before, set(after["handoff_ids"]))  # no second Handoff record created
        self.assertEqual("completed", after["task_status"])

    def test_CP4_task_durable_process_exits_before_cleanup_fresh_process_converges(self):
        """CP4: Task projection durable; process exits before the
        cleanup_evidence merge / claim release; a fresh process converges
        cleanup and releases runtime authority."""
        _seed(self.tmpdir)
        crash = _partial(self.tmpdir, "task")
        self.assertEqual("crashed_after_task", crash["status"])
        mid = _verify(self.tmpdir)
        self.assertEqual("completed", mid["task_status"])
        self.assertNotEqual("complete", (mid["execution_cleanup_evidence"] or {}).get("persistence"))

        recovered = _recover(self.tmpdir)
        self.assertTrue(recovered["result"])
        final = _verify(self.tmpdir)
        self.assertEqual("complete", final["execution_cleanup_evidence"]["persistence"])

    def test_CP5_cleanup_released_but_materialization_attention_retains_terminal_authority(self):
        """CP5: cleanup released but materialization stuck in attention
        (a permanent Drive failure on the Handoff view) -- a fresh process
        must still see the terminal authority intact and retryable-in-
        principle (the bind is untouched), never fabricating full
        completion and never losing the winner."""
        _seed(self.tmpdir)
        crash = _partial(self.tmpdir, "attention")
        self.assertEqual("crashed_with_materialization_attention", crash["status"])

        final = _verify(self.tmpdir)
        self.assertIsNotNone(final["task_root_document"]["terminal"], "terminal authority must survive a permanent materialization failure")
        self.assertEqual("attention", final["task_root_document"]["materialization"]["handoff"]["status"])
        # A fresh process reading this state must never claim persistence
        # is complete while materialization is stuck in attention.
        self.assertNotEqual("complete", (final["execution_cleanup_evidence"] or {}).get("persistence"))


if __name__ == "__main__":
    unittest.main()
