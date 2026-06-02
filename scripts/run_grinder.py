#!/usr/bin/env python3
"""
Le Grinder
====================================
Quant rules  : intraday rotation  |  no stop loss  |  +5% profit target  |  3:55 PM late-lock
Edge source  : momentum continuation on high-volume up-days + EMA trend filter
Entry timing : 9:31 AM ET every weekday, with same-morning catch-up on restart
AI analysis  : claude CLI analyses top candidates each morning

Usage:
    python scripts/run_grinder.py                       # 24/7 autonomous mode
    python scripts/run_grinder.py --now                 # skip overnight wait (debug)
    python scripts/run_grinder.py --now --yahoo         # skip wait + use Yahoo most active watchlist
    python scripts/run_grinder.py --buy-today --yahoo   # buy immediately with Yahoo watchlist
    python scripts/run_grinder.py --balance 95          # override cash (default: live fetch)
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kzer_bot.grinder_strategy import (
    BestEffortStrategy,
    FallbackStrategy,
    FuturesBias,
    GrinderMarketData,
    GrinderPick,
    GrinderStrategy,
    SmartGrinderStrategy,
    SmartMarketContext,
    WATCHLIST,
    get_futures_bias,
)
from kzer_bot.telegram import TelegramConfigError, send_message

DATA         = ROOT / "data"
POS_FILE     = DATA / "open_position.json"
AUTO_SCRIPT  = ROOT / "scripts" / "wealthsimple_auto.py"
HISTORY_CSV  = DATA / "trade_history.csv"
PNL_LEDGER   = DATA / "pnl_ledger.json"
SESSION_FILE = DATA / "session_info.json"
LOG_FILE     = DATA / "grinder.log"
PROFILE_FILE  = DATA / "company_profiles.json"
SCAN_STATE_FILE = DATA / "scan_state.json"
PYTHON       = sys.executable
TZ           = ZoneInfo("America/Toronto")

DATA.mkdir(exist_ok=True)

_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB

_SELL_HOUR             = 15
_SELL_MINUTE           = 55
_BUY_HOUR              = 9
_BUY_MINUTE            = 35    # 4 min after overnight sell to allow fill settlement
_BUY_CATCHUP_HOUR      = 15    # last valid buy entry — never be in cash during market
_BUY_CATCHUP_MINUTE    = 30
_OVERNIGHT_SELL_HOUR   = 9     # sell overnight positions at market open
_OVERNIGHT_SELL_MINUTE = 31
_BUY_DELAY_MINUTES = 0
_SHORTLIST_SIZE    = 150
_FULL_REFRESH_TTL  = 24 * 3600
_CACHED_SCAN_TTL   = 18 * 3600
_MIN_COVERAGE_FOR_CACHE = 0.35
_DEPLOY_PCT           = 100       # 100% of balance deployed per trade
_PROFIT_TARGET_PCT    = 10.0      # default fallback profit target (adaptive per trade)
_last_rapport_t: float = 0.0
_last_combined_t: float = 0.0          # timestamp of last combined report send
_REPORT_INTERVAL_SECS = 3 * 3600       # every 3 hours


def _dynamic_profit_target(atr_pct: float) -> float:
    """ATR-adaptive profit target: 3.5× daily ATR, floored at 7%, capped at 15%."""
    return round(max(7.0, min(15.0, atr_pct * 3.5)), 1)


def _filter_extended_at_buy(picks: list) -> "GrinderPick":
    """
    At actual buy time, check if the top pick has already run too far since scan.
    If live price is >4% above scan price, skip to next pick (chasing = bad).
    Falls back to picks[0] after checking top 3.
    """
    for pick in picks[:3]:
        try:
            fi   = yf.Ticker(pick.symbol).fast_info
            live = float(fi.last_price or 0)
            if live > 0 and pick.last_close > 0:
                gap = (live - pick.last_close) / pick.last_close * 100
                if gap > 4.0:
                    log(f"  {pick.symbol} already +{gap:.1f}% from scan price ${pick.last_close:.2f} — chasing, trying next pick")
                    continue
        except Exception:
            pass
        return pick
    return picks[0]
_TRAILING_STOP_TRIGGER_PCT  = 2.0  # activate trailing stop at +2.0%
_TRAILING_STOP_DISTANCE_PCT = 1.0  # trail by 1.0% from peak
_LATE_LOCK_PCT        = 2.0       # legacy constant — kept for messages only (always sell at 3:55 PM now)
_MIN_SMART_HOLD_SCORE = 20        # hold overnight if re-scan smart score still >= this

# ── After-hours / extended-hours trading ──────────────────────────────────────
_AH_BUY_START_HOUR  = 16   # 4:00 PM ET — AH buy window opens
_AH_BUY_END_HOUR    = 19   # 7:57 PM ET — stop new AH entries (AH closes 8 PM)
_AH_BUY_END_MINUTE  = 57
_AH_PROFIT_PCT      = 3.0  # sell AH position immediately at +3%
_AH_LIMIT_PREMIUM   = 0.05   # limit buy 5% above current AH price (ensures fill)
_AH_SELL_PREMIUM    = 0.01   # set limit sell target at +1% above AH entry
_AH_MIN_PCT         = 0.3    # minimum after-hours gain to be a candidate (+0.3%)
_AH_WATCHLIST_SIZE  = 80     # number of tickers to scan for AH plays

# ── Pre-market / extended-hours morning trading ──────────────────────────────
_PM_BUY_START_HOUR  = 7    # 7:00 AM ET — Wealthsimple US pre-market opens
_PM_BUY_END_HOUR    = 9    # 9:29 AM ET — stop new PM entries
_PM_BUY_END_MINUTE  = 29
_PM_PROFIT_PCT      = 2.0  # sell PM position immediately at +2%
_PM_LIMIT_PREMIUM   = 0.05   # limit buy 5% above current PM price (ensures fill)
_PM_MIN_PCT         = 0.3    # minimum pre-market gain to be a candidate (+0.3%)
_PM_WATCHLIST_SIZE  = 80     # number of tickers to scan for PM plays
LEGACY_FILE          = DATA / "legacy_position.json"


# ──────────────────────────────────────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_et():%Y-%m-%d %H:%M:%S} ET] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = line.encode(enc, errors="replace").decode(enc)
        try:
            print(safe, flush=True)
        except Exception:
            pass
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            LOG_FILE.rename(LOG_FILE.with_suffix(".log.old"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    """Send trade/event notification to Telegram and log it."""
    log(f"  [event] {msg[:120].replace(chr(10), ' ')}")
    try:
        send_message(msg)
    except TelegramConfigError:
        pass
    except Exception as exc:
        log(f"  Telegram notify failed: {exc}")


def _notify_report(msg: str) -> None:
    """Send to Telegram — used exclusively for the 9:30 AM and 4 PM scheduled reports."""
    try:
        send_message(msg)
        log("  → Telegram report sent.")
    except TelegramConfigError as exc:
        log(f"  Telegram not configured: {exc}")
    except Exception as exc:
        log(f"  Telegram report failed: {exc}")


def _pnl_color(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def _pnl_arrow(v: float) -> str:
    return "📈" if v >= 0 else "📉"


def _buy_time_for_bias(bias: FuturesBias) -> tuple[int, int]:
    return _BUY_HOUR, _BUY_MINUTE


def _load_name_map() -> dict[str, str]:
    path = ROOT / "config" / "universe.csv"
    names: dict[str, str] = {}
    if not path.exists():
        return names
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                symbol = (row.get("symbol") or "").strip()
                name = (row.get("name") or "").strip()
                if symbol and name:
                    names[symbol] = name
    except Exception:
        return {}
    return names


NAME_MAP = _load_name_map()


def _load_profile_cache() -> dict[str, dict]:
    if not PROFILE_FILE.exists():
        return {}
    try:
        payload = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_profile_cache(cache: dict[str, dict]) -> None:
    try:
        PROFILE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


_PROFILE_CACHE = _load_profile_cache()


def _company_profile(symbol: str) -> dict[str, str]:
    cached = _PROFILE_CACHE.get(symbol)
    if isinstance(cached, dict) and cached.get("name"):
        return cached  # type: ignore[return-value]

    profile = {
        "name": NAME_MAP.get(symbol, symbol),
        "sector": "",
        "industry": "",
        "summary": "",
    }

    try:
        info = yf.Ticker(symbol).get_info()
        profile["name"] = (
            info.get("longName")
            or info.get("shortName")
            or profile["name"]
        )
        profile["sector"] = str(info.get("sector") or "").strip()
        profile["industry"] = str(info.get("industry") or "").strip()
        summary = str(info.get("longBusinessSummary") or "").strip()
        if summary:
            profile["summary"] = summary
    except Exception:
        pass

    _PROFILE_CACHE[symbol] = profile
    _save_profile_cache(_PROFILE_CACHE)
    return profile


def _company_line(symbol: str) -> str:
    profile = _company_profile(symbol)
    parts = [f"{symbol} - {profile['name']}"]
    if profile.get("sector"):
        parts.append(profile["sector"])
    if profile.get("industry"):
        parts.append(profile["industry"])
    return " | ".join(parts)


def _company_details(symbol: str) -> str:
    profile = _company_profile(symbol)
    summary = profile.get("summary", "").strip()
    if summary:
        return summary if len(summary) <= 240 else summary[:237].rstrip() + "..."
    sector = profile.get("sector", "").strip()
    industry = profile.get("industry", "").strip()
    pieces = [p for p in (sector, industry) if p]
    return " / ".join(pieces) if pieces else "Business description unavailable"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except Exception:
        return None


def _pick_to_dict(pick: GrinderPick) -> dict:
    return {
        "symbol": pick.symbol,
        "last_close": pick.last_close,
        "score": pick.score,
        "yesterday_pct": pick.yesterday_pct,
        "rel_volume": pick.rel_volume,
        "atr_pct": pick.atr_pct,
        "close_strength": pick.close_strength,
        "above_ema5": pick.above_ema5,
        "above_ema20": pick.above_ema20,
        "strategy_name": pick.strategy_name,
        "premarket_gap_pct": getattr(pick, "premarket_gap_pct", 0.0),
    }


def _pick_from_dict(data: dict) -> GrinderPick | None:
    try:
        return GrinderPick(
            symbol=str(data["symbol"]),
            last_close=float(data["last_close"]),
            score=float(data["score"]),
            yesterday_pct=float(data["yesterday_pct"]),
            rel_volume=float(data["rel_volume"]),
            atr_pct=float(data["atr_pct"]),
            close_strength=float(data["close_strength"]),
            above_ema5=bool(data["above_ema5"]),
            above_ema20=bool(data["above_ema20"]),
            strategy_name=str(data["strategy_name"]),
            premarket_gap_pct=float(data.get("premarket_gap_pct", 0.0)),
        )
    except Exception:
        return None


def _load_scan_state() -> dict:
    if not SCAN_STATE_FILE.exists():
        return {}
    try:
        payload = json.loads(SCAN_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_scan_state(
    *,
    picks: list[GrinderPick],
    bias: FuturesBias,
    buy_plan: str,
    strategy_name: str,
    futures_detail: str,
    shortlist: list[str],
    full_refresh: bool,
    scan_symbols: list[str],
) -> None:
    try:
        payload = {
            "updated": now_et().isoformat(),
            "last_full_scan": now_et().isoformat() if full_refresh else _load_scan_state().get("last_full_scan"),
            "bias": bias.value,
            "buy_plan": buy_plan,
            "strategy_name": strategy_name,
            "futures_detail": futures_detail,
            "shortlist": shortlist[:_SHORTLIST_SIZE],
            "scan_symbols": scan_symbols,
            "picks": [_pick_to_dict(p) for p in picks[:10]],
        }
        SCAN_STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_cached_scan_result() -> tuple[list[GrinderPick], FuturesBias, str, str, str, list[str]] | None:
    state = _load_scan_state()
    if not state:
        return None
    updated = _parse_ts(state.get("updated"))
    if updated is None:
        return None
    if (now_et() - updated).total_seconds() > _CACHED_SCAN_TTL:
        return None
    picks: list[GrinderPick] = []
    for raw in state.get("picks", []) if isinstance(state.get("picks", []), list) else []:
        pick = _pick_from_dict(raw)
        if pick is not None:
            picks.append(pick)
    if not picks:
        return None
    try:
        bias = FuturesBias(state.get("bias", "neutral"))
    except Exception:
        bias = FuturesBias.NEUTRAL
    buy_plan = str(state.get("buy_plan", ""))
    strategy_name = str(state.get("strategy_name", "Cached"))
    futures_detail = str(state.get("futures_detail", "cached"))
    shortlist = [str(s) for s in state.get("shortlist", []) if str(s)]
    return picks, bias, buy_plan, strategy_name, futures_detail, shortlist


def _choose_scan_symbols(force_full: bool = False) -> tuple[list[str], bool]:
    state = _load_scan_state()
    updated = _parse_ts(state.get("updated"))
    last_full = _parse_ts(state.get("last_full_scan"))
    shortlist = [str(s) for s in state.get("shortlist", []) if str(s)]

    use_full = force_full or not shortlist
    if not use_full:
        if last_full is None:
            use_full = True
        else:
            use_full = (now_et() - last_full).total_seconds() >= _FULL_REFRESH_TTL

    base = WATCHLIST if use_full else shortlist

    # Blend in Yahoo trending — puts that day's real movers at the front of the scan
    try:
        ctx = SmartMarketContext.load_or_fetch()
        new_trending = [
            s for s in ctx.trending
            if s and "." not in s and s not in base
        ]
        if new_trending:
            base = new_trending[:30] + base
    except Exception:
        pass

    return base, use_full


def _build_shortlist(md: GrinderMarketData, picks: list[GrinderPick]) -> list[str]:
    shortlist: list[str] = []
    for pick in picks:
        if pick.symbol not in shortlist:
            shortlist.append(pick.symbol)

    for snap in sorted(md.all_snapshots(), key=lambda s: s.score, reverse=True):
        if snap.symbol not in shortlist:
            shortlist.append(snap.symbol)
        if len(shortlist) >= _SHORTLIST_SIZE:
            break
    return shortlist[:_SHORTLIST_SIZE]


# ──────────────────────────────────────────────────────────────────────────────
# PnL ledger helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_total_pnl() -> float:
    if not PNL_LEDGER.exists():
        return 0.0
    try:
        return sum(t.get("realizedPnl", 0) for t in json.loads(PNL_LEDGER.read_text()))
    except Exception:
        return 0.0


def _get_trade_stats() -> dict:
    empty = {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0,
             "total_pnl_pct": 0.0, "starting_balance": 0.0}
    try:
        bal = float(json.loads(SESSION_FILE.read_text()).get("startingBalance", 0))
    except Exception:
        bal = 0.0
    if not PNL_LEDGER.exists():
        return {**empty, "starting_balance": bal}
    try:
        trades     = json.loads(PNL_LEDGER.read_text())
        total_pnl  = sum(t.get("realizedPnl", 0) for t in trades)
        total_cost = sum(t.get("buyCost", 0) for t in trades)
        wins       = sum(1 for t in trades if t.get("realizedPnl", 0) >= 0)
        return {
            "count": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / bal * 100) if bal else 0.0,
            "starting_balance": bal,
        }
    except Exception:
        return {**empty, "starting_balance": bal}


def _record_trade(symbol: str, buy_cost: float, sell_value: float, qty: float) -> float:
    trades = []
    if PNL_LEDGER.exists():
        try:
            trades = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass
    trades.append({
        "symbol": symbol, "quantity": qty, "buyCost": buy_cost,
        "sellValue": sell_value, "realizedPnl": sell_value - buy_cost,
        "time": datetime.now().isoformat(),
    })
    PNL_LEDGER.write_text(json.dumps(trades, indent=2))
    return sum(t.get("realizedPnl", 0) for t in trades)


def _append_trade_history(symbol: str, side: str, price: float, shares: float,
                          cost: float, pnl: float, strategy_name: str) -> None:
    write_header = not HISTORY_CSV.exists()
    with HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp", "symbol", "side", "price", "shares",
                        "cost", "pnl", "strategy"])
        w.writerow([now_et().isoformat(), symbol, side,
                    f"{price:.4f}", f"{shares:.4f}",
                    f"{cost:.2f}", f"{pnl:.2f}", strategy_name])


def _next_weekday_buy() -> datetime:
    candidate = now_et().replace(hour=_BUY_HOUR, minute=_BUY_MINUTE, second=0, microsecond=0)
    if now_et() >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _time_until_sell() -> float:
    now = now_et()
    t = now.replace(hour=_SELL_HOUR, minute=_SELL_MINUTE, second=0, microsecond=0)
    return max(0.0, (t - now).total_seconds())


def _is_quiet_weekend() -> bool:
    """True when we should suppress non-critical Telegram noise.
    Quiet window: all day Saturday + Sunday before 6 PM ET (futures open at ~6 PM Sun).
    """
    n = now_et()
    if n.weekday() == 5:        # Saturday — all day
        return True
    if n.weekday() == 6 and n.hour < 18:   # Sunday before 6 PM
        return True
    return False


def _get_ws_live_price(symbol: str, shares: float | None = None) -> float | None:
    """
    Fetch live price from Wealthsimple's trade page via the already-running browser.
    Covers overnight Blue Ocean ATS sessions that Yahoo Finance doesn't track.
    Returns None if browser is not running or price cannot be read.
    """
    try:
        args_list = [PYTHON, str(AUTO_SCRIPT), "quote", "--symbol", symbol]
        if shares:
            args_list += ["--shares", str(shares)]
        r = subprocess.run(args_list, cwd=ROOT, capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if line.startswith("WS_PRICE:"):
                price = float(line.split(":", 1)[1])
                return price if price > 0 else None
    except Exception as exc:
        log(f"  [ws_price] {exc}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Universe refresh
# ──────────────────────────────────────────────────────────────────────────────

def refresh_universe_if_stale() -> None:
    """US mode — no TMX fetch needed. Log current watchlist size."""
    log(f"US stock mode — using hardcoded NYSE/NASDAQ universe ({len(WATCHLIST)} tickers)")


# ──────────────────────────────────────────────────────────────────────────────
# Buy timing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _next_buy_dt(bias: FuturesBias) -> datetime:
    """Return the next upcoming buy datetime (ET) accounting for weekends."""
    now = now_et()
    buy_h, buy_m = _buy_time_for_bias(bias)
    target = now.replace(hour=buy_h, minute=buy_m, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    while target.weekday() >= 5:  # skip Sat/Sun
        target += timedelta(days=1)
    return target


def _buy_timing_line(bias: FuturesBias) -> str:
    """
    Returns a natural-language timing line for Telegram game plans.
    Examples:
      'Buying in 43 min  (9:31 AM ET today)'
      'Buying in 11h 22min  (9:31 AM ET tomorrow)'
      'Buying in 2d 4h  (Monday 9:31 ET - weekend)'
    """
    now    = now_et()
    target = _next_buy_dt(bias)
    diff   = target - now
    total_mins = max(0, int(diff.total_seconds() / 60))
    h, m   = divmod(total_mins, 60)
    days   = h // 24
    h      = h % 24

    if days > 0:
        countdown = f"{days}d {h}h"
        when_note = f"Monday {target:%H:%M} ET — weekend" if target.weekday() == 0 else f"{target:%A} {target:%H:%M} ET"
    elif h > 0:
        countdown = f"{h}h {m:02d}min"
        same_day  = target.date() == now.date()
        when_note = f"{target:%I:%M %p} ET {'today' if same_day else 'tomorrow'}"
    else:
        countdown = f"{m} min"
        when_note = f"{target:%I:%M %p} ET today"

    style = "market order"

    return f"Buying in <b>{countdown}</b>  ({when_note}, {style})"


def _day_label(bias: FuturesBias) -> str:
    """'TODAY' or 'TOMORROW' (or weekday name for weekend schedules)."""
    now    = now_et()
    target = _next_buy_dt(bias)
    if target.date() == now.date():
        return "TODAY"
    elif (target.date() - now.date()).days == 1:
        return "TOMORROW"
    else:
        return target.strftime("%A").upper()  # e.g. MONDAY


# ──────────────────────────────────────────────────────────────────────────────
# Claude CLI  — AI market analysis
# ──────────────────────────────────────────────────────────────────────────────

def get_ai_analysis(picks: list[GrinderPick], bias: FuturesBias,
                    futures_detail: str, balance: float) -> str:
    """
    Calls an available CLI helper to analyse the top scan candidates.
    Falls back to deterministic output if no CLI is available or the CLI fails.
    """
    if not picks:
        return ""

    def _deterministic_fallback() -> str:
        top = picks[0]
        direction = "bullish" if top.yesterday_pct >= 0 else "mixed"
        return (
            f"- Top pick: {top.symbol} ({top.strategy_name})\n"
            f"- Score: {top.score:.1f} | yesterday: {top.yesterday_pct:+.1f}% | rel vol: {top.rel_volume:.1f}x\n"
            f"- Why: price is above the trend filters and closed near the high\n"
            f"- Risk: lower volume or a weak open can fade the setup\n"
            f"- Expected move: base case {direction}, no guarantee"
        )

    # Build a concise data table for the CLI, if one is present.
    lines = []
    for i, p in enumerate(picks[:5], 1):
        lines.append(
            f"#{i} {p.symbol}  close=${p.last_close:.2f}  "
            f"yesterday={p.yesterday_pct:+.1f}%  relVol={p.rel_volume:.1f}x  "
            f"ATR%={p.atr_pct:.1f}%  closeStrength={p.close_strength:.0%}  "
            f"EMA5={'✅' if p.above_ema5 else '❌'}  EMA20={'✅' if p.above_ema20 else '❌'}  "
            f"score={p.score:.1f}({p.confidence})  strategy={p.strategy_name}"
        )
    picks_block = "\n".join(lines)

    prompt = f"""You are an expert quantitative trader specialising in TSX momentum day-trading.

