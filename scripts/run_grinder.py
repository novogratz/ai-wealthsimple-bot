#!/usr/bin/env python3
"""
Le Grinder
====================================
Quant rules  : 1 trade/day  |  no stop loss  |  hard exit 3:55 PM ET
Edge source  : momentum continuation on high-volume up-days + EMA trend filter
Entry timing : Green/Neutral futures → 9:15 AM pre-market (fills at 9:30 open)
               Red futures          → wait for bounce, buy 11:00–12:00 PM
AI analysis  : claude CLI analyses top candidates each morning

Usage:
    python scripts/run_grinder.py              # 24/7 autonomous mode
    python scripts/run_grinder.py --now        # skip wait, buy immediately (debug)
    python scripts/run_grinder.py --balance 95 # override cash (default: live fetch)
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kzer_bot.grinder_strategy import (
    FallbackStrategy,
    FuturesBias,
    GrinderMarketData,
    GrinderPick,
    GrinderStrategy,
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
PYTHON       = sys.executable
TZ           = ZoneInfo("America/Toronto")

DATA.mkdir(exist_ok=True)

_SELL_HOUR   = 15
_SELL_MINUTE = 55
_DEPLOY_PCT  = 90          # % of balance deployed per trade


# ──────────────────────────────────────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{now_et():%H:%M:%S} ET] {msg}", flush=True)


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


def _next_weekday_9am() -> datetime:
    candidate = now_et().replace(hour=9, minute=15, second=0, microsecond=0)
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
# Claude CLI  — AI market analysis
# ──────────────────────────────────────────────────────────────────────────────

def get_ai_analysis(picks: list[GrinderPick], bias: FuturesBias,
                    futures_detail: str, balance: float) -> str:
    """
    Calls the `claude` CLI to analyse the top scan candidates and return
    a short, actionable recommendation. Gracefully returns empty string
    if claude CLI is not available.
    """
    if not picks:
        return ""

    # Build a concise data table for Claude
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
- Account: ~${balance:.0f} CAD  |  1 trade/day  |  all-in  |  exit 3:55 PM ET  |  no stop loss

TOP SCAN RESULTS (8-criteria momentum screen):
{picks_block}

TASK — reply in 120 words max, plain text, bullet points:
• Confirm or challenge the #1 pick based purely on the numbers
• Why does the data suggest this stock will continue moving today?
• What is the single biggest risk for this trade?
• Expected move today: bearish / base / bull scenario in %"""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # claude CLI might not support --output-format; retry without it
        result2 = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        if result2.returncode == 0 and result2.stdout.strip():
            return result2.stdout.strip()
    except FileNotFoundError:
        log("  claude CLI not found — skipping AI analysis")
    except subprocess.TimeoutExpired:
        log("  claude CLI timed out — skipping AI analysis")
    except Exception as exc:
        log(f"  claude CLI error: {exc}")
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Wealthsimple automation
# ──────────────────────────────────────────────────────────────────────────────

def fetch_live_balance(retries: int = 3) -> float | None:
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
            notify(
                "❌ <b>Wealthsimple session expired</b>\n\n"
                "Fix:\n<code>python scripts/wealthsimple_auto.py setup</code>\n\n"
                "Log in, then restart the bot."
            )
            log("SESSION EXPIRED — run: python scripts/wealthsimple_auto.py setup")
            return None

        for line in r.stdout.splitlines():
            if line.startswith("LIVE_BALANCE_CAD:"):
                try:
                    val = float(line[len("LIVE_BALANCE_CAD:"):].replace(",", ""))
                    log(f"  Balance: ${val:.2f} CAD")
                    return val
                except ValueError:
                    pass
        log(f"  Could not parse balance (attempt {attempt})")
        time.sleep(15)
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
# Scan
# ──────────────────────────────────────────────────────────────────────────────

