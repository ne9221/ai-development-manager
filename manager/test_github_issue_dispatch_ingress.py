import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from manager.github_dispatch_client import GitHubApiError, GitHubNotFound
from manager.github_issue_dispatch_ingress import (
    ALLOWED_AUTHORS_ENV, parse_allowed_authors, poll_github_issue_dispatch_requests, read_request,
)
from manager.tasks import TaskError
from manager.test_task_claims import MemoryClaimRegistry

REPO = "ne9221/ai-development-manager"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
ALLOWED = frozenset({"ne9221"})


def request(request_id="gh-issue-1", **changes):
    value = {
        "request_id": request_id, "project_id": "ai-development-manager",
        "title": "Harmless GitHub issue ingress proof", "goal": "Return a short status report without changing files.",
        "preferred_provider": "codex", "priority": "normal", "created_at": "2026-08-27T11:59:00Z",
    }
    value.update(changes)
    return value


def _issue(number, issue_id, body, author="ne9221", state="open", is_pr=False):
    entry = {"id": issue_id, "number": number, "state": state, "body": body, "user": {"login": author}}
    if is_pr:
        entry["pull_request"] = {"url": "https://api.github.com/..."}
    return entry


class FakeGitHubIssueClient:
    """Fake manager.github_dispatch_client.GitHubApiClient -- never touches
    real GitHub. Holds one open-issues listing."""

    def __init__(self, issues=(), repo=REPO, repo_full_name=None):
        self.repo = repo
        self.repo_full_name = repo_full_name if repo_full_name is not None else repo
        self._issues = list(issues)
        self.list_issues_calls = []

    def get_repo(self, repo):
        if repo != self.repo:
            raise GitHubNotFound("github_not_found", "no such repo")
        return {"full_name": self.repo_full_name}

    def list_issues(self, repo, state="open", per_page=20):
        self.list_issues_calls.append((repo, state, per_page))
        if repo != self.repo:
            raise GitHubApiError("github_api_error", "wrong repo")
        return list(self._issues)


class ParseAllowedAuthorsTests(unittest.TestCase):
    def test_comma_separated_case_insensitive(self):
        self.assertEqual(frozenset({"ne9221", "someoneelse"}), parse_allowed_authors("ne9221, SomeoneElse"))

    def test_empty_or_none_yields_empty_frozenset(self):
        self.assertEqual(frozenset(), parse_allowed_authors(""))
        self.assertEqual(frozenset(), parse_allowed_authors(None))


class ReadRequestTests(unittest.TestCase):
    def test_valid_issue_body_is_accepted(self):
        import json
        issue = _issue(1, 1001, json.dumps(request()))
        document = read_request(issue, ALLOWED, now=NOW)
        self.assertEqual("gh-issue-1", document["request_id"])

    def test_json_fenced_body_is_accepted(self):
        import json
        body = "Here is my request:\n```json\n" + json.dumps(request()) + "\n```\nthanks"
        issue = _issue(1, 1001, body)
        document = read_request(issue, ALLOWED, now=NOW)
        self.assertEqual("gh-issue-1", document["request_id"])

    def test_disallowed_author_rejected(self):
        import json
        issue = _issue(1, 1001, json.dumps(request()), author="random-stranger")
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_missing_author_rejected(self):
        import json
        issue = {"id": 1001, "number": 1, "state": "open", "body": json.dumps(request()), "user": None}
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_pull_request_entry_rejected(self):
        import json
        issue = _issue(1, 1001, json.dumps(request()), is_pr=True)
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_non_json_body_rejected(self):
        issue = _issue(1, 1001, "just a plain-text issue, not a dispatch request")
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_empty_body_rejected(self):
        issue = _issue(1, 1001, None)
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_schema_invalid_document_rejected(self):
        import json
        bad = {k: v for k, v in request().items() if k != "request_id"}
        issue = _issue(1, 1001, json.dumps(bad))
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)

    def test_stale_request_rejected(self):
        import json
        issue = _issue(1, 1001, json.dumps(request(created_at="2026-08-01T00:00:00Z")))
        with self.assertRaises(TaskError):
            read_request(issue, ALLOWED, now=NOW)


