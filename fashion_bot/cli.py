from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import load_settings, load_universe
from .market_data import YFinanceMarketData
from .paper import PaperBroker
from .runner import run_paper_once
from .schedule import now_in_market_tz, is_weekday, is_market_session, should_force_exit
from .strategy import FashionStrategy
from .telegram import TelegramConfigError, send_message, trade_message


ROOT = Path(__file__).resolve().parents[1]


def build_strategy() -> FashionStrategy:
    settings = load_settings(ROOT / "config" / "settings.toml")
    universe = load_universe(ROOT / "config" / "universe.csv")
    return FashionStrategy(settings=settings, universe=universe, market_data=YFinanceMarketData())


def cmd_scan(args: argparse.Namespace) -> int:
    strategy = build_strategy()
    picks = strategy.rank(cash=args.cash)
    if not picks:
        print("No tradable Canadian candidates passed the filters.")
        return 1

    print("Rank Symbol  Price   Shares Score  Reason")
    for i, pick in enumerate(picks[: args.limit], start=1):
        print(
            f"{i:>4} {pick.symbol:<7} "
            f"{pick.last_price:>6.2f} {pick.shares:>6} "
            f"{pick.score:>5.2f}  {pick.reason}"
        )
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    strategy = build_strategy()
    picks = strategy.rank(cash=args.cash)
    if not picks:
        print("No manual order prompt: no candidate passed the filters.")
        return 1
    pick = picks[0]
    print("Manual review order prompt")
    print(f"Action: BUY, only if you independently agree")
    print(f"Symbol: {pick.symbol}")
    print(f"Shares: {pick.shares}")
    print(f"Reference price: ${pick.last_price:.2f} CAD")
    print(f"Estimated cost: ${pick.last_price * pick.shares:.2f} CAD")
    print(f"Score: {pick.score:.2f}")
    print(f"Reason: {pick.reason}")
    print("No order was placed. Enter it manually in Wealthsimple only if you choose.")

    if args.accessible:
        script = ROOT / "scripts" / "review-ticket.ps1"
        subprocess.run(
            [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-NoProfile",
                "-File",
                str(script),
                "-Action",
                "BUY",
                "-Symbol",
                pick.symbol,
                "-Shares",
                str(pick.shares),
                "-ReferencePrice",
                f"{pick.last_price:.2f}",
                "-Reason",
                pick.reason,
            ],
            check=False,
        )
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    strategy = build_strategy()
    broker = PaperBroker(path=ROOT / "data" / "paper_trades.csv", cash=args.cash)
    event = run_paper_once(strategy=strategy, broker=broker, cash=args.cash)
    print(event)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    text = trade_message(
        event=args.event,
        symbol=args.symbol,
        shares=args.shares,
        price=args.price,
        message=args.message,
    )
    if args.print_only:
        print(text)
        return 0

    try:
        send_message(text)
    except TelegramConfigError as exc:
        print(f"Telegram not configured: {exc}")
        return 2
    except RuntimeError as exc:
        print(str(exc))
        return 1

    print("Telegram notification sent.")
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    data = YFinanceMarketData()
    snap = data.snapshot(args.symbol)
    if snap is None:
        print(f"No quote available for {args.symbol}.")
        return 1

    payload = {
        "symbol": snap.symbol,
        "last_price": snap.last_price,
        "previous_close": snap.previous_close,
        "open_price": snap.open_price,
        "day_high": snap.day_high,
        "latest_volume": snap.latest_volume,
        "avg_volume": snap.avg_volume,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{snap.symbol} ${snap.last_price:.2f} CAD")
    return 0


def _get_total_pnl() -> float:
    pnl_file = ROOT / "data" / "pnl_ledger.json"
    if not pnl_file.exists():
        return 0.0
    try:
        trades = json.loads(pnl_file.read_text())
        return sum(t.get("realizedPnl", 0) for t in trades)
    except Exception:
        return 0.0


def _record_and_get_total_pnl(symbol: str, buy_cost: float, sell_value: float, quantity: float) -> float:
    pnl_file = ROOT / "data" / "pnl_ledger.json"
    trades = []
    if pnl_file.exists():
        try:
            trades = json.loads(pnl_file.read_text())
        except Exception:
            trades = []
    trades.append({
        "symbol": symbol,
        "quantity": quantity,
        "buyCost": buy_cost,
        "sellValue": sell_value,
        "realizedPnl": sell_value - buy_cost,
        "time": __import__("datetime").datetime.now().isoformat(),
    })
    pnl_file.write_text(json.dumps(trades, indent=2))
    return sum(t.get("realizedPnl", 0) for t in trades)


def _pnl_color(pnl: float) -> str:
    return "🟢" if pnl >= 0 else "🔴"


def _pnl_arrow(pnl: float) -> str:
    return "📈" if pnl >= 0 else "📉"


def _get_trade_stats() -> dict:
    from datetime import date as _date
    empty = {"count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "total_pnl_pct": 0.0,
             "today_pnl": 0.0, "today_pnl_pct": 0.0, "today_count": 0}
    pnl_file = ROOT / "data" / "pnl_ledger.json"
    if not pnl_file.exists():
        return empty
    try:
        trades = json.loads(pnl_file.read_text())
        today = _date.today().isoformat()
        today_trades = [t for t in trades if t.get("time", "").startswith(today)]

        total_pnl = sum(t.get("realizedPnl", 0) for t in trades)
        total_cost = sum(t.get("buyCost", 0) for t in trades)
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

        today_pnl = sum(t.get("realizedPnl", 0) for t in today_trades)
        today_cost = sum(t.get("buyCost", 0) for t in today_trades)
        today_pnl_pct = (today_pnl / today_cost * 100) if today_cost > 0 else 0.0

        wins = sum(1 for t in trades if t.get("realizedPnl", 0) >= 0)
        return {
            "count": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "today_pnl": today_pnl,
            "today_pnl_pct": today_pnl_pct,
            "today_count": len(today_trades),
        }
    except Exception:
        return empty


def _msg_holding_start(symbol: str, entry: float, shares: float, cost: float, all_time_pnl: float, exit_time: str) -> str:
    at_color = _pnl_color(all_time_pnl)
    lines = [
        f"🛒 <b>Position opened</b>",
        f"",
        f"🎫 Symbol: <code>{symbol}</code>",
        f"🔢 Shares held: <b>{shares:.4f}</b>",
        f"💵 Entry price: <b>${entry:.2f} CAD</b>",
        f"💰 Total invested: <b>${cost:.2f} CAD</b>",
        f"⏰ Auto-sell at: <b>{exit_time} ET</b>",
        f"",
        f"{at_color} All-time realized PnL: <b>${all_time_pnl:+.2f} CAD</b>",
    ]
    return "\n".join(lines)


def _msg_update(symbol: str, entry: float, price: float, shares: float, pos_value: float,
                unreal_pnl: float, pnl_pct: float, all_time_pnl: float) -> str:
    trade_color = _pnl_color(unreal_pnl)
    at_color = _pnl_color(all_time_pnl)
    arrow = _pnl_arrow(unreal_pnl)
    stats = _get_trade_stats()
    win_rate = (stats["wins"] / stats["count"] * 100) if stats["count"] > 0 else 0.0
    today_color = _pnl_color(stats["today_pnl"])
    lines = [
        f"{trade_color} <b>Portfolio Update</b>",
        f"",
        f"🎫 <code>{symbol}</code>  |  {arrow} ${price:.2f} CAD",
        f"🔢 Shares held: <b>{shares:.4f}</b>",
        f"💵 Entry: ${entry:.2f}  →  Now: ${price:.2f}",
        f"💼 Position value: <b>${pos_value:.2f} CAD</b>",
        f"",
        f"{arrow} Unrealized PnL: <b>${unreal_pnl:+.2f} CAD ({pnl_pct:+.2f}%)</b>",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Account Stats</b>",
        f"{today_color} Today's realized PnL: <b>${stats['today_pnl']:+.2f} CAD ({stats['today_pnl_pct']:+.2f}%)</b>",
        f"{at_color} All-time realized PnL: <b>${all_time_pnl:+.2f} CAD ({stats['total_pnl_pct']:+.2f}%)</b>",
        f"🔢 Total trades: <b>{stats['count']}</b>  |  🏆 Win rate: <b>{win_rate:.0f}%</b>",
        f"",
        f"{'🚀 Account GREEN since launch!' if all_time_pnl >= 0 else '⚠️ Account RED — need to recover'}",
    ]
    return "\n".join(lines)


def _msg_selling(symbol: str, price: float, shares: float, unreal_pnl: float, pnl_pct: float) -> str:
    arrow = _pnl_arrow(unreal_pnl)
    lines = [
        f"⏳ <b>Closing position now</b>",
        f"",
        f"🎫 <code>{symbol}</code>",
        f"💵 Exit price: <b>${price:.2f} CAD</b>",
        f"🔢 Selling: <b>{shares:.4f} shares</b>",
        f"{arrow} Estimated PnL: <b>${unreal_pnl:+.2f} CAD ({pnl_pct:+.2f}%)</b>",
    ]
    return "\n".join(lines)


def _msg_sold(symbol: str, price: float, shares: float, cost: float,
              trade_pnl: float, pnl_pct: float, all_time_pnl: float) -> str:
    trade_color = _pnl_color(trade_pnl)
    at_color = _pnl_color(all_time_pnl)
    proceeds = shares * price
    stats = _get_trade_stats()
    win_rate = (stats["wins"] / stats["count"] * 100) if stats["count"] > 0 else 0.0
    today_color = _pnl_color(stats["today_pnl"])
    lines = [
        f"{'🚀' if trade_pnl >= 0 else '📉'} <b>Position closed — {symbol}</b>",
        f"",
        f"🎫 <code>{symbol}</code>",
        f"💵 Exit price: <b>${price:.2f} CAD</b>",
        f"🔢 Shares sold: <b>{shares:.4f}</b>",
        f"💰 Total invested: ${cost:.2f} CAD",
        f"💰 Proceeds: <b>${proceeds:.2f} CAD</b>",
        f"",
        f"{trade_color} Trade PnL: <b>${trade_pnl:+.2f} CAD ({pnl_pct:+.2f}%)</b>",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Account Stats</b>",
        f"{today_color} Today: <b>${stats['today_pnl']:+.2f} CAD ({stats['today_pnl_pct']:+.2f}%)</b>  [{stats['today_count']} trade(s)]",
        f"{at_color} All-time: <b>${all_time_pnl:+.2f} CAD ({stats['total_pnl_pct']:+.2f}%)</b>",
        f"🔢 Total trades: <b>{stats['count']}</b>  |  🏆 Win rate: <b>{win_rate:.0f}%</b>",
        f"{'🟢 Account is GREEN since launch 🚀' if all_time_pnl >= 0 else '🔴 Account is RED — keep grinding 💪'}",
    ]
    return "\n".join(lines)


def cmd_watch(args: argparse.Namespace) -> int:
    strategy = build_strategy()
    trading = strategy.settings.trading

    pos_file = args.position_file.resolve()
    if not pos_file.exists():
        print("NO_POSITION")
        return 1

    pos = json.loads(pos_file.read_text())
    symbol = pos["symbol"]
    entry_price = float(pos.get("buyPrice", 0))
    shares = float(pos.get("shares", 0))
    buy_cost = float(pos.get("estimatedCost", entry_price * shares))

    auto_script = ROOT / "scripts" / "wealthsimple_auto.py"

    print(f"Holding {symbol}:  entry=${entry_price:.2f}  shares={shares:.4f}", flush=True)
    print(f"  Selling after {trading.force_exit} ET", flush=True)

    all_time_pnl = _get_total_pnl()
    try:
        send_message(_msg_holding_start(symbol, entry_price, shares, buy_cost, all_time_pnl, trading.force_exit))
    except (TelegramConfigError, RuntimeError):
        pass

    last_telegram_update = 0.0

    while True:
        now = now_in_market_tz(trading)

        if not is_weekday(now):
            print(f"{now:%H:%M} ET - weekend, sleeping 15 min", flush=True)
            time.sleep(900)
            continue

        snap = strategy.market_data.snapshot(symbol)
        if snap is None:
            print(f"{now:%H:%M} ET - no quote, retry in 60s", flush=True)
            time.sleep(60)
            continue

        last_price = snap.last_price
        if last_price <= 0:
            print(f"{now:%H:%M} ET - stale quote ({last_price}), retry in 60s", flush=True)
            time.sleep(60)
            continue

        unrealized_pnl = shares * last_price - buy_cost
        pnl_pct = unrealized_pnl / buy_cost * 100
        position_value = shares * last_price

        if should_force_exit(now, trading):
            print(f"\nSELL SIGNAL: force exit near close  (${last_price:.2f})", flush=True)
            print(f"Price: ${last_price:.2f}  Estimated PnL: ${unrealized_pnl:+.2f}", flush=True)

            try:
                send_message(_msg_selling(symbol, last_price, shares, unrealized_pnl, pnl_pct))
            except (TelegramConfigError, RuntimeError):
                pass

            sell_args = [sys.executable or "python", str(auto_script), "sell", "--symbol", symbol, "--sell-all"]
            print(f"Running: {' '.join(sell_args)}", flush=True)

            MAX_SELL_RETRIES = 3
            result = None
            sell_stdout = ""
            order_submitted = False

            for attempt in range(1, MAX_SELL_RETRIES + 1):
                result = subprocess.run(sell_args, capture_output=True, text=True, timeout=180)
                sell_stdout = result.stdout
                for line in sell_stdout.splitlines():
                    print(f"  {line}", flush=True)

                # Check if the order was actually submitted (browser may crash AFTER placing it)
                for line in sell_stdout.splitlines():
                    if line.startswith("ORDER_RESULT_JSON:"):
                        try:
                            data = json.loads(line[len("ORDER_RESULT_JSON:"):])
                            if data.get("submitted"):
                                order_submitted = True
                        except Exception:
                            pass

                if result.returncode == 0 or order_submitted:
                    break

                print(f"Sell attempt {attempt}/{MAX_SELL_RETRIES} failed (exit {result.returncode})", flush=True)
                if attempt < MAX_SELL_RETRIES:
                    try:
                        send_message(trade_message("error", message=f"⚠️ Sell attempt {attempt}/{MAX_SELL_RETRIES} failed for {symbol}. Retrying in 60s..."))
                    except (TelegramConfigError, RuntimeError):
                        pass
                    time.sleep(60)

            sell_ok = (result is not None) and (result.returncode == 0 or order_submitted)
            if not sell_ok:
                print("All sell attempts failed.", flush=True)
                try:
                    send_message(trade_message("error", message=f"❌ All {MAX_SELL_RETRIES} sell attempts failed for {symbol}. Restart bot to retry."))
                except (TelegramConfigError, RuntimeError):
                    pass
                return 1

            if order_submitted and result.returncode != 0:
                print("Order was submitted but browser crashed after — treating as success.", flush=True)

            # Parse actual fill from ORDER_RESULT_JSON for accurate PnL
            actual_price = last_price
            actual_qty = shares
            for line in sell_stdout.splitlines():
                if line.startswith("ORDER_RESULT_JSON:"):
                    try:
                        data = json.loads(line[len("ORDER_RESULT_JSON:"):])
                        if data.get("estimated_quantity"):
                            actual_qty = float(data["estimated_quantity"])
                        if data.get("estimated_value") and actual_qty > 0:
                            actual_price = float(data["estimated_value"]) / actual_qty
                    except Exception:
                        pass

            actual_proceeds = actual_qty * actual_price
            trade_pnl = actual_proceeds - buy_cost
            trade_pnl_pct = trade_pnl / buy_cost * 100

            all_time_pnl = _record_and_get_total_pnl(symbol, buy_cost, actual_proceeds, actual_qty)
            pos_file.unlink(missing_ok=True)
            print(f"\nPosition closed. Trade PnL: ${trade_pnl:+.2f}  All-time: ${all_time_pnl:+.2f}", flush=True)
            try:
                send_message(_msg_sold(symbol, actual_price, actual_qty, buy_cost, trade_pnl, trade_pnl_pct, all_time_pnl))
            except (TelegramConfigError, RuntimeError):
                pass
            return 0

        print(f"{now:%H:%M} ET | ${last_price:.2f} ({pnl_pct:+.2f}%) | unrealized ${unrealized_pnl:+.2f}", flush=True)

        in_session = is_market_session(now, trading)
        update_interval = 300 if in_session else 14400  # 5 min during market, 4h outside
        if time.time() - last_telegram_update >= update_interval:
            all_time_pnl = _get_total_pnl()
            try:
                send_message(_msg_update(symbol, entry_price, last_price, shares, position_value, unrealized_pnl, pnl_pct, all_time_pnl))
            except (TelegramConfigError, RuntimeError):
                pass
            last_telegram_update = time.time()

        time.sleep(args.interval)


def cmd_balance(args: argparse.Namespace) -> int:
    auto_script = ROOT / "scripts" / "wealthsimple_auto.py"
    print("Fetching live balance from Wealthsimple...", flush=True)
    
    result = subprocess.run(
        [sys.executable or "python", str(auto_script), "balance"],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    if result.returncode != 0:
        print(f"Failed to fetch balance: {result.stderr}")
        return 1
    
    match = re.search(r"LIVE_BALANCE_CAD:([0-9,.]+)", result.stdout)
    if not match:
        print("Could not parse balance from output.")
        print(f"STDOUT: {result.stdout}")
        return 1
        
    balance = float(match.group(1))
    print(f"Live Balance: ${balance:.2f} CAD")
    
    # Also fetch PnL to include in the same update
    pnl_file = ROOT / "data" / "pnl_ledger.json"
    pnl_info = ""
    if pnl_file.exists():
        try:
            trades = json.loads(pnl_file.read_text())
            total_pnl = sum(trade.get("realizedPnl", 0) for trade in trades)
            trade_count = len(trades)
            pnl_info = f"\n📊 <b>Overall PnL:</b> ${total_pnl:+.2f} CAD ({trade_count} trades)"
        except Exception:
            pass

    msg = f"<b>Current Portfolio Status</b>\n💵 Unregistered Cash: ${balance:.2f} CAD{pnl_info}\n✅ Live from Wealthsimple"
    try:
        send_message(trade_message("info", message=msg))
        print("Telegram notification sent.")
    except Exception as e:
        print(f"Failed to send Telegram: {e}")
        
    return 0


def cmd_pnl(args: argparse.Namespace) -> int:
    pnl_file = ROOT / "data" / "pnl_ledger.json"
    if not pnl_file.exists():
        print("No PnL history found.")
        return 0
    
    try:
        trades = json.loads(pnl_file.read_text())
        total_pnl = sum(trade.get("realizedPnl", 0) for trade in trades)
        trade_count = len(trades)
        
        print(f"Total Realized PnL: ${total_pnl:.2f} CAD ({trade_count} trades)")
        
        if args.notify:
            status = "🚀 <b>BIG WIN</b>" if total_pnl > 0 else "📉 <b>BIG LOSE</b>" if total_pnl < 0 else "ℹ️ <b>INFO</b>"
            msg = f"{status}\n\n📊 <b>Overall Portfolio Performance</b>\n💰 Total Realized PnL: ${total_pnl:+.2f} CAD\n🔢 Total Trades: {trade_count}"
            send_message(trade_message("info", message=msg))
            print("Telegram notification sent.")
            
    except Exception as e:
        print(f"Error calculating PnL: {e}")
        return 1
        
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="fashion_bot")
    sub = parser.add_subparsers(required=True)

    scan = sub.add_parser("scan", help="rank Canadian-stock candidates")
    scan.add_argument("--cash", type=float, default=17.24)
    scan.add_argument("--limit", type=int, default=5)
    scan.set_defaults(func=cmd_scan)

    prompt = sub.add_parser("prompt", help="print a manual order prompt")
    prompt.add_argument("--cash", type=float, default=17.24)
    prompt.add_argument("--accessible", action="store_true", help="open a large spoken review ticket")
    prompt.set_defaults(func=cmd_prompt)

    paper = sub.add_parser("paper", help="run one guarded paper-trading cycle")
    paper.add_argument("--cash", type=float, default=17.24)
    paper.set_defaults(func=cmd_paper)

    notify = sub.add_parser("notify", help="send a Telegram notification")
    notify.add_argument(
        "--event",
        required=True,
        choices=[
            "scan_top",
            "scan_candidates",
            "buy_preparing",
            "buy_review",
            "buy_submitted",
            "sell_preparing",
            "sell_review",
            "sell_submitted",
            "error",
            "info",
        ],
    )
    notify.add_argument("--symbol")
    notify.add_argument("--shares", type=int)
    notify.add_argument("--price", type=float)
    notify.add_argument("--message")
    notify.add_argument("--print-only", action="store_true", help="print instead of sending")
    notify.set_defaults(func=cmd_notify)

    quote = sub.add_parser("quote", help="fetch one market-data snapshot")
    quote.add_argument("--symbol", required=True)
    quote.add_argument("--json", action="store_true", help="print machine-readable JSON")
    quote.set_defaults(func=cmd_quote)

    balance = sub.add_parser("balance", help="fetch live balance and notify telegram")
    balance.set_defaults(func=cmd_balance)

    pnl = sub.add_parser("pnl", help="calculate total realized pnl")
    pnl.add_argument("--notify", action="store_true", help="send result to telegram")
    pnl.set_defaults(func=cmd_pnl)

    watch = sub.add_parser("watch", help="hold a live position and auto-sell near close")
    watch.add_argument("--position-file", type=Path, default=ROOT / "data" / "open_position.json")
    watch.add_argument("--interval", type=int, default=60, help="seconds between price checks")
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    raise SystemExit(args.func(args))
