from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "yfinance_cache"
CACHE.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(CACHE))


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    last_price: float
    previous_close: float
    open_price: float
    day_high: float
    avg_volume: float
    latest_volume: float


class MarketData(Protocol):
    def snapshot(self, symbol: str) -> Snapshot | None:
        ...


class YFinanceMarketData:
    def snapshot(self, symbol: str) -> Snapshot | None:
        ticker = yf.Ticker(symbol)
        daily = ticker.history(period="45d", interval="1d", auto_adjust=False)
        intraday = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if daily.empty or intraday.empty:
            return None

        daily = _drop_empty_rows(daily)
        intraday = _drop_empty_rows(intraday)
        if len(daily) < 21 or intraday.empty:
            return None

        last_daily = daily.iloc[-1]
        prev_daily = daily.iloc[-2] if len(daily) > 1 else daily.iloc[-1]
        last_intraday = intraday.iloc[-1]
        avg_volume = float(daily["Volume"].tail(20).mean())

        return Snapshot(
            symbol=symbol,
            last_price=float(last_intraday["Close"]),
            previous_close=float(prev_daily["Close"]),
            open_price=float(last_daily["Open"]),
            day_high=float(intraday["High"].max()),
            avg_volume=avg_volume,
            latest_volume=float(intraday["Volume"].sum()),
        )


def _drop_empty_rows(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.dropna(subset=["Open", "High", "Close", "Volume"])
