import json
import tempfile
import unittest
from subprocess import CompletedProcess
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import scripts.run_spy_options as bot
import kzer_bot.spy_options_strategy as option_strategy
from kzer_bot.spy_options_strategy import OptionContract, OptionsPosition, is_strike_within_otm_bounds


def contract(ask: float = 0.50) -> OptionContract:
    return OptionContract(
        expiry="2026-08-10",
        strike=640.0,
        option_type="call",
        last_price=ask,
        bid=max(ask - 0.02, 0),
        ask=ask,
        mid=max(ask - 0.01, 0),
        iv=0.2,
        volume=1000,
        open_interest=2000,
    )


class PositionSizingTests(unittest.TestCase):
    def test_telegram_reporter_publishes_immediately_before_scheduled_loop(self):
        was_set = bot._reporter_stop.is_set()
        bot._reporter_stop.set()
        try:
            with patch.object(bot, "_target_message", return_value="tomorrow estimate"), \
                 patch.object(bot, "notify") as notify:
                bot._telegram_reporter_loop()
            notify.assert_called_once_with("tomorrow estimate")
        finally:
            if not was_set:
                bot._reporter_stop.clear()

    def test_report_cadence_tracks_market_phase(self):
        self.assertEqual(bot._report_interval_minutes(datetime(2026, 8, 10, 8, 45, tzinfo=bot.TZ)), 30)
        self.assertEqual(bot._report_interval_minutes(datetime(2026, 8, 10, 9, 15, tzinfo=bot.TZ)), 15)
        self.assertEqual(bot._report_interval_minutes(datetime(2026, 8, 10, 9, 45, tzinfo=bot.TZ)), 5)
        self.assertEqual(bot._report_interval_minutes(datetime(2026, 8, 10, 10, 0, tzinfo=bot.TZ)), 15)
        self.assertEqual(bot._report_interval_minutes(datetime(2026, 8, 10, 16, 0, tzinfo=bot.TZ)), 30)

    def test_flatish_and_green_open_select_early_puts(self):
        self.assertEqual(option_strategy.select_opening_play(0.20), ("put", "9:31"))
        self.assertEqual(option_strategy.select_opening_play(0.00), ("put", "9:31"))
        self.assertEqual(option_strategy.select_opening_play(-0.15), ("put", "9:31"))

    def test_clearly_red_open_selects_reversal_call(self):
        self.assertEqual(option_strategy.select_opening_play(-0.16), ("call", "9:45 reversal"))

    def test_daily_bias_override_is_date_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bias.json"
            path.write_text(json.dumps({"date": "2026-08-10", "force": "put"}))
            with patch.object(bot, "DAILY_BIAS_FILE", path):
                self.assertEqual(bot._load_daily_bias_override("2026-08-10"), "put")
                self.assertIsNone(bot._load_daily_bias_override("2026-08-11"))

    def test_daily_bias_override_rejects_invalid_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bias.json"
            path.write_text(json.dumps({"date": "2026-08-10", "force": "short"}))
            with patch.object(bot, "DAILY_BIAS_FILE", path):
                self.assertIsNone(bot._load_daily_bias_override("2026-08-10"))

    def test_next_report_is_clock_aligned(self):
        n = datetime(2026, 8, 10, 10, 7, 30, tzinfo=bot.TZ)
        self.assertEqual(bot._seconds_until_next_report(n), 450)

    def test_never_forces_an_unaffordable_contract(self):
        self.assertEqual(bot.calc_max_contracts(0.60, 50.0), 0)

    def test_uses_maximum_affordable_whole_contracts(self):
        self.assertEqual(bot.calc_max_contracts(0.10, 100.0), 10)

    def test_never_exceeds_live_cash(self):
        self.assertEqual(bot.calc_max_contracts(1.01, 500.0), 4)

    def test_strikes_are_bounded_by_live_spy_price(self):
        self.assertTrue(is_strike_within_otm_bounds("put", 772.0, 773.0))
        self.assertFalse(is_strike_within_otm_bounds("put", 774.0, 773.0))
        self.assertTrue(is_strike_within_otm_bounds("call", 774.0, 773.0))
        self.assertFalse(is_strike_within_otm_bounds("call", 772.0, 773.0))

    def test_quant_score_prefers_tight_liquid_contract(self):
        liquid = contract(0.35)
        liquid.strike = 775.0
        liquid.bid = 0.34
        liquid.volume = 10_000
        liquid.open_interest = 5_000
        illiquid = contract(0.35)
        illiquid.strike = 775.0
        illiquid.bid = 0.05
        illiquid.volume = 1
        illiquid.open_interest = 1
        good, _ = bot._contract_quant_score(liquid, 773.0)
        bad, _ = bot._contract_quant_score(illiquid, 773.0)
        self.assertGreater(good, bad)

    def test_auto_buy_passes_confirm_and_returns_submitted(self):
        result = CompletedProcess([], 0, stdout='ORDER_RESULT_JSON:{"submitted": true}', stderr="")
        with patch.object(bot, "EXECUTION_MODE", "auto"), \
             patch.object(bot, "_DRY_RUN", False), \
             patch.object(bot.subprocess, "run", return_value=result) as run:
            self.assertEqual(bot.execute_buy_option(contract(), 1, 100), "submitted")
            self.assertIn("--confirm", run.call_args.args[0])

    def test_review_buy_has_no_confirm_and_returns_review(self):
        result = CompletedProcess([], 0, stdout='ORDER_RESULT_JSON:{"submitted": false}', stderr="")
        with patch.object(bot, "EXECUTION_MODE", "review"), \
             patch.object(bot, "_DRY_RUN", False), \
             patch.object(bot.subprocess, "run", return_value=result) as run:
            self.assertEqual(bot.execute_buy_option(contract(), 1, 100), "review")
            self.assertNotIn("--confirm", run.call_args.args[0])

    def test_contract_candidates_are_strictly_otm_and_in_premium_band(self):
        import pandas as pd
        calls = pd.DataFrame([
            {"strike": 772, "bid": .39, "ask": .40, "lastPrice": .40, "impliedVolatility": .2, "volume": 1000, "openInterest": 1000},
            {"strike": 774, "bid": .19, "ask": .20, "lastPrice": .20, "impliedVolatility": .2, "volume": 1000, "openInterest": 1000},
            {"strike": 775, "bid": .39, "ask": .40, "lastPrice": .40, "impliedVolatility": .2, "volume": 1000, "openInterest": 1000},
        ])
        fake = SimpleNamespace(options=["2026-08-10"], option_chain=lambda expiry: SimpleNamespace(calls=calls, puts=calls))
        with patch.object(option_strategy.yf, "Ticker", return_value=fake):
            candidates = option_strategy.get_otm_contracts_in_range("call", 773, "2026-08-10")
        self.assertEqual([c.strike for c in candidates], [775])


class OwnershipLedgerTests(unittest.TestCase):
    def test_rejects_position_without_bot_ownership_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "position.json"
            ledger.write_text(json.dumps({"symbol": "SPY"}), encoding="utf-8")
            with patch.object(bot, "POS_FILE", ledger):
                self.assertIsNone(bot.load_position())

    def test_dry_sell_only_allows_exact_owned_contract(self):
        owned_contract = contract()
        owned = OptionsPosition(
            contract=owned_contract,
            contracts=1,
            entry_premium=0.50,
            entry_time=datetime.fromisoformat("2026-08-10T09:45:00-04:00"),
            entry_spy_price=640.0,
            cost_basis=50.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "position.json"
            with patch.object(bot, "POS_FILE", ledger), patch.object(bot, "_DRY_RUN", True):
                bot.save_position(owned)
                self.assertEqual(bot.execute_sell_option(owned_contract, 1), "dry")
                wrong = contract()
                wrong.strike = 641.0
                self.assertEqual(bot.execute_sell_option(wrong, 1), "failed")


if __name__ == "__main__":
    unittest.main()
