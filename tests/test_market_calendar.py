import unittest
from datetime import date

from kzer_bot.market_calendar import is_early_close, is_trading_day, market_close_time


class MarketCalendarTests(unittest.TestCase):
    def test_known_2026_holidays(self):
        self.assertFalse(is_trading_day(date(2026, 1, 1)))
        self.assertFalse(is_trading_day(date(2026, 4, 3)))
        self.assertFalse(is_trading_day(date(2026, 12, 25)))

    def test_regular_day_and_early_close(self):
        self.assertTrue(is_trading_day(date(2026, 8, 10)))
        self.assertTrue(is_early_close(date(2026, 11, 27)))
        self.assertEqual(market_close_time(date(2026, 11, 27)), (13, 0))