SETUP:
- Date: {now_et():%Y-%m-%d}
- US Futures bias: {bias.value.upper()} ({futures_detail})
- Account: ~${balance:.0f} USD  |  1 trade/day  |  all-in  |  exit 3:55 PM ET  |  no stop loss

TOP SCAN RESULTS (8-criteria momentum screen):
{picks_block}

TASK — reply in 120 words max, plain text, bullet points:
• Confirm or challenge the #1 pick based purely on the numbers
• Why does the data suggest this stock will continue moving today?
• What is the single biggest risk for this trade?
• Expected move today: bearish / base / bull scenario in %"""

    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Wealthsimple automation
# ──────────────────────────────────────────────────────────────────────────────

def _start_keepalive() -> "subprocess.Popen | None":
    """Launch wealthsimple_auto.py keepalive as a background daemon."""
    try:
        proc = subprocess.Popen(
            [PYTHON, str(AUTO_SCRIPT), "keepalive"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"Keepalive started (PID {proc.pid}) — refreshing WS session every 2 min")
        return proc
    except Exception as exc:
        log(f"Keepalive start failed: {exc}")
        return None


def fetch_live_balance(retries: int = 3) -> float | None:
    _session_expired = False
    for attempt in range(1, retries + 1):
        log(f"Fetching live balance (attempt {attempt}/{retries})...")
        try:
            r = subprocess.run(
                [PYTHON, str(AUTO_SCRIPT), "balance"],
                cwd=ROOT, capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            log(f"  Balance error: {exc}")
            time.sleep(15)
            continue

        combined = r.stdout + r.stderr
        if "session_expired" in combined.lower() or "session expired" in combined.lower():
            _session_expired = True
            log(f"  Session expired — auto-login attempted, retrying ({attempt}/{retries})...")
            time.sleep(20)
            continue

        for line in r.stdout.splitlines():
            for prefix in ("LIVE_BALANCE_USD:", "LIVE_BALANCE_CAD:"):
                if line.startswith(prefix):
                    try:
                        val = float(line[len(prefix):].replace(",", ""))
                        log(f"  Balance: ${val:.2f} USD")
                        return val
                    except ValueError:
                        pass
        log(f"  Could not parse balance (attempt {attempt})")
        time.sleep(15)

    if _session_expired:
        notify(
            "❌ <b>Wealthsimple session expired — auto-login failed</b>\n\n"
            "Run: <code>python scripts/wealthsimple_auto.py setup</code>\n"
            "Log in manually, then restart the bot."
        )
        log("SESSION EXPIRED after all retries — run: python scripts/wealthsimple_auto.py setup")
    return None


def _parse_order_result(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("ORDER_RESULT_JSON:"):
            try:
                return json.loads(line[len("ORDER_RESULT_JSON:"):])
            except Exception:
                pass
    return {}


def fetch_position_details(symbol: str, retries: int = 3) -> dict | None:
    """
    Call the wealthsimple_auto.py position command to get actual fill details
    (fill_price, fill_quantity, fill_value) from the Wealthsimple trade page.
    Returns None if position not found or all retries fail.
    """
    for attempt in range(1, retries + 1):
        log(f"Fetching position details for {symbol} (attempt {attempt}/{retries})...")
        try:
            r = subprocess.run(
                [PYTHON, str(AUTO_SCRIPT), "position", "--symbol", symbol],
                cwd=ROOT, capture_output=True, text=True, timeout=120,
            )
            for line in r.stdout.splitlines():
                if line.startswith("ORDER_RESULT_JSON:"):
                    data = json.loads(line[len("ORDER_RESULT_JSON:"):])
                    if data.get("fill_price") and data.get("fill_quantity"):
                        log(f"  Position: {data['fill_quantity']:.4f} sh @ ${data['fill_price']:.4f}")
                        return data
                if line.strip() == "POSITION_NOT_FOUND":
                    log(f"  Position not found for {symbol} on WS trade page")
                    return None
            combined = (r.stdout + r.stderr).lower()
            if "session_expired" in combined or "session expired" in combined:
                log(f"  Session expired — auto-login in progress, retrying...")
                time.sleep(20)
                continue
            # Log unexpected output for debugging
            if r.stdout.strip() or r.stderr.strip():
                log(f"  Unexpected position output: {r.stdout.strip()[-200:]} | stderr: {r.stderr.strip()[-200:]}")
        except Exception as exc:
            log(f"  Position fetch error: {exc}")
            time.sleep(15)
            continue
        break
    log(f"  Could not fetch position details for {symbol} after {retries} attempts")
    return None


def _refresh_position_cost_from_yf(symbol: str, pos: dict) -> dict | None:
    """
    Fallback: estimate actual fill price/cost from yfinance daily data.
    Uses the day's open price as the fill price (closest to 9:30 AM market open).
    Returns updated position dict if successful, None otherwise.
    """
    try:
        hist = yf.download(symbol, period="1d", progress=False, auto_adjust=True)
        if hist is not None and not hist.empty:
            open_price = float(hist["Open"].iloc[-1])
            if open_price > 0:
                old_shares = float(pos.get("shares", 0))
                old_cost = float(pos.get("estimatedCost", 0))
                new_shares = old_shares if old_shares > 0 else (old_cost / open_price if open_price > 0 else 0)
                new_cost = new_shares * open_price
                pos["buyPrice"] = round(open_price, 4)
                if new_shares > 0:
                    pos["shares"] = round(new_shares, 4)
                pos["estimatedCost"] = round(new_cost, 2)
                log(f"  Refreshed cost from yfinance open=${open_price:.4f}  ({old_shares:.4f} sh @ ${open_price:.4f})")
                return pos
        fi = yf.Ticker(symbol).fast_info
        last_price = fi.last_price
        if last_price and last_price > 0:
            old_shares = float(pos.get("shares", 0))
            old_cost = float(pos.get("estimatedCost", 0))
            new_shares = old_shares if old_shares > 0 else (old_cost / last_price if last_price > 0 else 0)
            new_cost = new_shares * last_price
            pos["buyPrice"] = round(last_price, 4)
            if new_shares > 0:
                pos["shares"] = round(new_shares, 4)
            pos["estimatedCost"] = round(new_cost, 2)
            log(f"  Refreshed cost from yfinance last=${last_price:.4f}")
            return pos
    except Exception as exc:
        log(f"  Yfinance cost refresh failed: {exc}")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Scan diagnostics
# ──────────────────────────────────────────────────────────────────────────────

def _log_scan_diagnostics(md: GrinderMarketData) -> None:
    """Log top 5 tickers by raw score + which main-strategy filter each fails."""
    snaps = md.all_snapshots()
    log(f"  Diagnostics: {len(snaps)}/{len(WATCHLIST)} tickers returned data")
    if not snaps:
        return

    top = sorted(snaps, key=lambda s: s.score, reverse=True)[:5]
    log("  Top 5 by momentum score (main strategy filter failures):")
    for s in top:
        fails = []
        if not (1.0 <= s.last_close <= 1000.0):
            fails.append(f"price=${s.last_close:.2f}")
        if s.avg_volume_20 < 500_000:
            fails.append(f"avgvol={s.avg_volume_20/1e3:.0f}k<500k")
        pct = s.yesterday_pct_change
        if not (1.5 <= pct <= 15.0):
            fails.append(f"pct={pct:+.1f}%")
        if s.rel_volume < 1.5:
            fails.append(f"relvol={s.rel_volume:.1f}x<1.5x")
        if s.atr_pct < 1.0:
            fails.append(f"atr={s.atr_pct:.1f}%<1.0%")
        if s.last_close <= s.ema20:
            fails.append("below_ema20")
        if s.last_close <= s.ema5:
            fails.append("below_ema5")
        if s.close_strength < 0.40:
            fails.append(f"closestr={s.close_strength:.0%}<40%")
        reason = " | ".join(fails) if fails else "PASSES ALL FILTERS"
        log(f"    {s.symbol:12s}  score={s.score:7.1f}  pct={pct:+5.1f}%"
            f"  relvol={s.rel_volume:.1f}x  ->  {reason}")


# ──────────────────────────────────────────────────────────────────────────────
# Scan
# ──────────────────────────────────────────────────────────────────────────────

def run_scan(balance: float, scan_symbols: list[str] | None = None,
             full_refresh: bool = False) -> tuple[list[GrinderPick], FuturesBias, str, str, str]:
    """
    Returns (picks, bias, buy_plan, strategy_name, futures_detail).
    picks is the full sorted list (may be empty).
    strategy_name reflects which strategy produced the picks.
    """
    scan_symbols = scan_symbols or WATCHLIST

    log("Checking US futures (ES=F)...")
    bias, futures_detail = get_futures_bias()
    log(f"  Bias: {bias.value.upper()}  |  {futures_detail}")

    log(f"Scanning {len(scan_symbols)} US tickers (NYSE/NASDAQ) (main 8-criteria)...")
    md = GrinderMarketData()

    # Batch-download all tickers at once (3-5x faster than sequential)
    def _progress(done: int, total: int) -> None:
        if done % 500 == 0 or done == total:
            log(f"  Data: {done}/{total} tickers downloaded...")
    md.prefetch(scan_symbols, progress_cb=_progress)
    log(f"  Prefetch complete — {len(md.all_snapshots())} tickers with data")

    # Tier 0 — Smart multi-signal composite (primary)
    log("  Fetching market context (TSX + sector ETFs)...")
    try:
        ctx = SmartMarketContext.load_or_fetch()
        sector_parts = "  |  ".join(
            f"{k.replace('.TO', '')}: {v:+.1f}%"
            for k, v in ctx.sector_returns.items()
        )
        log(f"  SPX 5d: {ctx.spy_5d_pct:+.2f}%  |  {sector_parts}")
        if ctx.trending:
            log(f"  Yahoo trending: {len(ctx.trending)} TSX stocks")
    except Exception as exc:
        log(f"  Market context unavailable: {exc}")
        ctx = None

    picks = SmartGrinderStrategy(md, ctx).scan(scan_symbols)
    log(f"  Smart strategy: {len(picks)} candidate(s).")
    strategy_name = "Smart Strategy"

    # Tier 1 — Main 8-criteria (fallback)
    if not picks:
        _log_scan_diagnostics(md)
        log("  No smart picks — running main 8-criteria...")
        picks = GrinderStrategy(md).scan(scan_symbols)
        strategy_name = "Main Strategy"
        log(f"  Main strategy: {len(picks)} candidate(s).")

    # Tier 2 — Relaxed fallback
    if not picks:
        log("  No main picks — running fallback...")
        picks = FallbackStrategy(md).scan(scan_symbols)
        strategy_name = "Fallback Original Strategy" if picks else ""
        log(f"  Fallback: {len(picks)} candidate(s).")

    # Tier 3 — Guaranteed pick
    if not picks:
        log("  No fallback picks — running best-effort guaranteed pick...")
        picks = BestEffortStrategy(md).scan(scan_symbols)
        strategy_name = "Best Available" if picks else "No Strategy"
        log(f"  Best-effort: {len(picks)} candidate(s).")

    buy_plan = "Buy at 9:31 AM ET (market order)"

    shortlist_source = picks if picks else [GrinderPick(
        symbol=s.symbol,
        last_close=s.last_close,
        score=s.score,
        yesterday_pct=s.yesterday_pct_change,
        rel_volume=s.rel_volume,
        atr_pct=s.atr_pct,
        close_strength=s.close_strength,
        above_ema5=(s.last_close > s.ema5),
        above_ema20=(s.last_close > s.ema20),
        strategy_name="Shortlist Seed",
    ) for s in sorted(md.all_snapshots(), key=lambda s: s.score, reverse=True)[:_SHORTLIST_SIZE]]

    if picks:
        shortlist = _build_shortlist(md, picks)
        _save_scan_state(
            picks=picks,
            bias=bias,
            buy_plan=buy_plan,
            strategy_name=strategy_name,
            futures_detail=futures_detail,
            shortlist=shortlist,
            full_refresh=full_refresh,
            scan_symbols=scan_symbols,
        )
        return picks, bias, buy_plan, strategy_name, futures_detail

    cached = _load_cached_scan_result()
    coverage = (len(md.all_snapshots()) / len(scan_symbols)) if scan_symbols else 0.0
    shortlist = _build_shortlist(md, shortlist_source)
    _save_scan_state(
        picks=[],
        bias=bias,
        buy_plan=buy_plan,
        strategy_name=strategy_name,
        futures_detail=futures_detail,
        shortlist=shortlist,
        full_refresh=full_refresh,
        scan_symbols=scan_symbols,
    )
    if cached is not None and (coverage < _MIN_COVERAGE_FOR_CACHE or len(scan_symbols) > 1000):
        cached_picks, _, cached_buy_plan, cached_strategy, cached_fut, cached_shortlist = cached
        log("  Using cached scan state because live data coverage was low.")
        _save_scan_state(
            picks=cached_picks,
            bias=bias,
            buy_plan=cached_buy_plan or buy_plan,
            strategy_name=cached_strategy,
            futures_detail=cached_fut or futures_detail,
            shortlist=cached_shortlist or shortlist,
            full_refresh=full_refresh,
            scan_symbols=scan_symbols,
        )
        return cached_picks, bias, cached_buy_plan or buy_plan, cached_strategy, cached_fut or futures_detail

    return picks, bias, buy_plan, strategy_name, futures_detail


# ──────────────────────────────────────────────────────────────────────────────
# Telegram message builders
# ──────────────────────────────────────────────────────────────────────────────

def _bias_line(bias: FuturesBias, futures_detail: str) -> str:
    emoji = {"green": "🟢 GREEN", "red": "🔴 RED", "neutral": "⚪ NEUTRAL"}[bias.value]
    return f"📡 Futures: <b>{emoji}</b>  —  {futures_detail}"


def _criteria_explanation(pick: GrinderPick) -> str:
    strat = pick.strategy_name
    if strat == "Smart Strategy":
        return (
            f"━━━ <b>SMART STRATEGY (composite)</b> ━━━━\n"
            f"  ✅ Price: <b>${pick.last_close:.2f}</b>  ($1.50–$50.00)  |  Above EMA20\n"
            f"  ✅ Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>  |  "
            f"Rel Vol: <b>{pick.rel_volume:.1f}x</b>\n"
            f"  ✅ Multi-timeframe momentum cascade (1d / 5d / 20d)\n"
            f"  ✅ Volume accumulation trend (5d vs 20d avg)\n"
            f"  ✅ OBV smart-money direction signal (10-session)\n"
            f"  ✅ Relative strength vs TSX composite (5d)\n"
            f"  ✅ Breakout proximity to 20-day high\n"
            f"  🎯 Composite score: <b>{pick.score:.1f} / 100</b>"
        )
    if strat == "Main Strategy":
        return (
            f"━━━ <b>8-CRITERIA PASSED</b> ━━━━\n"
            f"  ✅ Price: <b>${pick.last_close:.2f}</b>  ($2.00–$40.00)\n"
            f"  ✅ Avg Vol: <b>≥300k</b>\n"
            f"  ✅ Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>  (+1.5% to +12%)\n"
            f"  ✅ Rel Vol: <b>{pick.rel_volume:.1f}x</b>  (≥1.5x)\n"
            f"  ✅ ATR: <b>{pick.atr_pct:.2f}%</b>  (≥1.5%)\n"
            f"  ✅ Above EMA20 ✅ EMA5\n"
            f"  ✅ Close Strength: <b>{pick.close_strength:.0%}</b>  (≥0.40)\n"
            f"  ✅ Market Cap ≥ $25M"
        )
    if strat == "Fallback":
        return (
            f"━━━ <b>FALLBACK CRITERIA PASSED</b> ━━━━\n"
            f"  ✅ Price: <b>${pick.last_close:.2f}</b>  ($1.00–$40.00)\n"
            f"  ✅ Avg Vol: <b>≥100k</b>\n"
            f"  ✅ Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>  (+1.0% to +15%)\n"
            f"  ✅ Rel Vol: <b>{pick.rel_volume:.1f}x</b>  (≥1.0x)\n"
            f"  ✅ Above EMA20\n"
            f"  ✅ Market Cap ≥ $25M\n\n"
            f"  ⚠️ Missed main: ATR or close-strength or EMA5 threshold"
        )
    if strat == "Best Available":
        return (
            f"━━━ <b>BEST AVAILABLE (no filters)</b> ━━━━\n"
            f"  ⚠️ No stock passed normal filters\n"
            f"  🎯 Highest raw momentum score from full universe\n"
            f"  📈 Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>\n"
            f"  🔥 Rel Vol: <b>{pick.rel_volume:.1f}x</b>\n"
            f"  📊 Above EMA5 {'✅' if pick.above_ema5 else '❌'}  "
            f"Above EMA20 {'✅' if pick.above_ema20 else '❌'}"
        )
    return ""


def build_scan_message(
    picks: list[GrinderPick],
    bias: FuturesBias,
    futures_detail: str,
    buy_plan: str,
    strategy_name: str,
    balance: float,
    ai_analysis: str,
    label: str,
) -> str:
    day   = _day_label(bias)
    header = f"🌅 <b>Le Grinder — {label}</b>"

    if not picks:
        return (
            f"{header}\n\n"
            f"{_bias_line(bias, futures_detail)}\n\n"
            f"🔍 Scanned {len(WATCHLIST)} tickers — <b>no data returned from any ticker</b>.\n"
            f"📋 Plan: <b>SKIP {day}</b> — check internet connection / yfinance."
        )

    top        = picks[0]
    company_ln = _company_line(top.symbol)
    company_dt = _company_details(top.symbol)
    deploy     = balance * _DEPLOY_PCT / 100
    shares_est = int(deploy // top.last_close) if top.last_close > 0 else 0
    stats      = _get_trade_stats()
    at_pnl     = stats["total_pnl"]
    at_color   = _pnl_color(at_pnl)
    is_best    = (strategy_name == "Best Available")
    timing     = _buy_timing_line(bias)

    others = "  ".join(
        f"<code>{p.symbol}</code> ${p.last_close:.2f} score {p.score:.0f}"
        for p in picks[1:4]
    )

    if is_best:
        scan_line   = (
            f"🔍 Scanned <b>{len(WATCHLIST)}</b> US tickers (NYSE/NASDAQ)\n"
            f"   ⚠️ <b>No clean setups</b> — using Best Available pick"
        )
        pick_header = (
            f"⚠️━━━━━━━━━━━━━━━━━━━━━━━━⚠️\n"
            f"📌 <b>{day}'S PICK (Best Available): <code>{top.symbol}</code>  "
            f"(${top.last_close:.2f} USD)</b>\n"
            f"⚠️━━━━━━━━━━━━━━━━━━━━━━━━⚠️\n"
            f"<i>No stock passed normal filters — highest-scoring from full universe.\n"
            f"Trade with reduced size if desired.</i>"
        )
    else:
        scan_line   = (
            f"🔍 Scanned <b>{len(WATCHLIST)}</b> US tickers (NYSE/NASDAQ)\n"
            f"   ✅ <b>{len(picks)}</b> passed  |  Strategy: <b>{strategy_name}</b>"
        )
        pick_header = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>{day}'S PICK: <code>{top.symbol}</code>  (${top.last_close:.2f} USD)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    msg = (
        f"{header}\n\n"
        f"{_bias_line(bias, futures_detail)}\n\n"
        f"{scan_line}\n\n"
        f"{pick_header}\n"
        f"🏢 <b>{company_ln}</b>\n"
        f"📝 {company_dt}\n\n"
        f"<b>WHY THIS STOCK:</b>\n"
        f"  📈 Yesterday: <b>{top.yesterday_pct:+.2f}%</b>  on  "
        f"<b>{top.rel_volume:.1f}x normal volume</b>\n"
        f"  🔥 ATR(14): <b>{top.atr_pct:.2f}%</b> of price\n"
        f"  💪 Closed: <b>{top.close_strength:.0%}</b> of day range\n"
        f"  📊 Trend: EMA5 {'✅' if top.above_ema5 else '❌'}  "
        f"EMA20 {'✅' if top.above_ema20 else '❌'}\n"
        f"  🎯 Score: <b>{top.score:.1f}</b>  ({top.confidence})\n"
        f"  🌅 Pre-market gap: <b>{top.premarket_gap_pct:+.2f}%</b>\n\n"
        f"{_criteria_explanation(top)}\n"
    )

    if others and not is_best:
        msg += f"Other candidates:  {others}\n\n"

    # Build step-by-step game plan
    is_morning = "5 AM" in label or "Morning" in label
    plan_title = "TODAY'S GAME PLAN" if is_morning else "TOMORROW'S GAME PLAN"

    open_pos = None
    try:
        if POS_FILE.exists():
            open_pos = json.loads(POS_FILE.read_text())
    except Exception:
        pass

    plan_steps = []
    if open_pos:
        held_sym = open_pos.get("symbol", "position")
        held_entry = float(open_pos.get("buyPrice", 0))
        plan_steps.append(
            f"  1️⃣  <b>9:31 AM ET</b> — Sell <code>{held_sym}</code> (overnight @ ${held_entry:.2f})"
        )
        plan_steps.append(
            f"  2️⃣  <b>9:35 AM ET</b> — Buy <code>{top.symbol}</code>  ~${top.last_close:.2f}"
            f"  (~{shares_est} sh,  ${deploy:.0f} USD)"
        )
        plan_steps.append(
            f"  3️⃣  Autonomous exit — +{_PROFIT_TARGET_PCT:.0f}% target anytime or +{_LATE_LOCK_PCT:.0f}% lock at 3:55 PM"
        )
    else:
        plan_steps.append(
            f"  1️⃣  <b>9:35 AM ET</b> — Buy <code>{top.symbol}</code>  ~${top.last_close:.2f}"
            f"  (~{shares_est} sh,  ${deploy:.0f} USD)"
        )
        plan_steps.append(
            f"  2️⃣  Autonomous exit — +{_PROFIT_TARGET_PCT:.0f}% target anytime or +{_LATE_LOCK_PCT:.0f}% lock at 3:55 PM  |  Intraday rotation enabled"
        )

    plan_body = "\n".join(plan_steps)

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>{plan_title}  @pmarx</b>\n"
        f"{plan_body}\n\n"
        f"  📡 Futures: <b>{bias.value.upper()}</b>  |  Strategy: <b>{strategy_name}</b>\n"
        f"  💰 Budget: <b>${balance:.2f}</b>  →  deploying <b>{_DEPLOY_PCT}%</b> = <b>${deploy:.2f} USD</b>\n"
        f"  🎯 Target: <b>+1.5% to +3%</b> momentum continuation\n\n"
        f"{at_color} All-time PnL: <b>${at_pnl:+.2f} USD</b>"
        f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
    )

    if ai_analysis:
        msg += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Analysis:</b>\n{ai_analysis}"
        )

    return msg


def build_buy_message(
    pick: GrinderPick,
    balance: float,
    bias: FuturesBias,
    futures_detail: str,
    ai_analysis: str,
    is_bounce: bool = False,
) -> str:
    deploy     = balance * _DEPLOY_PCT / 100
    shares_est = int(deploy // pick.last_close) if pick.last_close > 0 else 0
    entry_mode = "9:31 AM ET market order"
    bias_line  = _bias_line(bias, futures_detail)

    msg = (
        f"🛒 <b>BUYING NOW — <code>{pick.symbol}</code></b>\n"
        f"🏢 <b>{_company_line(pick.symbol)}</b>\n"
        f"📝 {_company_details(pick.symbol)}\n\n"
        f"⏰ <b>{entry_mode}</b>\n"
        f"💵 Entry: ~<b>${pick.last_close:.2f} USD</b>\n"
        f"🔢 Est. shares: ~<b>{shares_est}</b>  ({_DEPLOY_PCT}% of ${balance:.2f})\n"
        f"💰 Deploying: <b>${deploy:.2f} USD</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>EDGE FOR THIS TRADE:</b>\n"
        f"  ✅ Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>  on  <b>{pick.rel_volume:.1f}× volume</b> → continuation setup\n"
        f"  ✅ ATR: <b>{pick.atr_pct:.2f}%</b> → room to run 1–3 % today\n"
        f"  ✅ Trend: EMA5 {'✅' if pick.above_ema5 else '❌'}  EMA20 {'✅' if pick.above_ema20 else '❌'} → trend intact\n"
        f"  ✅ Close strength: <b>{pick.close_strength:.0%}</b> → buyers held the close\n"
        f"  {bias_line}\n"
        f"  🎯 Score: <b>{pick.score:.1f}</b>  ({pick.confidence})\n\n"
        f"{_criteria_explanation(pick)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>PLAN:</b>  {pick.strategy_name}\n"
        f"🎯 Exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  +{_LATE_LOCK_PCT:.0f}% lock at 3:55 PM  |  No stop loss — intraday rotation enabled"
    )

    if ai_analysis:
        msg += f"\n\n🤖 <b>Analysis:</b>\n{ai_analysis}"

    return msg


def build_red_waiting_message(pick: GrinderPick | None, futures_detail: str) -> str:
    pick_line = (
        f"Top setup: <code>{pick.symbol}</code>  ${pick.last_close:.2f}"
        f"  (score {pick.score:.0f})"
        if pick else "No main candidate found yet."
    )
    return (
        f"🔴 <b>RED FUTURES — Waiting for Bounce</b>\n\n"
        f"📡 {futures_detail}\n"
        f"Market opening weak — do NOT buy into the red open.\n\n"
        f"{pick_line}\n\n"
        f"<b>WHY WE WAIT:</b>\n"
        f"  • Red openings often flush early longs first\n"
        f"  • Better entry after market finds support (10:30–11:30 AM)\n"
        f"  • Reduces adverse selection, improves R/R ratio\n\n"
        f"<b>WATCHING FOR AT 11 AM:</b>\n"
        f"  ✅ Pick holding above yesterday's low\n"
        f"  ✅ ES=F showing stabilization or reversal\n"
        f"  ✅ Volume picking up on the bounce\n\n"
        f"⏰ <b>Re-scanning at 11:00 AM ET</b>\n"
        f"If setup no longer holds → SKIP today"
    )


def build_update_message(
    symbol: str, entry: float, price: float, shares: float, cost: float,
    next_sell_dt: "datetime | None" = None,
) -> str:
    """30-min executive summary: current position + today's realized trades + all-time P&L."""
    unrealized = shares * price - cost
    pnl_pct    = unrealized / cost * 100 if cost else 0.0
    pos_color  = _pnl_color(unrealized)
    pos_arrow  = "📈" if unrealized >= 0 else "📉"

    if next_sell_dt is not None:
        secs_left  = max(0, (next_sell_dt - now_et()).total_seconds())
        sell_label = f"{next_sell_dt:%I:%M %p} ET"
    else:
        secs_left  = max(0, _time_until_sell())
        sell_label = "3:55 PM ET"
    mins_left = int(secs_left / 60)
    h_left    = mins_left // 60
    m_left    = mins_left % 60
    time_str  = f"{h_left}h {m_left:02d}min" if h_left else f"{m_left} min"

    # Today's realized trades
    stats  = _get_trade_stats()
    ledger = []
    if PNL_LEDGER.exists():
        try:
            ledger = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass
    today_str    = now_et().strftime("%Y-%m-%d")
    today_trades = [t for t in ledger if str(t.get("time", "")).startswith(today_str)]
    daily_pnl    = sum(t.get("realizedPnl", 0) for t in today_trades)
    daily_cost   = sum(t.get("buyCost", 0)     for t in today_trades)
    daily_pct    = (daily_pnl / daily_cost * 100) if daily_cost else 0.0
    daily_wins   = sum(1 for t in today_trades if t.get("realizedPnl", 0) >= 0)
    daily_loss   = len(today_trades) - daily_wins
    d_color      = _pnl_color(daily_pnl)

    trade_lines = ""
    for t in today_trades:
        t_pnl  = t.get("realizedPnl", 0)
        t_cost = t.get("buyCost", 0) or 1
        t_pct  = t_pnl / t_cost * 100
        ic     = _pnl_color(t_pnl)
        trade_lines += f"  {ic} <code>{t['symbol']}</code>  <b>{t_pct:+.1f}%</b>  (${t_pnl:+.2f})\n"
    if not trade_lines:
        trade_lines = "  <i>No realized trades today.</i>\n"

    all_wr   = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0
    at_color = _pnl_color(stats["total_pnl"])

    return (
        f"📊 <b>LE GRINDER  •  {now_et():%H:%M} ET</b>\n\n"
        f"<b>POSITION  •  <code>{symbol}</code></b>\n"
        f"  {pos_arrow} ${price:.2f}  (entry ${entry:.2f})  "
        f"{pos_color} <b>{pnl_pct:+.1f}%  (${unrealized:+.2f})</b>\n"
        f"  ⏰ Exit in <b>{time_str}</b>  ({sell_label})"
        f"  |  🎯 +{_PROFIT_TARGET_PCT:.0f}%  |  🛡️ Trail +{_TRAILING_STOP_TRIGGER_PCT:.0f}%\n\n"
        f"<b>TODAY  •  {len(today_trades)} trade{'s' if len(today_trades) != 1 else ''}</b>\n"
        f"{trade_lines}"
        f"  {d_color} Day P&L:  <b>{daily_pnl:+.2f} USD  ({daily_pct:+.1f}%)</b>"
        f"  |  🏆 <b>{daily_wins}W / {daily_loss}L</b>\n\n"
        f"<b>ALL TIME  •  {stats['count']} trades</b>\n"
        f"  {at_color} P&L:  <b>{stats['total_pnl']:+.2f} USD</b>  ({stats['total_pnl_pct']:+.1f}% ROI)"
        f"  |  🏆 <b>{stats['wins']}W / {stats['losses']}L</b>  —  <b>{all_wr:.0f}% win rate</b>"
    )


