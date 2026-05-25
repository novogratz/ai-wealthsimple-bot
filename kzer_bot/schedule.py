from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import TradingSettings


def parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def now_in_market_tz(settings: TradingSettings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def is_market_session(dt: datetime, settings: TradingSettings) -> bool:
    if not is_weekday(dt):
        return False
    current = dt.time()
    return parse_clock(settings.market_open) <= current <= parse_clock(settings.market_close)


def can_open_position(dt: datetime, settings: TradingSettings) -> bool:
    if not is_market_session(dt, settings):
        return False
    return parse_clock(settings.market_open) <= dt.time() <= parse_clock(settings.latest_entry)


def should_force_exit(dt: datetime, settings: TradingSettings) -> bool:
    if not is_market_session(dt, settings):
        return False
    return dt.time() >= parse_clock(settings.force_exit)
