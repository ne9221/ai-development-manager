import subprocess
import unittest
from unittest import mock

from manager import win_background_guard


class WinBackgroundGuardTest(unittest.TestCase):
    def setUp(self):
        win_background_guard._installed = False
        self._original_init = subprocess.Popen.__init__

    def tearDown(self):
        subprocess.Popen.__init__ = self._original_init
        win_background_guard._installed = False

    def test_noop_on_non_windows(self):
        with mock.patch.object(win_background_guard.sys, "platform", "linux"):
            win_background_guard.install_hidden_subprocess_guard()
        self.assertFalse(win_background_guard._installed)
        self.assertIs(subprocess.Popen.__init__, self._original_init)

    def test_patches_popen_with_hidden_startupinfo_and_creationflags_on_windows(self):
        captured = {}

        def fake_init(self, *args, **kwargs):
            captured["startupinfo"] = kwargs.get("startupinfo")
            captured["creationflags"] = kwargs.get("creationflags")

        subprocess.Popen.__init__ = fake_init
        with mock.patch.object(win_background_guard.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "STARTUPINFO", create=True) as fake_startupinfo_cls, \
                mock.patch.object(subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            fake_startupinfo_cls.return_value = mock.Mock(dwFlags=0)
            win_background_guard.install_hidden_subprocess_guard()
            subprocess.Popen.__init__(mock.Mock())

        self.assertTrue(win_background_guard._installed)
        self.assertEqual(captured["creationflags"], 0x08000000)
        self.assertIsNotNone(captured["startupinfo"])
        self.assertEqual(captured["startupinfo"].wShowWindow, 0)

    def test_second_install_call_is_idempotent_noop(self):
        with mock.patch.object(win_background_guard.sys, "platform", "win32"), \
                mock.patch.object(subprocess, "STARTUPINFO", create=True), \
                mock.patch.object(subprocess, "STARTF_USESHOWWINDOW", 1, create=True):
            win_background_guard.install_hidden_subprocess_guard()
            patched_init = subprocess.Popen.__init__
            win_background_guard.install_hidden_subprocess_guard()
            self.assertIs(subprocess.Popen.__init__, patched_init)


if __name__ == "__main__":
    unittest.main()