def _pick_why(p: GrinderPick) -> str:
    """One-line human reason for a pick based on available GrinderPick fields."""
    reasons = []
    if p.yesterday_pct >= 5:
        reasons.append(f"🚀 +{p.yesterday_pct:.1f}% yesterday")
    elif p.yesterday_pct >= 2:
        reasons.append(f"📈 +{p.yesterday_pct:.1f}% yesterday")
    else:
        reasons.append(f"+{p.yesterday_pct:.1f}% yesterday")

    if p.rel_volume >= 3:
        reasons.append(f"🔥 {p.rel_volume:.1f}x vol")
    elif p.rel_volume >= 1.5:
        reasons.append(f"{p.rel_volume:.1f}x vol")

    if p.above_ema5 and p.above_ema20:
        reasons.append("Stage 2 trend")
    elif p.above_ema20:
        reasons.append("above EMA20")

    if p.close_strength >= 0.85:
        reasons.append("closed at day high")
    elif p.close_strength >= 0.65:
        reasons.append("strong close")

    if p.atr_pct >= 4:
        reasons.append(f"ATR {p.atr_pct:.1f}% (volatile)")

    # Sector context from cached market data
    try:
        from kzer_bot.grinder_strategy import _SECTOR_MAP, SMART_CONTEXT_CACHE
        sector = _SECTOR_MAP.get(p.symbol)
        if sector and SMART_CONTEXT_CACHE.exists():
            ctx_raw = json.loads(SMART_CONTEXT_CACHE.read_text())
            sr = ctx_raw.get("sector_returns", {}).get(sector, 0)
            if sr >= 3:
                reasons.append(f"{sector} sector +{sr:.1f}%")
    except Exception:
        pass

    return "  •  ".join(reasons)


def _quick_scan_picks(scan_symbols: list[str], current_symbol: str | None = None) -> list[GrinderPick]:
    """
    Run a fresh SmartGrinderStrategy scan on scan_symbols and return picks,
    excluding current_symbol. Falls back to cached picks if scan fails.
    Runs in ~15-30s for a 150-ticker shortlist (batch yfinance download).
    """
    try:
        log(f"30-min watchlist refresh: scanning {len(scan_symbols)} tickers...")
        md  = GrinderMarketData()
        md.prefetch(scan_symbols)
        ctx = SmartMarketContext.load_or_fetch()
        picks = SmartGrinderStrategy(md, ctx).scan(scan_symbols)
        if not picks:
            picks = GrinderStrategy(md).scan(scan_symbols)
        if not picks:
            picks = FallbackStrategy(md).scan(scan_symbols)
        picks = [p for p in picks if p.symbol != current_symbol]
        log(f"  Watchlist refresh done: {len(picks)} picks")
        return picks
    except Exception as exc:
        log(f"  Watchlist refresh error: {exc} — using cached picks")
        # Fall back to cached scan state picks
        state = _load_scan_state()
        cached: list[GrinderPick] = []
        for raw in state.get("picks", []):
            p = _pick_from_dict(raw)
            if p and p.symbol != current_symbol:
                cached.append(p)
        return cached


def build_watchlist_alert(
    picks: list[GrinderPick],
    current_symbol: str | None = None,
    current_price: float = 0.0,
    entry: float = 0.0,
    shares: float = 0.0,
    cost: float = 0.0,
) -> str | None:
    """
    Build a Telegram message with:
    - Today's bot pick (current position live P&L)
    - Top 3 picks the user can trade manually (fresh scan)
    """
    top3 = [p for p in picks if p.symbol != current_symbol][:3]
    if not top3 and not current_symbol:
        return None

    lines = []

    # ── Today's bot pick ─────────────────────────────────────────────────
    if current_symbol and entry > 0:
        pnl       = (current_price - entry) * shares if current_price > 0 else 0.0
        pnl_pct   = (current_price - entry) / entry * 100 if entry > 0 else 0.0
        price_str = f"${current_price:.2f}" if current_price > 0 else "n/a"
        ic        = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"🤖 <b>Bot's pick:</b> <code>{current_symbol}</code>  "
            f"{shares:.0f} sh @ ${entry:.2f}  →  {price_str}  "
            f"{ic} <b>{pnl_pct:+.1f}%</b>  (${pnl:+.2f})"
        )

    # ── Top 3 manual picks ────────────────────────────────────────────────
    if top3:
        lines.append(f"\n<b>Top {len(top3)} picks if you had more cash:</b>")
        medals = ["1️⃣", "2️⃣", "3️⃣"]
        for i, p in enumerate(top3):
            conf_emoji = "🔥" if p.score >= 80 else ("⚡" if p.score >= 50 else "📊")
            gap_str = f"  gap {p.premarket_gap_pct:+.1f}%" if abs(p.premarket_gap_pct) >= 0.5 else ""
            lines.append(
                f"{medals[i]} {conf_emoji} <code>{p.symbol}</code>  "
                f"<b>${p.last_close:.2f}</b>  [score {p.score:.0f}{gap_str}]\n"
                f"    {_pick_why(p)}"
            )
    else:
        lines.append("\n<i>No additional picks found right now.</i>")

    return (
        f"👀 <b>Watchlist — {now_et():%H:%M} ET</b>\n\n"
        + "\n\n".join(lines)
    )


def _get_weekly_pnl(ledger: list) -> float:
    """Sum realized P&L for the current calendar week (Mon–Sun)."""
    now = now_et()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return sum(
        t.get("realizedPnl", 0) for t in ledger
        if _parse_ts(str(t.get("time", ""))) is not None
        and _parse_ts(str(t.get("time", ""))) >= week_start
    )


def build_daily_report() -> str:
    """Daily Quant Summary — sent at 4:00 PM ET every trading day."""
    stats = _get_trade_stats()
    ledger = []
    if PNL_LEDGER.exists():
        try:
            ledger = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass

    now       = now_et()
    today_str = now.strftime("%Y-%m-%d")
    today_trades = [t for t in ledger if str(t.get("time", "")).startswith(today_str)]

    daily_pnl  = sum(t.get("realizedPnl", 0) for t in today_trades)
    daily_cost = sum(t.get("buyCost", 0) for t in today_trades)
    daily_pct  = (daily_pnl / daily_cost * 100) if daily_cost else 0.0
    daily_wins = sum(1 for t in today_trades if t.get("realizedPnl", 0) > 0)
    daily_loss = sum(1 for t in today_trades if t.get("realizedPnl", 0) < 0)
    daily_flat = len(today_trades) - daily_wins - daily_loss
    daily_wr   = (daily_wins / len(today_trades) * 100) if today_trades else 0.0

    weekly_pnl = _get_weekly_pnl(ledger)
    all_wr     = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0
    avg_pnl    = (daily_pnl / len(today_trades)) if today_trades else 0.0
    best_today = max(today_trades, key=lambda t: t.get("realizedPnl", 0), default=None)

    d_color  = _pnl_color(daily_pnl)
    w_color  = _pnl_color(weekly_pnl)
    at_color = _pnl_color(stats["total_pnl"])

    # ── Live balance ────────────────────────────────────────────────────────
    live_bal = None
    try:
        live_bal = fetch_live_balance(retries=2)
    except Exception:
        pass

    sb = stats.get("starting_balance") or 0.0
    if live_bal is not None:
        bal_str = f"<b>${live_bal:.2f} USD</b>"
        if sb:
            delta = live_bal - sb
            bal_str += f"  ({'📈' if delta >= 0 else '📉'} {delta/sb*100:+.1f}% vs session open)"
    else:
        bal_str = "<i>n/a</i>"

    roi_pct = stats["total_pnl_pct"]

    # ── Per-trade lines ─────────────────────────────────────────────────────
    trade_lines = ""
    for t in today_trades:
        pnl  = t.get("realizedPnl", 0)
        cost = t.get("buyCost", 0) or 1
        sell = t.get("sellValue", 0)
        qty  = t.get("quantity", 0) or 1
        pct  = pnl / cost * 100
        ic   = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        buy_px  = cost / qty
        sell_px = sell / qty
        trade_lines += (
            f"  {ic} <code>{t['symbol']:<6}</code>  "
            f"<b>${buy_px:.2f} → ${sell_px:.2f}</b>  "
            f"<b>{pct:+.1f}%</b>  (<b>${pnl:+.2f}</b>)\n"
            f"        {qty:.0f} sh  ·  cost ${cost:.2f}  ·  sold ${sell:.2f}\n"
        )
    if not trade_lines:
        trade_lines = "  <i>No closed trades today — position held overnight.</i>\n"

    flat_str = f" / {daily_flat}=" if daily_flat else ""
    wr_str   = f"{daily_wr:.0f}% win rate" if today_trades else "—"

    # ── Best trade of day ────────────────────────────────────────────────────
    best_line = ""
    if best_today:
        bp = best_today.get("realizedPnl", 0)
        best_line = f"  🏆 Best trade:   <code>{best_today['symbol']}</code>  <b>${bp:+.2f}</b>\n"

    # ── Open position ────────────────────────────────────────────────────────
    open_section = ""
    if POS_FILE.exists():
        try:
            pos   = json.loads(POS_FILE.read_text())
            sym   = pos.get("symbol", "")
            if sym:
                entry = float(pos.get("buyPrice", 0))
                sh    = float(pos.get("shares", 0))
                live  = _get_ws_live_price(sym, shares=sh)
                if not live:
                    fi   = yf.Ticker(sym).fast_info
                    live = float(fi.last_price or entry)
                price = live or entry
                upct  = (price - entry) / entry * 100 if entry else 0
                upnl  = (price - entry) * sh
                ic2   = "📈" if upct >= 0 else "📉"
                is_ah = bool(pos.get("afterHours")) or pos.get("strategyName", "") in ("After-Hours Limit", "Pre-Market Limit")
                sell_note = f"9:35 AM ET {_next_weekday_buy():%a %b %d}" if is_ah else "3:55 PM ET tomorrow"
                open_section = (
                    f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>OPEN POSITION</b>\n"
                    f"  {ic2} <code>{sym}</code>  {sh:.0f} sh @ ${entry:.2f}  →  ${price:.2f}  "
                    f"<b>{upct:+.1f}%  (${upnl:+.2f} unrealized)</b>\n"
                    f"  🔔 Selling: <b>{sell_note}</b>\n"
                )
        except Exception:
            pass

    if not open_section:
        open_section = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>OPEN POSITION</b>\n"
            f"  <i>No open position — cash rotates into next AH pick tonight.</i>\n"
        )

    return (
        f"📊 <b>LE GRINDER — DAILY QUANT SUMMARY</b>\n"
        f"<i>{now:%A, %b %d %Y}  ·  4:00 PM ET</i>\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>PORTFOLIO</b>\n"
        f"  💵 Balance:      {bal_str}\n"
        f"  {d_color} Today P&L:   <b>${daily_pnl:+.2f}  ({daily_pct:+.1f}%)</b>\n"
        f"  {w_color} Week P&L:    <b>${weekly_pnl:+.2f}</b>\n"
        f"  {at_color} All-time:    <b>${stats['total_pnl']:+.2f}  ({roi_pct:+.1f}% ROI)</b>\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>TODAY'S TRADES</b>  ·  {len(today_trades)} closed  ·  "
        f"<b>{daily_wins}W / {daily_loss}L{flat_str}</b>  ·  {wr_str}\n\n"
        f"{trade_lines}\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>SESSION STATS</b>\n"
        f"{best_line}"
        f"  📈 Avg/trade:    <b>${avg_pnl:+.2f}</b>\n"
        f"  🏦 All-time:    <b>{stats['count']} trades  ·  {stats['wins']}W/{stats['losses']}L  ·  {all_wr:.0f}% win rate</b>\n"

        f"{open_section}"

        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🤖 Le Grinder  ·  Autonomous US Equity Bot</i>"
    )


