import tempfile
import unittest
from pathlib import Path

from kzer_bot.quant_research import (
    ShadowLedger, ShadowTrade, calibrated_probability, performance,
    promotion_decision, validate_quote, walk_forward,
)


class QuantResearchTests(unittest.TestCase):
    def test_quote_quality_rejects_wide_illiquid_contract(self):
        result = validate_quote(bid=.10, ask=.50, volume=1, open_interest=2)
        self.assertFalse(result.valid)
        self.assertEqual(len(result.reasons), 3)

    def test_quote_quality_accepts_liquid_contract(self):
        self.assertTrue(validate_quote(bid=.39, ask=.40, volume=500, open_interest=1000).valid)

    def test_quote_quality_rejects_stale_quote(self):
        result = validate_quote(
            bid=.39, ask=.40, volume=500, open_interest=1000,
            quote_age_seconds=45, max_quote_age_seconds=30,
        )
        self.assertFalse(result.valid)

    def test_performance_reports_drawdown(self):
        result = performance([10, -4, -8, 3])
        self.assertEqual(result.trades, 4)
        self.assertEqual(result.max_drawdown, 12)

    def test_shadow_ledger_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ShadowLedger(Path(tmp) / "shadow.jsonl")
            trade = ShadowTrade("now", "id", "2026-08-10", "call", 640, 1, .3, .4, .4, 70, "review")
            ledger.append(trade); ledger.append(trade)
            self.assertEqual(len(ledger.path.read_text().splitlines()), 2)

    def test_walk_forward_does_not_use_future_to_select_threshold(self):
        rows = [{"timestamp": str(i), "score": str(10 + i), "return": str(1 if i % 2 else -1)} for i in range(40)]
        result = walk_forward(rows, train_size=20, test_size=10)
        self.assertEqual(len(result), 2)

    def test_calibration_refuses_small_sample(self):
        result = calibrated_probability([{"score": 20, "return": 1}], 20)
        self.assertFalse(result.calibrated)

    def test_promotion_requires_out_of_sample_evidence(self):
        decision = promotion_decision([], {
            "minimum_trades": 60, "minimum_expectancy": 0,
            "minimum_profit_factor": 1.1, "maximum_drawdown": 5,
            "minimum_profitable_windows_pct": .6,
        })
        self.assertFalse(decision.promoted)


if __name__ == "__main__":
    unittest.main()
