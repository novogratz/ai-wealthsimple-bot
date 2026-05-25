from __future__ import annotations

from .paper import PaperBroker
from .schedule import can_open_position, now_in_market_tz, should_force_exit
from .strategy import KzerStrategy


def run_paper_once(strategy: KzerStrategy, broker: PaperBroker, cash: float) -> str:
    now = now_in_market_tz(strategy.settings.trading)

    if broker.position is not None:
        snap = strategy.market_data.snapshot(broker.position.symbol)
        if snap is None:
            return "Paper hold: no fresh quote for open position."

        if should_force_exit(now, strategy.settings.trading):
            return broker.sell(snap.last_price, "force exit near close")
        return f"Paper HOLD {broker.position.shares} {broker.position.symbol} at ${snap.last_price:.2f}."

    if not can_open_position(now, strategy.settings.trading):
        return f"Paper idle: outside entry window at {now:%Y-%m-%d %H:%M %Z}."

    picks = strategy.rank(cash=cash)
    if not picks:
        return "Paper idle: no candidate passed filters."
    pick = picks[0]
    return broker.buy(pick.symbol, pick.shares, pick.last_price, pick.reason)
