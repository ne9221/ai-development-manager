import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors.publish_drive import PublisherError, credentials_with_source
from manager.drive_auth import status


class RefreshError(Exception): pass
class DefaultCredentialsError(Exception): pass


class FakeCredential:
    def __init__(self, valid=True, expired=False, refresh_token="refresh", refresh_error=None):
        self.valid, self.expired, self.refresh_token, self.refresh_error = valid, expired, refresh_token, refresh_error
    def refresh(self, _request):
        if self.refresh_error: raise self.refresh_error
        self.valid, self.expired = True, False
    def to_json(self): return '{"token":"replacement"}'


def oauth(credential=None, default_error=True, flow_credential=None):
    class Credentials:
        @staticmethod
        def from_authorized_user_file(_path, _scopes): return credential
    class GoogleAuth:
        @staticmethod
        def default(scopes):
            if default_error: raise DefaultCredentialsError()
            return FakeCredential(), "default"
    class Flow:
        @staticmethod
        def from_client_secrets_file(_path, _scopes):
            return type("Runner", (), {"run_local_server": lambda self, port: flow_credential})()
    return {"Credentials": Credentials, "google_auth": GoogleAuth, "Request": object,
            "InstalledAppFlow": Flow, "DefaultCredentialsError": DefaultCredentialsError, "RefreshError": RefreshError}


class DriveAuthTests(unittest.TestCase):
    def token(self, directory, text="{}"):
        path = Path(directory) / "token.json"; path.write_text(text, encoding="utf-8"); return path

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

    def test_desktop_authorization_saves_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.token(directory, "old")
            env = {"GOOGLE_DRIVE_TOKEN": str(path), "GOOGLE_OAUTH_CLIENT_SECRETS": "client.json"}
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(PublisherError, "did not return valid"):
                    credentials_with_source(allow_interactive=True, oauth=oauth(credential=None, flow_credential=FakeCredential(valid=False)))
                self.assertEqual("old", path.read_text(encoding="utf-8"))
                credentials_with_source(allow_interactive=True, oauth=oauth(credential=None, flow_credential=FakeCredential()))
                self.assertNotEqual("old", path.read_text(encoding="utf-8"))

    def test_health_output_redacts_token_content(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GOOGLE_DRIVE_TOKEN": str(self.token(directory, '{"refresh_token":"secret-value"}'))}, clear=False):
            with patch("manager.drive_auth.credentials_with_source", side_effect=PublisherError("Google OAuth reauthorization required")):
                result = status()
        self.assertTrue(result["reauth_required"])
        self.assertNotIn("secret-value", str(result))


if __name__ == "__main__": unittest.main()
