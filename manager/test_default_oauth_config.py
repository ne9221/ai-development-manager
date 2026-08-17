import unittest

from manager.default_oauth_config import DEFAULT_OAUTH_CONFIG, UNPROVISIONED_SENTINEL, load_default_oauth_config


class DefaultOAuthConfigTests(unittest.TestCase):
    def test_shape_is_a_desktop_installed_client(self):
        config = load_default_oauth_config()
        self.assertIn("installed", config)
        installed = config["installed"]
        for field in ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"):
            self.assertIn(field, installed)

    def test_bundled_client_is_explicitly_unprovisioned(self):
        # The repository does not ship a real Google OAuth client. This must stay a
        # sentinel until an ADM maintainer provisions a real Desktop OAuth client;
        # collectors.publish_drive relies on this sentinel to fail closed.
        installed = DEFAULT_OAUTH_CONFIG["installed"]
        self.assertEqual(UNPROVISIONED_SENTINEL, installed["client_id"])
        self.assertEqual(UNPROVISIONED_SENTINEL, installed["client_secret"])


if __name__ == "__main__": unittest.main()