def build_morning_report() -> str:
    """9:30 AM morning briefing — open bell summary sent to Telegram."""
    stats  = _get_trade_stats()
    ledger = []
    if PNL_LEDGER.exists():
        try:
            ledger = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass

    now        = now_et()
    today_str  = now.strftime("%Y-%m-%d")
    today_trades = [t for t in ledger if str(t.get("time", "")).startswith(today_str)]
    daily_pnl  = sum(t.get("realizedPnl", 0) for t in today_trades)
    weekly_pnl = _get_weekly_pnl(ledger)
    at_color   = _pnl_color(stats["total_pnl"])
    w_color    = _pnl_color(weekly_pnl)

    # ── Live balance ──────────────────────────────────────────────────────
    live_bal = None
    try:
        live_bal = fetch_live_balance(retries=2)
    except Exception:
        pass
    bal_str = f"<b>${live_bal:.2f} USD</b>" if live_bal else "<i>n/a</i>"

    # ── Futures bias ──────────────────────────────────────────────────────
    bias_line = ""
    try:
        _ctx = _SMC.load_or_fetch()
        bias = _ctx.get("market_bias", "")
        spy5 = _ctx.get("spy_5d_pct", 0.0)
        if bias:
            b_ic = "🟢" if bias == "GREEN" else ("🔴" if bias == "RED" else "⚪")
            bias_line = f"  {b_ic} Futures:     <b>{bias}</b>  (SPY 5d {spy5:+.1f}%)\n"
    except Exception:
        pass

    # ── Open position ──────────────────────────────────────────────────────
    open_section = ""
    action_line  = "  🔄 Scanning for best pick — deploying at market open.\n"
    if POS_FILE.exists():
        try:
            pos  = json.loads(POS_FILE.read_text())
            sym  = pos.get("symbol", "")
            if sym:
                entry  = float(pos.get("buyPrice", 0))
                sh     = float(pos.get("shares", 0))
                cost   = float(pos.get("estimatedCost", 0)) or (entry * sh)
                live   = _get_ws_live_price(sym, shares=sh)
                if not live:
                    fi   = yf.Ticker(sym).fast_info
                    live = float(fi.last_price or entry)
                price  = live or entry
                upct   = (price - entry) / entry * 100 if entry else 0
                upnl   = (price - entry) * sh
                ic2    = "📈" if upct >= 0 else "📉"
                is_ah  = bool(pos.get("afterHours")) or pos.get("strategyName", "") in ("After-Hours Limit", "Pre-Market Limit")
                open_section = (
                    f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>OPEN POSITION</b>\n"
                    f"  {ic2} <code>{sym}</code>  {sh:.0f} sh @ ${entry:.2f}  →  ${price:.2f}  "
                    f"<b>{upct:+.1f}%  (${upnl:+.2f} unrealized)</b>\n"
                )
                if is_ah:
                    action_line = f"  🔔 AH position → <b>market sell at 9:35 AM + rotate</b>\n"
                else:
                    action_line = f"  🔔 Regular hold → <b>profit target / trail / 3:55 PM lock</b>\n"
        except Exception:
            pass

    return (
        f"🌅 <b>LE GRINDER — MORNING REPORT</b>\n"
        f"<i>{now:%A, %b %d %Y}  ·  9:30 AM ET</i>\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>PORTFOLIO</b>\n"
        f"  💵 Balance:      {bal_str}\n"
        f"  {w_color} Week P&L:    <b>${weekly_pnl:+.2f}</b>\n"
        f"  {at_color} All-time:    <b>${stats['total_pnl']:+.2f}  ({stats['total_pnl_pct']:+.1f}% ROI)</b>\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>MARKET OPEN</b>\n"
        f"{bias_line}"
        f"{action_line}"
        f"{open_section}"

        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🤖 Le Grinder  ·  Autonomous US Equity Bot  ·  Next report: 4:00 PM ET</i>"
    )


def build_weekly_report() -> str:
    """Weekly Quant Summary — sent every Friday at 5 PM ET."""
    stats = _get_trade_stats()
    ledger = []
    if PNL_LEDGER.exists():
        try:
            ledger = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass

    now = now_et()
    # Week window: Mon 00:00 → Sun 23:59 of the current calendar week
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=6, hours=23, minutes=59)

    week_trades = [
        t for t in ledger
        if _parse_ts(str(t.get("time", ""))) is not None
        and _parse_ts(str(t.get("time", ""))) >= week_start
    ]

    weekly_pnl  = sum(t.get("realizedPnl", 0) for t in week_trades)
    weekly_cost = sum(t.get("buyCost", 0) for t in week_trades)
    weekly_pct  = (weekly_pnl / weekly_cost * 100) if weekly_cost else 0.0
    weekly_wins = sum(1 for t in week_trades if t.get("realizedPnl", 0) > 0)
    weekly_loss = sum(1 for t in week_trades if t.get("realizedPnl", 0) < 0)
    weekly_flat = len(week_trades) - weekly_wins - weekly_loss
    weekly_wr   = (weekly_wins / len(week_trades) * 100) if week_trades else 0.0
    avg_pnl     = (weekly_pnl / len(week_trades)) if week_trades else 0.0

    best  = max(week_trades, key=lambda t: t.get("realizedPnl", 0), default=None)
    worst = min(week_trades, key=lambda t: t.get("realizedPnl", 0), default=None)

    # Most traded symbol
    from collections import Counter
    sym_counts = Counter(t.get("symbol", "") for t in week_trades)
    top_sym, top_count = sym_counts.most_common(1)[0] if sym_counts else ("—", 0)

    w_color  = _pnl_color(weekly_pnl)
    at_color = _pnl_color(stats["total_pnl"])
    all_wr   = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0

    # ── Live balance ────────────────────────────────────────────────────────
    live_bal = None
    try:
        live_bal = fetch_live_balance(retries=2)
    except Exception:
        pass

    sb = stats.get("starting_balance") or 0.0
    if live_bal is not None:
        equity_line = f"  💵 Account:  <b>${live_bal:.2f} USD</b>"
        if sb:
            delta     = live_bal - sb
            delta_pct = delta / sb * 100
            equity_line += f"  {'📈' if delta >= 0 else '📉'} <b>{delta_pct:+.1f}%</b> vs session open"
        equity_line += "\n"
    else:
        equity_line = ""

    # ── Per-trade lines grouped by day ──────────────────────────────────────
    day_abbr = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    trade_lines = ""
    for t in week_trades:
        pnl  = t.get("realizedPnl", 0)
        cost = t.get("buyCost", 0) or 1
        pct  = pnl / cost * 100
        ic   = _pnl_color(pnl)
        ts   = _parse_ts(str(t.get("time", "")))
        day  = day_abbr.get(ts.weekday(), "???") if ts else "???"
        trade_lines += (
            f"  {ic} <b>{day}</b>  <code>{t['symbol']}</code>"
            f"  <b>{pct:+.1f}%</b>  (${pnl:+.2f})\n"
        )
    if not trade_lines:
        trade_lines = "  <i>No closed trades this week.</i>\n"

    # ── Best / worst ────────────────────────────────────────────────────────
    best_line  = (
        f"  🏆 Best:   <code>{best['symbol']}</code>  ${best.get('realizedPnl', 0):+.2f}\n"
        if best else ""
    )
    worst_line = (
        f"  💔 Worst:  <code>{worst['symbol']}</code>  ${worst.get('realizedPnl', 0):+.2f}\n"
        if worst else ""
    )

    # ── Open position ───────────────────────────────────────────────────────
    open_section = ""
    if POS_FILE.exists():
        try:
            pos   = json.loads(POS_FILE.read_text())
            sym   = pos["symbol"]
            entry = float(pos.get("buyPrice", 0))
            sh    = float(pos.get("shares", 0))
            fi    = yf.Ticker(sym).fast_info
            price = float(fi.last_price or entry)
            upct  = (price - entry) / entry * 100 if entry else 0
            upnl  = (price - entry) * sh
            ic2   = "📈" if upct >= 0 else "📉"
            is_ah = bool(pos.get("afterHours")) or pos.get("strategyName", "") in ("After-Hours Limit", "Pre-Market Limit")
            if is_ah:
                _next_sell_dt = _next_weekday_buy()
                sell_note = f"9:35 AM ET {_next_sell_dt:%a %b %d}"
            else:
                sell_note = "3:55 PM ET"
            open_section = (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>OPEN POSITION</b>  (selling: {sell_note})\n"
                f"  {ic2} <code>{sym}</code>  {sh:.0f} sh @ ${entry:.2f}"
                f"  →  ${price:.2f}  <b>{upct:+.1f}%</b>  (${upnl:+.2f} unrealized)\n"
            )
        except Exception:
            pass

    flat_str = f" / {weekly_flat}flat" if weekly_flat else ""
    return (
        f"🗓️ <b>LE GRINDER — Weekly Quant Summary</b>\n"
        f"Week of {week_start:%b %d} – {week_end:%b %d, %Y}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💹 <b>WEEKLY PERFORMANCE</b>\n"
        f"{equity_line}"
        f"  {w_color} Week P&L:  <b>${weekly_pnl:+.2f} USD</b>  ({weekly_pct:+.1f}%)\n"
        f"  {at_color} All-time P&L:  <b>${stats['total_pnl']:+.2f} USD</b>  ({stats['total_pnl_pct']:+.1f}% ROI)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>TRADE LOG  •  {len(week_trades)} trades</b>"
        f"  |  🏆 <b>{weekly_wins}W/{weekly_loss}L{flat_str}</b>  ({weekly_wr:.0f}% win rate)\n"
        f"{trade_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>WEEK STATS</b>\n"
        f"{best_line}"
        f"{worst_line}"
        f"  📈 Avg P&L/trade:  <b>${avg_pnl:+.2f}</b>\n"
        f"  ⚡ Most traded:  <code>{top_sym}</code>  ({top_count}×)\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>ALL TIME  •  {stats['count']} trades</b>\n"
        f"  {at_color} P&L:  <b>${stats['total_pnl']:+.2f} USD</b>\n"
        f"  🏆 <b>{stats['wins']}W / {stats['losses']}L</b>  —  <b>{all_wr:.0f}% win rate</b>"
        f"{open_section}"
    )


def build_sell_message(
    symbol: str, entry: float, exit_price: float, shares: float,
    cost: float, trade_pnl: float, at_pnl: float,
    sell_label: str = "",
) -> str:
    pnl_pct    = trade_pnl / cost * 100 if cost else 0.0
    proceeds   = shares * exit_price
    color      = _pnl_color(trade_pnl)
    at_color   = _pnl_color(at_pnl)
    stats      = _get_trade_stats()
    win_rate   = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0
    label_line = f"  ⏰ {sell_label}\n" if sell_label else ""

    return (
        f"🏁 <b>SOLD — <code>{symbol}</code></b>\n\n"
        f"{label_line}"
        f"  {color} <b>{pnl_pct:+.1f}%</b>  (${trade_pnl:+.2f})\n"
        f"  Entry: ${entry:.2f}  →  Exit: ${exit_price:.2f}"
        f"  |  {shares:.0f} sh  |  cost ${cost:.2f}\n\n"
        f"<b>ALL TIME  •  {stats['count']} trades</b>\n"
        f"  {at_color} P&L: <b>${at_pnl:+.2f} USD</b>  ({stats['total_pnl_pct']:+.1f}% ROI)\n"
        f"  🏆 <b>{stats['wins']}W / {stats['losses']}L</b>"
        f"  —  {win_rate:.0f}% win rate\n"
        f"  {'🚀 GREEN — keep grinding!' if at_pnl >= 0 else '💪 RED — grind it back!'}"
    )


def build_rapport_live() -> str:
    """Template-style rapport live — sent on startup and every 8h."""
    now = now_et()
    stats = _get_trade_stats()
    ledger: list = []
    if PNL_LEDGER.exists():
        try:
            ledger = json.loads(PNL_LEDGER.read_text())
        except Exception:
            pass

    today_str    = now.strftime("%Y-%m-%d")
    today_trades = [t for t in ledger if str(t.get("time", "")).startswith(today_str)]
    daily_pnl    = sum(t.get("realizedPnl", 0) for t in today_trades)
    daily_wins   = sum(1 for t in today_trades if t.get("realizedPnl", 0) > 0)
    daily_loss   = sum(1 for t in today_trades if t.get("realizedPnl", 0) < 0)

    live_bal = None
    try:
        live_bal = fetch_live_balance(retries=2)
    except Exception:
        pass

    at_pnl  = stats["total_pnl"]
    capital = live_bal if live_bal is not None else ((stats.get("starting_balance") or 0) + at_pnl)
    initial = max(0.01, capital - at_pnl)
    gain_pct  = at_pnl / initial * 100 if initial > 0 else 0.0
    cap_color = "🟢" if at_pnl >= 0 else "🔴"
    cap_line  = (
        f"  {cap_color} Capital : <b>${capital:.2f}</b>"
        f"  ({at_pnl:+.2f}$, {gain_pct:+.1f}% depuis le début)"
    )

    trade_lines = ""
    for t in today_trades:
        pnl     = t.get("realizedPnl", 0)
        cost    = t.get("buyCost", 0) or 1
        sell    = t.get("sellValue", 0)
        qty     = t.get("quantity", 0) or 1
        pct     = pnl / cost * 100
        buy_px  = cost / qty
        sell_px = sell / qty
        ic      = "🟢" if pnl >= 0 else "🔴"
        trade_lines += (
            f"  {ic} {pnl:+.2f}$ ({pct:+.1f}%)"
            f"  <code>{t['symbol']}</code>  ${buy_px:.2f} → ${sell_px:.2f}\n"
        )
    if not trade_lines:
        trade_lines = "  <i>Aucun trade fermé aujourd'hui.</i>\n"

    open_lines  = "  <i>Aucune position ouverte.</i>\n"
    latent_line = ""
    n_open      = 0
    if POS_FILE.exists():
        try:
            pos  = json.loads(POS_FILE.read_text())
            sym  = pos.get("symbol", "")
            if sym:
                entry  = float(pos.get("buyPrice", 0))
                sh     = float(pos.get("shares", 0))
                cost_p = float(pos.get("estimatedCost", 0)) or (entry * sh)
                live_p = None
                try:
                    ticker = yf.Ticker(sym)
                    fi     = ticker.fast_info
                    live_p = float(fi.last_price or 0) or None
                    # Prefer post/pre-market price when available (AH/PM sessions)
                    try:
                        info    = ticker.info
                        pm_px   = float(info.get("postMarketPrice") or 0)
                        pre_px  = float(info.get("preMarketPrice") or 0)
                        ext_px  = pm_px or pre_px
                        if ext_px > 0:
                            live_p = ext_px
                    except Exception:
                        pass
                except Exception:
                    pass
                price   = live_p or entry
                upnl    = (price - entry) * sh
                upct    = (price - entry) / entry * 100 if entry else 0
                cur_val = sh * price
                n_open  = 1
                open_lines  = (
                    f"  ⚪ <b>{sym}</b> ({sh:.0f} sh) :"
                    f" ${entry:.2f} → ${price:.2f}"
                    f"  |  ${cost_p:.2f} → ${cur_val:.2f}"
                    f"  ({upnl:+.2f}$, {upct:+.1f}%)\n"
                )
                latent_line = f"  Latent : {upnl:+.2f}$\n"
        except Exception:
            pass

    _months_fr = [
        "janv.", "févr.", "mars", "avr.", "mai", "juin",
        "juill.", "août", "sept.", "oct.", "nov.", "déc.",
    ]
    date_str = f"{now.day} {_months_fr[now.month - 1]} {now.year} {now:%H:%M} ET"
    d_sign   = "+" if daily_pnl >= 0 else ""

    return (
        f"RAPPORT LIVE — {date_str}\n"
        f"Le Grinder · WEALTHSIMPLE BOT\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"PROFITS &amp; PERTES :\n"
        f"{cap_line}\n\n"
        f"TRADES DU JOUR"
        f" (Total : {len(today_trades)}, Réussis : {daily_wins},"
        f" Ratés : {daily_loss},"
        f" Gains du jour : {d_sign}${abs(daily_pnl):.2f})\n"
        f"{trade_lines}\n"
        f"POSITIONS OUVERTES ({n_open}) :\n"
        f"{open_lines}"
        f"{latent_line}\n"
        f"Le Grinder · WEALTHSIMPLE BOT"
    )


def _send_rapport_live() -> None:
    global _last_rapport_t
    try:
        msg = build_rapport_live()
        send_message(msg)
        _last_rapport_t = time.time()
        log("  → Rapport LIVE envoyé.")
    except TelegramConfigError as exc:
        log(f"  Telegram non configuré: {exc}")
    except Exception as exc:
        log(f"  Rapport LIVE échoué: {exc}")


def _combined_report() -> None:
    """Send top picks then rapport live — fires every 3 hours."""
    global _last_combined_t
    if time.time() - _last_combined_t < _REPORT_INTERVAL_SECS:
        return
    _last_combined_t = time.time()
    label = now_et().strftime("%Hh%M ET")
    log(f"Rapport combiné — {label}...")
    _send_top_picks(label)
    _send_rapport_live()


def build_top_picks_message(label: str) -> str | None:
    """French top-3 picks message — sent at 6 AM and 4 PM ET every weekday."""
    state = _load_scan_state()
    raw_picks = state.get("picks", [])
    picks: list[GrinderPick] = []
    for raw in (raw_picks if isinstance(raw_picks, list) else []):
        p = _pick_from_dict(raw)
        if p:
            picks.append(p)

    if not picks:
        return None

    try:
        bias = FuturesBias(state.get("bias", "neutral"))
    except Exception:
        bias = FuturesBias.NEUTRAL

    bias_emoji = {"green": "🟢 VERT", "red": "🔴 ROUGE", "neutral": "⚪ NEUTRE"}[bias.value]
    medals = ["1️⃣", "2️⃣", "3️⃣"]
    lines = []
    for i, p in enumerate(picks[:3]):
        conf = "🔥" if p.score >= 80 else ("⚡" if p.score >= 50 else "📊")
        why  = _pick_why(p)
        lines.append(
            f"{medals[i]} {conf} <code>{p.symbol}</code>  <b>${p.last_close:.2f}</b>  [score {p.score:.0f}]\n"
            f"   {why}"
        )

    return (
        f"💸 <b>TOP 3 DU JOUR — {label}</b>\n\n"
        f"Si j'avais du cash, j'investirais dans :\n\n"
        + "\n\n".join(lines)
        + f"\n\n📡 Futures : <b>{bias_emoji}</b>\n"
        f"<i>Le Grinder · NYSE/NASDAQ</i>"
    )


