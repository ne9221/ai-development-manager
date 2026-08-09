import unittest

from collectors.claude import CollectorError, normalize


class ClaudeParserTest(unittest.TestCase):
    def test_both_windows(self):
        provider = normalize({"rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": "2026-08-09T08:00:00Z"},
            "seven_day": {"used_percentage": 40.5, "resets_at": "2026-08-15T08:00:00+00:00"},
        }}, "2026-08-09T03:00:00Z")
        self.assertEqual([window["name"] for window in provider["windows"]], ["five_hour", "seven_day"])
        self.assertEqual(provider["windows"][0]["remaining_percent"], 75)

    def test_missing_five_hour(self):
        provider = normalize({"rate_limits": {
            "seven_day": {"used_percentage": 10, "resets_at": None},
        }})
        self.assertEqual([window["name"] for window in provider["windows"]], ["seven_day"])

    def test_missing_seven_day(self):
        provider = normalize({"rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": None},
        }})
        self.assertEqual([window["name"] for window in provider["windows"]], ["five_hour"])

    def test_both_missing_is_unknown(self):
        provider = normalize({})
        self.assertEqual(provider["windows"], [])
        self.assertEqual(provider["status"], "unknown")
        self.assertEqual(provider["confidence"], "unknown")

    def test_malformed_input(self):
        with self.assertRaises(CollectorError):
            normalize({"rate_limits": {"five_hour": {"used_percentage": "25"}}})


if __name__ == "__main__":
    unittest.main()
