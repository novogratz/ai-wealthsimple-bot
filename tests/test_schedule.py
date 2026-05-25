from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from fashion_bot.config import TradingSettings
from fashion_bot.schedule import can_open_position, is_market_session, should_force_exit


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.settings = TradingSettings(
            timezone="America/Toronto",
            market_open="09:30",
            market_close="16:00",
            latest_entry="10:00",
            force_exit="15:55",
        )
        self.tz = ZoneInfo("America/Toronto")

    def test_entry_window(self):
        self.assertTrue(can_open_position(datetime(2026, 5, 25, 9, 30, tzinfo=self.tz), self.settings))
        self.assertTrue(can_open_position(datetime(2026, 5, 25, 10, 0, tzinfo=self.tz), self.settings))
        self.assertFalse(can_open_position(datetime(2026, 5, 25, 10, 1, tzinfo=self.tz), self.settings))

    def test_session_and_exit(self):
        self.assertFalse(is_market_session(datetime(2026, 5, 24, 12, 0, tzinfo=self.tz), self.settings))
        self.assertTrue(is_market_session(datetime(2026, 5, 25, 15, 59, tzinfo=self.tz), self.settings))
        self.assertTrue(should_force_exit(datetime(2026, 5, 25, 15, 55, tzinfo=self.tz), self.settings))


if __name__ == "__main__":
    unittest.main()