def _send_top_picks(label: str) -> None:
    try:
        msg = build_top_picks_message(label)
        if msg:
            send_message(msg)
            log(f"  → Top picks envoyés ({label}).")
        else:
            log(f"  Pas de picks disponibles pour {label}.")
    except TelegramConfigError as exc:
        log(f"  Telegram non configuré: {exc}")
    except Exception as exc:
        log(f"  Top picks échoué ({label}): {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Timing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sleep_until(target: datetime, label: str) -> None:
    secs = (target - now_et()).total_seconds()
    if secs > 1:
        log(f"Sleeping {secs/60:.1f} min until {label} ({target:%H:%M} ET)...")
        time.sleep(secs)


def _passed_today(hour: int, minute: int = 0) -> bool:
    now = now_et()
    return now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def wait_for_buy_window(
    bias: FuturesBias,
    pick: GrinderPick | None,
    futures_detail: str,
) -> bool:
    """Block until 9:31 AM ET, or catch up immediately later that morning."""
    now = now_et()
    target = now.replace(hour=_BUY_HOUR, minute=_BUY_MINUTE, second=0, microsecond=0)
    cutoff = now.replace(hour=_BUY_CATCHUP_HOUR, minute=_BUY_CATCHUP_MINUTE, second=0, microsecond=0)

    if now > cutoff:
        log(f"Buy window ({_BUY_HOUR:02d}:{_BUY_MINUTE:02d}-{_BUY_CATCHUP_HOUR:02d}:{_BUY_CATCHUP_MINUTE:02d}) already passed.")
        return False
    if now < target:
        _sleep_until(target, "9:31 AM buy window")
    else:
        log(f"Buy target was {_BUY_HOUR:02d}:{_BUY_MINUTE:02d}; catching up immediately at {now:%H:%M} ET.")
    return True

# ──────────────────────────────────────────────────────────────────────────────
# Buy
# ──────────────────────────────────────────────────────────────────────────────

def execute_buy(pick: GrinderPick, balance: float, bias: FuturesBias,
                futures_detail: str, ai_analysis: str,
                fixed_shares: int | None = None) -> bool:
    is_bounce = (bias == FuturesBias.RED)
    deploy    = balance * _DEPLOY_PCT / 100
    shares_est = int(deploy // pick.last_close) if pick.last_close > 0 else 0
    use_shares = fixed_shares or shares_est

    # Skip stocks we can't afford even 1 whole share of (WS won't do fractional for all tickers)
    if shares_est == 0 and not fixed_shares:
        log(f"Skipping {pick.symbol}: price ${pick.last_close:.2f} > deploy ${deploy:.2f} — can't buy 1 share")
        return False

    notify(build_buy_message(pick, balance, bias, futures_detail, ai_analysis, is_bounce))
    log(f"Placing buy: {pick.symbol}  ~{use_shares} sh @ ~${pick.last_close:.2f}")

    if fixed_shares:
        result = subprocess.run(
            [PYTHON, str(AUTO_SCRIPT), "buy", "--symbol", pick.symbol, "--shares", str(fixed_shares)],
            cwd=ROOT, capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            [PYTHON, str(AUTO_SCRIPT), "buy", "--symbol", pick.symbol, "--max-dollars"],
            cwd=ROOT, capture_output=True, text=True,
        )
    for line in result.stdout.splitlines():
        print(f"  {line}", flush=True)

    if result.returncode != 0:
        combined = result.stdout + result.stderr
        if "session expired" in combined.lower() or "log in" in combined.lower():
            notify(
                "❌ <b>Wealthsimple session expired</b>\n\n"
                "<code>python scripts/wealthsimple_auto.py setup</code>\n\n"
                "Log in, then restart the bot."
            )
            log("SESSION EXPIRED — run: python scripts/wealthsimple_auto.py setup")
        else:
            notify(f"❌ <b>Buy FAILED</b> for <code>{pick.symbol}</code> — check logs.")
        return False

    order = _parse_order_result(result.stdout)
    if not order.get("submitted"):
        notify(f"❌ <b>Buy FAILED</b> for <code>{pick.symbol}</code> — order not submitted.")
        log(f"Buy failed for {pick.symbol}: order not submitted.")
        return False

    # Use actual fill data from WS if available, else compute from estimates
    if "fill_price" in order:
        actual_buy_price = float(order["fill_price"])
        actual_qty       = float(order.get("fill_quantity", 0))
        actual_cost      = float(order.get("fill_value", 0))
        if not actual_qty or not actual_cost:
            actual_buy_price = pick.last_close
            actual_cost = float(order.get("estimated_value", deploy) or deploy)
            actual_qty  = float(order.get("estimated_quantity") or
                                (actual_cost / pick.last_close if pick.last_close else shares_est))
    else:
        actual_cost = float(order.get("estimated_value", deploy) or deploy)
        actual_qty  = float(order.get("estimated_quantity") or
                            (actual_cost / pick.last_close if pick.last_close else shares_est))
        actual_buy_price = actual_cost / actual_qty if actual_qty else pick.last_close

    profit_target = _dynamic_profit_target(pick.atr_pct)
    pos = {
        "symbol": pick.symbol, "buyPrice": round(actual_buy_price, 4),
        "shares": actual_qty, "estimatedCost": actual_cost,
        "sellAll": True, "strategyName": pick.strategy_name,
        "time": now_et().isoformat(),
        "profitTargetPct": profit_target,
        "atrPct": round(pick.atr_pct, 2),
    }
    POS_FILE.write_text(json.dumps(pos))
    _append_trade_history(pick.symbol, "BUY", actual_buy_price, actual_qty,
                          actual_cost, 0.0, pick.strategy_name)

    at_pnl  = _get_total_pnl()
    at_color = _pnl_color(at_pnl)
    notify(
        f"✅ <b>Buy order submitted</b>\n\n"
        f"🎫 <code>{pick.symbol}</code>\n"
        f"🔢 Shares: <b>{actual_qty:.4f}</b>  |  💵 Entry: <b>${actual_buy_price:.2f} USD</b>\n"
        f"💰 Invested: <b>${actual_cost:.2f} USD</b>\n"
        f"🎯 Target: <b>+{profit_target:.1f}%</b>  (ATR {pick.atr_pct:.1f}%)  |  No stop loss\n\n"
        f"{at_color} All-time PnL: <b>${at_pnl:+.2f} USD</b>"
    )
    log(f"Buy confirmed: {actual_qty:.4f} sh @ ${actual_buy_price:.2f}  cost ${actual_cost:.2f}  target +{profit_target:.1f}%")
    return True


def wait_for_fill_confirm(symbol: str) -> None:
    now = now_et()
    confirm_t = now.replace(hour=9, minute=45, second=0, microsecond=0)
    secs = (confirm_t - now).total_seconds()
    if secs > 0:
        log(f"Pre-market order queued — waiting {secs/60:.1f} min for 9:45 fill check...")
        time.sleep(secs)
    log("Confirming fill at 9:45 AM...")

    # Refresh position cost from Wealthsimple — the limit order may have
    # filled at a different price than the limit we submitted.
    fill = fetch_position_details(symbol)
    if fill and fill.get("fill_price") and POS_FILE.exists():
        try:
            pos = json.loads(POS_FILE.read_text())
            old_entry = float(pos.get("buyPrice", 0))
            pos["buyPrice"] = round(fill["fill_price"], 4)
            pos["shares"] = fill["fill_quantity"]
            pos["estimatedCost"] = fill["fill_value"]
            pos["_costRefreshed"] = True
            POS_FILE.write_text(json.dumps(pos, indent=2, default=str))
            log(f"  Updated cost basis: ${old_entry:.4f} → ${fill['fill_price']:.4f}  (was estimated/limit price)")
        except Exception as exc:
            log(f"  Could not update position cost: {exc}")
    elif POS_FILE.exists():
        pos = json.loads(POS_FILE.read_text())
        yf_result = _refresh_position_cost_from_yf(symbol, pos)
        if yf_result is not None:
            pos.update(yf_result)
            pos["_costRefreshed"] = True
            POS_FILE.write_text(json.dumps(pos, indent=2, default=str))
            log(f"  Cost refreshed via yfinance fallback")

    balance = fetch_live_balance(retries=2)
    bal_str = f"${balance:.2f} USD" if balance else "N/A"
    notify(
        f"✅ <b>Graphite order filled!</b>\n\n"
        f"🎫 <code>{symbol}</code>  filled at market open\n"
        f"💰 Live balance: <b>{bal_str}</b>\n"
        f"⏰ Monitoring until <b>3:55 PM ET</b>  |  Updates every 30 min"
    )


def wait_after_pick(pick: GrinderPick, bias: FuturesBias, futures_detail: str) -> None:
    if _BUY_DELAY_MINUTES <= 0:
        return
    delay_secs = _BUY_DELAY_MINUTES * 60
    notify(
        f"🕒 <b>Pick locked</b>\n\n"
        f"🎫 <code>{pick.symbol}</code>\n"
        f"🏢 {_company_line(pick.symbol)}\n"
        f"⏳ Buying in <b>{_BUY_DELAY_MINUTES} min</b> after confirmation\n"
        f"📡 {_bias_line(bias, futures_detail)}"
    )
    log(f"Waiting {_BUY_DELAY_MINUTES} min after pick selection before buy...")
    time.sleep(delay_secs)


# ──────────────────────────────────────────────────────────────────────────────
# After-hours / extended-hours trading
# ──────────────────────────────────────────────────────────────────────────────

def _is_afterhours_window() -> bool:
    """True if we're in the weekday 4:00 PM – 7:57 PM ET after-hours window."""
    n = now_et()
    if n.weekday() >= 5:
        return False
    after_open  = n.hour >= _AH_BUY_START_HOUR
    before_close = n.hour < _AH_BUY_END_HOUR or (
        n.hour == _AH_BUY_END_HOUR and n.minute < _AH_BUY_END_MINUTE
    )
    return after_open and before_close


def _is_premarket_window() -> bool:
    """True if we're in the weekday 7:00 AM – 9:29 AM ET pre-market window."""
    n = now_et()
    if n.weekday() >= 5:
        return False
    after_open = n.hour >= _PM_BUY_START_HOUR
    before_close = n.hour < _PM_BUY_END_HOUR or (
        n.hour == _PM_BUY_END_HOUR and n.minute < _PM_BUY_END_MINUTE
    )
    return after_open and before_close


def _scan_afterhours(watchlist: list[str], min_pct: float = _AH_MIN_PCT) -> list[dict]:
    """
    Scan top tickers for after-hours momentum.
    Score = pct^1.5 * vol_bonus — superlinear so big movers on big volume dominate.
    """
    import yfinance as yf
    picks = []
    # If min_pct <= 0, we are in fallback mode — expand search to 250 tickers to ensure we find one
    limit = 250 if min_pct <= 0 else _AH_WATCHLIST_SIZE
    subset = watchlist[:limit]
    log(f"After-hours scan: checking {len(subset)} tickers (min {min_pct:.1f}%)...")
    for sym in subset:
        try:
            fi    = yf.Ticker(sym).fast_info
            close = fi.previous_close
            last  = fi.last_price
            if not close or not last or close <= 0 or last < 0.50:
                continue
            ah_pct = (last / close - 1) * 100
            if ah_pct < min_pct:
                continue
            
            # HARD FILTER: No fractional shares in PM/AH on Wealthsimple.
            # Must be < $10 to ensure we can actually buy enough whole shares.
            if last >= 10.0:
                continue
            
            reg_vol = getattr(fi, "regular_market_volume", 0) or 0
            avg_vol = getattr(fi, "three_month_average_volume", 0) or 0
            vol_ratio = (reg_vol / avg_vol) if avg_vol > 0 else 1.0
            vol_bonus = min(max(vol_ratio, 0.5), 4.0)
            score = (max(ah_pct, 0) ** 1.5) * vol_bonus
            picks.append({
                "symbol":    sym,
                "close":     round(close, 4),
                "ah_price":  round(last, 4),
                "ah_pct":    round(ah_pct, 2),
                "vol_ratio": round(vol_ratio, 1),
                "score":     round(score, 3),
            })
        except Exception:
            pass
    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks


def _afterhours_buy(pick: dict, balance: float) -> bool:
    """Place a limit buy order during extended hours for the given AH pick."""
    sym        = pick["symbol"]
    ah_price   = pick["ah_price"]
    limit_price = round(ah_price * (1 + _AH_LIMIT_PREMIUM), 2)
    shares_est  = max(1, int(balance / limit_price))

    notify(
        f"🌙 <b>After-hours limit BUY — <code>{sym}</code></b>\n\n"
        f"📈 AH gain: <b>+{pick['ah_pct']:.2f}%</b>  from close ${pick['close']:.2f}\n"
        f"💵 Current AH price: ${ah_price:.2f}  |  Limit: <b>${limit_price:.2f}</b>\n"
        f"📦 ~{shares_est} shares  |  💰 ~${balance:.0f} USD\n"
        f"🎯 AH profit target: +{_AH_PROFIT_PCT:.0f}%  |  Fallback: sell at 9:35 AM"
    )

    buy_result = subprocess.run(
        [
            PYTHON, str(AUTO_SCRIPT), "buy",
            "--symbol", sym,
            "--shares", str(shares_est),
            "--price",  f"{limit_price:.2f}",
        ],
        capture_output=True, text=True, timeout=180,
    )
    for line in buy_result.stdout.splitlines():
        print(f"  {line}", flush=True)

    order_data = _parse_order_result(buy_result.stdout)
    if not order_data.get("submitted"):
        log(f"AH buy failed for {sym}: order not submitted.")
        notify(f"❌ After-hours buy failed for <code>{sym}</code>.")
        return False

    # Use actual fill data from WS if available
    if "fill_price" in order_data:
        actual_price   = float(order_data["fill_price"])
        actual_shares  = float(order_data.get("fill_quantity", shares_est))
        actual_value   = float(order_data.get("fill_value", actual_shares * actual_price))
    else:
        # Use the AH price from the scan — fi.last_price returns regular session close in extended hours
        actual_price  = ah_price
        actual_shares = float(shares_est)
        actual_value  = actual_shares * actual_price

    pos = {
        "symbol":        sym,
        "buyPrice":      round(actual_price, 4),
        "shares":        actual_shares,
        "estimatedCost": actual_value,
        "sellAll":       True,
        "strategyName":  "After-Hours Limit",
        "afterHours":    True,
        "time":          now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos, indent=2))
    _append_trade_history(sym, "BUY", actual_price, actual_shares, actual_value, 0.0, "After-Hours Limit")

    log(f"AH buy confirmed: {actual_shares:.4f} sh {sym} @ ${actual_price:.4f}  cost ${actual_value:.2f}")
    notify(
        f"✅ <b>AH buy confirmed — <code>{sym}</code></b>\n\n"
        f"💰 {actual_shares:.4f} sh @ ${actual_price:.2f}  (cost ${actual_value:.2f})\n"
        f"🎯 Profit target: +{_AH_PROFIT_PCT:.0f}%  |  Exits at 9:35 AM if not hit"
    )
    return True


def _afterhours_sell_limit(*args, **kwargs) -> bool:
    """DISABLED — never limit sell in extended hours. Wait for 9:31 AM market open."""
    log("_afterhours_sell_limit called but is permanently disabled — no limit sells in extended hours.")
    return False


def _afterhours_hold_loop(symbol: str, entry: float, shares: float,
                           cost: float, strat: str) -> None:
    """
    Monitor an after-hours position every 10 min.
    Sell immediately at +3% via limit order, else hold to 7:45 PM then overnight.
    """
    import yfinance as yf

    log(f"AH hold loop: {symbol}  {shares:.4f} sh @ ${entry:.2f}")
    notify(
        f"📊 <b>After-hours position</b> — <code>{symbol}</code>\n\n"
        f"💰 {shares:.4f} sh @ ${entry:.2f}  |  cost ${cost:.2f}\n"
        f"🎯 AH profit target: +{_AH_PROFIT_PCT:.0f}%  |  Auto-sell at 7:45 PM or 9:35 AM"
    )

    last_ah_notify_t = 0.0
    _AH_NOTIFY_INTERVAL = 600  # Telegram every 10 min; price check every 60s
    while True:
        n = now_et()
        # At 7:45 PM → AH window closing, hold overnight and let 9:31 AM handle it
        past_ah_close = n.hour > 19 or (n.hour == 19 and n.minute >= 45)
        if past_ah_close:
            log(f"AH window closed — {symbol} held overnight, 9:31 AM decision tomorrow")
            notify(
                f"🌙 <b>AH window closed — holding overnight</b>\n\n"
                f"🎫 <code>{symbol}</code>  @ ${entry:.2f}\n"
                f"📋 No stop loss — morning decision at <b>9:31 AM ET</b>"
            )
            return

        # Price check every 60s; Telegram update every 10 min
        try:
            fi    = yf.Ticker(symbol).fast_info
            price = fi.last_price
            if price and entry > 0:
                pnl_pct = (price - entry) / entry * 100
                if pnl_pct >= _AH_PROFIT_PCT:
                    log(f"AH PROFIT TARGET: {pnl_pct:+.1f}% — no limit sells in extended hours, waiting for 9:31 AM market open")
                    notify(
                        f"🎯 <b>AH PROFIT TARGET HIT — <code>{symbol}</code></b>\n\n"
                        f"📈 {pnl_pct:+.1f}%  |  AH price: ${price:.2f}\n"
                        f"⏳ No limit sells in extended hours — holding until 9:31 AM market open sell"
                    )
                    return
                if time.time() - last_ah_notify_t >= _AH_NOTIFY_INTERVAL:
                    log(f"AH update: {symbol}  ${price:.2f}  ({pnl_pct:+.2f}%)")
                    last_ah_notify_t = time.time()
        except Exception as exc:
            log(f"AH price check error: {exc}")

        time.sleep(60)  # check every 60s — was 600s


def _run_afterhours_strategy(balance: float, sell_existing: bool = False) -> None:
    """
    Full after-hours routine:
    1. If sell_existing → skip (no limit sells in extended hours, wait for 9:31 AM market open)
    2. Scan for best AH mover, place limit buy.
    3. Monitor with _afterhours_hold_loop().
    """
    if not _is_afterhours_window():
        log("Not in AH window — skipping after-hours strategy.")
        return

    # ── Step 1: skip if position exists — no limit sells in extended hours ──
    if sell_existing and POS_FILE.exists():
        try:
            pos    = json.loads(POS_FILE.read_text())
            sym    = pos["symbol"]
            log(f"Existing position {sym} — no limit sells in extended hours. Will sell at 9:31 AM market open.")
            notify(
                f"⏳ <b>AH window — holding <code>{sym}</code></b>\n\n"
                f"📋 No limit sells in extended hours\n"
                f"🔄 Will sell at <b>9:31 AM ET</b> market open\n"
                f"🌙 Skipping AH rotation today"
            )
            return
        except Exception as exc:
            log(f"AH sell step error: {exc}")

    # ── Step 2: scan for best AH mover ────────────────────────────────────
    if POS_FILE.exists():
        log("Existing position still open (will sell at 9:31 AM) — "
            "proceeding to AH buy scan with available cash.")

    # Use shortlist from yesterday for better accuracy
    scan_symbols, _ = _choose_scan_symbols()
    picks = _scan_afterhours(scan_symbols)
    if not picks:
        log(f"No AH picks at {_AH_MIN_PCT:.1f}% — trying best-effort (any positive AH mover)...")
        picks = _scan_afterhours(scan_symbols, min_pct=0.0)

    if not picks:
        log("No positive AH movers found — holding cash until market open.")
        notify(
            f"🌙 <b>After-hours scan complete</b>\n\n"
            f"No positive AH movers found — holding cash.\n"
            f"📅 Next entry: <b>9:35 AM ET tomorrow</b>"
        )
        return

    top = picks[:5]
    top_str = "\n".join(
        f"  • <code>{p['symbol']}</code>  AH:<b>+{p['ah_pct']:.2f}%</b>"
        f"  vol:{p.get('vol_ratio', 1.0):.1f}x  ${p['ah_price']:.2f}  [score:{p['score']:.0f}]"
        for p in top
    )
    log(f"AH top picks: {[p['symbol'] for p in top]}")
    notify(
        f"🔍 <b>After-hours scan results</b>\n\n"
        f"{top_str}\n\n"
        f"🛒 Buying top pick: <b>{top[0]['symbol']}</b>"
    )

    bought = _afterhours_buy(top[0], balance)
    if not bought:
        return

    # ── Step 3: monitor the AH position ───────────────────────────────────
    pos    = json.loads(POS_FILE.read_text())
    entry  = float(pos.get("buyPrice", top[0]["ah_price"]))
    shares = float(pos.get("shares", 1))
    cost   = float(pos.get("estimatedCost", balance))
    _afterhours_hold_loop(top[0]["symbol"], entry, shares, cost, "After-Hours Limit")


# ──────────────────────────────────────────────────────────────────────────────
# Pre-market / early-morning extended-hours trading
# ──────────────────────────────────────────────────────────────────────────────

def _scan_premarket(watchlist: list[str], min_pct: float = _PM_MIN_PCT) -> list[dict]:
    """
    Scan top tickers for pre-market momentum (7:00-9:29 AM ET).
    Score = pct^1.5 * vol_bonus — superlinear so big movers on big volume dominate.
    """
    import yfinance as yf
    picks = []
    # If min_pct <= 0, we are in fallback mode — expand search to 250 tickers to ensure we find one
    limit = 250 if min_pct <= 0 else _PM_WATCHLIST_SIZE
    subset = watchlist[:limit]
    log(f"Pre-market scan: checking {len(subset)} tickers (min {min_pct:.1f}%)...")
    for sym in subset:
        try:
            fi    = yf.Ticker(sym).fast_info
            close = fi.previous_close
            last  = fi.last_price
            if not close or not last or close <= 0 or last < 0.50:
                continue
            pm_pct = (last / close - 1) * 100
            if pm_pct < min_pct:
                continue
            
            # HARD FILTER: No fractional shares in PM/AH on Wealthsimple.
            # Must be < $10 to ensure we can actually buy enough whole shares with available balance.
            if last >= 10.0:
                continue
            
            reg_vol = getattr(fi, "regular_market_volume", 0) or 0
            avg_vol = getattr(fi, "three_month_average_volume", 0) or 0
            vol_ratio = (reg_vol / avg_vol) if avg_vol > 0 else 1.0
            vol_bonus = min(max(vol_ratio, 0.5), 4.0)
            score = (max(pm_pct, 0) ** 1.5) * vol_bonus
            picks.append({
                "symbol":    sym,
                "close":     round(close, 4),
                "pm_price":  round(last, 4),
                "pm_pct":    round(pm_pct, 2),
                "vol_ratio": round(vol_ratio, 1),
                "score":     round(score, 3),
            })
        except Exception:
            pass
    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks


def _premarket_buy(pick: dict, balance: float) -> bool:
    """Place a limit buy order during pre-market for the given PM pick."""
    sym         = pick["symbol"]
    pm_price    = pick["pm_price"]
    limit_price = round(pm_price * (1 + _PM_LIMIT_PREMIUM), 2)
    shares_est  = max(1, int(balance / limit_price))

    notify(
        f"🌅 <b>Pre-market limit BUY — <code>{sym}</code></b>\n\n"
        f"📈 PM gain: <b>+{pick['pm_pct']:.2f}%</b>  from close ${pick['close']:.2f}\n"
        f"💵 Current PM price: ${pm_price:.2f}  |  Limit: <b>${limit_price:.2f}</b>\n"
        f"📦 ~{shares_est} shares  |  💰 ~${balance:.0f} USD\n"
        f"🎯 Exit: +{_PM_PROFIT_PCT:.0f}% PM target  |  9:31 AM sell if not hit"
    )

    buy_result = subprocess.run(
        [
            PYTHON, str(AUTO_SCRIPT), "buy",
            "--symbol", sym,
            "--shares", str(shares_est),
            "--price",  f"{limit_price:.2f}",
        ],
        capture_output=True, text=True, timeout=180,
    )
    for line in buy_result.stdout.splitlines():
        print(f"  {line}", flush=True)

    order_data = _parse_order_result(buy_result.stdout)
    if not order_data.get("submitted"):
        log(f"PM buy failed for {sym}: order not submitted.")
        notify(f"❌ Pre-market buy failed for <code>{sym}</code>.")
        return False

    # Use actual fill data from WS if available
    if "fill_price" in order_data:
        actual_price   = float(order_data["fill_price"])
        actual_shares  = float(order_data.get("fill_quantity", shares_est))
        actual_value   = float(order_data.get("fill_value", actual_shares * actual_price))
    else:
        # Use the PM price from the scan — fi.last_price returns regular session close in extended hours
        actual_price  = pm_price
        actual_shares = float(shares_est)
        actual_value  = actual_shares * actual_price

    pos = {
        "symbol":        sym,
        "buyPrice":      round(actual_price, 4),
        "shares":        actual_shares,
        "estimatedCost": actual_value,
        "sellAll":       True,
        "strategyName":  "Pre-Market Limit",
        "afterHours":    True,
        "time":          now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos, indent=2))
    _append_trade_history(sym, "BUY", actual_price, actual_shares, actual_value, 0.0, "Pre-Market Limit")

    log(f"PM buy confirmed: {actual_shares:.4f} sh {sym} @ ${actual_price:.4f}  cost ${actual_value:.2f}")
    notify(
        f"✅ <b>PM buy confirmed — <code>{sym}</code></b>\n\n"
        f"💰 {actual_shares:.4f} sh @ ${actual_price:.2f}  (cost ${actual_value:.2f})\n"
        f"🎯 Profit target: +{_PM_PROFIT_PCT:.0f}%  |  Exits at 9:31 AM if not hit"
    )
    return True


