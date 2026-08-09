import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kzer_bot.market_events import event_blackout
from kzer_bot.strategy_config import load_strategy_config


class StrategyConfigTests(unittest.TestCase):
    def test_default_config_is_valid_and_hashable(self):
        cfg = load_strategy_config()
        self.assertEqual(cfg.get("contract", "premium_min"), 0.25)
        self.assertEqual(len(cfg.hash), 12)

    def test_event_blackout_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(json.dumps({"events": ["2026-08-10T10:00:00-04:00"]}))
            now = datetime(2026, 8, 10, 9, 45, tzinfo=ZoneInfo("America/Toronto"))
            blocked, _ = event_blackout(now, path, 30, 30)
            self.assertTrue(blocked)


if __name__ == "__main__":
    unittest.main()
