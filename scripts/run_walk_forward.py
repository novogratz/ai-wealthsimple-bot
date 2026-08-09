#!/usr/bin/env python3
"""Walk-forward evaluation for exported SPY 0DTE shadow outcomes."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kzer_bot.quant_research import load_replay_csv, promotion_decision, walk_forward
from kzer_bot.strategy_config import load_strategy_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path, help="CSV with timestamp, score, return")
    parser.add_argument("--train", type=int, default=60)
    parser.add_argument("--test", type=int, default=20)
    args = parser.parse_args()
    windows = walk_forward(load_replay_csv(args.csv), args.train, args.test)
    promotion = promotion_decision(windows, load_strategy_config().raw["promotion"])
    report = {"windows": windows, "promotion": {"promoted": promotion.promoted, "reasons": promotion.reasons}}
    for window in windows:
        for key, value in list(window.items()):
            if isinstance(value, float) and not math.isfinite(value):
                window[key] = None
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
