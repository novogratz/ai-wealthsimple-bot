"""Small dependency-free NYSE calendar used by the SPY 0DTE runner."""
from __future__ import annotations

from datetime import date, datetime, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7 + (n - 1) * 7)
    return d


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a, b = year % 19, year // 100
    c, d, e = year % 100, b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    easter = _easter(year)
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # MLK
        _nth_weekday(year, 2, 0, 3),       # Presidents Day
        easter - timedelta(days=2),         # Good Friday
        _last_weekday(year, 5, 0),          # Memorial Day
        _observed(date(year, 6, 19)),       # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),        # Labor Day
        _nth_weekday(year, 11, 3, 4),       # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return holidays


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def is_early_close(day: date) -> bool:
    # NYSE normally closes early after Thanksgiving and on Christmas Eve when
    # it falls on a weekday. July 3 is also normally an early close when open.
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    candidates = {thanksgiving + timedelta(days=1)}
    for d in (date(day.year, 7, 3), date(day.year, 12, 24)):
        if is_trading_day(d):
            candidates.add(d)
    return day in candidates


def market_close_time(day: date) -> tuple[int, int]:
    return (13, 0) if is_early_close(day) else (16, 0)


def next_trading_day(day: date, include_today: bool = False) -> date:
    candidate = day if include_today else day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate
