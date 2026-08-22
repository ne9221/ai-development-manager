import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

from collectors.claude_oauth import (
    AuthStaleError,
    CollectorError,
    CredentialsUnavailableError,
    RateLimitedError,
    collect,
    fetch_usage,
    normalize,
    read_access_token,
)


def _write_credentials(directory, token="test-access-token-not-real"):
    creds_path = Path(directory) / ".credentials.json"
    creds_path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "test-refresh-token-not-real",
            "expiresAt": 0,
            "refreshTokenExpiresAt": 0,
            "scopes": [],
            "subscriptionType": "test",
            "rateLimitTier": "test",
        }
    }), encoding="utf-8")
    return creds_path


class _FakeHTTPResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ClaudeOauthCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp_a = tempfile.TemporaryDirectory()
        self.temp_b = tempfile.TemporaryDirectory()
        self.base_a = Path(self.temp_a.name)
        self.base_b = Path(self.temp_b.name)

    def tearDown(self):
        self.temp_a.cleanup()
        self.temp_b.cleanup()

    # 1. A/B credentials path isolation
    def test_1_credentials_path_isolation(self):
        _write_credentials(self.base_a, token="token-account-a")
        _write_credentials(self.base_b, token="token-account-b")
        token_a = read_access_token(str(self.base_a))
        token_b = read_access_token(str(self.base_b))
        self.assertEqual("token-account-a", token_a)
        self.assertEqual("token-account-b", token_b)
        self.assertNotEqual(token_a, token_b)

    # 2. A's token is never used for B's request
    def test_2_token_never_crosses_accounts(self):
        _write_credentials(self.base_a, token="token-account-a")
        _write_credentials(self.base_b, token="token-account-b")
        seen_tokens = []

        def opener(request, timeout=None):
            auth = request.get_header("Authorization")
            seen_tokens.append(auth)
            return _FakeHTTPResponse(json.dumps({
                "five_hour": {"utilization": 10, "resets_at": None},
            }))

        collect(str(self.base_a), "account-a", opener=opener)
        collect(str(self.base_b), "account-b", opener=opener)
        self.assertEqual(2, len(seen_tokens))
        self.assertIn("token-account-a", seen_tokens[0])
        self.assertIn("token-account-b", seen_tokens[1])
        self.assertNotEqual(seen_tokens[0], seen_tokens[1])

    # 3. 200 with five_hour + seven_day normalizes both windows
    def test_3_200_normalizes_both_windows(self):
        payload = {
            "five_hour": {"utilization": 42.0, "resets_at": "2026-08-22T06:00:00Z"},
            "seven_day": {"utilization": 23.0, "resets_at": "2026-08-26T20:00:00Z"},
        }
        provider = normalize(payload, account_id="account-a", captured_at="2026-08-22T05:00:00Z")
        names = [w["name"] for w in provider["windows"]]
        self.assertEqual(["five_hour", "seven_day"], names)
        self.assertEqual("claude_oauth_usage", provider["source"])
        self.assertEqual("official", provider["source_type"])
        self.assertEqual("account-a", provider["account_id"])

    # 4. utilization -> used/remaining percent correctness
    def test_4_utilization_to_used_remaining_percent(self):
        payload = {"five_hour": {"utilization": 84.0, "resets_at": None}}
        provider = normalize(payload, account_id="account-b")
        window = provider["windows"][0]
        self.assertEqual(84.0, window["used_percent"])
        self.assertEqual(16.0, window["remaining_percent"])

    # 5. reset timestamp preserved/normalized to UTC Z-form
    def test_5_reset_timestamp_normalized(self):
        payload = {"five_hour": {"utilization": 0.0, "resets_at": "2026-08-22T09:59:59.771254+00:00"}}
        provider = normalize(payload, account_id="account-a")
        self.assertEqual("2026-08-22T09:59:59Z", provider["windows"][0]["resets_at"])

    # 6. missing weekly window -> UNKNOWN (absent), never padded with 0
    def test_6_missing_weekly_is_unknown_not_zero(self):
        payload = {"five_hour": {"utilization": 10.0, "resets_at": None}}
        provider = normalize(payload, account_id="account-a")
        names = [w["name"] for w in provider["windows"]]
        self.assertNotIn("seven_day", names)
        self.assertIn("seven_day", provider["metadata"]["missing_windows"])

    # 7. HTTP 429 -> RateLimitedError, honors Retry-After, no retry loop
    def test_7_429_raises_rate_limited_with_retry_after(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                hdrs={"Retry-After": "30"}, fp=io.BytesIO(b""),
            )

        with self.assertRaises(RateLimitedError) as ctx:
            fetch_usage("some-token", opener=opener)
        self.assertEqual("30", ctx.exception.retry_after)

    # 8. HTTP 401 -> AuthStaleError (fail closed, not "unavailable" masquerading as data)
    def test_8_401_raises_auth_stale(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized",
                hdrs=None, fp=io.BytesIO(b""),
            )

        with self.assertRaises(AuthStaleError):
            fetch_usage("some-token", opener=opener)

    # 9. malformed JSON body -> CollectorError, not a crash/guessed value
    def test_9_malformed_json_raises_collector_error(self):
        def opener(request, timeout=None):
            return _FakeHTTPResponse("not json{{{")

        with self.assertRaises(CollectorError):
            fetch_usage("some-token", opener=opener)

    # 10. token never appears in any exception raised by this module
    def test_10_token_never_in_exception_text(self):
        secret_token = "sk-super-secret-token-must-never-leak"
        _write_credentials(self.base_a, token=secret_token)

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 500, "Internal Server Error",
                hdrs=None, fp=io.BytesIO(b""),
            )

        try:
            collect(str(self.base_a), "account-a", opener=opener)
            self.fail("expected CollectorError")
        except CollectorError as exc:
            self.assertNotIn(secret_token, str(exc))
            self.assertNotIn(secret_token, repr(exc))

    # 11. two-account refresh does exactly one HTTP request per account
    #     (collect() itself only ever issues one request per call; the
    #     "one request per refresh cycle" contract at the multi-account
    #     level is covered by manager.test_claude_quota_truth's refresh()
    #     integration tests)
    def test_11_collect_issues_exactly_one_request(self):
        _write_credentials(self.base_a, token="token-account-a")
        call_count = {"n": 0}

        def opener(request, timeout=None):
            call_count["n"] += 1
            return _FakeHTTPResponse(json.dumps({"five_hour": {"utilization": 5, "resets_at": None}}))

        collect(str(self.base_a), "account-a", opener=opener)
        self.assertEqual(1, call_count["n"])

    # Missing credentials file -> CredentialsUnavailableError, fails closed
    def test_missing_credentials_file_fails_closed(self):
        empty_dir = tempfile.TemporaryDirectory()
        try:
            with self.assertRaises(CredentialsUnavailableError):
                read_access_token(str(Path(empty_dir.name)))
        finally:
            empty_dir.cleanup()

    # Default config_dir (None) resolves to ~/.claude, not an exception
    def test_default_config_dir_resolution_does_not_crash(self):
        # We can't assert a real token here (depends on the machine), only
        # that passing None does not itself raise a TypeError/AttributeError
        # before reaching the "file not found" / real-file branch.
        try:
            read_access_token(None)
        except CredentialsUnavailableError:
            pass  # acceptable on a machine with no ~/.claude/.credentials.json


if __name__ == "__main__":
    unittest.main()
