#!/usr/bin/env python3
"""
Full trading day in one command: scan -> buy -> hold -> sell at 15:55 ET.

Usage:
    python scripts/run_day.py                  # waits for 09:30 ET then goes
    python scripts/run_day.py --now            # skip the wait, run immediately
    python scripts/run_day.py --balance 50     # override cash amount
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fashion_bot.config import load_settings, load_universe
from fashion_bot.market_data import YFinanceMarketData
from fashion_bot.strategy import FashionStrategy
from fashion_bot.telegram import TelegramConfigError, send_message, trade_message

DATA = ROOT / "data"
POS_FILE = DATA / "open_position.json"
AUTO_SCRIPT = ROOT / "scripts" / "wealthsimple_auto.py"
PYTHON = sys.executable
TZ = ZoneInfo("America/Toronto")

DATA.mkdir(exist_ok=True)


def now_et() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    print(f"[{now_et():%H:%M:%S} ET] {msg}", flush=True)


def notify(msg: str, event: str = "info") -> None:
    try:
        send_message(trade_message(event, message=msg))
        log("  Telegram sent.")
    except TelegramConfigError as e:
        log(f"  Telegram not configured: {e}")
    except Exception as e:
        log(f"  Telegram failed: {e}")


def _next_entry_window() -> datetime:
    """Return the next 09:30 ET on a weekday, skipping weekends."""
    from datetime import timedelta
    now = now_et()
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def wait_for_entry() -> None:
    while True:
        now = now_et()
        if now.weekday() >= 5:
            nxt = _next_entry_window()
            secs = (nxt - now).total_seconds()
            log(f"Weekend — next entry window {nxt:%a %b %d %H:%M} ET ({secs/3600:.1f}h away). Sleeping 30 min...")
            time.sleep(1800)
            continue
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        latest = now.replace(hour=9, minute=35, second=0, microsecond=0)
        if now < target:
            secs = (target - now).total_seconds()
            log(f"Market opens in {secs/60:.1f} min — waiting...")
            time.sleep(min(secs, 300))
            continue
        if now > latest:
            # Past today's window — wait overnight for tomorrow
            nxt = _next_entry_window()
            secs = (nxt - now).total_seconds()
            log(f"Today's entry window closed. Next window {nxt:%a %b %d %H:%M} ET ({secs/3600:.1f}h away). Sleeping 30 min...")
            time.sleep(1800)
            continue
        log("Entry window open (09:30–09:35 ET) — proceeding.")
        return


def run_scan(balance: float) -> tuple[str, float, int]:
    settings = load_settings(ROOT / "config" / "settings.toml")
    universe = load_universe(ROOT / "config" / "universe.csv")
    strategy = FashionStrategy(
        settings=settings,
        universe=universe,
        market_data=YFinanceMarketData(),
    )

    log(f"Scanning TSX with ${balance:.2f} budget...")
    picks = strategy.rank(cash=balance)
    if not picks:
        log("No candidates passed filters - markets may be closed or universe too narrow.")
        sys.exit(1)

    pick = picks[0]
    est_shares = balance / pick.last_price if pick.last_price > 0 else 0.0
    log(f"TOP PICK : {pick.symbol}")
    log(f"Price    : ${pick.last_price:.2f}")
    log(f"Est. shares: ~{est_shares:.4f} (${balance:.2f} / ${pick.last_price:.2f})")
    log(f"Score    : {pick.score:.2f}")
    log(f"Reason   : {pick.reason}")
    notify(
        f"🔍 <b>Scan done — Top pick: <code>{pick.symbol}</code></b>\n\n"
        f"💵 Price: <b>${pick.last_price:.2f} CAD</b>\n"
        f"🔢 Est. shares: <b>~{est_shares:.4f}</b>  (${balance:.2f} budget)\n"
        f"📊 Score: {pick.score:.2f}  |  {pick.reason}",
        event="scan_top",
    )
    return pick.symbol, pick.last_price, pick.shares


def run_buy(symbol: str, price: float, shares_est: int) -> None:
    log(f"Opening Wealthsimple to buy {symbol} (max dollars)...")
    notify(f"Placing buy order for {symbol} @ ~${price:.2f}", event="buy_preparing")

    # Capture output to parse actual quantity/cost from ORDER_RESULT_JSON
    result = subprocess.run(
        [PYTHON, str(AUTO_SCRIPT), "buy", "--symbol", symbol, "--max-dollars"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        print(f"  {line}", flush=True)

    if result.returncode != 0:
        combined = result.stdout + result.stderr
        if "session expired" in combined.lower() or "log in" in combined.lower():
            msg = (
                "❌ <b>Wealthsimple session expired</b>\n\n"
                "Run this to fix it:\n"
                "<code>python scripts/wealthsimple_auto.py setup</code>\n\n"
                "Then restart the bot."
            )
            notify(msg, event="error")
            log("SESSION EXPIRED — run: python scripts/wealthsimple_auto.py setup")
        else:
            notify(f"❌ Buy FAILED for {symbol}", event="error")
        log("Buy automation failed.")
        sys.exit(1)

    # Use actual fill from ORDER_RESULT_JSON if available.
    # For dollar-based orders Wealthsimple may omit estimated_quantity, so derive
    # shares from estimated_value / price when the quantity field is missing.
    actual_qty: float = float(shares_est)
    actual_cost: float = price * shares_est
    for line in result.stdout.splitlines():
        if line.startswith("ORDER_RESULT_JSON:"):
            try:
                data = json.loads(line[len("ORDER_RESULT_JSON:"):])
                if data.get("estimated_value"):
                    actual_cost = float(data["estimated_value"])
                if data.get("estimated_quantity"):
                    actual_qty = float(data["estimated_quantity"])
                elif actual_cost > 0 and price > 0:
                    actual_qty = actual_cost / price
            except Exception:
                pass

    pos = {
        "symbol": symbol,
        "buyPrice": price,
        "shares": actual_qty,
        "estimatedCost": actual_cost,
        "sellAll": True,
        "time": now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos))
    log(f"Buy submitted: {actual_qty} shares @ ${price:.2f} (cost ${actual_cost:.2f}). Holding until 15:55 ET.")

    from fashion_bot.cli import _get_total_pnl
    all_time_pnl = _get_total_pnl()
    at_color = "🟢" if all_time_pnl >= 0 else "🔴"
    notify(
        f"🛒 Bought <code>{symbol}</code>\n\n"
        f"🔢 Shares: <b>{actual_qty:.4f}</b>\n"
        f"💵 Entry: <b>${price:.2f} CAD/share</b>\n"
        f"💰 Total invested: <b>${actual_cost:.2f} CAD</b>\n"
        f"⏰ Auto-sell at: <b>15:55 ET</b>\n\n"
        f"{at_color} All-time realized PnL: <b>${all_time_pnl:+.2f} CAD</b>",
        event="buy_submitted",
    )


def fetch_live_balance(retries: int = 3) -> float | None:
    for attempt in range(1, retries + 1):
        log(f"Fetching live balance from Wealthsimple (attempt {attempt}/{retries})...")
        try:
            result = subprocess.run(
                [PYTHON, str(AUTO_SCRIPT), "balance"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as e:
            log(f"  Balance fetch error: {e}")
            time.sleep(15)
            continue

        combined = result.stdout + result.stderr
        if "session_expired" in combined.lower() or "session expired" in combined.lower():
            notify(
                "❌ <b>Wealthsimple session expired</b>\n\n"
                "Fix it now:\n"
                "<code>python scripts/wealthsimple_auto.py setup</code>\n\n"
                "Log in to Wealthsimple in the Firefox window, then press ENTER. Restart the bot after.",
                event="error",
            )
            log("SESSION EXPIRED — run: python scripts/wealthsimple_auto.py setup")
            return None

        for line in result.stdout.splitlines():
            if line.startswith("LIVE_BALANCE_CAD:"):
                try:
                    val = float(line[len("LIVE_BALANCE_CAD:"):].replace(",", ""))
                    log(f"  Live balance: ${val:.2f} CAD")
                    return val
                except ValueError:
                    pass
        log(f"  Could not parse balance (attempt {attempt})")
        time.sleep(15)
    return None


def cleanup_screenshots() -> None:
    removed = list(DATA.glob("screen_*.png"))
    for f in removed:
        try:
            f.unlink()
        except Exception:
            pass
    if removed:
        log(f"Cleared {len(removed)} old screenshot(s).")


def run_watch() -> None:
    log("Watch loop started - checking price every 60s, selling at 15:55 ET...")
    for attempt in range(1, 4):
        result = subprocess.run(
            [PYTHON, "-m", "fashion_bot", "watch", "--position-file", str(POS_FILE)],
            cwd=ROOT,
        )
        if result.returncode == 0:
            return
        if not POS_FILE.exists():
            log("Position file gone — position already closed.")
            return
        log(f"Watch loop exited with error (attempt {attempt}/3).")
        if attempt < 3:
            notify(f"⚠️ Watch loop crashed (attempt {attempt}/3) — restarting in 30s...", event="error")
            time.sleep(30)
    notify("❌ Watch loop failed 3 times — manual intervention needed.", event="error")
    log("Watch loop failed 3 times. Check logs.")
    sys.exit(1)


def run_overnight_analysis() -> None:
    """Scan for tomorrow's pick every hour between market close and 9:30 ET open."""
    settings = load_settings(ROOT / "config" / "settings.toml")
    universe = load_universe(ROOT / "config" / "universe.csv")
    strategy = FashionStrategy(
        settings=settings,
        universe=universe,
        market_data=YFinanceMarketData(),
    )

    log("Overnight analysis: scanning universe for tomorrow's pick...")
    try:
        picks = strategy.rank(cash=1000)
        if picks:
            top = picks[0]
            log(f"Overnight top pick: {top.symbol} | score {top.score:.2f} | {top.reason}")
            notify(
                f"🌙 <b>Overnight scan — top pick: <code>{top.symbol}</code></b>\n\n"
                f"📊 Score: {top.score:.2f}  |  {top.reason}\n"
                f"💵 Last price: ${top.last_price:.2f} CAD",
                event="scan_top",
            )
        else:
            log("Overnight scan: no candidates found.")
    except Exception as e:
        log(f"Overnight scan error: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full trading day: scan -> buy -> hold -> sell at 15:55 ET — loops overnight"
    )
    parser.add_argument("--balance", type=float, default=None, help="Cash to deploy in CAD (default: fetch live from Wealthsimple)")
    parser.add_argument("--now", action="store_true", help="Skip the 09:30 ET wait")
    args = parser.parse_args()

    log("=== Fashion Bot — running continuously (sells 15:55, scans overnight, buys 09:30) ===")
    cleanup_screenshots()

    from fashion_bot.cli import _get_total_pnl

    # Resume an existing open position on restart before entering the main loop
    if POS_FILE.exists():
        balance = args.balance or fetch_live_balance() or 17.24
        (DATA / "session_info.json").write_text(
            json.dumps({"startingBalance": balance, "startTime": now_et().isoformat()})
        )
        pos = json.loads(POS_FILE.read_text())
        symbol = pos["symbol"]
        entry = float(pos.get("buyPrice", 0))
        cost = float(pos.get("estimatedCost", 0))
        shares = float(pos.get("shares", 0))
        if shares < 1.01 and cost > 0 and entry > 0:
            derived = cost / entry
            if derived > shares * 1.05:
                shares = derived
                log(f"Corrected share count: {shares:.4f} ({cost:.2f}/{entry:.2f})")
        all_time_pnl = _get_total_pnl()
        at_color = "🟢" if all_time_pnl >= 0 else "🔴"
        log(f"Resuming existing position: {symbol} {shares:.4f} shares @ ${entry:.2f}")
        notify(
            f"▶️ <b>Bot restarted — resuming position</b>\n\n"
            f"🎫 <code>{symbol}</code>\n"
            f"🔢 Shares: <b>{shares:.4f}</b>  |  💵 Entry: <b>${entry:.2f}</b>  |  💰 Cost: <b>${cost:.2f}</b>\n"
            f"⏰ Auto-sell at 15:55 ET\n\n"
            f"{at_color} All-time PnL: <b>${all_time_pnl:+.2f} CAD</b>",
            event="info",
        )
        run_watch()
        POS_FILE.unlink(missing_ok=True)

    # Main loop — runs forever: overnight scan → wait for 9:30 → buy → sell → repeat
    skip_wait = args.now
    last_overnight_scan: float = 0.0

    while True:
        # Overnight analysis: scan every hour while waiting for market open
        if not skip_wait:
            while True:
                now = now_et()
                target = now.replace(hour=9, minute=30, second=0, microsecond=0)
                # If we're within 5 min of open, stop scanning and go buy
                if now.weekday() < 5 and (target - now).total_seconds() <= 300:
                    break
                # Run overnight scan at most once per hour
                if time.monotonic() - last_overnight_scan >= 3600:
                    run_overnight_analysis()
                    last_overnight_scan = time.monotonic()
                # Sleep in 5-min chunks so we catch the open precisely
                time.sleep(300)
            wait_for_entry()

        skip_wait = False  # only skip on the very first iteration if --now passed

        # Hard guard: never buy outside 9:30–9:35 ET
        _now = now_et()
        _open = _now.replace(hour=9, minute=30, second=0, microsecond=0)
        _close = _now.replace(hour=9, minute=35, second=0, microsecond=0)
        if not (_open <= _now <= _close):
            log(f"Outside 09:30–09:35 entry window ({_now:%H:%M} ET) — waiting for next open.")
            notify(
                f"⚠️ <b>Buy skipped</b> — {_now:%H:%M} ET is outside entry window.\n"
                f"Scanning overnight and retrying tomorrow at 09:30.",
                event="error",
            )
            continue

        # Fetch fresh balance and buy
        balance = args.balance or fetch_live_balance() or 17.24
        (DATA / "session_info.json").write_text(
            json.dumps({"startingBalance": balance, "startTime": now_et().isoformat()})
        )
        all_time_pnl = _get_total_pnl()
        at_color = "🟢" if all_time_pnl >= 0 else "🔴"
        notify(
            f"🤖 <b>Fashion Bot — new trading day</b>\n\n"
            f"💼 Budget: <b>${balance:.2f} CAD</b>\n"
            f"⏰ Entry: <b>09:30–09:35 ET</b>  |  🏁 Auto-sell: <b>15:55 ET</b>\n\n"
            f"{at_color} All-time PnL: <b>${all_time_pnl:+.2f} CAD</b>",
            event="info",
        )
        log(f"Budget: ${balance:.2f} CAD")

        symbol, price, shares = run_scan(balance)
        run_buy(symbol, price, shares)
        run_watch()
        POS_FILE.unlink(missing_ok=True)

        cleanup_screenshots()
        log("Day complete — entering overnight scan loop for next trading day.")


if __name__ == "__main__":
    main()