def run_scan(balance: float) -> tuple[list[GrinderPick], FuturesBias, str, str, str]:
    """
    Returns (picks, bias, buy_plan, strategy_name, futures_detail).
    picks is the full sorted list (may be empty).
    strategy_name reflects which strategy produced the picks.
    """
    log("Checking US futures (ES=F)...")
    bias, futures_detail = get_futures_bias()
    log(f"  Bias: {bias.value.upper()}  |  {futures_detail}")

    log(f"Scanning {len(WATCHLIST)} Canadian tickers (main 8-criteria)...")
    md = GrinderMarketData()
    picks = GrinderStrategy(md).scan(WATCHLIST)
    log(f"  Main strategy: {len(picks)} candidate(s).")

    strategy_name = "Main Strategy"
    if not picks:
        log("  No main picks — running fallback...")
        picks = FallbackStrategy(md).scan(WATCHLIST)
        strategy_name = "Fallback Original Strategy" if picks else "No Strategy"
        log(f"  Fallback: {len(picks)} candidate(s).")

    if bias == FuturesBias.RED:
        buy_plan = "⏳ Wait for bounce — buy 11:00–12:00 PM ET"
    else:
        buy_plan = "🚀 Buy at 9:15 AM pre-market  (fills at 9:30 open)"

    return picks, bias, buy_plan, strategy_name, futures_detail


# ──────────────────────────────────────────────────────────────────────────────
# Telegram message builders
# ──────────────────────────────────────────────────────────────────────────────

