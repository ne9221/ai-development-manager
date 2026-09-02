"""One shared launcher for ADM's long-lived detached child processes.

Every process ADM must keep alive beyond the one-minute Scheduled Task tick
that spawned it -- the claimed-command worker, the AUTO_OPEN_ADM helper, and
the Dashboard launcher -- needs the SAME Windows creation-flag contract:

- no console window and no Ctrl-event coupling to the parent. Which flag
  achieves that depends on the child: a python child is happy with
  DETACHED_PROCESS (no console at all), but a console host like
  powershell.exe spawned with DETACHED_PROCESS exits immediately with
  status 0 without running its script -- live-reproduced 20260902: the
  Dashboard launcher never started and port 8501 never bound, while the
  identical argv under CREATE_NO_WINDOW bound the port in ~2s and stayed
  up. Such children pass no_window=True, which swaps DETACHED_PROCESS for
  CREATE_NO_WINDOW (a real but hidden console -- exactly what
  desktop/AdmCommon.ps1's own ProcessStartInfo.CreateNoWindow launcher
  has always used successfully);
- CREATE_BREAKAWAY_FROM_JOB so it leaves the Task Scheduler job object the
  tick runs inside. Without breakaway the child is terminated together
  with the job the moment the tick's job ends -- confirmed live 20260902:
  the Dashboard launched from inside the job never bound port 8501 and its
  process vanished, while the identical launcher started outside the job
  bound the port in under 17 seconds and stayed up.

The OSError fallback (retry without breakaway) is the exact behavior
manager.command_watcher._spawn_claimed_worker proved in production: a job
that forbids breakaway rejects the flag with OSError, and a live child
without breakaway is still better than no child at all. This module exists
so the three call sites cannot drift apart again.
"""

import os
import subprocess


def detached_creationflags(breakaway=True, no_window=False):
    """Windows creationflags for a long-lived detached child; 0 off Windows.

    ``no_window=True`` uses CREATE_NO_WINDOW instead of DETACHED_PROCESS
    for children that need a real (but hidden) console -- see this
    module's docstring for the powershell.exe evidence.
    """
    if os.name != "nt":
        return 0
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0) if no_window else getattr(subprocess, "DETACHED_PROCESS", 0)
    flags = base | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if breakaway:
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    return flags


def popen_detached(argv, no_window=False, **kwargs):
    """subprocess.Popen(argv, **kwargs) with the shared detached contract.

    Callers pass their own stdin/stdout/stderr/cwd/close_fds; this sets
    only ``creationflags`` (on Windows) and applies the proven
    breakaway-then-fallback sequence. ``no_window=True`` is for console
    hosts (powershell.exe) that must keep a hidden console instead of
    being fully detached. Any other exception propagates.
    """
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    flags = detached_creationflags(breakaway=True, no_window=no_window)
    if os.name == "nt":
        kwargs["creationflags"] = flags
    try:
        return subprocess.Popen(argv, **kwargs)
    except OSError:
        if os.name != "nt" or not (flags & breakaway):
            raise
        kwargs["creationflags"] = flags & ~breakaway
        return subprocess.Popen(argv, **kwargs)
