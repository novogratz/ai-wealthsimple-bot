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
_BUY_CATCHUP_HOUR      = 10
_BUY_CATCHUP_MINUTE    = 0
_OVERNIGHT_SELL_HOUR   = 9     # sell overnight positions at market open
_OVERNIGHT_SELL_MINUTE = 31
_BUY_DELAY_MINUTES = 0
_SHORTLIST_SIZE    = 150
_FULL_REFRESH_TTL  = 24 * 3600
_CACHED_SCAN_TTL   = 18 * 3600
_MIN_COVERAGE_FOR_CACHE = 0.35
_DEPLOY_PCT           = 100       # 100% of balance deployed per trade
_PROFIT_TARGET_PCT    = 5.0       # sell immediately when unrealized >= +5%
_LATE_LOCK_PCT        = 2.0       # sell at 3:55 PM if unrealized >= +2% (else hold overnight)
_MIN_SMART_HOLD_SCORE = 20        # hold overnight if re-scan smart score still >= this
_UNIVERSE_MAX_AGE = 7 * 86400  # refresh universe.json if older than 7 days
UNIVERSE_FILE     = ROOT / "data" / "universe.json"
UNIVERSE_SCRIPT   = ROOT / "scripts" / "update_universe.py"

# ── After-hours / extended-hours trading ──────────────────────────────────────
_AH_BUY_START_HOUR  = 16   # 4:00 PM ET — AH buy window opens
_AH_BUY_END_HOUR    = 19   # 7:50 PM ET — stop new AH entries (AH closes 8 PM)
_AH_BUY_END_MINUTE  = 50
_AH_PROFIT_PCT      = 3.0  # sell AH position immediately at +3%
_AH_LIMIT_PREMIUM   = 0.005  # pay up to 0.5% above current AH price on limit buy
_AH_SELL_PREMIUM    = 0.01   # set limit sell target at +1% above AH entry
_AH_MIN_PCT         = 0.3    # minimum after-hours gain to be a candidate (+0.3%)
_AH_WATCHLIST_SIZE  = 80     # number of tickers to scan for AH plays


