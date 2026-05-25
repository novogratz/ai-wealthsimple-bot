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


def wait_for_entry() -> None:
    while True:
        now = now_et()
        if now.weekday() >= 5:
            log("Weekend - sleeping 10 min...")
            time.sleep(600)
            continue
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)
        latest = now.replace(hour=9, minute=35, second=0, microsecond=0)
        if now < target:
            secs = (target - now).total_seconds()
            log(f"Market opens in {secs / 60:.1f} min - waiting...")
            time.sleep(min(secs, 300))
            continue
        if now > latest:
            log("Entry window missed (past 09:35 ET). Use --now to override.")
            sys.exit(1)
        log("Entry window open - proceeding.")
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
    log(f"TOP PICK : {pick.symbol}")
    log(f"Price    : ${pick.last_price:.2f}")
    log(f"Shares   : {pick.shares}")
    log(f"Score    : {pick.score:.2f}")
    log(f"Reason   : {pick.reason}")
    notify(f"Scan done. Buying {pick.symbol} @ ${pick.last_price:.2f} ({pick.shares} shares)", event="scan_top")
    return pick.symbol, pick.last_price, pick.shares


def run_buy(symbol: str, price: float, shares: int) -> None:
    log(f"Opening Wealthsimple to buy {symbol} (max dollars)...")
    notify(f"Placing buy order for {symbol} @ ~${price:.2f}", event="buy_preparing")
    result = subprocess.run(
        [PYTHON, str(AUTO_SCRIPT), "buy", "--symbol", symbol, "--max-dollars"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        log("Buy automation failed.")
        notify(f"Buy FAILED for {symbol}", event="error")
        sys.exit(1)

    pos = {
        "symbol": symbol,
        "buyPrice": price,
        "shares": shares,
        "estimatedCost": price * shares,
        "sellAll": True,
        "time": now_et().isoformat(),
    }
    POS_FILE.write_text(json.dumps(pos))
    log(f"Buy submitted. Holding until 15:55 ET.")

    from fashion_bot.cli import _get_total_pnl
    all_time_pnl = _get_total_pnl()
    at_color = "🟢" if all_time_pnl >= 0 else "🔴"
    notify(
        f"🛒 Bought <code>{symbol}</code>\n\n"
        f"🔢 Shares: <b>{shares}</b>\n"
        f"💵 Entry: <b>${price:.2f} CAD/share</b>\n"
        f"💰 Total invested: <b>${price * shares:.2f} CAD</b>\n"
        f"⏰ Auto-sell at: <b>15:55 ET</b>\n\n"
        f"{at_color} All-time realized PnL: <b>${all_time_pnl:+.2f} CAD</b>",
        event="buy_submitted"
    )


def run_sell(symbol: str, price: float, shares: int, buy_cost: float) -> None:
    log(f"Selling {symbol}...")
    notify(f"Placing sell order for {symbol}", event="sell_preparing")
    result = subprocess.run(
        [PYTHON, str(AUTO_SCRIPT), "sell", "--symbol", symbol, "--sell-all"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        log("Sell automation failed.")
        notify(f"Sell FAILED for {symbol}", event="error")
        sys.exit(1)

    estimated_proceeds = shares * price
    pnl = estimated_proceeds - buy_cost
    log(f"Sold {symbol}. Estimated PnL: ${pnl:+.2f}")
    notify(f"Sold {symbol} @ ~${price:.2f}\nEstimated PnL: ${pnl:+.2f} CAD", event="sell_submitted")


def run_watch() -> None:
    log("Watch loop started - checking price every 60s, selling at 15:55 ET...")
    result = subprocess.run(
        [PYTHON, "-m", "fashion_bot", "watch", "--position-file", str(POS_FILE)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        log("Watch/sell loop exited with an error.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full trading day: scan -> buy -> hold -> sell at 15:55 ET"
    )
    parser.add_argument("--balance", type=float, default=17.24, help="Cash to deploy in CAD")
    parser.add_argument("--now", action="store_true", help="Skip the 09:30 ET wait")
    args = parser.parse_args()

    log("=== Fashion Bot - Full Day ===")
    log(f"Balance: ${args.balance:.2f} CAD | Auto-sell at: 15:55 ET")

    # Resume existing position instead of re-buying on restart
    if POS_FILE.exists():
        pos = json.loads(POS_FILE.read_text())
        symbol = pos["symbol"]
        log(f"Resuming existing position: {symbol} (skipping scan + buy)")
        notify(f"▶️ Resuming open position in {symbol} — going straight to watch loop.", event="info")
        run_watch()
        log("Done.")
        return

    if not args.now:
        wait_for_entry()

    symbol, price, shares = run_scan(args.balance)
    run_buy(symbol, price, shares)
    run_watch()
    log("Done. Check data/screen_sell_done.png to verify the sell.")


if __name__ == "__main__":
    main()
