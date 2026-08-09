import unittest

from collectors.codex import normalize


class NormalizeTest(unittest.TestCase):
    def test_null_and_multiple_windows(self):
        empty = normalize({"result": {"rateLimits": {"primary": None, "secondary": None}}})
        self.assertEqual(empty["providers"][0]["windows"], [])

        multiple = normalize({"result": {"rateLimits": {
            "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1786250000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1786300000},
        }}})
        self.assertEqual(len(multiple["providers"][0]["windows"]), 2)
        self.assertEqual(multiple["providers"][0]["windows"][0]["remaining_percent"], 80)
        self.assertEqual(multiple["providers"][0]["metadata"]["raw_resets_at"]["primary"], 1786250000)


if __name__ == "__main__":
    unittest.main()
