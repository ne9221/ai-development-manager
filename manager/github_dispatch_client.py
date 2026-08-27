"""Minimal GitHub REST (Contents API) HTTP client for GitHub dispatch ingress.

Deliberately thin: this is a transport wrapper only, mirroring the shape
manager.gcs_lock_registry.GCSLockRegistry already uses for GCS (a small
class wrapping an injectable HTTP session, one method per REST call, no
retry/caching cleverness). manager.github_dispatch_ingress is the only
caller of this client's methods; every ADMISSION/validation decision lives
there, never here.

Real network calls only ever happen through GitHubApiClient.default(), which
requires an explicit token (see TOKEN_ENV) -- this module never falls back
to an unauthenticated call, and never logs or embeds the token anywhere
except the one outgoing Authorization header.
"""

import base64
import os

from manager.tasks import TaskError

API_ROOT = "https://api.github.com"
TOKEN_ENV = "ADM_GITHUB_DISPATCH_INGRESS_TOKEN"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 30


class GitHubApiError(TaskError):
    """A GitHub API call returned an unexpected/non-2xx response, or the
    response body itself was not shaped as expected. Always safe to surface
    in a rejection record's message -- never carries the raw Authorization
    header or token."""


class GitHubNotFound(GitHubApiError):
    """A definite 404 -- the repo, branch, or path does not exist (or the
    token cannot see it). Distinguished from other failures so callers can
    treat "directory does not exist yet" (no requests submitted yet, a
    legitimate quiescent state -- git has no empty directories) differently
    from a real misconfiguration/outage."""


class GitHubApiClient:
    """Thin wrapper around one authenticated `requests.Session`. Every
    method returns already-`.json()`-decoded response bodies (or raises) --
    callers never touch the underlying session or headers directly."""

    def __init__(self, token, session=None):
        if not isinstance(token, str) or not token.strip():
            raise TaskError(f"{TOKEN_ENV} is required")
        self.token = token
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    @classmethod
    def default(cls, token=None):
        return cls(token or os.environ.get(TOKEN_ENV))

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _get(self, url, params=None):
        try:
            response = self.session.get(url, params=params, headers=self._headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            raise GitHubApiError("github_transport_error", "GitHub API request failed") from exc
        if response.status_code == 404:
            raise GitHubNotFound("github_not_found", f"GitHub API 404: {url}")
        if response.status_code != 200:
            raise GitHubApiError("github_api_error", f"GitHub API request failed: HTTP {response.status_code}")
        try:
            return response.json()
        except Exception as exc:
            raise GitHubApiError("github_malformed_response", "GitHub API response was not valid JSON") from exc

    def get_repo(self, repo):
        """GET /repos/{owner}/{repo} -- used only to confirm the configured
        repo identity (full_name) actually resolves to what was configured,
        catching a typo or a renamed/redirected repo rather than trusting
        the caller-supplied string on its own say-so."""
        return self._get(f"{API_ROOT}/repos/{repo}")

    def get_branch(self, repo, branch):
        """GET /repos/{owner}/{repo}/branches/{branch} -- existence check
        only. Raises GitHubNotFound if the branch does not exist, which the
        caller treats as a real misconfiguration (fail closed), unlike a
        missing directory path within an existing branch."""
        return self._get(f"{API_ROOT}/repos/{repo}/branches/{branch}")

    def list_directory(self, repo, path, branch):
        """GET /repos/{owner}/{repo}/contents/{path}?ref={branch}. Returns a
        list of shallow entries (name, path, sha, size, type) -- never file
        content. Raises GitHubNotFound if the path does not exist on this
        branch (a legitimate "nothing submitted yet" state -- git has no
        empty directories, so a dispatch-requests directory that has never
        received a commit simply does not exist)."""
        result = self._get(f"{API_ROOT}/repos/{repo}/contents/{path}", params={"ref": branch})
        if not isinstance(result, list):
            raise GitHubApiError("github_malformed_response", f"{path!r} is not a directory on branch {branch!r}")
        return result

    def get_file(self, repo, path, branch):
        """GET /repos/{owner}/{repo}/contents/{path}?ref={branch} for one
        specific file path. Returns {"name", "path", "sha", "size",
        "content" (base64), "encoding"}."""
        result = self._get(f"{API_ROOT}/repos/{repo}/contents/{path}", params={"ref": branch})
        if not isinstance(result, dict) or result.get("type") != "file":
            raise GitHubApiError("github_malformed_response", f"{path!r} is not a file on branch {branch!r}")
        return result

    @staticmethod
    def decode_file_content(file_document):
        """Decode a get_file() response's base64 `content` into raw bytes.
        Fails closed (GitHubApiError) on any encoding other than base64
        (the only encoding the Contents API documents for this endpoint) or
        malformed base64, rather than guessing."""
        if file_document.get("encoding") != "base64":
            raise GitHubApiError("github_malformed_response", "GitHub file content encoding was not base64")
        raw = file_document.get("content")
        if not isinstance(raw, str):
            raise GitHubApiError("github_malformed_response", "GitHub file content was missing")
        try:
            return base64.b64decode(raw.replace("\n", ""), validate=True)
        except Exception as exc:
            raise GitHubApiError("github_malformed_response", "GitHub file content was not valid base64") from exc
