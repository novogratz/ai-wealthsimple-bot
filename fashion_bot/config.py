from __future__ import annotations

import csv
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RiskSettings:
    max_cash_per_trade: float
    cash_buffer: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    min_price: float
    max_price: float
    min_avg_volume: int


@dataclass(frozen=True)
class TradingSettings:
    timezone: str
    market_open: str
    market_close: str
    latest_entry: str
    force_exit: str


@dataclass(frozen=True)
class Settings:
    trading: TradingSettings
    risk: RiskSettings


@dataclass(frozen=True)
class Ticker:
    symbol: str
    name: str


def load_settings(path: Path) -> Settings:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Settings(
        trading=TradingSettings(**raw["trading"]),
        risk=RiskSettings(**raw["risk"]),
    )


def load_universe(path: Path) -> list[Ticker]:
    with path.open(newline="", encoding="utf-8") as f:
        return [Ticker(row["symbol"], row["name"]) for row in csv.DictReader(f)]
