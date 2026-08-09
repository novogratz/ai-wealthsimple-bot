import json
import tempfile
import unittest
from subprocess import CompletedProcess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import scripts.run_spy_options as bot
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
    def test_never_forces_an_unaffordable_contract(self):
        self.assertEqual(bot.calc_max_contracts(0.60, 50.0), 0)

    def test_uses_maximum_affordable_whole_contracts(self):
        self.assertEqual(bot.calc_max_contracts(0.10, 100.0), 10)

    def test_never_exceeds_live_cash(self):
        self.assertEqual(bot.calc_max_contracts(1.01, 500.0), 4)

    def test_strikes_are_bounded_by_live_spy_price(self):
        self.assertTrue(is_strike_within_otm_bounds("put", 768.0, 773.0))
        self.assertFalse(is_strike_within_otm_bounds("put", 767.0, 773.0))
        self.assertTrue(is_strike_within_otm_bounds("call", 777.0, 773.0))
        self.assertFalse(is_strike_within_otm_bounds("call", 779.0, 773.0))

    def test_quant_score_prefers_tight_liquid_contract(self):
        liquid = contract(0.35)
        liquid.strike = 768.5
        liquid.bid = 0.34
        liquid.volume = 10_000
        liquid.open_interest = 5_000
        illiquid = contract(0.35)
        illiquid.strike = 768.5
        illiquid.bid = 0.05
        illiquid.volume = 1
        illiquid.open_interest = 1
        good, _ = bot._contract_quant_score(liquid, 773.0)
        bad, _ = bot._contract_quant_score(illiquid, 773.0)
        self.assertGreater(good, bad)

    def test_live_buy_stops_at_manual_review(self):
        result = CompletedProcess([], 0, stdout='ORDER_RESULT_JSON:{"submitted": false}', stderr="")
        with patch.object(bot, "_DRY_RUN", False), patch.object(bot.subprocess, "run", return_value=result) as run:
            self.assertEqual(bot.execute_buy_option(contract(), 1, 100), "review")
            self.assertNotIn("--confirm", run.call_args.args[0])


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
