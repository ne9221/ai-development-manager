"""Bundled ADM Desktop OAuth public-client configuration.

This module intentionally does NOT contain a real Google OAuth client_id or
client_secret. Fresh-machine OAuth bootstrap requires an ADM maintainer to
provision a real Google Cloud "Desktop app" OAuth client and replace the
sentinel values below (or ship them through another mechanism outside the
repository). Until that happens, `collectors.publish_drive` treats this
config as explicitly unprovisioned and fails closed with a precise message
rather than attempting to authorize with placeholder values.
"""

UNPROVISIONED_SENTINEL = "UNPROVISIONED"

DEFAULT_OAUTH_CONFIG = {
    "installed": {
        "client_id": UNPROVISIONED_SENTINEL,
        "client_secret": UNPROVISIONED_SENTINEL,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def load_default_oauth_config():
    """Return the bundled ADM Desktop OAuth client config (may be unprovisioned)."""
    return DEFAULT_OAUTH_CONFIG
