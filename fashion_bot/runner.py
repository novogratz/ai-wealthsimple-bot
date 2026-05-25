from __future__ import annotations

from .paper import PaperBroker
from .schedule import can_open_position, now_in_market_tz, should_force_exit
from .strategy import FashionStrategy


def run_paper_once(strategy: FashionStrategy, broker: PaperBroker, cash: float) -> str:
    now = now_in_market_tz(strategy.settings.trading)

    if broker.position is not None:
        snap = strategy.market_data.snapshot(broker.position.symbol)
        if snap is None:
            return "Paper hold: no fresh quote for open position."
        broker.mark(snap.last_price)

        risk = strategy.settings.risk
        pos = broker.position
        stop = pos.entry_price * (1.0 - risk.stop_loss_pct)
        target = pos.entry_price * (1.0 + risk.take_profit_pct)
        trail = pos.peak_price * (1.0 - risk.trailing_stop_pct)

        if snap.last_price <= stop:
            return broker.sell(snap.last_price, "stop loss")
        if snap.last_price >= target:
            return broker.sell(snap.last_price, "take profit")
        if snap.last_price <= trail:
            return broker.sell(snap.last_price, "trailing stop")
        if should_force_exit(now, strategy.settings.trading):
            return broker.sell(snap.last_price, "force exit near close")
        return f"Paper HOLD {pos.shares} {pos.symbol} at ${snap.last_price:.2f}."

    if not can_open_position(now, strategy.settings.trading):
        return f"Paper idle: outside entry window at {now:%Y-%m-%d %H:%M %Z}."

    picks = strategy.rank(cash=cash)
    if not picks:
        return "Paper idle: no candidate passed filters."
    pick = picks[0]
    return broker.buy(pick.symbol, pick.shares, pick.last_price, pick.reason)
