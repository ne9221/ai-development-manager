import json
import logging
import unittest
from unittest.mock import patch

from collectors.publish_drive import PublisherError, credentials_with_source
from collectors.test_drive_auth import FakeCredential, oauth
from cloud.drive_credentials import DriveWriteCredentialError, user_oauth_write_credentials


SECRET_MARKER = "refresh-token-secret-value"


def _source(oauth_doubles, **credentials_with_source_kwargs):
    """Build a credentials_source callable matching user_oauth_write_credentials'
    real call signature: credentials_source(allow_interactive, persist_refreshed_token)."""
    def _call(allow_interactive, persist_refreshed_token):
        return credentials_with_source(
            allow_interactive=allow_interactive,
            persist_refreshed_token=persist_refreshed_token,
            oauth=oauth_doubles,
            **credentials_with_source_kwargs,
        )
    return _call


class UserOauthWriteCredentialsTests(unittest.TestCase):
    def test_valid_existing_token_is_write_capable(self):
        creds, source = user_oauth_write_credentials(
            credentials_source=_source(oauth(FakeCredential())))
        self.assertEqual("existing_token", source)
        self.assertTrue(creds.valid)

    def test_expired_token_with_valid_refresh_succeeds(self):
        creds, source = user_oauth_write_credentials(
            credentials_source=_source(oauth(FakeCredential(valid=False, expired=True))))
        self.assertEqual("refreshed_token", source)
        self.assertTrue(creds.valid)

    def test_missing_token_fails_closed(self):
        with self.assertRaises(PublisherError):
            user_oauth_write_credentials(
                credentials_source=_source(oauth(credential=None, default_error=True)))

    def test_adc_service_account_only_fails_closed(self):
        with self.assertRaises(DriveWriteCredentialError):
            user_oauth_write_credentials(
                credentials_source=_source(oauth(credential=None, default_error=False)))

    def test_malformed_token_fails_closed_without_leaking_credential(self):
        class BrokenCredentials:
            @staticmethod
            def from_authorized_user_file(_path, _scopes):
                raise ValueError(SECRET_MARKER)

        broken_oauth = oauth(credential=None, default_error=True)
        broken_oauth["Credentials"] = BrokenCredentials
        with self.assertRaises(PublisherError) as ctx:
            user_oauth_write_credentials(credentials_source=_source(broken_oauth))
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))

    def test_never_calls_interactive_flow(self):
        class ExplodingFlow:
            @staticmethod
            def from_client_secrets_file(_path, _scopes):
                raise AssertionError("interactive flow must never be invoked from Cloud Run write path")

        broken_oauth = oauth(credential=None, default_error=True)
        broken_oauth["InstalledAppFlow"] = ExplodingFlow
        with self.assertRaises(PublisherError):
            user_oauth_write_credentials(credentials_source=_source(broken_oauth))

    def test_wrapper_reuses_shared_credentials_with_source_by_default(self):
        # Proves this module adds a policy layer only, not a second OAuth
        # implementation: the default parameter is literally the same
        # function object collectors.publish_drive already exposes.
        self.assertIs(user_oauth_write_credentials.__defaults__[0], credentials_with_source)

    def test_credential_source_identification_is_exact(self):
        for double, expected in [
            (FakeCredential(), "existing_token"),
            (FakeCredential(valid=False, expired=True), "refreshed_token"),
        ]:
            _, source = user_oauth_write_credentials(credentials_source=_source(oauth(double)))
            self.assertEqual(expected, source)

    def test_invalid_refresh_token_fails_closed_without_leaking_secret(self):
        # An invalid/expired refresh token is the normal "malformed secret"
        # shape (google-auth raises its dedicated RefreshError, which the
        # shared credentials_with_source() maps to a generic category label,
        # never echoing exception text -- unlike other unexpected errors).
        from collectors.test_drive_auth import RefreshError
        broken_oauth = oauth(FakeCredential(valid=False, expired=True, refresh_error=RefreshError(SECRET_MARKER)))
        with self.assertRaises(PublisherError) as ctx:
            user_oauth_write_credentials(credentials_source=_source(broken_oauth))
        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertIn("invalid_refresh_token", str(ctx.exception))

    def test_drive_write_credential_error_message_never_carries_secret(self):
        # DriveWriteCredentialError (the type this module itself raises) is
        # built only from the safe `source` enum string, never from creds.
        try:
            user_oauth_write_credentials(
                credentials_source=_source(oauth(credential=None, default_error=False)))
        except DriveWriteCredentialError as exc:
            self.assertNotIn(SECRET_MARKER, str(exc))
            self.assertIn("application_default", str(exc))
        else:
            self.fail("expected DriveWriteCredentialError")

    def test_default_wrapper_disables_refreshed_token_persistence(self):
        # The real (unpatched) call user_oauth_write_credentials makes to
        # credentials_source must request persist_refreshed_token=False --
        # the Cloud Run token store is a read-only Secret Manager mount, so
        # a refresh must never attempt a write-back.
        captured = {}

        def spy_source(allow_interactive, persist_refreshed_token):
            captured["allow_interactive"] = allow_interactive
            captured["persist_refreshed_token"] = persist_refreshed_token
            return credentials_with_source(
                allow_interactive=allow_interactive,
                persist_refreshed_token=persist_refreshed_token,
                oauth=oauth(FakeCredential(valid=False, expired=True)),
            )

        user_oauth_write_credentials(credentials_source=spy_source)
        self.assertEqual(False, captured["allow_interactive"])
        self.assertEqual(False, captured["persist_refreshed_token"])

    def test_cold_start_refresh_never_writes_back_to_readonly_mount(self):
        # Reproduces the real P0: an expired access token on a Cloud Run
        # instance must refresh successfully in memory without calling
        # _write_token (which would OSError [Errno 30] on the read-only
        # Secret Manager volume).
        with patch("collectors.publish_drive._write_token") as write_token:
            creds, source = user_oauth_write_credentials(
                credentials_source=_source(oauth(FakeCredential(valid=False, expired=True))))
        write_token.assert_not_called()
        self.assertEqual("refreshed_token", source)
        self.assertTrue(creds.valid)


