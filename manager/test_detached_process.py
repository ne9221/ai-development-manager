import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from manager.detached_process import detached_creationflags, popen_detached


@unittest.skipUnless(os.name == "nt", "Windows job-object semantics")
class DetachedProcessTests(unittest.TestCase):
    def test_flags_include_breakaway_from_job(self):
        # The whole point of this module (live 20260902): a child spawned
        # from inside a Task Scheduler tick must leave the tick's job object
        # or it is terminated with the job. DETACHED_PROCESS alone does not
        # do that -- only CREATE_BREAKAWAY_FROM_JOB does.
        flags = detached_creationflags()
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(detached_creationflags(breakaway=False) & subprocess.CREATE_BREAKAWAY_FROM_JOB)

    def test_popen_detached_passes_breakaway_and_caller_kwargs(self):
        with patch("manager.detached_process.subprocess.Popen", return_value=Mock(pid=7)) as popen:
            process = popen_detached(["x.exe", "--flag"], cwd="C:\\w", stdin=subprocess.DEVNULL, close_fds=True)
        self.assertEqual(7, process.pid)
        popen.assert_called_once()
        self.assertEqual(["x.exe", "--flag"], popen.call_args.args[0])
        kwargs = popen.call_args.kwargs
        self.assertEqual("C:\\w", kwargs["cwd"])
        self.assertIs(subprocess.DEVNULL, kwargs["stdin"])
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB)

    def test_no_window_swaps_detached_for_create_no_window(self):
        """powershell.exe with DETACHED_PROCESS exits immediately (status 0,
        script never runs) because it gets no console at all -- live 20260902:
        cold start reported dashboard_start_timeout and no Streamlit process
        ever appeared, while the identical argv under CREATE_NO_WINDOW bound
        port 8501 in ~2s and stayed up. Console hosts therefore keep a real
        but hidden console; breakaway is unchanged."""
        flags = detached_creationflags(no_window=True)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        with patch("manager.detached_process.subprocess.Popen", return_value=Mock(pid=3)) as popen:
            popen_detached(["powershell.exe", "-File", "x.ps1"], no_window=True)
        spawned = popen.call_args.kwargs["creationflags"]
        self.assertTrue(spawned & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(spawned & subprocess.DETACHED_PROCESS)

    def test_popen_detached_retries_without_breakaway_only_on_oserror(self):
        # Exactly the fallback _spawn_claimed_worker proved in production: a
        # job that forbids breakaway rejects the flag with OSError, and a
        # live child without breakaway beats no child at all.
        attempts = []

        def popen(argv, **kwargs):
            attempts.append(kwargs["creationflags"])
            if len(attempts) == 1:
                raise OSError("job does not allow breakaway")
            return Mock(pid=9)

        with patch("manager.detached_process.subprocess.Popen", side_effect=popen):
            process = popen_detached(["x.exe"])
        self.assertEqual(9, process.pid)
        self.assertEqual(2, len(attempts))
        self.assertTrue(attempts[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(attempts[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(attempts[1] & subprocess.DETACHED_PROCESS)
        # Any other exception is not retried.
        with patch("manager.detached_process.subprocess.Popen", side_effect=ValueError("bad argv")):
            with self.assertRaises(ValueError):
                popen_detached(["x.exe"])


if __name__ == "__main__":
    unittest.main()
