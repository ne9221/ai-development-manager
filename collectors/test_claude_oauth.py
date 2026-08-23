import inspect
import io
import json
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import collectors.claude_oauth as claude_oauth_module
from collectors.claude_oauth import (
    AuthRefreshNotPersistedError,
    AuthStaleError,
    CollectorError,
    CredentialsUnavailableError,
    RateLimitedError,
    collect,
    fetch_usage,
    normalize,
    read_access_token,
)


def _far_future_ms():
    return int((time.time() + 3600) * 1000)


def _far_past_ms():
    return int((time.time() - 3600) * 1000)


def _write_credentials(directory, token="test-access-token-not-real", expires_at=None):
    # Defaults to a far-future expiry so existing tests that don't care about
    # the expiry/refresh path exercise the fast (fresh-token) path, exactly
    # as they did before the refresh bridge existed.
    if expires_at is None:
        expires_at = _far_future_ms()
    creds_path = Path(directory) / ".credentials.json"
    creds_path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": token,
            "refreshToken": "test-refresh-token-not-real",
            "expiresAt": expires_at,
            "refreshTokenExpiresAt": 0,
            "scopes": [],
            "subscriptionType": "test",
            "rateLimitTier": "test",
        }
    }), encoding="utf-8")
    return creds_path


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


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


