import unittest

from kzer_bot.config import RiskSettings, Settings, Ticker, TradingSettings
from kzer_bot.market_data import Snapshot
from kzer_bot.strategy import KzerStrategy


class FakeMarketData:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def snapshot(self, symbol):
        return self.snapshots.get(symbol)


class StrategyTests(unittest.TestCase):
    def settings(self):
        return Settings(
            trading=TradingSettings("America/Toronto", "09:30", "16:00", "10:00", "15:55"),
            risk=RiskSettings(17.24, 0.25, 0.0125, 0.025, 0.01, 1.0, 17.0, 100000),
        )

    def test_affordable_liquid_positive_momentum_candidate(self):
        strategy = KzerStrategy(
            settings=self.settings(),
            universe=[Ticker("AAA.TO", "AAA"), Ticker("BBB.TO", "BBB")],
            market_data=FakeMarketData(
                {
                    "AAA.TO": Snapshot("AAA.TO", 8.0, 7.5, 7.8, 8.1, 200000, 50000),
                    "BBB.TO": Snapshot("BBB.TO", 20.0, 19.0, 19.5, 20.5, 300000, 80000),
                }
            ),
        )

        picks = strategy.rank(cash=17.24)

        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].symbol, "AAA.TO")
        self.assertEqual(picks[0].shares, 2)


if __name__ == "__main__":
    unittest.main()
