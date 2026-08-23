"""Combined contract tests for the HOME hands-off Drive ingress convergence
(integration/home-handsoff-ingress-final-20260823): merges the standalone
runner (6a7f0df), the Windows dedicated Scheduled Task trigger (6d62cea),
the final bounded/absolute-bucket-fairness ingress (fd489df), and the
Command Watcher embedded-ingress decoupling switch (0faee84).

Most of the 20 required combined contract items are already covered by the
pre-existing, carried-over test suites for each merged commit:
  manager/test_drive_dispatch_watcher.py   (runner: items 1, 2, 4, 9)
  manager/test_drive_dispatch_ingress.py   (bounded ingress: items 2, 9, 11,
                                             12, 13, 14, 15, 16)
  manager/test_command_watcher.py          (embedded-ingress switch: items
                                             7, 8, 10)
  manager/DriveDispatchIngress.Tests.ps1   (Windows trigger: items 5, 6)

This module adds the items that only became checkable once all four lanes
were actually merged together onto one branch, or that only exist because
of the two convergence hardenings applied on top of the merge:
  - item 3:  a single canonical GCS lock-bucket env var name used
             consistently by every merged component (no drift back to an
             older/invented name).
  - item 17: the runner's stderr on failure is deterministic and
             secret-safe (hardening A).
  - item 18: hardening A never swallows the nonzero failure exit code.
  - item 19: ingress alone (runner + bounded ingress module) never creates
             a provider Task/Execution as a side effect of merely polling.
"""

import ast
import inspect
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from manager import drive_dispatch_watcher
from manager.gcs_lock_registry import BUCKET_ENV
from manager.tasks import TaskError


class CanonicalLockBucketEnvVarTests(unittest.TestCase):
    """Item 3: ADM_LOCK_GCS_BUCKET (manager.gcs_lock_registry.BUCKET_ENV) is
    the one canonical GCS idempotency bucket env var name -- no merged
    component reintroduces an older/invented name
    (ADM_DRIVE_INGRESS_IDEMPOTENCY_BUCKET/_OBJECT, dropped by 6d62cea)."""

    def test_bucket_env_constant_is_adm_lock_gcs_bucket(self):
        self.assertEqual("ADM_LOCK_GCS_BUCKET", BUCKET_ENV)

    def test_runner_module_resolves_bucket_via_canonical_constant(self):
        source = inspect.getsource(drive_dispatch_watcher)
        self.assertIn("BUCKET_ENV", source)
        self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_BUCKET", source)
        self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_OBJECT", source)

    def test_windows_wrapper_scripts_use_canonical_env_var_only(self):
        manager = os.path.dirname(__file__)
        for name in ("run_drive_dispatch_ingress.ps1", "install_drive_dispatch_ingress.ps1"):
            path = os.path.join(manager, name)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("ADM_LOCK_GCS_BUCKET", text, name)
            self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_BUCKET", text, name)
            self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_OBJECT", text, name)

    def test_command_watcher_source_uses_canonical_env_var_only(self):
        from manager import command_watcher
        source = inspect.getsource(command_watcher)
        self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_BUCKET", source)
        self.assertNotIn("ADM_DRIVE_INGRESS_IDEMPOTENCY_OBJECT", source)


