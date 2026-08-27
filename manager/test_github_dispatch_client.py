import subprocess
import unittest
from unittest.mock import patch

from manager import github_dispatch_client
from manager.github_dispatch_client import GitHubApiClient, TOKEN_ENV
from manager.tasks import TaskError


class GitHubApiClientDefaultTokenResolutionTests(unittest.TestCase):
    """GitHubApiClient.default() must try, in order: an explicit token
    argument, then TOKEN_ENV, then this machine's own `git credential`
    helper -- never an unauthenticated call, never a value written to
    disk or logged by this resolution itself."""

    def test_explicit_token_argument_wins_over_everything(self):
        with patch.dict("os.environ", {TOKEN_ENV: "env-token"}), \
             patch("manager.github_dispatch_client._resolve_token_via_git_credential_manager",
                   return_value="cred-manager-token"):
            client = GitHubApiClient.default(token="explicit-token")
        self.assertEqual(client.token, "explicit-token")

    def test_env_var_wins_over_credential_manager_fallback(self):
        with patch.dict("os.environ", {TOKEN_ENV: "env-token"}), \
             patch("manager.github_dispatch_client._resolve_token_via_git_credential_manager",
                   return_value="cred-manager-token"):
            client = GitHubApiClient.default()
        self.assertEqual(client.token, "env-token")

    def test_falls_back_to_git_credential_manager_when_nothing_else_set(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("manager.github_dispatch_client._resolve_token_via_git_credential_manager",
                   return_value="resolved-from-git-credential-manager"):
            client = GitHubApiClient.default()
        self.assertEqual(client.token, "resolved-from-git-credential-manager")

    def test_no_credential_anywhere_raises_task_error_without_leaking_anything(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("manager.github_dispatch_client._resolve_token_via_git_credential_manager", return_value=None):
            with self.assertRaises(TaskError):
                GitHubApiClient.default()


class ResolveTokenViaGitCredentialManagerTests(unittest.TestCase):
    """The credential-helper fallback itself: must call the real `git
    credential fill` interface with the exact protocol/host ADM host
    push/fetch already authenticates against for this repo, parse only the
    password= line, and fail soft (None) on any unavailability -- never
    raise, never write anything to disk."""

    def test_parses_password_line_from_successful_fill(self):
        completed = subprocess.CompletedProcess(
            args=["git", "credential", "fill"], returncode=0,
            stdout="protocol=https\nhost=github.com\nusername=someuser\npassword=gho_abc123\n", stderr="")
        with patch("manager.github_dispatch_client.subprocess.run", return_value=completed) as mock_run:
            token = github_dispatch_client._resolve_token_via_git_credential_manager()
        self.assertEqual(token, "gho_abc123")
        called_args, called_kwargs = mock_run.call_args
        self.assertEqual(called_args[0], ["git", "credential", "fill"])
        self.assertEqual(called_kwargs["input"], "protocol=https\nhost=github.com\n\n")

    def test_nonzero_exit_returns_none(self):
        completed = subprocess.CompletedProcess(args=["git", "credential", "fill"], returncode=1,
                                                stdout="", stderr="fatal: could not read Username")
        with patch("manager.github_dispatch_client.subprocess.run", return_value=completed):
            self.assertIsNone(github_dispatch_client._resolve_token_via_git_credential_manager())

    def test_missing_password_line_returns_none(self):
        completed = subprocess.CompletedProcess(args=["git", "credential", "fill"], returncode=0,
                                                stdout="protocol=https\nhost=github.com\n", stderr="")
        with patch("manager.github_dispatch_client.subprocess.run", return_value=completed):
            self.assertIsNone(github_dispatch_client._resolve_token_via_git_credential_manager())

    def test_helper_unavailable_or_timeout_returns_none_not_raise(self):
        with patch("manager.github_dispatch_client.subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(github_dispatch_client._resolve_token_via_git_credential_manager())
        with patch("manager.github_dispatch_client.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            self.assertIsNone(github_dispatch_client._resolve_token_via_git_credential_manager())


if __name__ == "__main__":
    unittest.main()
