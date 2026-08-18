import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collectors.publish_drive import PublisherError, credentials_with_source
from manager import drive_auth
from manager.drive_auth import status


VALID_INSTALLED_CONFIG = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-client-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


class RefreshError(Exception): pass
class DefaultCredentialsError(Exception): pass


class FakeCredential:
    def __init__(self, valid=True, expired=False, refresh_token="refresh", refresh_error=None):
        self.valid, self.expired, self.refresh_token, self.refresh_error = valid, expired, refresh_token, refresh_error
    def refresh(self, _request):
        if self.refresh_error: raise self.refresh_error
        self.valid, self.expired = True, False
    def to_json(self): return '{"token":"replacement"}'


class LeakyCredential(FakeCredential):
    """Carries obviously-secret-looking values so leak-detection tests have something to catch."""
    def __init__(self):
        super().__init__()
        self.token = "ACCESS-TOKEN-SECRET-VALUE"
        self.refresh_token = "REFRESH-TOKEN-SECRET-VALUE"


def oauth(credential=None, default_error=True, flow_credential=None, run_local_server_error=None, record=None):
    class Credentials:
        @staticmethod
        def from_authorized_user_file(_path, _scopes): return credential
    class GoogleAuth:
        @staticmethod
        def default(scopes):
            if default_error: raise DefaultCredentialsError()
            return FakeCredential(), "default"
    def _runner():
        def run_local_server(_self, port):
            if run_local_server_error: raise run_local_server_error
            return flow_credential
        return type("Runner", (), {"run_local_server": run_local_server})()
    class Flow:
        @staticmethod
        def from_client_secrets_file(path, _scopes):
            if record is not None: record.append(("from_client_secrets_file", path))
            return _runner()
        @staticmethod
        def from_client_config(config, _scopes):
            if record is not None: record.append(("from_client_config", config))
            return _runner()
    return {"Credentials": Credentials, "google_auth": GoogleAuth, "Request": object,
            "InstalledAppFlow": Flow, "DefaultCredentialsError": DefaultCredentialsError, "RefreshError": RefreshError}


