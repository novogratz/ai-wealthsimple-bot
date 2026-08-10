import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from kzer_bot.contract_preview import estimate_target_contract


class ContractPreviewTests(unittest.TestCase):
    def test_weekend_preview_targets_next_session_and_stays_otm(self):
        now = datetime(2026, 8, 9, 21, 30, tzinfo=ZoneInfo("America/Toronto"))
        preview = estimate_target_contract("put", 773.0, 15.0, now)
        self.assertEqual(preview.expiry, "2026-08-10")
        self.assertLess(preview.strike, preview.spot)
        self.assertGreater(preview.theoretical_premium, 0)


if __name__ == "__main__":
    unittest.main()
