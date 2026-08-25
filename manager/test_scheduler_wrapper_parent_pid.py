"""Windows PowerShell regression coverage for scheduler wrapper PID capture."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


MANAGER_DIR = Path(__file__).resolve().parent
POWERSHELL = shutil.which("powershell")
WRAPPERS = (
    "run_command_watcher.ps1",
    "run_drive_dispatch_ingress.ps1",
    "run_refresh.ps1",
    "run_session_center_supervisor.ps1",
)
START = '$env:ADM_SCHEDULER_WRAPPER_PID = "$PID"'
END = '$env:ADM_SCHEDULER_WRAPPER_PARENT_PID = "$wrapperParentPid"'


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class SchedulerWrapperParentPidTests(unittest.TestCase):
    def test_native_cim_lookup_for_current_powershell_pid(self):
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
             '$ErrorActionPreference="Stop"; $p=Get-CimInstance Win32_Process -Filter "ProcessId=$PID"; if ($null -eq $p -or [int64]$p.ParentProcessId -le 0) { throw "missing positive parent PID" }; $p.ParentProcessId'],
            capture_output=True,
            text=True,
        )
        if result.returncode and "0x80041003" in result.stderr:
            self.skipTest("local WMI policy denies Win32_Process CIM lookup")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertGreater(int(result.stdout.strip()), 0)

    def run_fragment(self, wrapper_name):
        source = (MANAGER_DIR / wrapper_name).read_text(encoding="utf-8")
        fragment = source[source.index(START):source.index(END) + len(END)]
        self.assertNotIn(r'\"', fragment)
        script = f'''$fragment = [scriptblock]::Create(@'
{fragment}
'@)
function Get-CimInstance {{
    [CmdletBinding()]
    param([string]$ClassName, [string]$Filter)
    $script:filter = $Filter
    [pscustomobject]@{{ ParentProcessId = 4242 }}
}}
& $fragment
$real = [pscustomobject]@{{
    host_pid = "$PID"
    wrapper_pid = $env:ADM_SCHEDULER_WRAPPER_PID
    parent_pid = $env:ADM_SCHEDULER_WRAPPER_PARENT_PID
    filter = $script:filter
}}
Remove-Item Env:ADM_SCHEDULER_WRAPPER_PARENT_PID -ErrorAction SilentlyContinue
function Get-CimInstance {{ throw "forced malformed lookup" }}
& $fragment
$failed = [pscustomobject]@{{ parent_pid = $env:ADM_SCHEDULER_WRAPPER_PARENT_PID }}
@($real, $failed) | ConvertTo-Json -Compress
'''
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_parent_pid_capture_and_fail_closed_for_every_wrapper(self):
        for wrapper_name in WRAPPERS:
            with self.subTest(wrapper=wrapper_name):
                real, failed = self.run_fragment(wrapper_name)
                self.assertEqual(real["host_pid"], real["wrapper_pid"])
                self.assertEqual(f"ProcessId={real['host_pid']}", real["filter"])
                self.assertEqual("4242", real["parent_pid"])
                self.assertEqual("", failed["parent_pid"] or "")