class DefaultWriteServiceFactoryTests(unittest.TestCase):
    """Exercises cloud.app.default_write_service_factory, the real Cloud Run wiring."""

    def test_user_oauth_success_builds_service_and_logs_safe_source(self):
        import cloud.app as app_module

        fake_creds = FakeCredential()
        built = {}

        def fake_build(*_args, **kwargs):
            built.update(kwargs)
            return object()

        with patch("cloud.app.user_oauth_write_credentials", return_value=(fake_creds, "existing_token")), \
             patch("googleapiclient.discovery.build", fake_build), \
             self.assertLogs("runtime_bridge_cloud", logging.INFO) as logs:
            service = app_module.default_write_service_factory()
        self.assertIsNotNone(service)
        self.assertIs(built.get("credentials"), fake_creds)
        logged = "".join(logs.output)
        self.assertIn("existing_token", logged)
        self.assertIn("drive_write_credential", logged)

    def test_adc_only_fails_closed_before_building_any_service(self):
        import cloud.app as app_module
        from cloud.drive_credentials import DriveWriteCredentialError

        def never_build(*_args, **_kwargs):
            raise AssertionError("must not build a Drive service when only ADC is available")

        with patch("cloud.app.user_oauth_write_credentials",
                   side_effect=DriveWriteCredentialError("write-required Drive path requires user OAuth credentials, got source=application_default")), \
             patch("googleapiclient.discovery.build", never_build):
            with self.assertRaises(DriveWriteCredentialError):
                app_module.default_write_service_factory()

    def test_refreshed_token_credential_logs_safe_source_only(self):
        # Same guarantee as the existing_token case, but for the cold-start
        # refresh path specifically: the log line must carry only the safe
        # "refreshed_token" enum string, never token/secret material.
        import cloud.app as app_module

        class LeakyRefreshedCredential:
            valid = True
            token = "ACCESS-TOKEN-SECRET-VALUE"
            refresh_token = "REFRESH-TOKEN-SECRET-VALUE"

        built = {}

        def fake_build(*_args, **kwargs):
            built.update(kwargs)
            return object()

        with patch("cloud.app.user_oauth_write_credentials",
                   return_value=(LeakyRefreshedCredential(), "refreshed_token")), \
             patch("googleapiclient.discovery.build", fake_build), \
             self.assertLogs("runtime_bridge_cloud", logging.INFO) as logs:
            service = app_module.default_write_service_factory()
        self.assertIsNotNone(service)
        logged = "".join(logs.output)
        self.assertIn("refreshed_token", logged)
        self.assertNotIn("ACCESS-TOKEN-SECRET-VALUE", logged)
        self.assertNotIn("REFRESH-TOKEN-SECRET-VALUE", logged)

    def test_readonly_factory_is_unaffected_and_stays_on_adc(self):
        import cloud.app as app_module

        captured_scopes = {}

        class FakeGoogleAuth:
            @staticmethod
            def default(scopes):
                captured_scopes["scopes"] = scopes
                return FakeCredential(), "project"

        with patch("google.auth", FakeGoogleAuth), patch("googleapiclient.discovery.build", lambda *a, **k: object()):
            app_module.default_service_factory()
        self.assertEqual(["https://www.googleapis.com/auth/drive.readonly"], captured_scopes["scopes"])


if __name__ == "__main__":
    unittest.main()