def _premarket_sell_limit(*args, **kwargs) -> bool:
    """DISABLED — never limit sell in extended hours. Wait for 9:31 AM market open."""
    log("_premarket_sell_limit called but is permanently disabled — no limit sells in extended hours.")
    return False


def _premarket_hold_loop(symbol: str, entry: float, shares: float,
                         cost: float, strat: str) -> None:
    """Monitor a pre-market position until 9:29 AM or +2% hit."""
    import yfinance as yf
    log(f"PM hold loop: {symbol}  {shares:.4f} sh @ ${entry:.2f}")
    notify(
        f"📊 <b>Pre-market position</b> — <code>{symbol}</code>\n\n"
        f"💰 {shares:.4f} sh @ ${entry:.2f}  |  cost ${cost:.2f}\n"
        f"🎯 PM profit target: +{_PM_PROFIT_PCT:.0f}%  |  Auto-sell at 9:29 AM or 9:31 AM"
    )

    last_pm_notify_t = 0.0
    _PM_NOTIFY_INTERVAL = 600
    while True:
        n = now_et()
        if n.hour >= 9 and n.minute >= 29:
            log(f"PM window closing — {symbol} held until 9:31 AM decision")
            return

        try:
            fi    = yf.Ticker(symbol).fast_info
            price = fi.last_price
            if price and entry > 0:
                pnl_pct = (price - entry) / entry * 100
                if pnl_pct >= _PM_PROFIT_PCT:
                    log(f"PM PROFIT TARGET: {pnl_pct:+.1f}% — no limit sells in extended hours, waiting for 9:31 AM market open")
                    notify(
                        f"🎯 <b>PM PROFIT TARGET HIT — <code>{symbol}</code></b>\n\n"
                        f"📈 {pnl_pct:+.1f}%  |  PM price: ${price:.2f}\n"
                        f"⏳ No limit sells in extended hours — holding until 9:31 AM market open sell"
                    )
                    return
                if time.time() - last_pm_notify_t >= _PM_NOTIFY_INTERVAL:
                    log(f"PM update: {symbol}  ${price:.2f}  ({pnl_pct:+.2f}%)")
                    last_pm_notify_t = time.time()
        except Exception as exc:
            log(f"PM price check error: {exc}")

        time.sleep(60)


def _save_legacy_position(symbol: str, entry: float, shares: float,
                           cost: float, strat: str) -> None:
    """Save current position as legacy before overwriting position file."""
    legacy = {
        "symbol":   symbol,
        "entry":    entry,
        "shares":   shares,
        "cost":     cost,
        "strategy": strat,
        "saved_at": now_et().isoformat(),
    }
    LEGACY_FILE.write_text(json.dumps(legacy, indent=2))
    log(f"Saved legacy position: {symbol} {shares:.4f} sh @ ${entry:.2f}")


def _sell_legacy_position() -> bool:
    """Sell the legacy position if one exists."""
    if not LEGACY_FILE.exists():
        return True
    try:
        legacy = json.loads(LEGACY_FILE.read_text())
        sym    = legacy["symbol"]
        entry  = float(legacy["entry"])
        shares = float(legacy["shares"])
        cost   = float(legacy["cost"])
        strat  = legacy.get("strategy", "Legacy")
        log(f"Selling legacy position: {sym} {shares:.4f} sh @ ${entry:.2f}")
        notify(
            f"🔄 <b>Selling legacy position — <code>{sym}</code></b>\n\n"
            f"📋 Position from pre-market rotation needs to be closed\n"
            f"💰 {shares:.4f} sh @ ${entry:.2f}  |  cost ${cost:.2f}"
        )
        _execute_sell_order(sym, entry, shares, cost, strat, label="9:31 AM Legacy Sell")
        LEGACY_FILE.unlink(missing_ok=True)
        return True
    except Exception as exc:
        log(f"Legacy sell error: {exc}")
        LEGACY_FILE.unlink(missing_ok=True)
        return False


