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

from kzer_bot.config import load_settings, load_universe
from kzer_bot.market_data import YFinanceMarketData
from kzer_bot.strategy import KzerStrategy
from kzer_bot.telegram import TelegramConfigError, send_message, trade_message

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


def build_status_telegram(balance: float | None) -> str:
    """Build the status message for Telegram (HTML)."""
    pnl_file = DATA / "pnl_ledger.json"
    trades = []
    if pnl_file.exists():
        try:
            trades = json.loads(pnl_file.read_text())
        except Exception:
            pass

    total_pnl  = sum(t.get("realizedPnl", 0) for t in trades)
    wins       = sum(1 for t in trades if t.get("realizedPnl", 0) > 0)
    losses     = sum(1 for t in trades if t.get("realizedPnl", 0) < 0)
    n          = len(trades)
    win_rate   = (wins / n * 100) if n else 0.0
    # ROI = all-time profit as % of current account balance
    roi        = (total_pnl / balance * 100) if balance else 0.0
    pnl_emoji  = "🟢" if total_pnl >= 0 else "🔴"
    pnl_sign   = "+" if total_pnl >= 0 else ""

    pos_line = "None"
    if POS_FILE.exists():
        try:
            pos = json.loads(POS_FILE.read_text())
            pos_line = (
                f"<code>{pos['symbol']}</code>  "
                f"{pos.get('shares', 0):.4f} sh @ ${pos.get('buyPrice', 0):.2f}"
            )
        except Exception:
            pos_line = "Error reading position"

    bal_str = f"<b>${balance:.2f} CAD</b>" if balance is not None else "<i>fetching...</i>"

    recent = ""
    for t in trades[-5:]:
        pnl  = t.get("realizedPnl", 0)
        sym  = t.get("symbol", "?")
        sign = "+" if pnl >= 0 else ""
        emoji = "🟢" if pnl >= 0 else "🔴"
        ts   = t.get("time", "")[:10]
        recent += f"\n  {emoji} <code>{sym}</code>  {sign}{pnl:.2f} CAD   {ts}"

    now = now_et()
    nxt = _next_entry_window()
    if now.weekday() >= 5:
        next_event = f"Buy at {nxt:%a %b %d %H:%M} ET"
    elif now.hour < 9 or (now.hour == 9 and now.minute < 31):
        mins = int((now.replace(hour=9, minute=31, second=0, microsecond=0) - now).total_seconds() // 60)
        next_event = f"Buy today at 09:31 ET  ({mins} min away)"
    elif now.hour < 16:
        next_event = "Sell today at 15:55 ET"
    else:
        next_event = f"Buy at {nxt:%a %b %d %H:%M} ET"

    lines = [
        "📊 <b>kzeR Wealthsimple Bot — Status</b>",
        "",
        f"💰 Balance:      {bal_str}",
        f"{pnl_emoji} All-time PnL:  <b>{pnl_sign}{total_pnl:.2f} CAD</b>  ({roi:+.2f}% ROI)",
        f"🏆 Record:       <b>{wins}W / {losses}L</b>  ({win_rate:.0f}% win rate)",
        f"📈 Total trades: <b>{n}</b>",
        "",
        f"📂 Open position: {pos_line}",
        f"⏭ Next action:  {next_event}",
    ]
    if recent:
        lines += ["", "🕐 <b>Last 5 trades:</b>" + recent]

    return "\n".join(lines)


def notify_status(balance: float | None) -> None:
    """Send status update to Telegram. Fetches live balance if not provided."""
    if balance is None:
        log("Fetching live balance for status update...")
        balance = fetch_live_balance(retries=2)
    try:
        send_message(build_status_telegram(balance))
        log("  Status sent to Telegram.")
    except TelegramConfigError as e:
        log(f"  Telegram not configured: {e}")
    except Exception as e:
        log(f"  Telegram status failed: {e}")


def print_status_banner(balance: float | None) -> None:
    pnl_file = DATA / "pnl_ledger.json"
    trades = []
    if pnl_file.exists():
        try:
            trades = json.loads(pnl_file.read_text())
        except Exception:
            pass

    total_pnl   = sum(t.get("realizedPnl", 0) for t in trades)
    wins        = sum(1 for t in trades if t.get("realizedPnl", 0) > 0)
    losses      = sum(1 for t in trades if t.get("realizedPnl", 0) < 0)
    n           = len(trades)
    win_rate    = (wins / n * 100) if n else 0.0
    roi         = (total_pnl / balance * 100) if balance else 0.0
    pnl_arrow   = "^" if total_pnl >= 0 else "v"
    pnl_sign    = "+" if total_pnl >= 0 else ""

    # open position
    pos_line = "None"
    if POS_FILE.exists():
        try:
            pos = json.loads(POS_FILE.read_text())
            pos_line = (
                f"{pos['symbol']}  {pos.get('shares', 0):.4f} shares "
                f"@ ${pos.get('buyPrice', 0):.2f}  (cost ${pos.get('estimatedCost', 0):.2f})"
            )
        except Exception:
            pos_line = "Error reading position"

    # next scheduled event
    now = now_et()
    if now.weekday() >= 5:
        nxt = _next_entry_window()
        next_event = f"Buy at {nxt:%a %b %d %H:%M} ET"
    elif now.hour < 9 or (now.hour == 9 and now.minute < 31):
        next_event = f"Buy today at 09:31 ET  ({(now.replace(hour=9,minute=31,second=0,microsecond=0)-now).seconds//60} min away)"
    elif now.hour < 16:
        next_event = "Sell today at 15:55 ET"
    elif now.hour < 17:
        next_event = "5 PM post-close scan coming up"
    elif now.hour < 6:
        next_event = "6 AM pre-market scan coming up"
    else:
        nxt = _next_entry_window()
        next_event = f"Buy at {nxt:%a %b %d %H:%M} ET"

    bal_str = f"${balance:.2f} CAD" if balance is not None else "fetching..."

    W = 54
    div = "-" * W
    sep = f"+{div}+"
    def row(left, right=""):
        content = f"  {left:<22} {right}"
        return f"|{content:<{W}}|"
    def center_row(text):
        return f"|{text:^{W}}|"

    print(flush=True)
    print(sep, flush=True)
    print(center_row("  kzeR WEALTHSIMPLE BOT  --  STATUS"), flush=True)
    print(sep, flush=True)
    print(row("Account balance", bal_str), flush=True)
    print(row("All-time PnL", f"{pnl_arrow} {pnl_sign}{total_pnl:.2f} CAD  ({roi:+.2f}% ROI)"), flush=True)
    print(row("Record", f"{wins}W / {losses}L  ({win_rate:.0f}% win rate)"), flush=True)
    print(row("Total trades", str(n)), flush=True)
    print(sep, flush=True)
    print(row("Open position", pos_line[:W-26]), flush=True)
    print(row("Next action", next_event[:W-26]), flush=True)
    print(sep, flush=True)
    if trades:
        print(center_row("Recent trades"), flush=True)
        for t in trades[-5:]:
            pnl   = t.get("realizedPnl", 0)
            sym   = t.get("symbol", "?")
            arrow = "^" if pnl >= 0 else "v"
            sign  = "+" if pnl >= 0 else ""
            ts    = t.get("time", "")[:10]
            line  = f"{arrow} {sym:<12} {sign}{pnl:.2f} CAD   {ts}"
            print(f"|    {line:<{W-2}}  |", flush=True)
    else:
        print(center_row("No trades yet -- let's get it"), flush=True)
    print(sep, flush=True)
    print(flush=True)


def notify(msg: str, event: str = "info") -> None:
    try:
        send_message(trade_message(event, message=msg))
        log("  Telegram sent.")
    except TelegramConfigError as e:
        log(f"  Telegram not configured: {e}")
    except Exception as e:
        log(f"  Telegram failed: {e}")


def _next_entry_window() -> datetime:
    """Return the next 09:31 ET on a weekday, skipping weekends."""
    from datetime import timedelta
    now = now_et()
    candidate = now.replace(hour=9, minute=31, second=0, microsecond=0)
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
        target = now.replace(hour=9, minute=31, second=0, microsecond=0)
        latest = now.replace(hour=9, minute=36, second=0, microsecond=0)
        if now < target:
            secs = (target - now).total_seconds()
            log(f"Market opens in {secs/60:.1f} min — waiting...")
            time.sleep(min(secs, 300))
            continue
        if now > latest:
            nxt = _next_entry_window()
            secs = (nxt - now).total_seconds()
            log(f"Today's entry window closed. Next window {nxt:%a %b %d %H:%M} ET ({secs/3600:.1f}h away). Sleeping 30 min...")
            time.sleep(1800)
            continue
        log("Entry window open (09:31–09:36 ET) — proceeding.")
        return


def run_scan(balance: float) -> tuple[str, float, int]:
    settings = load_settings(ROOT / "config" / "settings.toml")
    universe = load_universe(ROOT / "config" / "universe.csv")
    strategy = KzerStrategy(
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
    log(f"Buy submitted: {actual_qty} shares @ ${price:.2f} (cost ${actual_cost:.2f}). Holding until 15:55 ET.")  # sell time from settings.toml

    from kzer_bot.cli import _get_total_pnl
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
            [PYTHON, "-m", "kzer_bot", "watch", "--position-file", str(POS_FILE)],
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


_SCAN_LABELS = {
    "startup":   ("🚀", "Startup scan"),
    "5pm":       ("🌆", "5 PM post-close scan"),
    "6am":       ("🌅", "6 AM pre-market confirmation"),
}


def run_overnight_analysis(slot: str) -> None:
    """Scan the universe and send the top pick to Telegram. slot is one of startup/5pm/6am."""
    emoji, label = _SCAN_LABELS.get(slot, ("🔍", slot))
    log(f"{label}: scanning universe for tomorrow's pick...")
    settings = load_settings(ROOT / "config" / "settings.toml")
    universe = load_universe(ROOT / "config" / "universe.csv")
    strategy = KzerStrategy(
        settings=settings,
        universe=universe,
        market_data=YFinanceMarketData(),
    )
    try:
        picks = strategy.rank(cash=1000)
        if not picks:
            log(f"{label}: no candidates found.")
            notify(f"{emoji} <b>{label}</b> — no candidates passed filters.", event="scan_top")
            return
        top = picks[0]
        others = ", ".join(f"<code>{p.symbol}</code>" for p in picks[1:4]) or "—"
        log(f"{label} top pick: {top.symbol} | score {top.score:.2f} | {top.reason}")
        notify(
            f"{emoji} <b>{label}</b>\n\n"
            f"🏆 Top pick: <code>{top.symbol}</code>\n"
            f"💵 Last price: <b>${top.last_price:.2f} CAD</b>\n"
            f"📊 Score: <b>{top.score:.2f}</b>  |  {top.reason}\n\n"
            f"Also watching: {others}",
            event="scan_top",
        )
    except Exception as e:
        log(f"{label} error: {e}")


def _passed(now: "datetime", hour: int, minute: int) -> bool:
    """True if wall-clock time has reached or passed hour:minute today."""
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= t


def wait_for_open_with_scans(scans_done: set[str], last_status: list) -> None:
    """
    Sleep until the 09:31 ET entry window, firing scheduled scans and status pings.
    scans_done tracks which slots fired this cycle (mutated in place).
    last_status is a one-element list holding the monotonic time of the last status ping.
    """
    # Immediate startup scan + status
    if "startup" not in scans_done:
        run_overnight_analysis("startup")
        notify_status(None)
        last_status[0] = time.monotonic()
        scans_done.add("startup")

    while True:
        now = now_et()
        # Within 5 min of 9:31 on a weekday → stop waiting, go buy
        target_open = now.replace(hour=9, minute=31, second=0, microsecond=0)
        if now.weekday() < 5 and 0 <= (target_open - now).total_seconds() <= 300:
            break

        # 5 PM post-close scan + status
        if _passed(now, 17, 0) and "5pm" not in scans_done:
            run_overnight_analysis("5pm")
            notify_status(None)
            last_status[0] = time.monotonic()
            scans_done.add("5pm")

        # 6 AM pre-market confirmation + status
        if _passed(now, 6, 0) and "6am" not in scans_done:
            run_overnight_analysis("6am")
            notify_status(None)
            last_status[0] = time.monotonic()
            scans_done.add("6am")

        # Every 4 hours send a status ping even if no scan fired
        if time.monotonic() - last_status[0] >= 4 * 3600:
            notify_status(None)
            last_status[0] = time.monotonic()

        # Reset slots at midnight so they fire again next cycle
        if now.hour == 0 and now.minute < 1:
            scans_done.discard("5pm")
            scans_done.discard("6am")

        if now.minute % 30 == 0 and now.second < 60:
            nxt = _next_entry_window()
            log(f"Waiting for market open — next entry {nxt:%a %b %d %H:%M} ET")

        time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full trading day: scan -> buy -> hold -> sell at 15:55 ET — loops overnight"
    )
    parser.add_argument("--balance", type=float, default=None, help="Cash to deploy in CAD (default: fetch live from Wealthsimple)")
    parser.add_argument("--now", action="store_true", help="Skip the 09:30 ET wait")
    args = parser.parse_args()

    log("=== kzeR Wealthsimple Bot — running continuously (sells 15:55, scans overnight, buys 09:31) ===")
    cleanup_screenshots()

    from kzer_bot.cli import _get_total_pnl

    # Show status dashboard immediately on start
    _startup_balance = fetch_live_balance() if args.balance is None else args.balance
    print_status_banner(_startup_balance)

    # Resume an existing open position on restart before entering the main loop
    if POS_FILE.exists():
        balance = _startup_balance or 17.24
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

    # Main loop — runs forever: overnight scans → wait for 9:30 → buy → sell → repeat
    skip_wait = args.now
    scans_done: set[str] = set()
    last_status: list = [0.0]

    while True:
        if not skip_wait:
            wait_for_open_with_scans(scans_done, last_status)
            wait_for_entry()

        skip_wait = False  # only skip on the very first iteration if --now passed
        # Reset scan slots for the next overnight cycle after each trading day
        scans_done.clear()

        # Hard guard: never buy outside 9:31–9:36 ET
        _now = now_et()
        _open = _now.replace(hour=9, minute=31, second=0, microsecond=0)
        _close = _now.replace(hour=9, minute=36, second=0, microsecond=0)
        if not (_open <= _now <= _close):
            log(f"Outside 09:31–09:36 entry window ({_now:%H:%M} ET) — waiting for next open.")
            notify(
                f"⚠️ <b>Buy skipped</b> — {_now:%H:%M} ET is outside entry window.\n"
                f"Scanning overnight and retrying tomorrow at 09:31.",
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
            f"🤖 <b>kzeR Wealthsimple Bot — new trading day</b>\n\n"
            f"💼 Budget: <b>${balance:.2f} CAD</b>\n"
            f"⏰ Entry: <b>09:31 ET</b>  |  🏁 Auto-sell: <b>15:55 ET</b>\n\n"
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