# ──────────────────────────────────────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_et():%Y-%m-%d %H:%M:%S} ET] {msg}"
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            LOG_FILE.rename(LOG_FILE.with_suffix(".log.old"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    try:
        send_message(msg)
        log("  → Telegram sent.")
    except TelegramConfigError as exc:
        log(f"  Telegram not configured: {exc}")
    except Exception as exc:
        log(f"  Telegram failed: {exc}")


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

    if use_full:
        return WATCHLIST, True
    return shortlist, False


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
            "total_pnl_pct": (total_pnl / total_cost * 100) if total_cost else 0.0,
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
        log(f"  SPX 5d: {ctx.tsx_5d_pct:+.2f}%  |  {sector_parts}")
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
        f"  🎯 Score: <b>{top.score:.1f}</b>  ({top.confidence})\n\n"
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
    unrealized = shares * price - cost
    pnl_pct    = unrealized / cost * 100 if cost else 0.0
    color      = _pnl_color(unrealized)
    arrow      = _pnl_arrow(unrealized)
    if next_sell_dt is not None:
        secs_left = max(0, (next_sell_dt - now_et()).total_seconds())
        mins_left = int(secs_left / 60)
        sell_label = f"{next_sell_dt:%I:%M %p} ET"
    else:
        mins_left  = int(_time_until_sell() / 60)
        sell_label = "3:55 PM ET"
    h_left     = mins_left // 60
    m_left     = mins_left % 60
    stats      = _get_trade_stats()
    at_color   = _pnl_color(stats["total_pnl"])
    time_str   = f"{h_left}h {m_left:02d}min" if h_left else f"{m_left} min"

    return (
        f"{color} <b>Position Update — <code>{symbol}</code></b>  |  {now_et():%H:%M} ET\n\n"
        f"  {arrow} Price: <b>${price:.2f}</b>  (entry ${entry:.2f})\n"
        f"  {color} Unrealized P&L: <b>${unrealized:+.2f} USD ({pnl_pct:+.2f}%)</b>\n"
        f"  💼 Position value: <b>${shares * price:.2f} USD</b>\n\n"
        f"  ⏰ Selling in <b>{time_str}</b>  ({sell_label})\n"
        f"  {at_color} All-time: <b>${stats['total_pnl']:+.2f} USD</b>"
        f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
    )


def build_sell_message(
    symbol: str, entry: float, exit_price: float, shares: float,
    cost: float, trade_pnl: float, at_pnl: float,
) -> str:
    pnl_pct    = trade_pnl / cost * 100 if cost else 0.0
    proceeds   = shares * exit_price
    color      = _pnl_color(trade_pnl)
    at_color   = _pnl_color(at_pnl)
    stats      = _get_trade_stats()
    win_rate   = (stats["wins"] / stats["count"] * 100) if stats["count"] else 0.0

    return (
        f"🏁 <b>ALL SOLD — <code>{symbol}</code></b>\n\n"
        f"  ⏰ 3:55 PM ET — Hard close executed\n"
        f"  Entry: ${entry:.2f}  →  Exit: <b>${exit_price:.2f} USD</b>\n"
        f"  🔢 Shares sold: {shares:.4f}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TODAY'S RESULT:</b>\n"
        f"  {color} P&L: <b>${trade_pnl:+.2f} USD ({pnl_pct:+.2f}%)</b>\n"
        f"  💰 Invested: ${cost:.2f}  →  Proceeds: <b>${proceeds:.2f}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ACCOUNT:</b>\n"
        f"  {at_color} All-time PnL: <b>${at_pnl:+.2f} USD</b>"
        f"  ({stats['total_pnl_pct']:+.2f}% ROI)\n"
        f"  🏆 Record: <b>{stats['wins']}W / {stats['losses']}L</b>"
        f"  ({win_rate:.0f}% win rate)  |  {stats['count']} total trades\n"
        f"  {'🚀 Account GREEN — keep it going!' if at_pnl >= 0 else '💪 Account RED — grind it back!'}"
    )


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

    order       = _parse_order_result(result.stdout)
    actual_cost = float(order.get("estimated_value", deploy) or deploy)
    actual_qty  = float(order.get("estimated_quantity") or
                        (actual_cost / pick.last_close if pick.last_close else shares_est))

    pos = {
        "symbol": pick.symbol, "buyPrice": pick.last_close,
        "shares": actual_qty, "estimatedCost": actual_cost,
        "sellAll": True, "strategyName": pick.strategy_name,
        "time": now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos))
    _append_trade_history(pick.symbol, "BUY", pick.last_close, actual_qty,
                          actual_cost, 0.0, pick.strategy_name)

    at_pnl  = _get_total_pnl()
    at_color = _pnl_color(at_pnl)
    notify(
        f"✅ <b>Buy order submitted</b>\n\n"
        f"🎫 <code>{pick.symbol}</code>\n"
        f"🔢 Shares: <b>{actual_qty:.4f}</b>  |  💵 Entry: <b>${pick.last_close:.2f} USD</b>\n"
        f"💰 Invested: <b>${actual_cost:.2f} USD</b>\n"
        f"🎯 Target: <b>+{_PROFIT_TARGET_PCT:.0f}%</b>  |  3:55 PM lock if +{_LATE_LOCK_PCT:.0f}%  |  No stop loss\n\n"
        f"{at_color} All-time PnL: <b>${at_pnl:+.2f} USD</b>"
    )
    log(f"Buy confirmed: {actual_qty:.4f} sh @ ${pick.last_close:.2f}  cost ${actual_cost:.2f}")
    return True


def wait_for_fill_confirm(symbol: str) -> None:
    now = now_et()
    confirm_t = now.replace(hour=9, minute=45, second=0, microsecond=0)
    secs = (confirm_t - now).total_seconds()
    if secs > 0:
        log(f"Pre-market order queued — waiting {secs/60:.1f} min for 9:45 fill check...")
        time.sleep(secs)
    log("Confirming fill at 9:45 AM...")
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
    """True if we're in the weekday 4:00 PM – 7:30 PM ET after-hours window."""
    n = now_et()
    if n.weekday() >= 5:
        return False
    after_open  = n.hour >= _AH_BUY_START_HOUR
    before_close = n.hour < _AH_BUY_END_HOUR or (
        n.hour == _AH_BUY_END_HOUR and n.minute < _AH_BUY_END_MINUTE
    )
    return after_open and before_close


def _scan_afterhours(watchlist: list[str]) -> list[dict]:
    """
    Scan top tickers for after-hours momentum.
    Returns list of dicts sorted by AH gain, filtered to min _AH_MIN_PCT.
    Uses yf fast_info (one-by-one but lightweight) for speed.
    """
    import yfinance as yf
    picks = []
    subset = watchlist[:_AH_WATCHLIST_SIZE]
    log(f"After-hours scan: checking {len(subset)} tickers...")
    for sym in subset:
        try:
            fi = yf.Ticker(sym).fast_info
            close = fi.previous_close
            last  = fi.last_price
            if not close or not last or close <= 0:
                continue
            ah_pct = (last / close - 1) * 100
            if ah_pct < _AH_MIN_PCT:
                continue
            picks.append({
                "symbol":  sym,
                "close":   round(close, 4),
                "ah_price": round(last, 4),
                "ah_pct":  round(ah_pct, 2),
                "score":   round(ah_pct, 2),
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
    if not order_data.get("submitted") and buy_result.returncode != 0:
        log(f"AH buy failed for {sym}")
        notify(f"❌ After-hours buy failed for <code>{sym}</code>.")
        return False

    actual_shares = float(order_data.get("estimated_quantity") or shares_est)
    actual_value  = float(order_data.get("estimated_value") or (actual_shares * limit_price))
    actual_price  = actual_value / actual_shares if actual_shares else limit_price

    pos = {
        "symbol":        sym,
        "buyPrice":      actual_price,
        "shares":        actual_shares,
        "estimatedCost": actual_value,
        "sellAll":       True,
        "strategyName":  "After-Hours Limit",
        "afterHours":    True,
        "time":          now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos, indent=2))
    _append_trade_history(sym, "BUY", actual_price, actual_shares, actual_value, 0.0, "After-Hours Limit")

    log(f"AH buy confirmed: {actual_shares:.4f} sh {sym} @ ${actual_price:.2f}  cost ${actual_value:.2f}")
    notify(
        f"✅ <b>AH buy confirmed — <code>{sym}</code></b>\n\n"
        f"💰 {actual_shares:.4f} sh @ ${actual_price:.2f}  (cost ${actual_value:.2f})\n"
        f"🎯 Profit target: +{_AH_PROFIT_PCT:.0f}%  |  Exits at 9:35 AM if not hit"
    )
    return True


def _afterhours_sell_limit(symbol: str, entry: float, shares: float, cost: float,
                            limit_price: float, label: str = "AH target") -> bool:
    """Place a limit sell order during extended hours."""
    notify(
        f"🎯 <b>AH limit SELL — <code>{symbol}</code></b>\n\n"
        f"📉 Limit: <b>${limit_price:.2f}</b>  |  Entry: ${entry:.2f}\n"
        f"💰 Reason: {label}"
    )
    # Pass --shares so the limit sell form can fill fractional amount if Max gives whole numbers
    sell_cmd = [
        PYTHON, str(AUTO_SCRIPT), "sell",
        "--symbol", symbol,
        "--sell-all",
        "--price", f"{limit_price:.2f}",
        "--shares", str(int(shares) if shares >= 1 else 1),
    ]
    sell_result = subprocess.run(
        sell_cmd,
        capture_output=True, text=True, timeout=180,
    )
    for line in sell_result.stdout.splitlines():
        print(f"  {line}", flush=True)

    order_data = _parse_order_result(sell_result.stdout)
    submitted  = order_data.get("submitted") or sell_result.returncode == 0
    if not submitted:
        log(f"AH limit sell failed for {symbol}")
        notify(f"⚠️ AH limit sell order failed for <code>{symbol}</code> — will retry at 9:31 AM.")
        return False

    # Use actual shares sold and compute proper proceeds
    actual_qty   = float(order_data.get("estimated_quantity") or shares)
    actual_value = actual_qty * limit_price  # limit price × qty = proper proceeds
    actual_price = actual_value / actual_qty if actual_qty else limit_price
    trade_pnl    = actual_value - cost
    at_pnl       = _record_trade(symbol, cost, actual_value, actual_qty)

    _append_trade_history(symbol, "SELL", actual_price, actual_qty, cost, trade_pnl, "After-Hours Limit")
    POS_FILE.unlink(missing_ok=True)
    notify(build_sell_message(symbol, entry, actual_price, actual_qty, cost, trade_pnl, at_pnl))
    log(f"AH sell confirmed: ${trade_pnl:+.2f} trade P&L  |  All-time: ${at_pnl:+.2f}")
    return True


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

        # Check profit target every cycle
        try:
            fi    = yf.Ticker(symbol).fast_info
            price = fi.last_price
            if price and entry > 0:
                pnl_pct = (price - entry) / entry * 100
                if pnl_pct >= _AH_PROFIT_PCT:
                    log(f"AH PROFIT TARGET: {pnl_pct:+.1f}% — limit sell now")
                    notify(
                        f"🎯 <b>AH PROFIT TARGET HIT — <code>{symbol}</code></b>\n\n"
                        f"📈 {pnl_pct:+.1f}%  |  AH price: ${price:.2f}\n"
                        f"💰 Placing limit sell at ${price:.2f}"
                    )
                    _afterhours_sell_limit(symbol, entry, shares, cost, round(price, 2), "AH profit target")
                    return
                log(f"AH update: {symbol}  ${price:.2f}  ({pnl_pct:+.2f}%)")
        except Exception as exc:
            log(f"AH price check error: {exc}")

        time.sleep(600)  # 10-min intervals during AH


def _run_afterhours_strategy(balance: float, sell_existing: bool = False) -> None:
    """
    Full after-hours routine:
    1. If sell_existing → limit-sell the current position first.
    2. Scan for best AH mover, place limit buy.
    3. Monitor with _afterhours_hold_loop().
    """
    if not _is_afterhours_window():
        log("Not in AH window — skipping after-hours strategy.")
        return

    # ── Step 1: limit-sell the existing position ──────────────────────────
    if sell_existing and POS_FILE.exists():
        try:
            pos    = json.loads(POS_FILE.read_text())
            sym    = pos["symbol"]
            entry  = float(pos.get("buyPrice", 0))
            shares = float(pos.get("shares", 0))
            cost   = float(pos.get("estimatedCost", shares * entry))

            import yfinance as yf
            fi  = yf.Ticker(sym).fast_info
            ah  = fi.last_price or entry
            limit_price = round(max(ah, entry * 1.003), 2)  # at least entry+0.3%

            log(f"AH limit sell of existing position: {sym} @ ${limit_price:.2f}")
            sold = _afterhours_sell_limit(sym, entry, shares, cost,
                                           limit_price, "AH strategy entry")
            if not sold:
                log("AH sell of existing position failed — not placing new AH buy.")
                return
        except Exception as exc:
            log(f"AH sell step error: {exc}")
            return

    # ── Step 2: scan for best AH mover ────────────────────────────────────
    if POS_FILE.exists():
        log("Position still open after AH sell attempt — skipping AH buy scan.")
        return

    from kzer_bot.grinder_strategy import _load_watchlist
    watchlist = _load_watchlist()
    picks = _scan_afterhours(watchlist)

    if not picks:
        log("No AH picks found — holding cash until market open.")
        notify(
            f"🌙 <b>After-hours scan complete</b>\n\n"
            f"No tickers meet the +{_AH_MIN_PCT:.1f}% AH threshold — holding cash.\n"
            f"📅 Next entry: <b>9:35 AM ET tomorrow</b>"
        )
        return

    top = picks[:5]
    top_str = "\n".join(
        f"  • <code>{p['symbol']}</code>  AH: <b>+{p['ah_pct']:.2f}%</b>  @ ${p['ah_price']:.2f}"
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
# Sell execution helper (used for both 9:31 AM overnight sell and 3:55 PM sell)
# ──────────────────────────────────────────────────────────────────────────────

def _execute_sell_order(
    symbol: str, entry: float, shares: float, cost: float, strat: str,
    label: str = "3:55 PM ET",
) -> None:
    from kzer_bot.market_data import YFinanceMarketData
    snap = YFinanceMarketData().snapshot(symbol)
    exit_price = snap.last_price if snap else entry
    unrealized  = shares * exit_price - cost
    pnl_pct     = unrealized / cost * 100 if cost else 0.0

    notify(
        f"⏳ <b>Closing at {label}</b>\n\n"
        f"🎫 <code>{symbol}</code>  |  💵 ${exit_price:.2f}\n"
        f"{_pnl_arrow(unrealized)} Est. P&L: <b>${unrealized:+.2f} USD ({pnl_pct:+.2f}%)</b>"
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
    actual_value = float(order_data.get("estimated_value") or (actual_qty * exit_price))
    actual_price = actual_value / actual_qty if actual_qty else exit_price
    trade_pnl    = actual_value - cost
    at_pnl       = _record_trade(symbol, cost, actual_value, actual_qty)

    _append_trade_history(symbol, "SELL", actual_price, actual_qty, cost, trade_pnl, strat)
    POS_FILE.unlink(missing_ok=True)

    notify(build_sell_message(symbol, entry, actual_price, actual_qty, cost, trade_pnl, at_pnl))
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
    If position file has forceSell=true, always rotates out.
    """
    log(f"Morning hold check for {symbol}...")
    # Honour explicit force-sell flag (e.g. user overrides or overnight session issue)
    try:
        if POS_FILE.exists():
            _pos = json.loads(POS_FILE.read_text())
            if _pos.get("forceSell"):
                log(f"  forceSell=true — rotating out of {symbol} unconditionally")
                notify(
                    f"🔄 <b>9:31 AM — Selling <code>{symbol}</code> (forced exit)</b>\n\n"
                    f"📋 Position flagged for mandatory exit\n"
                    f"🔍 Scanning for new pick at 9:35 AM..."
                )
                return False
    except Exception:
        pass
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
    symbol = pos["symbol"]
    entry  = float(pos.get("buyPrice", 0))
    cost   = float(pos.get("estimatedCost", 0))
    shares = float(pos.get("shares", 0))
    strat  = pos.get("strategyName", "")

    if shares < 0.01 and cost > 0 and entry > 0:
        shares = cost / entry
        log(f"Corrected share count to {shares:.4f}")

    from kzer_bot.market_data import YFinanceMarketData
    md = YFinanceMarketData()

    last_update_t = 0.0
    UPDATE_INTERVAL = 1800  # 30 min

    log(f"Holding {symbol}  {shares:.4f} sh @ ${entry:.2f}  (cost ${cost:.2f})")

    now = now_et()

    # ── Overnight path: after 3:55 PM, OR position bought on a previous calendar day ──
    pos_time      = _parse_ts(pos.get("time"))
    bought_prev_day = pos_time is not None and pos_time.date() < now.date()
    is_overnight  = (
        now.hour > _SELL_HOUR or
        (now.hour == _SELL_HOUR and now.minute >= _SELL_MINUTE) or
        bought_prev_day
    )
    if is_overnight:
        next_sell = now.replace(
            hour=_OVERNIGHT_SELL_HOUR, minute=_OVERNIGHT_SELL_MINUTE, second=0, microsecond=0,
        )
        # if 9:31 AM is already past today, push to tomorrow
        if now >= next_sell:
            next_sell += timedelta(days=1)
        while next_sell.weekday() >= 5:
            next_sell += timedelta(days=1)

        secs = (next_sell - now).total_seconds()
        log(f"After-hours buy — selling at market open {next_sell:%a %b %d %H:%M} ET ({secs/3600:.1f}h). Will buy new pick at 9:35 AM.")

        pre_open = now.hour < 9 or (now.hour == 9 and now.minute < 30)
        if pre_open:
            open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
            mins_to_open = max(0, int((open_t - now).total_seconds() / 60))
            notify(
                f"⏳ <b>Order placed — pending fill at open</b>\n\n"
                f"🎫 <code>{symbol}</code>  {shares:.4f} sh @ ~${entry:.2f}\n"
                f"💰 Deploying: <b>${cost:.2f} USD</b>  |  📋 {strat}\n"
                f"📋 Pre-market order — fills at <b>9:30 AM ET open</b>  ({mins_to_open} min)\n"
                f"🔄 Morning decision at <b>9:31 AM ET</b> — hold if trending or rotate to new pick\n"
                f"🎯 Target: +{_PROFIT_TARGET_PCT:.0f}%  |  3:55 PM lock: +{_LATE_LOCK_PCT:.0f}%  |  No stop loss"
            )
        else:
            notify(
                f"📊 <b>Position open — overnight hold</b>\n\n"
                f"🎫 <code>{symbol}</code>  |  {shares:.4f} sh @ ${entry:.2f}\n"
                f"💰 Cost: <b>${cost:.2f} USD</b>  |  📋 {strat}\n"
                f"🔄 Morning decision at <b>9:31 AM ET</b> — hold if trending or rotate to new pick\n"
                f"🎯 Target: +{_PROFIT_TARGET_PCT:.0f}%  |  3:55 PM lock: +{_LATE_LOCK_PCT:.0f}%  |  No stop loss"
            )

        fill_notified = not pre_open
        _scanned: set[str] = set()
        balance_approx = balance if balance > 0 else cost

        while now_et() < next_sell - timedelta(seconds=30):
            cur = now_et()

            # Fill confirmation at 9:30 AM market open
            if not fill_notified and (cur.hour > 9 or (cur.hour == 9 and cur.minute >= 30)):
                fill_notified = True
                wait_for_fill_confirm(symbol)
                log("Fill confirmed at market open.")

            # 5 PM preview scan (tonight)
            if "5pm" not in _scanned and cur.hour >= 17 and cur.weekday() < 5:
                _scanned.add("5pm")
                _run_overnight_scan("5 PM Tonight's Preview", balance_approx, "5pm")

            # 5 AM morning scan
            if "5am" not in _scanned and 5 <= cur.hour < 9:
                _scanned.add("5am")
                _run_overnight_scan("5 AM Morning Scan", balance_approx, "5am")

            # 30-min position update
            if time.time() - last_update_t >= UPDATE_INTERVAL:
                try:
                    snap = md.snapshot(symbol)
                    if snap:
                        if fill_notified:
                            notify(build_update_message(
                                symbol, entry, snap.last_price, shares, cost,
                                next_sell_dt=next_sell,
                            ))
                            log(f"30-min update: ${snap.last_price:.2f} ({(snap.last_price/entry-1)*100:+.2f}%)")
                        else:
                            cur2 = now_et()
                            open_t2 = cur2.replace(hour=9, minute=30, second=0, microsecond=0)
                            mins_left = max(0, int((open_t2 - cur2).total_seconds() / 60))
                            notify(
                                f"⏳ <b>Order Pending — <code>{symbol}</code></b>  |  {cur2:%H:%M} ET\n\n"
                                f"📋 Pre-market order fills at <b>9:30 AM ET open</b>\n"
                                f"🎫 {shares:.4f} sh @ ~${entry:.2f}  |  💰 ${cost:.2f} USD\n"
                                f"⏰ Market opens in <b>{mins_left} min</b>\n"
                                f"🔄 Sell at 9:31 AM → buy new pick at 9:35 AM"
                            )
                            log(f"Pre-open update: order pending, {mins_left} min to open")
                except Exception as exc:
                    log(f"30-min update error: {exc}")
                last_update_t = time.time()

            time.sleep(60)

        # ── Morning decision: sell or hold another day ─────────────────────
        _sleep_until(next_sell, "9:31 AM morning decision")
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
        f"🎯 Profit target: <b>+{_PROFIT_TARGET_PCT:.0f}%</b>  |  "
        f"Late lock: <b>+{_LATE_LOCK_PCT:.0f}%</b> at 3:55 PM\n"
        f"🌙 Below target at 3:55 PM → hold overnight, no stop loss"
    )

    while True:
        now = now_et()

        # ── 3:55 PM: sell if profitable enough, else hold overnight ───────
        if now.hour > _SELL_HOUR or (now.hour == _SELL_HOUR and now.minute >= _SELL_MINUTE):
            try:
                snap_eod = md.snapshot(symbol)
                price_eod = snap_eod.last_price if snap_eod else entry
            except Exception:
                price_eod = entry
            eod_pct = (price_eod - entry) / entry * 100 if entry > 0 else 0
            if eod_pct >= _LATE_LOCK_PCT:
                log(f"3:55 PM lock-in: {eod_pct:+.1f}% ≥ +{_LATE_LOCK_PCT:.0f}% — selling")
                _execute_sell_order(symbol, entry, shares, cost, strat, "3:55 PM lock-in")
            else:
                log(f"3:55 PM: {eod_pct:+.1f}% — below lock, holding overnight")
                notify(
                    f"🌙 <b>Holding overnight — <code>{symbol}</code></b>\n\n"
                    f"💼 {shares:.4f} sh @ ${entry:.2f}  |  ~${price_eod:.2f} ({eod_pct:+.1f}%)\n"
                    f"📋 Below +{_LATE_LOCK_PCT:.0f}% threshold — no stop loss, holding for recovery\n"
                    f"⏰ Morning decision at <b>9:31 AM tomorrow</b>"
                )
            return

        # ── forceSell flag check — immediate exit ─────────────────────────
        if POS_FILE.exists():
            try:
                _pos_check = json.loads(POS_FILE.read_text())
                if _pos_check.get("forceSell"):
                    log(f"forceSell flag detected — selling {symbol} immediately")
                    notify(
                        f"🔄 <b>Force sell triggered — <code>{symbol}</code></b>\n\n"
                        f"📋 Position flagged for immediate exit\n"
                        f"🔍 Will scan for new pick after sell..."
                    )
                    _execute_sell_order(symbol, entry, shares, cost, strat, "Force Sell")
                    return
            except Exception:
                pass

        # ── 30-min update + profit target check ───────────────────────────
        if time.time() - last_update_t >= UPDATE_INTERVAL:
            try:
                snap = md.snapshot(symbol)
                if snap:
                    price   = snap.last_price
                    pnl_pct = (price - entry) / entry * 100 if entry > 0 else 0

                    # Profit target: sell immediately if up 5%+ (after 10:30 AM)
                    after_1030 = now.hour > 10 or (now.hour == 10 and now.minute >= 30)
                    if pnl_pct >= _PROFIT_TARGET_PCT and after_1030:
                        log(f"PROFIT TARGET: {pnl_pct:+.1f}% ≥ +{_PROFIT_TARGET_PCT:.0f}% — selling now")
                        notify(
                            f"🎯 <b>PROFIT TARGET HIT — selling <code>{symbol}</code></b>\n\n"
                            f"📈 Unrealized: <b>{pnl_pct:+.1f}%</b>  |  Price: ${price:.2f}\n"
                            f"💰 Locking in gains — executing sell now"
                        )
                        _execute_sell_order(symbol, entry, shares, cost, strat, "Profit Target")
                        return

                    notify(build_update_message(symbol, entry, price, shares, cost))
                    log(f"30-min update: ${price:.2f} ({pnl_pct:+.2f}%)")
            except Exception as exc:
                log(f"30-min update error: {exc}")
            last_update_t = time.time()

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

    # Startup scan on first entry — skip silently after 9 AM if 5 AM scan is cached
    if "startup" not in scans_done:
        if now_et().hour >= 9 and _load_cached_scan_result() is not None:
            cached = _load_cached_scan_result()
            if cached:
                bias = cached[1]
                log("Near buy window with cached scan — skipping startup notification.")
        else:
            bias = _do_scan("Startup Scan")
        scans_done.add("startup")

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
            continue

        # 5 AM main scan + game plan
        if _should_fire("5am", 5, 0, scans_done) and not _passed_today(9, 10):
            bias = _do_scan("5 AM Morning Scan")
            scans_done.add("5am")

        # 5 PM next-day preview
        if _should_fire("5pm", 17, 0, scans_done):
            _do_scan("5 PM Tomorrow's Preview")
            scans_done.add("5pm")

        # Reset slots at midnight
        if now.hour == 0 and now.minute < 2:
            scans_done.discard("5am")
            scans_done.discard("5pm")

        # Near buy window → exit sleep loop
        if now.weekday() < 5:
            target_t = now.replace(hour=_BUY_HOUR, minute=_BUY_MINUTE, second=0, microsecond=0)
            catchup_t = now.replace(hour=_BUY_CATCHUP_HOUR, minute=_BUY_CATCHUP_MINUTE, second=0, microsecond=0)
            if 0 <= (target_t - now).total_seconds() <= 300 or target_t <= now <= catchup_t:
                log("Near buy window — exiting overnight loop.")
                return bias

        # Past buy window for today → sleep until 5 PM
        cutoff = now.replace(hour=_BUY_CATCHUP_HOUR, minute=_BUY_CATCHUP_MINUTE, second=0, microsecond=0)
        if now > cutoff and now.hour < 17:
            log("Buy window passed — sleeping until 5 PM scan.")
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
                        help="Skip 4000-stock scan and buy this single ticker immediately (e.g. SHOP.TO)")
    parser.add_argument("--lite", action="store_true",
                        help="Use hardcoded ~100 ticker watchlist instead of full 4000-stock universe (avoids rate limits)")
    parser.add_argument("--yahoo", action="store_true",
                        help="Use Yahoo Finance most active Canadian stocks (~755 tickers sorted by volume)")
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
        log(f"Yahoo mode: using top {len(WATCHLIST)} most active Canadian stocks")
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
        pos  = json.loads(POS_FILE.read_text())
        _now = now_et()
        _pre_open = _now.hour < 9 or (_now.hour == 9 and _now.minute < 30)
        log(f"Open position found: {pos['symbol']} — resuming hold/sell loop.")

        if _is_afterhours_window():
            # After-hours window: limit-sell existing position then scan for AH buy
            log("After-hours window — running AH strategy on existing position.")
            notify(
                f"🌙 <b>After-hours window detected</b>\n\n"
                f"🎫 Holding <code>{pos['symbol']}</code>  "
                f"{pos.get('shares', 0):.4f} sh @ ${pos.get('buyPrice', 0):.2f}\n"
                f"💡 Attempting limit sell + AH rotation..."
            )
            _run_afterhours_strategy(balance, sell_existing=True)
            # If position still open (sell failed), fall through to overnight hold
            if POS_FILE.exists():
                log("AH sell failed — falling back to overnight hold.")
                hold_and_sell(balance=balance)
        elif _pre_open:
            _open_t = _now.replace(hour=9, minute=30, second=0, microsecond=0)
            _mins = max(0, int((_open_t - _now).total_seconds() / 60))
            notify(
                f"⏳ <b>Bot restarted — order pending fill</b>\n\n"
                f"🎫 <code>{pos['symbol']}</code>  "
                f"{pos.get('shares', 0):.4f} sh @ ~${pos.get('buyPrice', 0):.2f}\n"
                f"📋 Pre-market order fills at <b>9:30 AM ET open</b>  ({_mins} min)\n"
                f"🎯 Autonomous exit: +{_PROFIT_TARGET_PCT:.0f}% target  |  lock at 3:55 PM if +{_LATE_LOCK_PCT:.0f}%"
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
        # nothing to clean up here — hold_and_sell / AH strategy manages pos file

    # ── Single-ticker mode (skip 4000-stock scan, buy immediately) ────────
    if args.ticker:
        raw_symbol = args.ticker.upper().strip()

        # Auto-append .TO if no exchange suffix given
        if "." not in raw_symbol:
            raw_symbol = f"{raw_symbol}.TO"

        log(f"Single-ticker mode: {raw_symbol}")
        log("Fetching data for single ticker...")

        md = GrinderMarketData()
        snap = md.snapshot(raw_symbol)

        # Fallback: try .V if .TO returned nothing
        if snap is None and raw_symbol.endswith(".TO"):
            alt = raw_symbol.replace(".TO", ".V")
            log(f"  No data for {raw_symbol}, trying {alt}...")
            snap = md.snapshot(alt)
            if snap is not None:
                raw_symbol = alt

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
    scans_done:    set[str] = set()
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

        # ── After-hours buy: no position + AH window + past regular close ──
        _now_main = now_et()
        _past_close = _now_main.hour > _SELL_HOUR or (
            _now_main.hour == _SELL_HOUR and _now_main.minute >= _SELL_MINUTE
        )
        if not POS_FILE.exists() and _is_afterhours_window() and _past_close:
            log("No position in AH window — scanning for after-hours buy.")
            _run_afterhours_strategy(balance, sell_existing=False)
            # If AH buy placed and monitored, fall through to overnight wait
            if not args.balance:
                fresh = fetch_live_balance(retries=2)
                if fresh:
                    balance = fresh

        if not skip_wait:
            bias = wait_overnight(bias, scans_done, last_status_t, balance)
        skip_wait = False
        scans_done.clear()

        # Weekday guard
        if now_et().weekday() >= 5:
            log("Weekend — back to overnight loop.")
            continue

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

        # Execute buy
        ok = execute_buy(top, balance, bias, fut_det, ai, fixed_shares=args.shares)
        if not ok:
            log("Buy failed — entering overnight loop.")
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
            top = _picks[0]
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