class SafeRunnerStderrTests(unittest.TestCase):
    """Items 17 & 18: hardening A. The runner's stderr on failure never
    contains the raw exception message, a credential/token value, an
    Authorization header value, or a secret-bearing absolute path -- and
    the nonzero-exit-code failure contract is preserved."""

    SECRET_TOKEN = "ya29.SUPER-SECRET-ACCESS-TOKEN-VALUE"
    SECRET_PATH = r"C:\Users\EE\.credentials\adm-service-account.json"
    AUTH_HEADER_VALUE = "Bearer ya29.SUPER-SECRET-ACCESS-TOKEN-VALUE"

    def _run_main_with_failure(self, exc):
        def boom():
            raise exc

        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1",
            "ADM_DRIVE_DISPATCH_INGRESS_OWNER": "owner@example.com",
            "ADM_LOCK_GCS_BUCKET": "adm-lock-bucket",
        }, clear=False), \
             patch("manager.drive_dispatch_watcher.build_service", boom), \
             patch("sys.stderr", stderr):
            exit_code = drive_dispatch_watcher.main(["--once"])
        return exit_code, stderr.getvalue()

    def _assert_no_secrets_leaked(self, stderr_text):
        self.assertNotIn(self.SECRET_TOKEN, stderr_text)
        self.assertNotIn(self.SECRET_PATH, stderr_text)
        self.assertNotIn(self.AUTH_HEADER_VALUE, stderr_text)
        self.assertNotIn("Authorization", stderr_text)
        self.assertNotIn(".credentials", stderr_text)

    def test_task_error_failure_stderr_is_deterministic_and_secret_safe(self):
        exc = TaskError(f"config invalid: token={self.SECRET_TOKEN} path={self.SECRET_PATH}")
        exit_code, stderr_text = self._run_main_with_failure(exc)
        self.assertEqual(1, exit_code)  # item 18
        self._assert_no_secrets_leaked(stderr_text)
        payload = json.loads(stderr_text.strip())
        self.assertEqual("error", payload["status"])
        self.assertEqual("TaskError", payload["error_kind"])
        self.assertNotIn(self.SECRET_TOKEN, payload["message"])
        self.assertNotIn(str(exc), stderr_text)

    def test_generic_exception_failure_stderr_is_deterministic_and_secret_safe(self):
        exc = RuntimeError(
            f"Drive API 401: {self.AUTH_HEADER_VALUE} response body path={self.SECRET_PATH}")
        exit_code, stderr_text = self._run_main_with_failure(exc)
        self.assertEqual(1, exit_code)  # item 18
        self._assert_no_secrets_leaked(stderr_text)
        payload = json.loads(stderr_text.strip())
        self.assertEqual("error", payload["status"])
        self.assertEqual("RuntimeError", payload["error_kind"])
        self.assertNotIn(str(exc), stderr_text)

    def test_key_error_failure_stderr_is_deterministic_and_secret_safe(self):
        # A third, differently-shaped exception type (dict-key style message)
        # to prove the safety property holds across exception categories,
        # not just the two explicitly-caught branches' happy paths.
        def boom():
            raise KeyError(self.SECRET_TOKEN)

        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "ADM_DRIVE_DISPATCH_INGRESS_FOLDER_ID": "folder-1",
            "ADM_DRIVE_DISPATCH_INGRESS_OWNER": "owner@example.com",
            "ADM_LOCK_GCS_BUCKET": "adm-lock-bucket",
        }, clear=False), \
             patch("manager.drive_dispatch_watcher.build_service", boom), \
             patch("sys.stderr", stderr):
            exit_code = drive_dispatch_watcher.main(["--once"])
        self.assertEqual(1, exit_code)
        stderr_text = stderr.getvalue()
        self._assert_no_secrets_leaked(stderr_text)
        payload = json.loads(stderr_text.strip())
        self.assertEqual("KeyError", payload["error_kind"])

    def test_missing_bucket_env_failure_still_returns_nonzero_and_is_safe(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"ADM_LOCK_GCS_BUCKET": ""}, clear=True), \
             patch("sys.stderr", stderr):
            exit_code = drive_dispatch_watcher.main(["--once"])
        self.assertEqual(1, exit_code)
        stderr_text = stderr.getvalue()
        payload = json.loads(stderr_text.strip())
        self.assertEqual("TaskError", payload["error_kind"])
        self.assertIsInstance(payload["message"], str)
        self.assertGreater(len(payload["message"]), 0)


class NoProviderExecutionSideEffectTests(unittest.TestCase):
    """Item 19: ingress (runner + bounded ingress module) creates Task and
    Command records representing an incoming request (via the existing
    cloud.dispatch_ingress.handle_dispatch() path) but never itself
    creates/launches a provider *execution* -- that remains solely Command
    Watcher's process_command()/poll_once() job."""

    FORBIDDEN_EXECUTION_NAMES = ("process_command", "launch_task", "ClaudeLauncher",
                                  "CodexLauncher", "AgRunner", "execution_runner")

    def test_runner_source_has_no_execution_launch_symbols(self):
        source = inspect.getsource(drive_dispatch_watcher)
        for name in self.FORBIDDEN_EXECUTION_NAMES:
            self.assertNotIn(name, source, f"drive_dispatch_watcher.py must never reference {name}")

    def test_bounded_ingress_module_has_no_execution_launch_symbols(self):
        from manager import drive_dispatch_ingress
        source = inspect.getsource(drive_dispatch_ingress)
        for name in self.FORBIDDEN_EXECUTION_NAMES:
            self.assertNotIn(name, source, f"drive_dispatch_ingress.py must never reference {name}")

    def test_runner_imports_exclude_command_watcher_module(self):
        source = inspect.getsource(drive_dispatch_watcher)
        tree = ast.parse(source)
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        self.assertNotIn("manager.command_watcher", imported_modules)


if __name__ == "__main__":
    unittest.main()
