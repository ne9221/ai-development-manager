"""Force every child process spawned by an unattended background worker to be
created without a visible console window, on Windows.

Root cause this closes: Command Watcher, Session Center Supervisor, and Quota
Refresh already launch their top-level PowerShell host with
``-WindowStyle Hidden``, but that only *hides* the console after Windows has
already created (and briefly shown) it -- and it does nothing at all for
grandchild processes. Live process-tree inspection on HOME showed
``manager.command_watcher`` shelling out through ``google.auth.default()``
(``manager/gcs_lock_registry.py``) to ``gcloud.cmd config get project`` via a
plain ``subprocess.check_output`` with no window-hiding flags, once per
scheduled poll -- a real, reproducible, per-minute console flash independent
of any of our own code paths.

Patching ``subprocess.Popen`` here, once, at process entry, closes that gap
and every future one like it (git, codex.cmd, npm, ...) without depending on
finding and fixing each individual call site, including inside third-party
libraries we do not control.
"""

import subprocess
import sys

_installed = False


def install_hidden_subprocess_guard():
    """Idempotently patch subprocess.Popen so it never creates a visible window.

    No-op on non-Windows platforms and on repeated calls within one process.
    """
    global _installed
    if _installed or sys.platform != "win32":
        return
    _installed = True

    create_no_window = 0x08000000  # subprocess.CREATE_NO_WINDOW
    original_init = subprocess.Popen.__init__

    def _hidden_init(self, *args, **kwargs):
        startupinfo = kwargs.get("startupinfo")
        if startupinfo is None:
            startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | create_no_window
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _hidden_init
