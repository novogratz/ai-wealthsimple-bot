"""Black-Scholes preview for closed-market SPY 0DTE planning only."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .market_calendar import is_trading_day, market_close_time, next_trading_day
from .spy_options_strategy import TZ
from .strategy_config import load_strategy_config


@dataclass(frozen=True)
class ContractPreview:
    expiry: str
    option_type: str
    strike: float
    theoretical_premium: float
    spot: float
    volatility: float
    assumptions: str


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _option_price(spot: float, strike: float, years: float, volatility: float, option_type: str) -> float:
    rate, dividend = 0.04, 0.012
    sigma_sqrt = volatility * math.sqrt(years)
    if sigma_sqrt <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (rate - dividend + volatility * volatility / 2) * years) / sigma_sqrt
    d2 = d1 - sigma_sqrt
    if option_type == "call":
        return spot * math.exp(-dividend * years) * _normal_cdf(d1) - strike * math.exp(-rate * years) * _normal_cdf(d2)
    return strike * math.exp(-rate * years) * _normal_cdf(-d2) - spot * math.exp(-dividend * years) * _normal_cdf(-d1)


def estimate_target_contract(option_type: str, spot: float, vix: float, now: datetime) -> ContractPreview | None:
    if option_type not in {"call", "put"} or spot <= 0:
        return None
    local = now.astimezone(TZ)
    same_session = is_trading_day(local.date()) and (local.hour, local.minute) < (16, 0)
    expiry = next_trading_day(local.date(), include_today=same_session)
    close_hour, close_minute = market_close_time(expiry)
    entry = datetime(expiry.year, expiry.month, expiry.day, 9, 45, tzinfo=TZ)
    close = datetime(expiry.year, expiry.month, expiry.day, close_hour, close_minute, tzinfo=TZ)
    years = max((close - entry).total_seconds(), 60) / (365.0 * 24 * 3600)
    volatility = max(vix / 100.0, 0.05)
    config = load_strategy_config()
    target = float(config.get("contract", "premium_mid"))
    lo = float(config.get("contract", "premium_min"))
    hi = float(config.get("contract", "premium_max"))
    center = round(spot)
    strikes = range(max(1, center - 30), center) if option_type == "put" else range(center + 1, center + 31)
    priced = [(float(k), _option_price(spot, float(k), years, volatility, option_type)) for k in strikes]
    eligible = [(k, p) for k, p in priced if lo <= p <= hi]
    pool = eligible or priced
    if not pool:
        return None
    strike, premium = min(pool, key=lambda item: abs(item[1] - target))
    return ContractPreview(
        expiry=expiry.isoformat(), option_type=option_type, strike=strike,
        theoretical_premium=premium, spot=spot, volatility=volatility,
        assumptions="unchanged SPY/VIX; Black-Scholes at 9:45 ET; not a live quote",
    )