def _run_premarket_strategy(balance: float) -> None:
    """
    Pre-market routine: scan NASDAQ movers, buy top pick with limit order.
    Saves current position as legacy first (only after confirmed buy).
    """
    if not _is_premarket_window():
        log("Not in pre-market window — skipping.")
        return

    # Don't rotate if the existing position is itself a PM/AH buy
    # (already deployed this morning, will sell at 9:35 AM)
    if POS_FILE.exists():
        try:
            _p = json.loads(POS_FILE.read_text())
            if bool(_p.get("afterHours")) or _p.get("strategyName") in ("After-Hours Limit", "Pre-Market Limit"):
                log(f"Existing PM/AH position {_p.get('symbol')} — skipping PM rotation, holding until 9:35 AM sell")
                return
        except Exception:
            pass

    # Scan pre-market movers — use shortlist from yesterday for better accuracy
    scan_symbols, _ = _choose_scan_symbols()
    picks = _scan_premarket(scan_symbols)
    if not picks:
        log(f"No PM picks at {_PM_MIN_PCT:.1f}% — trying best-effort (any positive PM mover)...")
        picks = _scan_premarket(scan_symbols, min_pct=0.0)

    if not picks:
        log("No positive PM movers found — holding cash.")
        notify(
            f"🌅 <b>Pre-market scan complete</b>\n\n"
            f"No positive PM movers found in shortlist — holding cash.\n"
            f"📅 Next entry: <b>9:35 AM ET</b>"
        )
        return

    top = picks[:5]
    top_str = "\n".join(
        f"  • <code>{p['symbol']}</code>  PM:<b>+{p['pm_pct']:.2f}%</b>"
        f"  vol:{p.get('vol_ratio', 1.0):.1f}x  ${p['pm_price']:.2f}  [score:{p['score']:.0f}]"
        for p in top
    )
    log(f"PM top picks: {[p['symbol'] for p in top]}")
    notify(
        f"🔍 <b>Pre-market scan results</b>\n\n"
        f"{top_str}\n\n"
        f"🛒 Buying top pick: <b>{top[0]['symbol']}</b>"
    )

    # Save existing position as legacy (only if we have a confirmed buy target)
    pm_balance = balance
    if POS_FILE.exists():
        try:
            pos    = json.loads(POS_FILE.read_text())
            sym    = pos["symbol"]
            entry  = float(pos.get("buyPrice", 0))
            shares = float(pos.get("shares", 0))
            cost   = float(pos.get("estimatedCost", shares * entry))
            strat  = pos.get("strategyName", "overnight")
            _save_legacy_position(sym, entry, shares, cost, strat)
            log(f"Saved {sym} as legacy — will sell at 9:31 AM")
            # Subtract locked position value so PM buy uses only free cash
            pm_balance = max(10.0, balance - cost)
            log(f"PM buy budget: ${pm_balance:.2f} (total ${balance:.2f} minus locked ${cost:.2f})")
        except Exception as exc:
            log(f"Could not save legacy position: {exc}")

    bought = _premarket_buy(top[0], pm_balance)
    if bought:
        # monitor the PM position
        pos    = json.loads(POS_FILE.read_text())
        entry  = float(pos.get("buyPrice", top[0]["pm_price"]))
        shares = float(pos.get("shares", 1))
        cost   = float(pos.get("estimatedCost", balance))
        _premarket_hold_loop(top[0]["symbol"], entry, shares, cost, "Pre-Market Limit")
    if not bought:
        log("PM buy failed — restoring position.")
        # Remove phantom legacy if buy failed
        LEGACY_FILE.unlink(missing_ok=True)
        return

    log(f"Pre-market position active. Legacy will sell at 9:31 AM.")
    notify(
        f"🌅 <b>Pre-market position active</b>\n\n"
        f"🎫 <code>{top[0]['symbol']}</code> deployed with ${balance:.0f}\n"
        f"📋 Legacy position queued for 9:31 AM sell\n"
        f"🎯 +{_PM_PROFIT_PCT:.0f}% target during pre-market"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Sell execution helper (used for both 9:31 AM overnight sell and 3:55 PM sell)
# ──────────────────────────────────────────────────────────────────────────────

def _execute_sell_order(
    symbol: str, entry: float, shares: float, cost: float, strat: str,
    label: str = "3:55 PM ET",
) -> None:
    notify(
        f"⏳ <b>Selling at {label}</b>\n\n"
        f"🎫 <code>{symbol}</code>  |  Entry: ${entry:.2f}\n"
        f"🔢 Shares: {shares:.4f}  |  Cost: ${cost:.2f}\n"
        f"📋 Market order — selling now"
    )

    sell_result = None
    order_data: dict = {}
    order_submitted = False

    for attempt in range(1, 4):
        sell_result = subprocess.run(
            [PYTHON, str(AUTO_SCRIPT), "sell", "--symbol", symbol, "--sell-all"],
            capture_output=True, text=True, timeout=180,
        )
        for line in sell_result.stdout.splitlines():
            print(f"  {line}", flush=True)
        order_data = _parse_order_result(sell_result.stdout)
        if order_data.get("submitted"):
            order_submitted = True
        if sell_result.returncode == 0 or order_submitted:
            break
        log(f"Sell attempt {attempt}/3 failed (exit {sell_result.returncode})")
        if attempt < 3:
            notify(f"⚠️ Sell attempt {attempt}/3 failed — retrying in 60s...")
            time.sleep(60)

    sell_ok = sell_result is not None and (sell_result.returncode == 0 or order_submitted)
    if not sell_ok:
        notify(f"❌ All 3 sell attempts FAILED for <code>{symbol}</code>. Manual close required!")
        log("All sell attempts failed — manual intervention needed.")
        return

    actual_qty   = float(order_data.get("estimated_quantity") or shares)
    actual_value = float(order_data.get("estimated_value") or 0)
    if not actual_value:
        # WS didn't return sell proceeds — use live yfinance price so P&L isn't $0
        try:
            _fi = yf.Ticker(symbol).fast_info
            _last = float(_fi.last_price or 0)
            actual_value = actual_qty * (_last if _last > 0 else entry)
        except Exception:
            actual_value = actual_qty * entry
    actual_price = actual_value / actual_qty if actual_qty else entry
    trade_pnl    = actual_value - cost
    at_pnl       = _record_trade(symbol, cost, actual_value, actual_qty)

    _append_trade_history(symbol, "SELL", actual_price, actual_qty, cost, trade_pnl, strat)
    POS_FILE.unlink(missing_ok=True)

    notify(build_sell_message(symbol, entry, actual_price, actual_qty, cost, trade_pnl, at_pnl, sell_label=label))
    log(f"Closed. Trade P&L: ${trade_pnl:+.2f}  All-time: ${at_pnl:+.2f}")


def _run_overnight_scan(label: str, balance: float, scan_type: str) -> None:
    """Fire a scan during the overnight hold loop and send result to Telegram."""
    try:
        sc_syms, full_ref = _choose_scan_symbols(force_full=False)
        source = "full universe" if full_ref else f"shortlist ({len(sc_syms)})"
        log(f"Running {label} using {source}...")
        picks, scan_bias, buy_plan, strat_name, fut_det = run_scan(
            balance, scan_symbols=sc_syms, full_refresh=full_ref,
        )
        ai  = get_ai_analysis(picks, scan_bias, fut_det, balance)
        msg = build_scan_message(picks, scan_bias, fut_det, buy_plan, strat_name, balance, ai, label)
        notify(msg)
        log(f"  → {label} complete.")
    except Exception as exc:
        log(f"{label} error: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# Hold + 30-min updates + sell
# ──────────────────────────────────────────────────────────────────────────────

def _morning_hold_decision(symbol: str, entry: float, shares: float,
                           cost: float, balance: float) -> bool:
    """
    At 9:31 AM, decide: keep holding or sell and rotate.
    Returns True = keep holding (skip sell + skip new buy today).
    Returns False = sell now and let main loop find a new pick.
    Criteria: stock still above EMA20 AND smart composite score >= _MIN_SMART_HOLD_SCORE.
    AH/PM positions never reach here — they sell unconditionally at 9:35 AM.
    """
    log(f"Morning hold check for {symbol}...")
    try:
        from kzer_bot.grinder_strategy import (
            GrinderMarketData as _GMD,
            SmartGrinderStrategy as _SGS,
            SmartMarketContext as _SMC,
        )
        _md  = _GMD()
        _ctx = _SMC.load_or_fetch()
        _md.prefetch([symbol])
        snap = _md.snapshot(symbol)
        if snap is None:
            log(f"  No data for {symbol} — rotating out")
            return False
        if snap.last_close <= snap.ema20:
            log(f"  {symbol} below EMA20 — rotating out")
            notify(
                f"🔄 <b>Morning Decision — rotating out of <code>{symbol}</code></b>\n\n"
                f"📉 Stock fell below EMA20 — trend broken\n"
                f"🔍 Scanning for new pick at 9:35 AM..."
            )
            return False
        picks = _SGS(_md, _ctx).scan([symbol])
        score = picks[0].score if picks else 0
        if score >= _MIN_SMART_HOLD_SCORE:
            log(f"  {symbol} smart score {score:.1f} ≥ {_MIN_SMART_HOLD_SCORE} — HOLDING")
            pos_pct = (snap.last_close - entry) / entry * 100 if entry > 0 else 0
            notify(
                f"📊 <b>Morning Decision — holding <code>{symbol}</code></b>\n\n"
                f"🎯 Smart score: <b>{score:.1f}/100</b> — still trending\n"
                f"💼 {shares:.4f} sh @ ${entry:.2f}  |  Now ${snap.last_close:.2f}"
                f" ({pos_pct:+.1f}%)\n"
                f"✅ Holding another day — no new buy today\n"
                f"🎯 Target: +{_PROFIT_TARGET_PCT:.0f}%  |  3:55 PM lock: +{_LATE_LOCK_PCT:.0f}%"
            )
            return True
        log(f"  {symbol} score {score:.1f} < {_MIN_SMART_HOLD_SCORE} — rotating out")
        notify(
            f"🔄 <b>Morning Decision — rotating out of <code>{symbol}</code></b>\n\n"
            f"📊 Smart score: <b>{score:.1f}</b> — momentum faded\n"
            f"🔍 Scanning for new pick at 9:35 AM..."
        )
        return False
    except Exception as exc:
        log(f"  Morning hold check error: {exc} — defaulting to sell")
        return False


def hold_and_sell(balance: float = 0.0) -> None:
    if not POS_FILE.exists():
        log("No open position — nothing to hold.")
        return

    pos    = json.loads(POS_FILE.read_text())
    # Strip stale forceSell flag from old position files — PM/AH positions
    # are now handled by the is_ah_position + afterHours logic.
    if pos.pop("forceSell", None) is not None:
        POS_FILE.write_text(json.dumps(pos, indent=2, default=str))
        log("  Removed stale forceSell flag from position file")
    symbol = pos["symbol"]
    entry  = float(pos.get("buyPrice", 0))
    cost   = float(pos.get("estimatedCost", 0))
    shares = float(pos.get("shares", 0))
    strat  = pos.get("strategyName", "")
    # ATR-adaptive profit target — falls back to module default if not saved
    _profit_target_pct = float(pos.get("profitTargetPct", _PROFIT_TARGET_PCT))

    if shares < 0.01 and cost > 0 and entry > 0:
        shares = cost / entry
        log(f"Corrected share count to {shares:.4f}")

    log(f"Profit target for this position: +{_profit_target_pct:.1f}%")

    from kzer_bot.market_data import YFinanceMarketData
    md = YFinanceMarketData()

    last_update_t = 0.0
    UPDATE_INTERVAL = 8 * 3600  # 8 hours
    max_price = 0.0

    log(f"Holding {symbol}  {shares:.4f} sh @ ${entry:.2f}  (cost ${cost:.2f})")

    # ── Refresh AH/PM position cost from Wealthsimple ─────────────────────
    # PM/AH limit buys use estimated prices until the order fills at market open.
    # If the bot restarts after market open (or fill confirm was missed), fetch
    # the actual executed price from WS so the cost basis is correct.
    is_ah_position = bool(pos.get("afterHours")) or strat in ("After-Hours Limit", "Pre-Market Limit")
    if is_ah_position and not pos.get("_costRefreshed"):
        fill = fetch_position_details(symbol)
        if fill and fill.get("fill_price") and fill.get("fill_quantity"):
            try:
                old_entry = float(pos.get("buyPrice", 0))
                old_shares = float(pos.get("shares", 0))
                pos["buyPrice"] = round(fill["fill_price"], 4)
                pos["shares"] = fill["fill_quantity"]
                pos["estimatedCost"] = fill["fill_value"]
                pos["_costRefreshed"] = True
                POS_FILE.write_text(json.dumps(pos, indent=2, default=str))
                entry = fill["fill_price"]
                shares = fill["fill_quantity"]
                cost = fill["fill_value"]
                log(f"  Refreshed cost basis: ${old_entry:.4f} → ${entry:.4f}  ({old_shares:.4f} → {shares:.4f} sh)")
            except Exception as exc:
                log(f"  Cost refresh failed: {exc}")
        else:
            # Fallback: try yfinance if WS position fetch failed
            yf_result = _refresh_position_cost_from_yf(symbol, pos)
            if yf_result is not None:
                pos.update(yf_result)
                pos["_costRefreshed"] = True
                POS_FILE.write_text(json.dumps(pos, indent=2, default=str))
                entry = float(pos.get("buyPrice", entry))
                shares = float(pos.get("shares", shares))
                cost = float(pos.get("estimatedCost", cost))
                log(f"  Cost refreshed via yfinance fallback")

    now = now_et()

    # ── Overnight path: after 3:55 PM, position bought on a previous day, OR
    #    same-day AH/PM position still before its 9:35 AM sell window ──
    pos_time        = _parse_ts(pos.get("time"))
    bought_prev_day = pos_time is not None and pos_time.date() < now.date()
    # AH/PM positions sell at 9:35 AM — if we're before that, wait in the overnight path
    _ah_sell_dt     = now.replace(hour=_BUY_HOUR, minute=_BUY_MINUTE, second=0, microsecond=0)
    ah_before_sell  = is_ah_position and now < _ah_sell_dt
    is_overnight    = (
        now.hour > _SELL_HOUR or
        (now.hour == _SELL_HOUR and now.minute >= _SELL_MINUTE) or
        bought_prev_day or
        ah_before_sell
    )
    if is_overnight:
        # AH/PM positions sell at 9:35 AM (same as next buy — rotate immediately)
        _sell_hour   = _BUY_HOUR   if is_ah_position else _OVERNIGHT_SELL_HOUR
        _sell_minute = _BUY_MINUTE if is_ah_position else _OVERNIGHT_SELL_MINUTE
        next_sell = now.replace(
            hour=_sell_hour, minute=_sell_minute, second=0, microsecond=0,
        )
        # if sell time is already past today, push to tomorrow
        if now >= next_sell:
            next_sell += timedelta(days=1)
        while next_sell.weekday() >= 5:
            next_sell += timedelta(days=1)

        secs = (next_sell - now).total_seconds()
        if is_ah_position:
            log(f"AH/PM position — market sell + rotation at 9:35 AM {next_sell:%a %b %d %H:%M} ET ({secs/3600:.1f}h).")
        else:
            log(f"Overnight position — morning decision at 9:31 AM {next_sell:%a %b %d %H:%M} ET ({secs/3600:.1f}h). Will buy new pick at 9:35 AM.")

        pre_open = now.hour < 9 or (now.hour == 9 and now.minute < 30)
        if pre_open:
            open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
            mins_to_open = max(0, int((open_t - now).total_seconds() / 60))
            notify(
                f"⏳ <b>Order placed — pending fill at open</b>\n\n"
                f"🎫 <code>{symbol}</code>  {shares:.4f} sh @ ~${entry:.2f}\n"
                f"💰 Deploying: <b>${cost:.2f} USD</b>  |  📋 {strat}\n"
                f"📋 Pre-market order — fills at <b>9:30 AM ET open</b>  ({mins_to_open} min)\n"
                f"🔄 {'Market sell + rotation at' if is_ah_position else 'Morning decision at'} <b>9:35 AM ET</b>\n"
                f"🎯 Target: +{_profit_target_pct:.1f}%  |  3:55 PM lock: +{_LATE_LOCK_PCT:.0f}%  |  No stop loss"
            )
        else:
            notify(
                f"📊 <b>Position open — overnight hold</b>\n\n"
                f"🎫 <code>{symbol}</code>  |  {shares:.4f} sh @ ${entry:.2f}\n"
                f"💰 Cost: <b>${cost:.2f} USD</b>  |  📋 {strat}\n"
                f"🔄 {'Market sell + rotation at <b>9:35 AM ET</b>' if is_ah_position else 'Morning decision at <b>9:31 AM ET</b> — hold if trending or rotate'}\n"
                f"🎯 Target: +{_profit_target_pct:.1f}%  |  3:55 PM lock: +{_LATE_LOCK_PCT:.0f}%  |  No stop loss"
            )

        fill_notified = not pre_open
        _scanned: set[str] = set()
        balance_approx = balance if balance > 0 else cost

        while now_et() < next_sell - timedelta(seconds=30):
            cur = now_et()

            # Fill confirmation at 9:30 AM market open
            # AH positions are already filled — skip the 9:45 confirm sleep so we don't miss 9:35 sell
            if not fill_notified and (cur.hour > 9 or (cur.hour == 9 and cur.minute >= 30)):
                fill_notified = True
                if not is_ah_position:
                    wait_for_fill_confirm(symbol)
                log("Fill confirmed at market open.")

            # 9:30 AM morning report (Mon–Fri only)
            if "morning_report" not in _scanned and cur.weekday() < 5 and (cur.hour > 9 or (cur.hour == 9 and cur.minute >= 30)):
                _scanned.add("morning_report")
                log("9:30 AM — rapport live actif, morning report ignoré.")

            # 4 PM daily quant summary (Mon–Fri only)
            if "daily_report" not in _scanned and cur.hour >= 16 and cur.weekday() < 5:
                _scanned.add("daily_report")
                log("4:00 PM — rapport live actif, daily summary ignoré.")

            # Background scans — silent, no Telegram
            if "5pm" not in _scanned and cur.hour >= 17 and cur.weekday() < 5:
                _scanned.add("5pm")
                _run_overnight_scan("5 PM Tonight's Preview", balance_approx, "5pm")

            if "sunday_preview" not in _scanned and cur.weekday() == 6 and cur.hour >= 18:
                _scanned.add("sunday_preview")
                log("Sunday 6 PM — futures open, running silent preview scan.")
                _run_overnight_scan("Sunday 6 PM — Futures Open Preview", balance_approx, "sunday")

            if "5am" not in _scanned and 5 <= cur.hour < 7 and cur.weekday() < 5:
                _scanned.add("5am")
                _run_overnight_scan("5 AM Morning Scan", balance_approx, "5am")

            _combined_report()

            # Pre-market buy (7:00-9:29 AM) — deploy cash into a position early
            if "pm" not in _scanned and cur.hour >= 7:
                _scanned.add("pm")
                _run_premarket_strategy(balance_approx)
                # Refresh position variables (PM buy may have overwritten POS_FILE)
                if POS_FILE.exists():
                    try:
                        _np = json.loads(POS_FILE.read_text())
                        symbol = str(_np.get("symbol", symbol))
                        entry  = float(_np.get("buyPrice", entry))
                        shares = float(_np.get("shares", shares))
                        cost   = float(_np.get("estimatedCost", cost))
                        strat  = str(_np.get("strategyName", strat))
                        log(f"PM update: position is now {symbol} {shares:.4f} sh @ ${entry:.2f}")
                    except Exception:
                        pass

            # 30-min position update (suppressed during quiet weekend)
            if time.time() - last_update_t >= UPDATE_INTERVAL:
                last_update_t = time.time()  # always reset timer to avoid burst on wake
                if _is_quiet_weekend():
                    log(f"Quiet weekend — skipping 30-min Telegram update.")
                    time.sleep(60)
                    continue
                try:
                    # Try Wealthsimple first — covers overnight/Blue Ocean ATS sessions
                    # that Yahoo Finance doesn't track (e.g. Sunday night futures open)
                    _live = _get_ws_live_price(symbol, shares=shares)
                    if not _live:
                        _fi2  = yf.Ticker(symbol).fast_info
                        _live = float(_fi2.last_price or 0)
                    _live = _live or entry
                    if fill_notified:
                        notify(build_update_message(
                            symbol, entry, _live, shares, cost,
                            next_sell_dt=next_sell,
                        ))
                        sc_syms2, _ = _choose_scan_symbols()
                        fresh2 = _quick_scan_picks(sc_syms2, current_symbol=symbol)
                        watchlist_msg = build_watchlist_alert(fresh2, symbol, _live, entry, shares, cost)
                        if watchlist_msg:
                            notify(watchlist_msg)
                        log(f"30-min update: ${_live:.2f} ({(_live/entry-1)*100:+.2f}%)")
                    else:
                        cur2 = now_et()
                        open_t2 = cur2.replace(hour=9, minute=30, second=0, microsecond=0)
                        mins_left = max(0, int((open_t2 - cur2).total_seconds() / 60))
                        notify(
                            f"⏳ <b>Order Pending — <code>{symbol}</code></b>  |  {cur2:%H:%M} ET\n\n"
                            f"📋 Pre-market order fills at <b>9:30 AM ET open</b>\n"
                            f"🎫 {shares:.4f} sh @ ~${entry:.2f}  |  💰 ${cost:.2f} USD\n"
                            f"⏰ Market opens in <b>{mins_left} min</b>\n"
                            f"🔴 {'SELL at 9:35 AM market open → rotate' if is_ah_position else 'Hold decision at 9:31 AM → target/trail/3:55 PM'}"
                        )
                        sc_syms2, _ = _choose_scan_symbols()
                        fresh2 = _quick_scan_picks(sc_syms2, current_symbol=symbol)
                        watchlist_msg = build_watchlist_alert(fresh2, symbol, _live, entry, shares, cost)
                        if watchlist_msg:
                            notify(watchlist_msg)
                        log(f"Pre-open update: order pending, {mins_left} min to open")
                except Exception as exc:
                    log(f"30-min update error: {exc}")

            time.sleep(60)

        # ── Morning exit ───────────────────────────────────────────────────
        if is_ah_position:
            # AH/PM positions: always market sell at 9:35 AM, then rotate — no hold decision
            _sleep_until(next_sell, "9:35 AM AH/PM sell + rotation")
            log(f"9:35 AM — market selling AH/PM position {symbol}, rotating to next pick.")
            notify(
                f"🔔 <b>9:35 AM — Selling AH/PM position</b>\n\n"
                f"🎫 <code>{symbol}</code>  {shares:.4f} sh @ ${entry:.2f}\n"
                f"🔄 Market sell → scanning for next pick now..."
            )
            _execute_sell_order(symbol, entry, shares, cost, strat, label="9:35 AM ET")
            return  # main loop scans + buys new pick immediately

        # Regular overnight positions: morning hold-or-rotate decision at 9:31 AM
        _sleep_until(next_sell, "9:31 AM morning decision")

        # Sell any legacy position from pre-market rotation (before hold decision)
        if LEGACY_FILE.exists():
            log("Legacy position detected — selling before morning decision...")
            _sell_legacy_position()
            # Re-read position file (may have been overwritten by PM buy)
            if POS_FILE.exists():
                try:
                    _new_pos = json.loads(POS_FILE.read_text())
                    symbol = _new_pos["symbol"]
                    entry  = float(_new_pos.get("buyPrice", 0))
                    shares = float(_new_pos.get("shares", 0))
                    cost   = float(_new_pos.get("estimatedCost", shares * entry))
                    strat  = _new_pos.get("strategyName", strat)
                    log(f"Post-legacy: current position is {symbol} {shares:.4f} sh @ ${entry:.2f}")
                except Exception:
                    pass

        if _morning_hold_decision(symbol, entry, shares, cost, balance):
            log("Morning hold: continuing into daytime monitoring")
            last_update_t = 0.0
            # fall through to daytime monitoring loop below
        else:
            _execute_sell_order(symbol, entry, shares, cost, strat, label="9:31 AM ET")
            return  # main loop will scan + buy new pick at 9:35 AM

    notify(
        f"📊 <b>Position open — autonomous hold</b>\n\n"
        f"🎫 <code>{symbol}</code>  |  {shares:.4f} sh @ ${entry:.2f}\n"
        f"💰 Cost: <b>${cost:.2f} USD</b>  |  📋 {strat}\n"
        f"🎯 Profit target: <b>+{_profit_target_pct:.1f}%</b>  |  "
        f"Late lock: <b>+{_LATE_LOCK_PCT:.0f}%</b> at 3:55 PM\n"
        f"🌙 Below target at 3:55 PM → hold overnight, no stop loss"
    )

    while True:
        now = now_et()

        # ── 3:55 PM: always sell — Rule 1: never hold cash, redeploy into AH ─
        if now.hour > _SELL_HOUR or (now.hour == _SELL_HOUR and now.minute >= _SELL_MINUTE):
            try:
                snap_eod = md.snapshot(symbol)
                price_eod = snap_eod.last_price if snap_eod else entry
            except Exception:
                price_eod = entry
            eod_pct = (price_eod - entry) / entry * 100 if entry > 0 else 0
            log(f"3:55 PM: {eod_pct:+.1f}% — selling always, AH rotation follows")
            _execute_sell_order(symbol, entry, shares, cost, strat, "3:55 PM")
            return

        # ── Profit target & Trailing stop: check every 60s ─────────────────
        try:
            snap = md.snapshot(symbol)
            if snap:
                price   = snap.last_price
                pnl_pct = (price - entry) / entry * 100 if entry > 0 else 0

                # Track peak price for trailing stop
                if max_price == 0.0:
                    max_price = price
                if price > max_price:
                    max_price = price
                
                max_pnl_pct = (max_price - entry) / entry * 100 if entry > 0 else 0

                # 1. Hard Profit Target (ATR-adaptive)
                if pnl_pct >= _profit_target_pct:
                    log(f"PROFIT TARGET: {pnl_pct:+.1f}% ≥ +{_profit_target_pct:.1f}% — selling now")
                    notify(
                        f"🎯 <b>PROFIT TARGET HIT — selling <code>{symbol}</code></b>\n\n"
                        f"📈 Unrealized: <b>{pnl_pct:+.1f}%</b>  |  Price: ${price:.2f}\n"
                        f"🎯 Target was +{_profit_target_pct:.1f}%  |  Locking in gains"
                    )
                    _execute_sell_order(symbol, entry, shares, cost, strat, "Profit Target")
                    return

                # 2. Trailing Stop (Triggered at +2%, trail by 1%)
                if max_pnl_pct >= _TRAILING_STOP_TRIGGER_PCT:
                    stop_price = max_price * (1 - _TRAILING_STOP_DISTANCE_PCT / 100)
                    if price <= stop_price:
                        log(f"TRAILING STOP: {pnl_pct:+.1f}% (peak {max_pnl_pct:+.1f}%) — selling now")
                        notify(
                            f"🛡️ <b>TRAILING STOP HIT — selling <code>{symbol}</code></b>\n\n"
                            f"📈 Current: <b>{pnl_pct:+.1f}%</b>  |  Peak: <b>{max_pnl_pct:+.1f}%</b>\n"
                            f"💰 Price: ${price:.2f}  |  Stop: ${stop_price:.2f}\n"
                            f"🔒 Protecting gains — executing sell now"
                        )
                        _execute_sell_order(symbol, entry, shares, cost, strat, "Trailing Stop")
                        return

                if time.time() - last_update_t >= UPDATE_INTERVAL:
                    notify(build_update_message(symbol, entry, price, shares, cost))
                    sc_syms, _ = _choose_scan_symbols()
                    fresh = _quick_scan_picks(sc_syms, current_symbol=symbol)
                    watchlist_msg = build_watchlist_alert(fresh, symbol, price, entry, shares, cost)
                    if watchlist_msg:
                        notify(watchlist_msg)
                    log(f"30-min update: ${price:.2f} ({pnl_pct:+.2f}%)")
                    last_update_t = time.time()
        except Exception as exc:
            log(f"Price check error: {exc}")

        _combined_report()
        time.sleep(60)


# ──────────────────────────────────────────────────────────────────────────────
# Overnight wait loop
# ──────────────────────────────────────────────────────────────────────────────

def _should_fire(slot: str, hour: int, minute: int, scans_done: set[str]) -> bool:
    return slot not in scans_done and _passed_today(hour, minute)


def wait_overnight(bias: FuturesBias, scans_done: set[str],
                   last_status_t: list, balance: float) -> FuturesBias:
    """
    Sleeps until the buy window, firing 5 AM scan and 5 PM preview.
    Returns the updated bias from the most recent scan.
    """

    def _do_scan(label: str) -> FuturesBias:
        scan_symbols, full_refresh = _choose_scan_symbols()
        source = "full universe" if full_refresh else f"shortlist ({len(scan_symbols)})"
        log(f"Running {label} using {source}...")
        picks, new_bias, buy_plan, strat_name, fut_det = run_scan(
            balance,
            scan_symbols=scan_symbols,
            full_refresh=full_refresh,
        )
        top = picks[0] if picks else None
        ai  = get_ai_analysis(picks, new_bias, fut_det, balance)
        msg = build_scan_message(picks, new_bias, fut_det, buy_plan, strat_name, balance, ai, label)
        notify(msg)
        last_status_t[0] = time.time()
        return new_bias

    # Startup scan on first entry — always send trading plan to Telegram
    if "startup" not in scans_done:
        bias = _do_scan("Startup Scan")
        scans_done.add("startup")

    _last_watchlist_t: float = 0.0  # tracks 30-min watchlist cadence in PM/overnight window

    # Immediate pre-market check on startup (Rule: no idle cash)
    if "pm" not in scans_done and _is_premarket_window() and not POS_FILE.exists():
        log("Startup: In pre-market window with cash — executing PM strategy.")
        scans_done.add("pm")
        _run_premarket_strategy(balance)
        if POS_FILE.exists():
            log("PM buy successful on startup — exiting to hold loop.")
            return bias

    # Immediate after-hours check on startup (Rule: no idle cash in AH window)
    if "ah" not in scans_done and _is_afterhours_window() and not POS_FILE.exists():
        log("Startup: In after-hours window with cash — executing AH strategy.")
        scans_done.add("ah")
        _run_afterhours_strategy(balance, sell_existing=False)
        if POS_FILE.exists():
            log("AH buy successful on startup — exiting to hold loop.")
            return bias

    while True:
        now = now_et()

        # Weekend
        if now.weekday() >= 5:
            nxt  = _next_weekday_buy()
            secs = (nxt - now).total_seconds()
            log(f"Weekend — next window {nxt:%a %b %d %H:%M} ET ({secs/3600:.1f}h). Sleeping 30 min.")
            time.sleep(1800)
            scans_done.discard("5am")
            scans_done.discard("5pm")
            scans_done.discard("ah")
            continue

        # 5 AM main scan + game plan
        if _should_fire("5am", 5, 0, scans_done) and not _passed_today(9, 10):
            bias = _do_scan("5 AM Morning Scan")
            scans_done.add("5am")

        # Scheduled combined report every 3 hours
        _combined_report()

        # 5 PM next-day preview
        if _should_fire("5pm", 17, 0, scans_done):
            _do_scan("5 PM Tomorrow's Preview")
            scans_done.add("5pm")

        # 4-8 PM after-hours buy — Rule: no idle cash in AH window
        if "ah" not in scans_done and _is_afterhours_window() and not POS_FILE.exists():
            log("AH window open with cash — executing after-hours strategy.")
            scans_done.add("ah")
            _run_afterhours_strategy(balance, sell_existing=False)
            if POS_FILE.exists():
                log("AH buy confirmed — exiting to hold loop.")
                return bias

        # 7 AM pre-market buy — Rule: no idle cash in PM window
        if "pm" not in scans_done and _is_premarket_window() and not POS_FILE.exists():
            scans_done.add("pm")
            _run_premarket_strategy(balance)
            if POS_FILE.exists():
                log("PM buy from overnight wait — exiting to hold loop.")
                return bias

        # 30-min watchlist alert during pre-market window (no position held)
        if _is_premarket_window() and not POS_FILE.exists() and (time.time() - _last_watchlist_t) >= 1800:
            try:
                log("Pre-market 30-min watchlist refresh...")
                sc_syms, _ = _choose_scan_symbols()
                fresh = _quick_scan_picks(sc_syms)
                wl_msg = build_watchlist_alert(fresh)
                if wl_msg:
                    notify(wl_msg)
            except Exception as _wl_exc:
                log(f"  Watchlist alert error: {_wl_exc}")
            _last_watchlist_t = time.time()

        # Reset slots at midnight
        if now.hour == 0 and now.minute < 2:
            scans_done.discard("5am")
            scans_done.discard("5pm")
            scans_done.discard("pm")
            scans_done.discard("ah")
            scans_done.discard("daily_report")
            scans_done.discard("weekly_report")
            failed_buys_today.clear()

        # Near buy window → exit sleep loop
        if now.weekday() < 5:
            target_t = now.replace(hour=_BUY_HOUR, minute=_BUY_MINUTE, second=0, microsecond=0)
            catchup_t = now.replace(hour=_BUY_CATCHUP_HOUR, minute=_BUY_CATCHUP_MINUTE, second=0, microsecond=0)
            if 0 <= (target_t - now).total_seconds() <= 300 or target_t <= now <= catchup_t:
                log("Near buy window — exiting overnight loop.")
                return bias

        # Past buy window, not in AH window → sleep until 5 PM check
        cutoff = now.replace(hour=_BUY_CATCHUP_HOUR, minute=_BUY_CATCHUP_MINUTE, second=0, microsecond=0)
        if now > cutoff and now.hour < 17 and not _is_afterhours_window():
            log("Buy window passed — sleeping 30 min (not in AH window).")
            time.sleep(1800)
            continue

        if now.minute % 30 == 0 and now.second < 60:
            log(f"Overnight — waiting for next buy window.")

        time.sleep(60)

    return bias


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Le Grinder — 1 trade/day, no stop, 3:55 PM exit"
    )
    parser.add_argument("--balance", type=float, default=None,
                        help="Cash in USD (default: live fetch from Wealthsimple)")
    parser.add_argument("--now", "--buy-now", action="store_true",
                        help="Skip all waiting and buy immediately (debug)")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Skip scan and buy this single US ticker immediately (e.g. NVDA)")
    parser.add_argument("--lite", action="store_true",
                        help="Use hardcoded ~350 ticker watchlist instead of full universe (avoids rate limits)")
    parser.add_argument("--yahoo", action="store_true",
                        help="Use Yahoo Finance most active US stocks sorted by volume")
    parser.add_argument("--buy-today", action="store_true",
                        help="Skip all timing (overnight wait + buy window) — scan and buy immediately")
    parser.add_argument("--shares", type=int, default=None,
                        help="Override buy to fixed share count (instead of 90% of balance)")
    args = parser.parse_args()

    # ── Startup ───────────────────────────────────────────────────────────
    log("=" * 60)
    log("Le Grinder — STARTING")
    log(f"Log file: {LOG_FILE}")
    log("=" * 60)

    _start_keepalive()

    if args.lite:
        from kzer_bot.grinder_strategy import _HARDCODED_WATCHLIST
        global WATCHLIST
        WATCHLIST = _HARDCODED_WATCHLIST
        log(f"Lite mode: using hardcoded watchlist ({len(WATCHLIST)} tickers)")
        # Clear cached scan state so lite watchlist is used (not stale shortlist)
        SCAN_STATE_FILE.unlink(missing_ok=True)
    elif args.yahoo:
        yahoo_file = DATA / "yahoo_watchlist.json"
        if not yahoo_file.exists():
            log("Yahoo watchlist file not found — fetching from Yahoo Finance...")
            from scripts.fetch_yahoo_most_active import fetch_all_symbols
            symbols = fetch_all_symbols()
            yahoo_file.write_text(json.dumps({
                "updated": now_et().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(symbols),
                "symbols": symbols,
            }, indent=2))
            log(f"Fetched {len(symbols)} Yahoo most active symbols")
        yahoo_data = json.loads(yahoo_file.read_text(encoding="utf-8"))
        WATCHLIST = yahoo_data["symbols"]
        log(f"Yahoo mode: using top {len(WATCHLIST)} most active US stocks")
        # Clear cached scan state so full Yahoo watchlist is used (not stale shortlist)
        SCAN_STATE_FILE.unlink(missing_ok=True)
        log("  Cleared cached scan state for fresh Yahoo scan")
    else:
        refresh_universe_if_stale()

    balance: float = args.balance or fetch_live_balance() or 100.0
    SESSION_FILE.write_text(json.dumps({
        "startingBalance": balance,
        "startTime": now_et().isoformat(),
    }))
    log(f"Starting balance: ${balance:.2f} USD")

    stats = _get_trade_stats()
    at_color = _pnl_color(stats["total_pnl"])

    if args.ticker:
        notify(
            f"🎯 <b>Le Grinder — Single-Ticker Mode</b>\n\n"
            f"💰 Balance: <b>${balance:.2f} USD</b>\n"
            f"📋 Target: <b>{args.ticker.upper()}</b>\n"
            f"⏰ Buying immediately — no scan, no wait\n"
            f"🎯 Exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  3:55 PM lock if +{_LATE_LOCK_PCT:.0f}%  |  Intraday rotation  |  No stop loss\n\n"
            f"{at_color} All-time PnL: <b>${stats['total_pnl']:+.2f} USD</b>"
            f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
        )
    elif args.yahoo:
        notify(
            f"🚀 <b>Le Grinder started — Yahoo Most Active Mode</b>\n\n"
            f"💰 Balance: <b>${balance:.2f} USD</b>\n"
            f"📋 Watchlist: <b>{len(WATCHLIST)} tickers</b>  (Yahoo most active CA — volume sorted)\n"
            f"📐 Strategy: 8-criteria momentum screen  +  Claude AI analysis\n"
            f"⏰ Entry: 9:31 AM ET every weekday\n"
            f"🎯 Exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  3:55 PM lock if +{_LATE_LOCK_PCT:.0f}%  |  Intraday rotation  |  No stop loss\n\n"
            f"{at_color} All-time PnL: <b>${stats['total_pnl']:+.2f} USD</b>"
            f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
        )
    elif args.lite:
        notify(
            f"🚀 <b>Le Grinder started — Lite Mode</b>\n\n"
            f"💰 Balance: <b>${balance:.2f} USD</b>\n"
            f"📋 Watchlist: <b>{len(WATCHLIST)} tickers</b>  (hardcoded — no rate limits)\n"
            f"📐 Strategy: 8-criteria momentum screen  +  Claude AI analysis\n"
            f"⏰ Entry: 9:31 AM ET every weekday\n"
            f"🎯 Exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  3:55 PM lock if +{_LATE_LOCK_PCT:.0f}%  |  Intraday rotation  |  No stop loss\n\n"
            f"{at_color} All-time PnL: <b>${stats['total_pnl']:+.2f} USD</b>"
            f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
        )
    else:
        notify(
            f"🚀 <b>Le Grinder started successfully</b>\n\n"
            f"💰 Balance: <b>${balance:.2f} USD</b>\n"
            f"📋 Watchlist: <b>{len(WATCHLIST)} US tickers (NYSE/NASDAQ)</b>  (TSX / TSXV / NEO)\n"
            f"📐 Strategy: 8-criteria momentum screen  +  Claude AI analysis\n"
            f"⏰ Entry: 9:31 AM ET every weekday\n"
            f"🎯 Exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  3:55 PM lock if +{_LATE_LOCK_PCT:.0f}%  |  Intraday rotation  |  No stop loss\n\n"
            f"{at_color} All-time PnL: <b>${stats['total_pnl']:+.2f} USD</b>"
            f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
        )

    # ── Resume open position ───────────────────────────────────────────────
    if POS_FILE.exists():
        try:
            pos = json.loads(POS_FILE.read_text())
        except Exception:
            pos = {}
        if not pos.get("symbol"):
            log("Position file exists but has no symbol — clearing stale state.")
            POS_FILE.write_text("{}")
            pos = {}
        if pos.get("symbol"):
            _now = now_et()
            _pre_open = _now.hour < 9 or (_now.hour == 9 and _now.minute < 30)
            log(f"Open position found: {pos['symbol']} — resuming hold/sell loop.")

            if _is_afterhours_window():
                log("After-hours window — holding existing position until 9:31 AM (no limit sells in extended hours).")
                notify(
                    f"🌙 <b>After-hours window detected</b>\n\n"
                    f"🎫 Holding <code>{pos['symbol']}</code>  "
                    f"{pos.get('shares', 0):.4f} sh @ ${pos.get('buyPrice', 0):.2f}\n"
                    f"📋 No limit sells in extended hours — holding until <b>9:31 AM ET</b> market open"
                )
                _run_afterhours_strategy(balance, sell_existing=True)
                if POS_FILE.exists():
                    hold_and_sell(balance=balance)
            elif _pre_open:
                _open_t = _now.replace(hour=9, minute=30, second=0, microsecond=0)
                _mins = max(0, int((_open_t - _now).total_seconds() / 60))
                _is_pm_ah = bool(pos.get("afterHours")) or pos.get("strategyName") in ("After-Hours Limit", "Pre-Market Limit")
                _exit_line = (
                    f"🔴 <b>SELL at 9:35 AM ET</b> → rotate to next pick"
                    if _is_pm_ah else
                    f"🎯 Hold decision at 9:31 AM → target/trail/3:55 PM exit"
                )
                notify(
                    f"⏳ <b>Bot restarted — order pending fill</b>\n\n"
                    f"🎫 <code>{pos['symbol']}</code>  "
                    f"{pos.get('shares', 0):.4f} sh @ ~${pos.get('buyPrice', 0):.2f}\n"
                    f"📋 Pre-market order fills at <b>9:30 AM ET open</b>  ({_mins} min)\n"
                    f"{_exit_line}"
                )
                hold_and_sell(balance=balance)
            else:
                notify(
                    f"▶️ <b>Bot restarted — resuming position</b>\n\n"
                    f"🎫 <code>{pos['symbol']}</code>  "
                    f"{pos.get('shares', 0):.4f} sh @ ${pos.get('buyPrice', 0):.2f}\n"
                    f"🎯 Autonomous: +{_PROFIT_TARGET_PCT:.0f}% target  |  +{_LATE_LOCK_PCT:.0f}% lock at 3:55 PM"
                )
                hold_and_sell(balance=balance)

    # ── Single-ticker mode (skip scan, buy immediately) ───────────────────
    if args.ticker:
        raw_symbol = args.ticker.upper().strip()

        log(f"Single-ticker mode: {raw_symbol}")
        log("Fetching data for single ticker...")

        md = GrinderMarketData()
        snap = md.snapshot(raw_symbol)

        if snap is None:
            log(f"No data for {raw_symbol} — aborting.")
            notify(f"❌ No market data for <code>{raw_symbol}</code> — aborting.")
            return

        pick = GrinderPick(
            symbol        = snap.symbol,
            last_close    = snap.last_close,
            score         = snap.score,
            yesterday_pct = snap.yesterday_pct_change,
            rel_volume    = snap.rel_volume,
            atr_pct       = snap.atr_pct,
            close_strength= snap.close_strength,
            above_ema5    = (snap.last_close > snap.ema5),
            above_ema20   = (snap.last_close > snap.ema20),
            strategy_name = "Single Ticker",
        )

        log(f"Pick: {pick.symbol}  ${pick.last_close:.2f}  score {pick.score:.1f}")
        bias, fut_det = get_futures_bias()
        ai = get_ai_analysis([pick], bias, fut_det, balance)

        ok = execute_buy(pick, balance, bias, fut_det, ai)
        if ok:
            hold_and_sell(balance=balance)
        return

    # ── Buy-today notification ────────────────────────────────────────────
    if args.buy_today:
        notify(
            f"⚡ <b>Le Grinder — BUY TODAY Mode</b>\n\n"
            f"Scanning <b>{len(WATCHLIST)}</b> tickers and buying <b>immediately</b>.\n"
            f"⚡ No timing wait — scan → buy → autonomous exit → intraday rotation\n"
            f"💰 Balance: <b>${balance:.2f} USD</b>"
        )

    # ── Main loop ─────────────────────────────────────────────────────────
    scans_done:      set[str] = set()
    failed_buys_today: set[str] = set()   # symbols that failed to buy — skip until midnight
    last_status_t: list     = [0.0]
    bias = FuturesBias.NEUTRAL
    skip_wait = args.now or args.buy_today

    while True:
        # Refresh balance each cycle
        if not args.balance:
            fresh = fetch_live_balance(retries=2)
            if fresh:
                balance = fresh
        SESSION_FILE.write_text(json.dumps({
            "startingBalance": balance,
            "startTime": now_et().isoformat(),
        }))

        # ── Window Check 1: After-hours buy ───────────────────────────────
        _now_main = now_et()
        _past_close = _now_main.hour > _SELL_HOUR or (
            _now_main.hour == _SELL_HOUR and _now_main.minute >= _SELL_MINUTE
        )
        
        # 9:30 AM morning report (Mon–Fri)
        if _now_main.weekday() < 5 and "morning_report" not in scans_done and (
            _now_main.hour > 9 or (_now_main.hour == 9 and _now_main.minute >= 30)
        ):
            log("9:30 AM — rapport live actif, morning report ignoré.")
            scans_done.add("morning_report")

        # Scheduled combined report every 3 hours
        _combined_report()

        if not POS_FILE.exists() and _is_afterhours_window() and _past_close:
            log("No position in AH window — scanning for after-hours buy.")
            _run_afterhours_strategy(balance, sell_existing=False)

        # ── Window Check 2: Pre-market rotation ────────────────────────────
        # (Rule: if no position, OR if we want to rotate out of an overnight hold)
        if "pm" not in scans_done and _is_premarket_window():
            log("Pre-market window open — checking for deployment/rotation...")
            scans_done.add("pm")
            _run_premarket_strategy(balance)
            # If PM buy successful, it enters hold_and_sell inside the strategy.
            # If it returns here, it means it either failed or didn't find anything.

        # ── Wait for regular market open if we skipped wait above or finished PM ──
        if not skip_wait:
            bias = wait_overnight(bias, scans_done, last_status_t, balance)
        skip_wait = False
        scans_done.clear()
        failed_buys_today.clear()  # new trading day — reset failed picks

        # Weekday guard
        if now_et().weekday() >= 5:
            log("Weekend — back to overnight loop.")
            continue

        # Regular hours position check
        if POS_FILE.exists():
            try:
                _pm_check = json.loads(POS_FILE.read_text())
                log(f"Active position {_pm_check.get('symbol','?')} found — entering hold loop.")
                hold_and_sell(balance=balance)
                if not args.balance:
                    fresh = fetch_live_balance(retries=2)
                    if fresh:
                        balance = fresh
                if POS_FILE.exists():
                    continue  # held overnight
            except Exception:
                pass

        # Fresh bias check right before the window
        log("Re-checking futures bias right before buy window...")
        bias, bias_detail = get_futures_bias()
        log(f"  Live bias: {bias.value.upper()}  {bias_detail}")

        # Final scan + AI analysis
        log("Final pre-buy scan + AI analysis...")
        scan_symbols, full_refresh = _choose_scan_symbols()
        picks, _, buy_plan, strat_name, fut_det = run_scan(
            balance,
            scan_symbols=scan_symbols,
            full_refresh=full_refresh,
        )
        ai = get_ai_analysis(picks, bias, fut_det, balance)

        # Filter out symbols that already failed to buy today
        picks = [p for p in picks if p.symbol not in failed_buys_today]

        if not picks:
            notify(
                "❌ <b>No data at buy time — skipping today</b>\n\n"
                "yfinance returned no usable data for any ticker.\n"
                "Check internet connection. Entering overnight loop for tomorrow."
            )
            log("No data at all — skipping today.")
            continue

        top = picks[0]

        # Wait for the correct window (sends red-waiting message if needed)
        # Skipped for --buy-today (buy immediately)
        should_buy = True
        if not args.buy_today:
            should_buy = wait_for_buy_window(bias, top, fut_det)
        if not should_buy:
            log("Buy window missed — entering overnight loop.")
            continue

        # Re-scan for red bias at 11 AM to get fresh price
        # Skipped for --buy-today (already have fresh data)
        if bias == FuturesBias.RED and not args.buy_today:
            log("Red bias: re-scanning at 11 AM for freshest data...")
            scan_symbols, full_refresh = _choose_scan_symbols()
            picks, _, _, strat_name, fut_det = run_scan(
                balance,
                scan_symbols=scan_symbols,
                full_refresh=full_refresh,
            )
            ai = get_ai_analysis(picks, bias, fut_det, balance)
            picks = [p for p in picks if p.symbol not in failed_buys_today]
            if not picks:
                notify("❌ <b>Bounce scan returned no data — skipping today.</b>")
                log("Bounce scan returned no data — skipping.")
                continue
            top = picks[0]

        if not args.buy_today:
            wait_after_pick(top, bias, fut_det)
        else:
            log("Buy-today mode: skipping 5-min wait, buying immediately.")
            notify(
                f"⚡ <b>Buying now — <code>{top.symbol}</code></b>\n"
                f"🏢 {_company_line(top.symbol)}\n"
                f"📡 {_bias_line(bias, fut_det)}"
            )

        # Extended-at-buy check: skip picks that already ran >4% from scan price
        top = _filter_extended_at_buy(picks)
        picks = [top] + [p for p in picks if p.symbol != top.symbol]

        # Execute buy — try up to 5 picks in order before giving up
        ok = False
        for attempt_pick in picks[:5]:
            ok = execute_buy(attempt_pick, balance, bias, fut_det, ai, fixed_shares=args.shares)
            if ok:
                top = attempt_pick
                break
            failed_buys_today.add(attempt_pick.symbol)
            remaining = [p.symbol for p in picks[:5] if p.symbol not in failed_buys_today]
            if remaining:
                log(f"Buy failed for {attempt_pick.symbol} — trying next: {remaining[0]}")
            else:
                log(f"Buy failed for {attempt_pick.symbol} — no more picks to try.")

        if not ok:
            notify("❌ <b>All picks failed — entering overnight loop</b>\n\nWill retry at next scan.")
            log("All picks failed — entering overnight loop.")
            continue

        # Fill confirm (pre-market orders only)
        now = now_et()
        if now.hour < 9 or (now.hour == 9 and now.minute < 30):
            wait_for_fill_confirm(top.symbol)

        # Intraday trading loop: hold, sell when done, rotate to next mover if time allows
        while True:
            hold_and_sell(balance=balance)
            if POS_FILE.exists():
                log("Position still open (autonomous hold) — re-monitoring tomorrow.")
                break
            POS_FILE.unlink(missing_ok=True)

            # Intraday rotation: if market still open (before 3:30 PM), find next mover
            _now = now_et()
            _cutoff = _now.replace(hour=15, minute=30, second=0, microsecond=0)
            if not (_now.weekday() < 5 and _now < _cutoff):
                log("Position closed — no time for intraday rotation.")
                # After-hours window: scan for AH momentum play
                if _is_afterhours_window():
                    log("AH window open — running after-hours strategy.")
                    fresh_ah = fetch_live_balance(retries=2)
                    if fresh_ah:
                        balance = fresh_ah
                    _run_afterhours_strategy(balance, sell_existing=False)
                break

            log("Position closed with time remaining — scanning for next intraday mover...")
            fresh = fetch_live_balance(retries=2)
            if fresh:
                balance = fresh
            sc_syms, _ = _choose_scan_symbols()
            _picks, _rebias, _, _strat, _futdet = run_scan(balance, scan_symbols=sc_syms)
            if not _picks:
                log("No intraday picks found — entering overnight loop.")
                break
            top = _filter_extended_at_buy(_picks)
            notify(
                f"⚡ <b>INTRADAY ROTATION — <code>{top.symbol}</code></b>\n\n"
                f"🔄 Previous position closed — locking in next mover\n"
                f"🎯 Score: <b>{top.score:.1f}</b>  |  Yesterday: {top.yesterday_pct:+.1f}%"
                f"  |  Vol: {top.rel_volume:.1f}x  |  ${top.last_close:.2f}\n"
                f"📋 Strategy: <b>{top.strategy_name}</b>  |  No stop loss"
            )
            ok = execute_buy(top, balance, _rebias, _futdet, "")
            if not ok:
                log("Intraday re-buy failed — entering overnight loop.")
                break


if __name__ == "__main__":
    main()
