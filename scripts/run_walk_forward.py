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

from kzer_bot.quant_research import load_replay_csv, walk_forward


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path, help="CSV with timestamp, score, return")
    parser.add_argument("--train", type=int, default=60)
    parser.add_argument("--test", type=int, default=20)
    args = parser.parse_args()
    report = walk_forward(load_replay_csv(args.csv), args.train, args.test)
    for window in report:
        for key, value in list(window.items()):
            if isinstance(value, float) and not math.isfinite(value):
                window[key] = None
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
