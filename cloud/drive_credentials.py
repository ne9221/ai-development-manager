#!/usr/bin/env python3
"""Cloud Run Drive write credential contract.

A write-required Drive path (Direct Dispatch ingress creating Task/Command
files) must run under a real user OAuth identity. The target Drive folder is
a regular consumer My Drive folder, not a Shared Drive, and a bare service
account has zero personal Drive storage quota of its own -- files.create()
403s with storageQuotaExceeded even when the service account holds folder
Editor access. Application Default Credentials (the Cloud Run runtime
service account) are therefore acceptable for read-only Drive access only.

This module does not implement a new OAuth mechanism: it reuses
collectors.publish_drive.credentials_with_source() (the same credential
loader the desktop Command Watcher already uses) and adds one policy layer
on top -- reject any source that is not a genuine user-derived OAuth token,
so a write-required caller fails closed instead of silently falling back to
ADC and only discovering the quota error at files.create() time.
"""

from collectors.publish_drive import PublisherError, credentials_with_source

USER_OAUTH_SOURCES = {"existing_token", "refreshed_token"}


class DriveWriteCredentialError(PublisherError):
    """Raised when a write-required Drive path cannot obtain user OAuth credentials."""


def user_oauth_write_credentials(credentials_source=credentials_with_source):
    """Return (creds, source) for a write-required Drive path, or fail closed.

    Always requests non-interactive credential resolution (Cloud Run has no
    browser to complete an interactive consent flow). Only existing_token and
    refreshed_token -- both genuine user OAuth tokens -- are accepted;
    application_default (service account ADC) and desktop_oauth (unreachable
    here) are rejected rather than silently used.
    """
    creds, source = credentials_source(allow_interactive=False)
    if source not in USER_OAUTH_SOURCES:
        raise DriveWriteCredentialError(
            f"write-required Drive path requires user OAuth credentials, got source={source}"
        )
    return creds, source
