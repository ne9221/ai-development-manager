import json
import unittest
from pathlib import Path

from collectors.antigravity import CollectorError, collect, normalize, validate
from manager.ag_language_server import AgLanguageServerClient, AgLsError, LanguageServerEndpoint
from manager.test_ag_language_server import FakeOpener, TOKEN, endpoint, quota_summary, user_status

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "status.schema.json"


class NormalizeTests(unittest.TestCase):
    def test_official_document_validates_and_carries_four_windows(self):
        document = normalize(user_status(), quota_summary(), language_server=endpoint().evidence(), captured_at="2026-09-02T12:30:00Z")
        validate(document, SCHEMA)
        entry = document["providers"][0]
        self.assertEqual(("antigravity", "automatic", "antigravity_language_server_quota_summary", "official", "official", "ok"),
                         (entry["provider"], entry["collection_mode"], entry["source"], entry["source_type"], entry["confidence"], entry["status"]))
        self.assertEqual("2026-09-02T12:30:00Z", entry["last_updated"])
        by_name = {window["name"]: window for window in entry["windows"]}
        self.assertEqual({"gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"}, set(by_name))
        self.assertEqual((10080, 67.5, 32.5, "2026-09-04T08:20:30Z"),
                         (by_name["gemini-weekly"]["duration_minutes"], by_name["gemini-weekly"]["remaining_percent"],
                          by_name["gemini-weekly"]["used_percent"], by_name["gemini-weekly"]["resets_at"]))
        self.assertEqual((300, 100.0, 0.0), (by_name["gemini-5h"]["duration_minutes"], by_name["gemini-5h"]["remaining_percent"], by_name["gemini-5h"]["used_percent"]))
        meta = entry["metadata"]
        self.assertEqual(("user@example.com", "Pro", "account", 28164), (meta["account_email"], meta["plan_name"], meta["quota_scope"], meta["language_server"]["pid"]))
        self.assertEqual([1.0, 0.0], [m["remaining_fraction"] for m in meta["models"]])
        self.assertNotIn(TOKEN, json.dumps(document))

    def test_exhausted_bucket_without_fraction_is_zero_not_missing(self):
        entry = normalize(user_status(), quota_summary(tp_weekly=None))["providers"][0]
        by_name = {window["name"]: window for window in entry["windows"]}
        self.assertEqual((0.0, 100.0), (by_name["3p-weekly"]["remaining_percent"], by_name["3p-weekly"]["used_percent"]))
        self.assertEqual("low", entry["status"])
        fully = normalize(user_status(), quota_summary(None, None, None, None))["providers"][0]
        self.assertEqual("exhausted", fully["status"])
        self.assertTrue(all(window["remaining_percent"] == 0.0 for window in fully["windows"]))

    def test_schema_change_and_missing_identity_fail_closed(self):
        with self.assertRaises(CollectorError) as ctx:
            normalize(user_status(), {"response": {}})
        self.assertEqual("quota_schema_changed", ctx.exception.classification)
        with self.assertRaises(CollectorError) as ctx:
            normalize(user_status(), {"response": {"groups": [{"buckets": [{"bucketId": "x", "remainingFraction": "1"}]}]}})
        self.assertEqual("quota_schema_changed", ctx.exception.classification)
        with self.assertRaises(CollectorError) as ctx:
            normalize({"userStatus": {"name": "no email"}}, quota_summary())
        self.assertEqual("account_identity_unavailable", ctx.exception.classification)

    def test_bad_reset_time_is_null_not_fabricated(self):
        summary = quota_summary()
        summary["response"]["groups"][0]["buckets"][0]["resetTime"] = "soon"
        entry = normalize(user_status(), summary)["providers"][0]
        self.assertIsNone(next(w for w in entry["windows"] if w["name"] == "gemini-weekly")["resets_at"])
        validate(normalize(user_status(), summary), SCHEMA)


class CollectTests(unittest.TestCase):
    def test_collect_uses_discovered_endpoint_and_redacts_raw(self):
        opener = FakeOpener({"GetUserStatus": (200, user_status()), "RetrieveUserQuotaSummary": (200, quota_summary())})
        raw, document = collect(5, discover=lambda timeout: endpoint(),
                                client_factory=lambda ep, timeout: AgLanguageServerClient(ep, opener=opener))
        validate(document, SCHEMA)
        self.assertEqual({"GetUserStatus", "RetrieveUserQuotaSummary"}, {call[1] for call in opener.calls})
        self.assertEqual("user@example.com", raw["user_status"]["userStatus"]["email"])
        self.assertNotIn("cascadeModelConfigData", raw["user_status"]["userStatus"])
        self.assertNotIn(TOKEN, json.dumps(raw))
        self.assertEqual(28164, raw["language_server"]["pid"])

    def test_collect_propagates_classification(self):
        def missing(timeout):
            raise AgLsError("ide_not_running", "no process")
        with self.assertRaises(CollectorError) as ctx:
            collect(5, discover=missing)
        self.assertEqual("ide_not_running", ctx.exception.classification)

        opener = FakeOpener({"GetUserStatus": (403, {"message": "Invalid CSRF token"})})
        with self.assertRaises(CollectorError) as ctx:
            collect(5, discover=lambda timeout: endpoint(), client_factory=lambda ep, timeout: AgLanguageServerClient(ep, opener=opener))
        self.assertEqual("rpc_unauthenticated", ctx.exception.classification)


if __name__ == "__main__":
    unittest.main()