class DriveAuthTests(unittest.TestCase):
    def token(self, directory, text="{}"):
        path = Path(directory) / "token.json"; path.write_text(text, encoding="utf-8"); return path

    def secrets_file(self, directory, config, name="client_secret.json"):
        path = Path(directory) / name
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    # -- existing-token / refresh behavior (must keep working) --------------

    def test_valid_token_and_successful_refresh(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory))}, clear=False):
            creds, source = credentials_with_source(oauth=oauth(FakeCredential()))
            self.assertEqual("existing_token", source); self.assertTrue(creds.valid)
            creds, source = credentials_with_source(oauth=oauth(FakeCredential(valid=False, expired=True)))
            self.assertEqual("refreshed_token", source); self.assertTrue(creds.valid)

    def test_invalid_refresh_requires_controlled_reauthorization(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory)), "GOOGLE_OAUTH_CLIENT_SECRETS": ""}, clear=False):
            with self.assertRaisesRegex(PublisherError, "invalid_refresh_token"):
                credentials_with_source(oauth=oauth(FakeCredential(valid=False, expired=True, refresh_error=RefreshError())))

    # -- persist_refreshed_token contract (Cloud Run read-only mount vs desktop) --

    def test_persist_refreshed_token_true_writes_back_by_default(self):
        # Desktop / normal callers (e.g. the Command Watcher) don't pass
        # persist_refreshed_token at all -- the default (True) must keep
        # writing the refreshed token back to disk, exactly as before this
        # parameter existed.
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory, "old"))}, clear=False):
            creds, source = credentials_with_source(oauth=oauth(FakeCredential(valid=False, expired=True)))
            self.assertEqual("refreshed_token", source)
            self.assertTrue(creds.valid)
            self.assertNotEqual("old", Path(os.environ["GOOGLE_DRIVE_TOKEN"]).read_text(encoding="utf-8"))

    def test_persist_refreshed_token_false_skips_write_back(self):
        # A Cloud Run caller with a read-only Secret Manager token mount must
        # pass persist_refreshed_token=False: the refresh still succeeds and
        # returns valid, usable credentials, but the on-disk token is left
        # untouched (a write there would OSError on a read-only mount).
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory, "old"))}, clear=False):
            creds, source = credentials_with_source(
                oauth=oauth(FakeCredential(valid=False, expired=True)), persist_refreshed_token=False)
            self.assertEqual("refreshed_token", source)
            self.assertTrue(creds.valid)
            self.assertEqual("old", Path(os.environ["GOOGLE_DRIVE_TOKEN"]).read_text(encoding="utf-8"))

    def test_persist_refreshed_token_false_survives_readonly_directory(self):
        # Direct reproduction of the P0: even when the token's parent
        # directory genuinely cannot be written to (simulating the
        # Secret Manager mount), persist_refreshed_token=False must not
        # attempt the write and so must not raise OSError.
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            with patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(token)}, clear=False), \
                 patch("collectors.publish_drive._write_token", side_effect=OSError(30, "Read-only file system")) as write_token:
                creds, source = credentials_with_source(
                    oauth=oauth(FakeCredential(valid=False, expired=True)), persist_refreshed_token=False)
            write_token.assert_not_called()
            self.assertEqual("refreshed_token", source)
            self.assertTrue(creds.valid)

    # -- fallback precedence: env override vs bundled default ---------------

    def test_env_override_uses_client_secrets_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            secrets = self.secrets_file(directory, VALID_INSTALLED_CONFIG)
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": str(secrets)}
            record = []
            with patch.dict(os.environ, env, clear=False):
                creds, source = credentials_with_source(allow_interactive=True, oauth=oauth(flow_credential=FakeCredential(), record=record))
            self.assertEqual("desktop_oauth", source)
            self.assertEqual(record, [("from_client_secrets_file", str(secrets))])

    def test_bundled_default_config_used_when_no_env_override(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": ""}
            record = []
            with patch.dict(os.environ, env, clear=False), \
                 patch("manager.default_oauth_config.load_default_oauth_config", return_value=VALID_INSTALLED_CONFIG):
                creds, source = credentials_with_source(allow_interactive=True, oauth=oauth(flow_credential=FakeCredential(), record=record))
            self.assertEqual("desktop_oauth", source)
            self.assertEqual(record, [("from_client_config", VALID_INSTALLED_CONFIG)])

    def test_unprovisioned_bundled_default_fails_closed_with_precise_message(self):
        # Exercises the real, shipped manager/default_oauth_config.py (no patching):
        # the repository does not contain a real ADM OAuth client, so this must fail closed.
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory)), "GOOGLE_OAUTH_CLIENT_SECRETS": ""}, clear=False
        ):
            with self.assertRaisesRegex(PublisherError, r"^ADM Desktop OAuth client configuration not provisioned$"):
                credentials_with_source(allow_interactive=True, oauth=oauth())

    def test_malformed_bundled_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory)), "GOOGLE_OAUTH_CLIENT_SECRETS": ""}, clear=False
        ):
            with patch("manager.default_oauth_config.load_default_oauth_config", return_value={"installed": {"client_id": "id"}}):
                with self.assertRaisesRegex(PublisherError, "malformed"):
                    credentials_with_source(allow_interactive=True, oauth=oauth())

    def test_wrong_client_type_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            secrets = self.secrets_file(directory, {"web": VALID_INSTALLED_CONFIG["installed"]})
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": str(secrets)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(PublisherError, "Web application client"):
                    credentials_with_source(allow_interactive=True, oauth=oauth())
            self.assertEqual("old", token.read_text(encoding="utf-8"))

    def test_empty_env_secrets_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            secrets = self.secrets_file(directory, {})
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": str(secrets)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(PublisherError, "malformed or empty"):
                    credentials_with_source(allow_interactive=True, oauth=oauth())

    # -- authorization outcomes: token integrity -----------------------------

    def test_denied_authorization_leaves_existing_token_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            secrets = self.secrets_file(directory, VALID_INSTALLED_CONFIG)
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": str(secrets)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(PublisherError, "Google Desktop OAuth authorization failed"):
                    credentials_with_source(allow_interactive=True, oauth=oauth(run_local_server_error=RuntimeError("access_denied")))
            self.assertEqual("old", token.read_text(encoding="utf-8"))

    def test_desktop_authorization_saves_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            secrets = self.secrets_file(directory, VALID_INSTALLED_CONFIG)
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": str(secrets)}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(PublisherError, "did not return valid"):
                    credentials_with_source(allow_interactive=True, oauth=oauth(flow_credential=FakeCredential(valid=False)))
                self.assertEqual("old", token.read_text(encoding="utf-8"))
                credentials_with_source(allow_interactive=True, oauth=oauth(flow_credential=FakeCredential()))
                self.assertNotEqual("old", token.read_text(encoding="utf-8"))

    def test_successful_authorization_writes_token_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            token = self.token(directory, "old")
            env = {"GOOGLE_DRIVE_TOKEN": str(token), "GOOGLE_OAUTH_CLIENT_SECRETS": ""}
            with patch.dict(os.environ, env, clear=False), \
                 patch("manager.default_oauth_config.load_default_oauth_config", return_value=VALID_INSTALLED_CONFIG):
                creds, source = credentials_with_source(allow_interactive=True, oauth=oauth(flow_credential=FakeCredential()))
            self.assertEqual("desktop_oauth", source)
            self.assertNotEqual("old", token.read_text(encoding="utf-8"))
            leftovers = [p for p in Path(directory).iterdir() if p.name.startswith(f".{token.name}.")]
            self.assertEqual([], leftovers)

    # -- no-leak guarantees ---------------------------------------------------

    def test_health_output_redacts_token_content(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory, '{"refresh_token":"secret-value"}'))}, clear=False):
            with patch("manager.drive_auth.credentials_with_source", side_effect=PublisherError("Google OAuth reauthorization required")):
                result = status()
        self.assertTrue(result["reauth_required"])
        self.assertNotIn("secret-value", str(result))

    def test_authorize_output_never_leaks_token(self):
        with patch("manager.drive_auth.credentials_with_source", return_value=(LeakyCredential(), "desktop_oauth")), \
             patch.object(sys, "argv", ["drive_auth.py", "authorize"]):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = drive_auth.main()
        self.assertEqual(0, exit_code)
        output = buffer.getvalue()
        self.assertNotIn("ACCESS-TOKEN-SECRET-VALUE", output)
        self.assertNotIn("REFRESH-TOKEN-SECRET-VALUE", output)


if __name__ == "__main__": unittest.main()