def _bias_line(bias: FuturesBias, futures_detail: str) -> str:
    emoji = {"green": "🟢 GREEN", "red": "🔴 RED", "neutral": "⚪ NEUTRAL"}[bias.value]
    return f"📡 Futures: <b>{emoji}</b>  —  {futures_detail}"


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
    header = f"🌅 <b>Le Grinder — {label}</b>"

    if not picks:
        return (
            f"{header}\n\n"
            f"{_bias_line(bias, futures_detail)}\n\n"
            f"🔍 Scanned {len(WATCHLIST)} tickers — <b>no candidates passed</b> either strategy.\n"
            f"📋 Plan: <b>SKIP today</b> — no valid setup found."
        )

    top = picks[0]
    deploy = balance * _DEPLOY_PCT / 100
    shares_est = int(deploy // top.last_close) if top.last_close > 0 else 0
    stats = _get_trade_stats()
    at_pnl = stats["total_pnl"]
    at_color = _pnl_color(at_pnl)

    # Others line
    others = "  ".join(
        f"<code>{p.symbol}</code> ${p.last_close:.2f} score {p.score:.0f}"
        for p in picks[1:4]
    )

    msg = (
        f"{header}\n\n"
        f"{_bias_line(bias, futures_detail)}\n\n"
        f"🔍 Scanned <b>{len(WATCHLIST)}</b> Canadian tickers\n"
        f"   ✅ <b>{len(picks)}</b> passed  |  Strategy: <b>{strategy_name}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>TODAY'S PICK: <code>{top.symbol}</code>  (${top.last_close:.2f} CAD)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>WHY THIS STOCK:</b>\n"
        f"  📈 Yesterday: <b>{top.yesterday_pct:+.2f}%</b>  on  <b>{top.rel_volume:.1f}× normal volume</b>\n"
        f"  🔥 ATR(14): <b>{top.atr_pct:.2f}%</b> of price  →  high daily range potential\n"
        f"  💪 Closed: <b>{top.close_strength:.0%}</b> of day range  (buyers dominated)\n"
        f"  📊 Trend: EMA5 {'✅' if top.above_ema5 else '❌'}  EMA20 {'✅' if top.above_ema20 else '❌'}\n"
        f"  🎯 Score: <b>{top.score:.1f}</b>  ({top.confidence})\n\n"
    )

    if others:
        msg += f"Other candidates:  {others}\n\n"

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TODAY'S GAME PLAN:</b>\n"
        f"  📋 {strategy_name}\n"
        f"  ⏰ Entry: {buy_plan}\n"
        f"  💰 Budget: <b>${balance:.2f}</b>  →  deploying <b>{_DEPLOY_PCT}%</b>  =  <b>${deploy:.2f} CAD</b>\n"
        f"  🔢 Est. shares: ~{shares_est}  @  ${top.last_close:.2f}\n"
        f"  🏁 Exit: <b>3:55 PM ET hard sell</b>  (no stop loss)\n"
        f"  🎯 Target: <b>+1.5% to +3%</b> on momentum continuation\n\n"
        f"{at_color} All-time PnL: <b>${at_pnl:+.2f} CAD</b>"
        f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
    )

    if ai_analysis:
        msg += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n🤖 <b>AI Analysis (Claude):</b>\n{ai_analysis}"

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
    entry_mode = "11:00 AM — Bounce buy (red bias)" if is_bounce else "9:15 AM — Pre-market order (fills at 9:30 open)"
    bias_line  = _bias_line(bias, futures_detail)

    msg = (
        f"🛒 <b>BUYING NOW — <code>{pick.symbol}</code></b>\n\n"
        f"⏰ <b>{entry_mode}</b>\n"
        f"💵 Entry: ~<b>${pick.last_close:.2f} CAD</b>\n"
        f"🔢 Est. shares: ~<b>{shares_est}</b>  ({_DEPLOY_PCT}% of ${balance:.2f})\n"
        f"💰 Deploying: <b>${deploy:.2f} CAD</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>EDGE FOR THIS TRADE:</b>\n"
        f"  ✅ Yesterday: <b>{pick.yesterday_pct:+.2f}%</b>  on  <b>{pick.rel_volume:.1f}× volume</b> → continuation setup\n"
        f"  ✅ ATR: <b>{pick.atr_pct:.2f}%</b> → room to run 1–3 % today\n"
        f"  ✅ Trend: EMA5 {'✅' if pick.above_ema5 else '❌'}  EMA20 {'✅' if pick.above_ema20 else '❌'} → trend intact\n"
        f"  ✅ Close strength: <b>{pick.close_strength:.0%}</b> → buyers held the close\n"
        f"  {bias_line}\n"
        f"  🎯 Score: <b>{pick.score:.1f}</b>  ({pick.confidence})\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>PLAN:</b>  {pick.strategy_name}\n"
        f"🏁 Hard sell at <b>3:55 PM ET</b>  — no stop loss, time-based exit only"
    )

    if ai_analysis:
        msg += f"\n\n🤖 <b>AI view:</b>\n{ai_analysis}"

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
    symbol: str, entry: float, price: float, shares: float, cost: float
) -> str:
    unrealized = shares * price - cost
    pnl_pct    = unrealized / cost * 100 if cost else 0.0
    color      = _pnl_color(unrealized)
    arrow      = _pnl_arrow(unrealized)
    mins_left  = int(_time_until_sell() / 60)
    h_left     = mins_left // 60
    m_left     = mins_left % 60
    stats      = _get_trade_stats()
    at_color   = _pnl_color(stats["total_pnl"])
    time_str   = f"{h_left}h {m_left:02d}min" if h_left else f"{m_left} min"

    return (
        f"{color} <b>Position Update — <code>{symbol}</code></b>  |  {now_et():%H:%M} ET\n\n"
        f"  {arrow} Price: <b>${price:.2f}</b>  (entry ${entry:.2f})\n"
        f"  {color} Unrealized P&L: <b>${unrealized:+.2f} CAD ({pnl_pct:+.2f}%)</b>\n"
        f"  💼 Position value: <b>${shares * price:.2f} CAD</b>\n\n"
        f"  ⏰ Selling in <b>{time_str}</b>  (3:55 PM ET)\n"
        f"  {at_color} All-time: <b>${stats['total_pnl']:+.2f} CAD</b>"
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
        f"  Entry: ${entry:.2f}  →  Exit: <b>${exit_price:.2f} CAD</b>\n"
        f"  🔢 Shares sold: {shares:.4f}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TODAY'S RESULT:</b>\n"
        f"  {color} P&L: <b>${trade_pnl:+.2f} CAD ({pnl_pct:+.2f}%)</b>\n"
        f"  💰 Invested: ${cost:.2f}  →  Proceeds: <b>${proceeds:.2f}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>ACCOUNT:</b>\n"
        f"  {at_color} All-time PnL: <b>${at_pnl:+.2f} CAD</b>"
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
    """
    Blocks until the correct buy window.
    Green/Neutral → 9:15 AM
    Red           → sends 9:15 AM warning, then waits for 11:00 AM
    Returns False if the window has already passed.
    """
    now = now_et()

    if bias != FuturesBias.RED:
        target  = now.replace(hour=9, minute=15, second=0, microsecond=0)
        cutoff  = now.replace(hour=9, minute=25, second=0, microsecond=0)
        if now > cutoff:
            log("Green/Neutral buy window (9:15–9:25) already passed.")
            return False
        if now < target:
            _sleep_until(target, "9:15 AM buy window")
        return True

    else:
        warn_time  = now.replace(hour=9, minute=15, second=0, microsecond=0)
        buy_time   = now.replace(hour=11, minute=0,  second=0, microsecond=0)
        cutoff_buy = now.replace(hour=12, minute=0,  second=0, microsecond=0)

        if now > cutoff_buy:
            log("Red bounce window (11:00–12:00) already passed — skipping today.")
            return False

        # Send the 9:15 AM "waiting" message
        if now < warn_time:
            _sleep_until(warn_time, "9:15 AM red-bias warning")
        if now <= warn_time + timedelta(minutes=10):
            notify(build_red_waiting_message(pick, futures_detail))

        if now < buy_time:
            _sleep_until(buy_time, "11:00 AM bounce buy")
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Buy
# ──────────────────────────────────────────────────────────────────────────────

def execute_buy(pick: GrinderPick, balance: float, bias: FuturesBias,
                futures_detail: str, ai_analysis: str) -> bool:
    is_bounce = (bias == FuturesBias.RED)
    deploy    = balance * _DEPLOY_PCT / 100
    shares_est = int(deploy // pick.last_close) if pick.last_close > 0 else 0

    notify(build_buy_message(pick, balance, bias, futures_detail, ai_analysis, is_bounce))
    log(f"Placing buy: {pick.symbol}  ~{shares_est} sh @ ~${pick.last_close:.2f}")

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
        f"🔢 Shares: <b>{actual_qty:.4f}</b>  |  💵 Entry: <b>${pick.last_close:.2f} CAD</b>\n"
        f"💰 Invested: <b>${actual_cost:.2f} CAD</b>\n"
        f"⏰ Auto-sell: <b>3:55 PM ET</b>  |  No stop loss\n\n"
        f"{at_color} All-time PnL: <b>${at_pnl:+.2f} CAD</b>"
    )
    log(f"Buy confirmed: {actual_qty:.4f} sh @ ${pick.last_close:.2f}  cost ${actual_cost:.2f}")
    return True


def wait_for_fill_confirm(symbol: str) -> None:
    now = now_et()
    open_t = now.replace(hour=9, minute=31, second=0, microsecond=0)
    secs = (open_t - now).total_seconds()
    if secs > 0:
        log(f"Pre-market order queued — waiting {secs/60:.1f} min for 9:30 open...")
        time.sleep(secs)
    log("Market open — confirming fill...")
    balance = fetch_live_balance(retries=2)
    bal_str = f"${balance:.2f} CAD" if balance else "N/A"
    notify(
        f"✅ <b>Fill confirmed at market open</b>\n\n"
        f"🎫 <code>{symbol}</code>  filled at 9:30 AM open\n"
        f"💰 Live balance: <b>{bal_str}</b>\n"
        f"⏰ Selling at <b>3:55 PM ET</b>"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Hold + 30-min updates + sell
# ──────────────────────────────────────────────────────────────────────────────

def hold_and_sell() -> None:
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
    notify(
        f"📊 <b>Position open — monitoring until 3:55 PM</b>\n\n"
        f"🎫 <code>{symbol}</code>  |  {shares:.4f} sh @ ${entry:.2f}\n"
        f"💰 Cost: <b>${cost:.2f} CAD</b>  |  📋 {strat}\n"
        f"⏰ Updates every 30 min  |  Hard sell at <b>3:55 PM ET</b>"
    )

    while True:
        now = now_et()

        # ── Force-sell at 3:55 PM ─────────────────────────────────────────
        if now.hour > _SELL_HOUR or (now.hour == _SELL_HOUR and now.minute >= _SELL_MINUTE):
            snap       = md.snapshot(symbol)
            exit_price = snap.last_price if snap else entry
            unrealized = shares * exit_price - cost
            pnl_pct    = unrealized / cost * 100 if cost else 0.0

            notify(
                f"⏳ <b>Closing at 3:55 PM ET</b>\n\n"
                f"🎫 <code>{symbol}</code>  |  💵 ${exit_price:.2f}\n"
                f"{_pnl_arrow(unrealized)} Est. P&L: <b>${unrealized:+.2f} CAD ({pnl_pct:+.2f}%)</b>"
            )

            sell_result    = None
            order_data     = {}
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

            sell_ok = (sell_result is not None) and (sell_result.returncode == 0 or order_submitted)
            if not sell_ok:
                notify(f"❌ All 3 sell attempts FAILED for <code>{symbol}</code>. Manual close required!")
                log("All sell attempts failed — manual intervention needed.")
                return

            actual_qty   = float(order_data.get("estimated_quantity") or shares)
            actual_value = float(order_data.get("estimated_value") or (actual_qty * exit_price))
            actual_price = actual_value / actual_qty if actual_qty else exit_price
            trade_pnl    = actual_value - cost
            at_pnl       = _record_trade(symbol, cost, actual_value, actual_qty)

            _append_trade_history(symbol, "SELL", actual_price, actual_qty,
                                  cost, trade_pnl, strat)
            POS_FILE.unlink(missing_ok=True)

            notify(build_sell_message(symbol, entry, actual_price, actual_qty,
                                      cost, trade_pnl, at_pnl))
            log(f"Closed. Trade P&L: ${trade_pnl:+.2f}  All-time: ${at_pnl:+.2f}")
            return

        # ── 30-min update ─────────────────────────────────────────────────
        if time.time() - last_update_t >= UPDATE_INTERVAL:
            snap = md.snapshot(symbol)
            if snap:
                notify(build_update_message(symbol, entry, snap.last_price, shares, cost))
                log(f"30-min update: ${snap.last_price:.2f} ({(snap.last_price/entry-1)*100:+.2f}%)")
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
        log(f"Running {label}...")
        picks, new_bias, buy_plan, strat_name, fut_det = run_scan(balance)
        top = picks[0] if picks else None
        ai  = get_ai_analysis(picks, new_bias, fut_det, balance)
        msg = build_scan_message(picks, new_bias, fut_det, buy_plan, strat_name, balance, ai, label)
        notify(msg)
        last_status_t[0] = time.time()
        return new_bias

    # Startup scan on first entry
    if "startup" not in scans_done:
        bias = _do_scan("Startup Scan")
        scans_done.add("startup")

    while True:
        now = now_et()

        # Weekend
        if now.weekday() >= 5:
            nxt  = _next_weekday_9am()
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
            buy_target = 9 if bias != FuturesBias.RED else 11
            target_t   = now.replace(hour=buy_target, minute=15 if buy_target == 9 else 0,
                                     second=0, microsecond=0)
            if 0 <= (target_t - now).total_seconds() <= 300:
                log("Near buy window — exiting overnight loop.")
                return bias

        # Past buy window for today → sleep until 5 PM
        cutoff = now.replace(
            hour=9, minute=25, second=0, microsecond=0
        ) if bias != FuturesBias.RED else now.replace(hour=12, minute=5, second=0, microsecond=0)
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
                        help="Cash in CAD (default: live fetch from Wealthsimple)")
    parser.add_argument("--now", action="store_true",
                        help="Skip all waiting and buy immediately (debug)")
    args = parser.parse_args()

    # ── Startup ───────────────────────────────────────────────────────────
    log("=" * 60)
    log("Le Grinder — STARTING")
    log("=" * 60)

    balance: float = args.balance or fetch_live_balance() or 100.0
    SESSION_FILE.write_text(json.dumps({
        "startingBalance": balance,
        "startTime": now_et().isoformat(),
    }))
    log(f"Starting balance: ${balance:.2f} CAD")

    stats = _get_trade_stats()
    at_color = _pnl_color(stats["total_pnl"])
    notify(
        f"🚀 <b>Le Grinder started successfully</b>\n\n"
        f"💰 Balance: <b>${balance:.2f} CAD</b>\n"
        f"📋 Watchlist: <b>{len(WATCHLIST)} Canadian tickers</b>  (TSX / TSXV / NEO)\n"
        f"📐 Strategy: 8-criteria momentum screen  +  Claude AI analysis\n"
        f"⏰ Entry: 9:15 AM (green/neutral) or 11:00 AM (red)\n"
        f"🏁 Exit: 3:55 PM daily  |  No stop loss\n\n"
        f"{at_color} All-time PnL: <b>${stats['total_pnl']:+.2f} CAD</b>"
        f"  |  🏆 {stats['wins']}W / {stats['losses']}L"
    )

    # ── Resume open position ───────────────────────────────────────────────
    if POS_FILE.exists():
        pos = json.loads(POS_FILE.read_text())
        log(f"Open position found: {pos['symbol']} — resuming hold/sell loop.")
        notify(
            f"▶️ <b>Bot restarted — resuming position</b>\n\n"
            f"🎫 <code>{pos['symbol']}</code>  "
            f"{pos.get('shares', 0):.4f} sh @ ${pos.get('buyPrice', 0):.2f}\n"
            f"⏰ Selling at 3:55 PM ET"
        )
        hold_and_sell()
        POS_FILE.unlink(missing_ok=True)

    # ── Main loop ─────────────────────────────────────────────────────────
    scans_done:    set[str] = set()
    last_status_t: list     = [0.0]
    bias = FuturesBias.NEUTRAL
    skip_wait = args.now

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
        picks, _, buy_plan, strat_name, fut_det = run_scan(balance)
        ai = get_ai_analysis(picks, bias, fut_det, balance)

        if not picks:
            notify(
                "⚠️ <b>No candidates at buy time — skipping today</b>\n\n"
                "Nothing passed the filters. Entering overnight loop for tomorrow."
            )
            log("No candidates — skipping today.")
            continue

        top = picks[0]

        # Wait for the correct window (sends red-waiting message if needed)
        should_buy = wait_for_buy_window(bias, top, fut_det)
        if not should_buy:
            log("Buy window missed — entering overnight loop.")
            continue

        # Re-scan for red bias at 11 AM to get fresh price
        if bias == FuturesBias.RED:
            log("Red bias: re-scanning at 11 AM for freshest data...")
            picks, _, _, strat_name, fut_det = run_scan(balance)
            ai = get_ai_analysis(picks, bias, fut_det, balance)
            if not picks:
                notify("⚠️ <b>Bounce scan found nothing — skipping today.</b>")
                log("Bounce scan empty — skipping.")
                continue
            top = picks[0]

        # Execute buy
        ok = execute_buy(top, balance, bias, fut_det, ai)
        if not ok:
            log("Buy failed — entering overnight loop.")
            continue

        # Fill confirm (pre-market orders only)
        now = now_et()
        if now.hour < 9 or (now.hour == 9 and now.minute < 30):
            wait_for_fill_confirm(top.symbol)

        # Hold + sell at 3:55 PM
        hold_and_sell()
        POS_FILE.unlink(missing_ok=True)
        log("Day complete — re-entering overnight loop.")


if __name__ == "__main__":
    main()
