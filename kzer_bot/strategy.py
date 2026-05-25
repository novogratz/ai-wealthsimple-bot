from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, Ticker
from .market_data import MarketData, Snapshot


@dataclass(frozen=True)
class Pick:
    symbol: str
    last_price: float
    shares: int
    score: float
    reason: str


class KzerStrategy:
    """Momentum scanner with conservative liquidity and affordability filters."""

    def __init__(self, settings: Settings, universe: list[Ticker], market_data: MarketData):
        self.settings = settings
        self.universe = universe
        self.market_data = market_data

    def rank(self, cash: float) -> list[Pick]:
        picks: list[Pick] = []
        budget = min(cash, self.settings.risk.max_cash_per_trade) - self.settings.risk.cash_buffer
        if budget <= 0:
            return []

        for ticker in self.universe:
            snap = self.market_data.snapshot(ticker.symbol)
            if snap is None:
                continue
            pick = self._score_snapshot(snap, budget)
            if pick is not None:
                picks.append(pick)
        return sorted(picks, key=lambda p: p.score, reverse=True)

    def _score_snapshot(self, snap: Snapshot, budget: float) -> Pick | None:
        risk = self.settings.risk
        if not (risk.min_price <= snap.last_price <= risk.max_price):
            return None
        if snap.avg_volume < risk.min_avg_volume:
            return None

        shares = int(budget // snap.last_price)
        if shares < 1:
            return None

        gap = pct_change(snap.open_price, snap.previous_close)
        intraday = pct_change(snap.last_price, snap.open_price)
        high_pullback = pct_change(snap.last_price, snap.day_high)
        rel_volume = snap.latest_volume / max(snap.avg_volume, 1.0)

        # Bias toward liquid names moving up today, while penalizing big pullbacks from the day high.
        score = (gap * 120.0) + (intraday * 180.0) + (rel_volume * 8.0) + (high_pullback * 80.0)
        if score <= 0:
            return None

        reason = (
            f"gap {gap:+.2%}, intraday {intraday:+.2%}, "
            f"rel_vol {rel_volume:.2f}, high_pullback {high_pullback:+.2%}"
        )
        return Pick(
            symbol=snap.symbol,
            last_price=snap.last_price,
            shares=shares,
            score=score,
            reason=reason,
        )


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current - previous) / previous