class ClaudeCliRefreshBridgeTests(unittest.TestCase):
    """Covers the stale-access-token recovery bridge: the Claude CLI is the
    sole credential authority, ADM never implements the OAuth refresh
    protocol itself, never writes credentials, and never reads/uses the
    refresh token directly. All CLI interaction is faked -- no real
    subprocess, network, or credential is ever touched."""

    def setUp(self):
        self.temp_a = tempfile.TemporaryDirectory()
        self.temp_b = tempfile.TemporaryDirectory()
        self.base_a = Path(self.temp_a.name)
        self.base_b = Path(self.temp_b.name)

    def tearDown(self):
        self.temp_a.cleanup()
        self.temp_b.cleanup()

    @staticmethod
    def _usage_opener(utilization=10):
        def opener(request, timeout=None):
            return _FakeHTTPResponse(json.dumps({"five_hour": {"utilization": utilization, "resets_at": None}}))
        return opener

    @staticmethod
    def _always_401_opener():
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""),
            )
        return opener

    @staticmethod
    def _401_then_success_opener():
        call_count = {"n": 0}

        def opener(request, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""),
                )
            return _FakeHTTPResponse(json.dumps({"five_hour": {"utilization": 5, "resets_at": None}}))
        return opener

    # 1. Fresh token -> usage fetch happens once, no CLI call made.
    def test_1_fresh_token_no_cli_call(self):
        _write_credentials(self.base_a, token="fresh-token")
        cli_calls = []

        def cli_run(argv, **kwargs):
            cli_calls.append((argv, kwargs))
            raise AssertionError("CLI must not be invoked for a fresh token")

        provider = collect(str(self.base_a), "account-a", opener=self._usage_opener(), cli_run=cli_run)
        self.assertEqual([], cli_calls)
        self.assertEqual(10.0, provider["windows"][0]["used_percent"])

    # 2. Explicitly expired token -> CLI preflight invoked -> fresh persisted
    #    token appears -> usage succeeds.
    def test_2_expired_token_recovers_via_cli(self):
        _write_credentials(self.base_a, token="stale-token", expires_at=_far_past_ms())
        cli_calls = []

        def cli_run(argv, **kwargs):
            cli_calls.append((argv, kwargs))
            # Simulate the real Claude CLI persisting a fresh token as a side
            # effect of the preflight -- ADM only re-reads the file, it never
            # writes it.
            _write_credentials(self.base_a, token="fresh-token-from-cli")
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        seen_tokens = []

        def opener(request, timeout=None):
            seen_tokens.append(request.get_header("Authorization"))
            return _FakeHTTPResponse(json.dumps({"five_hour": {"utilization": 5, "resets_at": None}}))

        provider = collect(str(self.base_a), "account-a", opener=opener, cli_run=cli_run)
        self.assertEqual(1, len(cli_calls))
        self.assertEqual(1, len(seen_tokens))
        self.assertIn("fresh-token-from-cli", seen_tokens[0])
        self.assertEqual("ok", provider["status"])

    # 3. Usage request returns 401 -> CLI preflight invoked -> fresh
    #    persisted token appears -> retry succeeds.
    def test_3_401_recovers_via_cli_retry(self):
        _write_credentials(self.base_a, token="stale-token")
        cli_calls = []

        def cli_run(argv, **kwargs):
            cli_calls.append((argv, kwargs))
            _write_credentials(self.base_a, token="fresh-token-from-cli")
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        seen_tokens = []
        call_count = {"n": 0}

        def opener(request, timeout=None):
            call_count["n"] += 1
            seen_tokens.append(request.get_header("Authorization"))
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""),
                )
            return _FakeHTTPResponse(json.dumps({"five_hour": {"utilization": 5, "resets_at": None}}))

        provider = collect(str(self.base_a), "account-a", opener=opener, cli_run=cli_run)
        self.assertEqual(1, len(cli_calls))
        self.assertEqual(2, call_count["n"])
        self.assertIn("stale-token", seen_tokens[0])
        self.assertIn("fresh-token-from-cli", seen_tokens[1])
        self.assertEqual("ok", provider["status"])

    # 4. CLI reports loggedIn=true but token unchanged after preflight ->
    #    AUTH_REFRESH_NOT_PERSISTED.
    def test_4_logged_in_but_unchanged_fails_not_persisted(self):
        _write_credentials(self.base_a, token="stale-token")

        def cli_run(argv, **kwargs):
            # Reports success but does not touch the credentials file at all.
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        with self.assertRaises(AuthRefreshNotPersistedError):
            collect(str(self.base_a), "account-a", opener=self._always_401_opener(), cli_run=cli_run)

    # 5. CLI reports loggedIn=false -> fails closed.
    def test_5_logged_out_fails_closed(self):
        _write_credentials(self.base_a, token="stale-token")

        def cli_run(argv, **kwargs):
            return _FakeCompletedProcess(1, json.dumps({"loggedIn": False}))

        with self.assertRaises(AuthRefreshNotPersistedError):
            collect(str(self.base_a), "account-a", opener=self._always_401_opener(), cli_run=cli_run)

    # 6. CLI subprocess result malformed / times out / unexpected nonzero
    #    exit -> fails closed (a distinct, non-looping CollectorError).
    def test_6a_cli_timeout_fails_closed(self):
        _write_credentials(self.base_a, token="stale-token")

        def cli_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        with self.assertRaises(CollectorError):
            collect(str(self.base_a), "account-a", opener=self._always_401_opener(), cli_run=cli_run)

    def test_6b_cli_malformed_output_fails_closed(self):
        _write_credentials(self.base_a, token="stale-token")

        def cli_run(argv, **kwargs):
            return _FakeCompletedProcess(0, "not json{{{")

        with self.assertRaises(CollectorError):
            collect(str(self.base_a), "account-a", opener=self._always_401_opener(), cli_run=cli_run)

    def test_6c_cli_unexpected_exit_code_fails_closed(self):
        _write_credentials(self.base_a, token="stale-token")

        def cli_run(argv, **kwargs):
            return _FakeCompletedProcess(2, json.dumps({"loggedIn": True}))

        with self.assertRaises(CollectorError):
            collect(str(self.base_a), "account-a", opener=self._always_401_opener(), cli_run=cli_run)

    # 7. account-a's real default (config_dir=None, i.e. the plain ~/.claude
    #    account with no CLAUDE_CONFIG_DIR override) invokes the CLI with
    #    env=None -- it inherits the ambient environment untouched, never an
    #    injected CLAUDE_CONFIG_DIR.
    def test_7_account_a_default_config_dir_leaves_env_untouched(self):
        seen_envs = []

        def cli_run(argv, **kwargs):
            seen_envs.append(kwargs.get("env"))
            return _FakeCompletedProcess(1, json.dumps({"loggedIn": False}))

        from collectors.claude_oauth import _refresh_access_token_via_cli
        with self.assertRaises(AuthRefreshNotPersistedError):
            _refresh_access_token_via_cli(None, {"accessToken": "x", "expiresAt": 0}, 10, cli_run, None)
        self.assertEqual([None], seen_envs)

    # 8. account-b (CLAUDE_CONFIG_DIR=C:\Users\EE\.claude-b in production)
    #    invokes the CLI with that exact config_dir injected.
    def test_8_account_b_uses_exact_config_dir_env(self):
        config_dir_b = str(self.base_b)
        _write_credentials(self.base_b, token="stale-token-b")
        seen_envs = []

        def cli_run(argv, **kwargs):
            seen_envs.append(kwargs.get("env"))
            _write_credentials(self.base_b, token="fresh-token-b")
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        collect(config_dir_b, "account-b", opener=self._401_then_success_opener(), cli_run=cli_run)
        self.assertEqual(1, len(seen_envs))
        self.assertEqual(config_dir_b, seen_envs[0]["CLAUDE_CONFIG_DIR"])

    # 9/10. account-a's collector run never reads account-b's credential
    #       file and vice versa, including during the CLI-refresh path.
    def test_9_10_accounts_never_cross_read_credentials(self):
        _write_credentials(self.base_a, token="token-account-a")
        _write_credentials(self.base_b, token="token-account-b")

        def cli_run(argv, **kwargs):
            self.fail("CLI must not be invoked when the token is fresh")

        provider_a = collect(str(self.base_a), "account-a", opener=self._usage_opener(), cli_run=cli_run)
        provider_b = collect(str(self.base_b), "account-b", opener=self._usage_opener(), cli_run=cli_run)
        self.assertEqual("account-a", provider_a["account_id"])
        self.assertEqual("account-b", provider_b["account_id"])

        # Now force each through the 401 -> CLI recovery path and confirm the
        # env/credential file touched always matches the account under test.
        touched_paths = []

        def make_cli_run(expected_config_dir, new_token):
            def cli_run_inner(argv, **kwargs):
                env = kwargs.get("env")
                if expected_config_dir is None:
                    self.assertIsNone(env)
                else:
                    self.assertEqual(expected_config_dir, env["CLAUDE_CONFIG_DIR"])
                    touched_paths.append(env["CLAUDE_CONFIG_DIR"])
                _write_credentials(Path(expected_config_dir), token=new_token)
                return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))
            return cli_run_inner

        collect(str(self.base_a), "account-a", opener=self._401_then_success_opener(),
                cli_run=make_cli_run(str(self.base_a), "fresh-a"))
        collect(str(self.base_b), "account-b", opener=self._401_then_success_opener(),
                cli_run=make_cli_run(str(self.base_b), "fresh-b"))
        self.assertEqual([str(self.base_a), str(self.base_b)], touched_paths)
        # account-a's file still carries account-a's token, never account-b's.
        self.assertEqual("fresh-a", read_access_token(str(self.base_a)))
        self.assertEqual("fresh-b", read_access_token(str(self.base_b)))

    # 11. 429/backoff state -> CLI preflight NOT invoked (rate limiting is
    #     never treated as an auth problem).
    def test_11_429_never_triggers_cli_preflight(self):
        _write_credentials(self.base_a, token="fresh-token")
        cli_calls = []

        def cli_run(argv, **kwargs):
            cli_calls.append(argv)
            raise AssertionError("429 must never trigger the CLI preflight")

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                hdrs={"Retry-After": "30"}, fp=io.BytesIO(b""),
            )

        with self.assertRaises(RateLimitedError) as ctx:
            collect(str(self.base_a), "account-a", opener=opener, cli_run=cli_run)
        self.assertEqual("30", ctx.exception.retry_after)
        self.assertEqual([], cli_calls)

    # 12. Retry-After/backoff timing/semantics are unchanged: RateLimitedError
    #     still carries the exact Retry-After header value, independent of
    #     the CLI bridge entirely.
    def test_12_retry_after_value_preserved(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                hdrs={"Retry-After": "77"}, fp=io.BytesIO(b""),
            )

        with self.assertRaises(RateLimitedError) as ctx:
            fetch_usage("some-token", opener=opener)
        self.assertEqual("77", ctx.exception.retry_after)

    # 13. Maximum one CLI preflight per account per collect() call, even on
    #     the expired-token pre-check path (never re-checked/repeated).
    def test_13_max_one_cli_preflight_per_call(self):
        _write_credentials(self.base_a, token="stale-token", expires_at=_far_past_ms())
        call_count = {"n": 0}

        def cli_run(argv, **kwargs):
            call_count["n"] += 1
            _write_credentials(self.base_a, token="fresh-token", expires_at=_far_future_ms())
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        collect(str(self.base_a), "account-a", opener=self._usage_opener(), cli_run=cli_run)
        self.assertEqual(1, call_count["n"])

    # 14. Maximum one usage retry after a successful refresh -- if the retry
    #     itself still comes back 401, this fails closed instead of looping
    #     or invoking the CLI a second time.
    def test_14_no_retry_loop_on_persistent_401(self):
        _write_credentials(self.base_a, token="stale-token")
        cli_calls = {"n": 0}

        def cli_run(argv, **kwargs):
            cli_calls["n"] += 1
            _write_credentials(self.base_a, token="fresh-token-still-rejected")
            return _FakeCompletedProcess(0, json.dumps({"loggedIn": True}))

        call_count = {"n": 0}

        def opener(request, timeout=None):
            call_count["n"] += 1
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""),
            )

        with self.assertRaises(AuthStaleError):
            collect(str(self.base_a), "account-a", opener=opener, cli_run=cli_run)
        self.assertEqual(1, cli_calls["n"])
        self.assertEqual(2, call_count["n"])

    # 15. Static/source-scan: collector source contains no direct
    #     credential-file WRITE API usage.
    def test_15_source_has_no_credential_write_api(self):
        source = inspect.getsource(claude_oauth_module)
        for forbidden in ("write_text(", "json.dump(", "open(", ".write(", "os.replace(", "os.rename("):
            self.assertNotIn(forbidden, source,
                              f"collector source must never write credentials (found {forbidden!r})")

    # 16. Static/source-scan: collector source never reads/uses the
    #     refreshToken field directly.
    def test_16_source_never_uses_refresh_token_field(self):
        source = inspect.getsource(claude_oauth_module)
        self.assertNotIn("refreshToken", source,
                          "collector must never read/use the refreshToken field directly")

    # 17. Tokens never appear in any exception message the collector
    #     produces, including on the CLI-refresh fail-closed paths.
    def test_17_no_token_leak_in_refresh_path_exceptions(self):
        secret_stale = "sk-stale-secret-must-never-leak"
        secret_fresh_attempt = "sk-fresh-secret-must-never-leak"
        _write_credentials(self.base_a, token=secret_stale)

        def cli_run(argv, **kwargs):
            return _FakeCompletedProcess(1, json.dumps({"loggedIn": False}))

        def opener(request, timeout=None):
            auth = request.get_header("Authorization") or ""
            self.assertNotIn(secret_fresh_attempt, auth)
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b""),
            )

        try:
            collect(str(self.base_a), "account-a", opener=opener, cli_run=cli_run)
            self.fail("expected AuthRefreshNotPersistedError")
        except CollectorError as exc:
            self.assertNotIn(secret_stale, str(exc))
            self.assertNotIn(secret_stale, repr(exc))


if __name__ == "__main__":
    unittest.main()