class PollGithubIssueDispatchRequestsTests(unittest.TestCase):
    def test_requires_allowed_authors_configured(self):
        client = FakeGitHubIssueClient([])
        with self.assertRaises(TaskError):
            poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO, allowed_authors=frozenset(), now=NOW)

    def test_valid_issue_creates_exactly_one_task(self):
        import json
        client = FakeGitHubIssueClient([_issue(1, 1001, json.dumps(request()))])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-issue-1", "task_id": "dispatch-gh-issue-1",
                                     "command_id": "dispatch-gh-issue-1", "status": "queued"})
        with patch("manager.github_issue_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO,
                                                          allowed_authors=ALLOWED, now=NOW,
                                                          registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])
        self.assertEqual(1, handler.call_count)
        payload = handler.call_args.args[3]
        self.assertEqual("codex", payload["provider"])
        self.assertEqual({"read_only": True}, payload["constraints"])

    def test_issue_id_used_for_rejection_key_not_number(self):
        client = FakeGitHubIssueClient([_issue(7, 99999, "not json at all")])
        registry = MemoryClaimRegistry()
        result = poll_github_issue_dispatch_requests(
            object(), object(), "bucket", client, REPO, allowed_authors=ALLOWED, now=NOW,
            rejection_registry_factory=lambda _bucket, issue_id: registry)
        self.assertFalse(result[0]["accepted"])
        self.assertEqual("99999", result[0]["issue_id"])
        self.assertIsNotNone(registry.document)
        self.assertEqual("rejected", registry.document["status"])

    def test_pull_requests_in_listing_are_skipped_not_crashed_on(self):
        import json
        client = FakeGitHubIssueClient([
            _issue(1, 1001, "irrelevant", is_pr=True),
            _issue(2, 1002, json.dumps(request("gh-issue-2"))),
        ])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-issue-2", "task_id": "dispatch-gh-issue-2",
                                     "command_id": "dispatch-gh-issue-2", "status": "queued"})
        with patch("manager.github_issue_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO,
                                                          allowed_authors=ALLOWED, now=NOW,
                                                          registry_factory=lambda *_args: object())
        accepted = [item for item in result if item["accepted"]]
        rejected = [item for item in result if not item["accepted"]]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))

    def test_max_candidates_bound_enforced(self):
        import json
        issues = [_issue(index, 1000 + index, json.dumps(request(f"gh-issue-{index}"))) for index in range(20)]
        client = FakeGitHubIssueClient(issues)
        handler = Mock(side_effect=lambda store, svc, factory, payload, request_created_at=None: {
            "accepted": True, "request_id": payload["request_id"],
            "task_id": f'dispatch-{payload["request_id"]}', "command_id": f'dispatch-{payload["request_id"]}',
            "status": "queued",
        })
        with patch("manager.github_issue_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO,
                                                          allowed_authors=ALLOWED, now=NOW,
                                                          registry_factory=lambda *_args: object(), max_candidates=5)
        self.assertEqual(5, handler.call_count)
        self.assertEqual(5, len(result))

    def test_unknown_repo_fails_closed(self):
        client = FakeGitHubIssueClient([], repo="other/repo")
        with self.assertRaises(TaskError):
            poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO,
                                                allowed_authors=ALLOWED, now=NOW)

    def test_env_var_fallback_used_when_not_passed_explicitly(self):
        import json
        client = FakeGitHubIssueClient([_issue(1, 1001, json.dumps(request()))])
        handler = Mock(return_value={"accepted": True, "request_id": "gh-issue-1", "task_id": "dispatch-gh-issue-1",
                                     "command_id": "dispatch-gh-issue-1", "status": "queued"})
        with patch.dict("os.environ", {ALLOWED_AUTHORS_ENV: "ne9221"}), \
             patch("manager.github_issue_dispatch_ingress.handle_dispatch", handler):
            result = poll_github_issue_dispatch_requests(object(), object(), "bucket", client, REPO, now=NOW,
                                                          registry_factory=lambda *_args: object())
        self.assertTrue(result[0]["accepted"])


if __name__ == "__main__":
    unittest.main()
