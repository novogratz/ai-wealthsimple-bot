"""Research, shadow-ledger, and validation primitives for the SPY 0DTE service."""
from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable


@dataclass(frozen=True)
class QuoteQuality:
    valid: bool
    reasons: tuple[str, ...]
    spread_pct: float
    quote_age_seconds: float | None = None
    source: str = "unknown"


def validate_quote(
    *, bid: float, ask: float, volume: int, open_interest: int,
    max_spread_pct: float = 0.25, min_volume: int = 100, min_open_interest: int = 250,
    quote_age_seconds: float | None = None, max_quote_age_seconds: float = 30.0,
    source: str = "unknown",
) -> QuoteQuality:
    reasons: list[str] = []
    if bid <= 0 or ask <= 0 or ask < bid:
        reasons.append("invalid two-sided quote")
    spread_pct = (ask - bid) / ask if ask > 0 and ask >= bid else math.inf
    if spread_pct > max_spread_pct:
        reasons.append(f"spread {spread_pct:.1%} exceeds {max_spread_pct:.0%}")
    if volume < min_volume:
        reasons.append(f"volume {volume} below {min_volume}")
    if open_interest < min_open_interest:
        reasons.append(f"open interest {open_interest} below {min_open_interest}")
    if quote_age_seconds is not None and quote_age_seconds > max_quote_age_seconds:
        reasons.append(f"quote age {quote_age_seconds:.0f}s exceeds {max_quote_age_seconds:.0f}s")
    return QuoteQuality(not reasons, tuple(reasons), spread_pct, quote_age_seconds, source)


@dataclass(frozen=True)
class ShadowTrade:
    timestamp: str
    decision_id: str
    expiry: str
    option_type: str
    strike: float
    contracts: int
    entry_bid: float
    entry_ask: float
    assumed_entry: float
    model_score: float
    live_mode: str
    status: str = "open"


class ShadowLedger:
    """Append-only JSONL ledger independent of broker/live position state."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, trade: ShadowTrade) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(trade), sort_keys=True) + "\n")


@dataclass(frozen=True)
class Performance:
    trades: int
    win_rate: float
    expectancy: float
    profit_factor: float
    max_drawdown: float
    sharpe: float


@dataclass(frozen=True)
class Calibration:
    calibrated: bool
    probability: float | None
    samples: int
    reason: str


def calibrated_probability(rows: list[dict], score: float, minimum_samples: int = 60) -> Calibration:
    """Laplace-smoothed empirical probability from nearby historical score observations."""
    usable = [r for r in rows if "score" in r and "return" in r]
    if len(usable) < minimum_samples:
        return Calibration(False, None, len(usable), f"need {minimum_samples} outcomes")
    ranked = sorted(usable, key=lambda r: abs(abs(float(r["score"])) - abs(score)))
    neighborhood = ranked[:max(30, len(ranked) // 5)]
    wins = sum(float(r["return"]) > 0 for r in neighborhood)
    probability = (wins + 1) / (len(neighborhood) + 2)
    return Calibration(True, probability, len(neighborhood), "empirical local-score calibration")


@dataclass(frozen=True)
class PromotionDecision:
    promoted: bool
    reasons: tuple[str, ...]


def promotion_decision(windows: list[dict], config: dict) -> PromotionDecision:
    reasons: list[str] = []
    trades = sum(int(w.get("trades", 0)) for w in windows)
    if trades < int(config["minimum_trades"]):
        reasons.append(f"only {trades} out-of-sample trades")
    active = [w for w in windows if int(w.get("trades", 0)) > 0]
    profitable = sum(float(w.get("expectancy", 0)) > float(config["minimum_expectancy"]) for w in active)
    profitable_pct = profitable / len(active) if active else 0.0
    if profitable_pct < float(config["minimum_profitable_windows_pct"]):
        reasons.append(f"profitable windows {profitable_pct:.0%}")
    finite_pf = [float(w["profit_factor"]) for w in active if w.get("profit_factor") not in (None, math.inf)]
    if finite_pf and mean(finite_pf) < float(config["minimum_profit_factor"]):
        reasons.append(f"profit factor {mean(finite_pf):.2f}")
    if active and max(float(w.get("max_drawdown", 0)) for w in active) > float(config["maximum_drawdown"]):
        reasons.append("maximum drawdown exceeded")
    return PromotionDecision(not reasons, tuple(reasons))


def performance(returns: Iterable[float]) -> Performance:
    values = list(returns)
    if not values:
        return Performance(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    sigma = pstdev(values)
    return Performance(
        trades=len(values),
        win_rate=len(wins) / len(values),
        expectancy=mean(values),
        profit_factor=sum(wins) / abs(sum(losses)) if losses else math.inf,
        max_drawdown=drawdown,
        sharpe=mean(values) / sigma * math.sqrt(252) if sigma > 0 else 0.0,
    )


def walk_forward(rows: list[dict], train_size: int, test_size: int) -> list[dict]:
    """Evaluate fixed score thresholds selected only on preceding training data."""
    if train_size < 10 or test_size < 1:
        raise ValueError("train_size must be >= 10 and test_size >= 1")
    results: list[dict] = []
    for start in range(0, len(rows) - train_size - test_size + 1, test_size):
        train = rows[start:start + train_size]
        test = rows[start + train_size:start + train_size + test_size]
        thresholds = sorted({float(row["score"]) for row in train})
        if not thresholds:
            continue
        minimum_trades = max(5, math.ceil(len(train) * 0.10))
        eligible = [
            t for t in thresholds
            if sum(abs(float(r["score"])) >= t for r in train) >= minimum_trades
        ]
        if not eligible:
            continue
        threshold = max(
            eligible,
            key=lambda t: performance(float(r["return"]) for r in train if abs(float(r["score"])) >= t).expectancy,
        )
        test_returns = [float(r["return"]) for r in test if abs(float(r["score"])) >= threshold]
        results.append({"start": start, "threshold": threshold, **asdict(performance(test_returns))})
    return results


def load_replay_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"timestamp", "score", "return"}
    if rows and not required.issubset(rows[0]):
        raise ValueError(f"CSV requires columns: {', '.join(sorted(required))}")
    return rows


def decision_id(now: datetime, option_type: str, strike: float) -> str:
    return f"{now:%Y%m%dT%H%M%S}-{option_type}-{strike:g}"
