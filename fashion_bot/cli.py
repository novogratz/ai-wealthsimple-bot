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
from .schedule import now_in_market_tz, is_weekday, should_force_exit
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
    shares = int(pos.get("shares", 0))
    buy_cost = float(pos.get("estimatedCost", entry_price * shares))

    auto_script = ROOT / "scripts" / "wealthsimple_auto.py"
    confirm = args.confirm

    print(f"Holding {symbol}:  entry=${entry_price:.2f}  shares={shares}", flush=True)
    print(f"  Selling after {trading.force_exit} ET", flush=True)

    try:
        text = trade_message("info", message=f"Holding {symbol} until close. Entry: ${entry_price:.2f}")
        send_message(text)
    except (TelegramConfigError, RuntimeError):
        pass

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

        if should_force_exit(now, trading):
            estimated_sell_value = shares * last_price
            pnl = estimated_sell_value - buy_cost
            print(f"\nSELL SIGNAL: force exit near close  (${last_price:.2f})", flush=True)
            print(f"Price: ${last_price:.2f}  Estimated PnL: ${pnl:.2f}", flush=True)

            try:
                text = trade_message("info", message=f"Force-exiting {symbol} near close")
                send_message(text)
            except (TelegramConfigError, RuntimeError):
                pass

            sell_args = [
                sys.executable or "python",
                str(auto_script),
                "sell",
                "--symbol", symbol,
                "--sell-all",
            ]
            if confirm:
                sell_args.append("--confirm")

            print(f"Running: {' '.join(sell_args)}", flush=True)
            result = subprocess.run(sell_args, timeout=180)

            if result.returncode != 0:
                print(f"Sell automation failed (exit {result.returncode})", flush=True)
                try:
                    text = trade_message("error", message=f"Sell automation failed for {symbol}")
                    send_message(text)
                except (TelegramConfigError, RuntimeError):
                    pass
                return 1

            print(f"\nPosition closed. PnL: ${pnl:.2f}", flush=True)
            try:
                text = trade_message("info", symbol=symbol, shares=shares, price=last_price,
                                     message=f"Sold {symbol}. PnL: ${pnl:.2f}")
                send_message(text)
            except (TelegramConfigError, RuntimeError):
                pass
            return 0

        pnl_pct = (last_price - entry_price) / entry_price * 100
        print(f"{now:%H:%M} ET | ${last_price:.2f} ({pnl_pct:+.2f}%)", flush=True)
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
    
    msg = f"Current Portfolio Status:\n💵 Unregistered Cash: ${balance:.2f} CAD\n✅ Live from Wealthsimple"
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
            status = "🚀 BIG WIN" if total_pnl > 0 else "📉 BIG LOSE" if total_pnl < 0 else "ℹ️ INFO"
            msg = f"{status}\n\n📊 **Overall Portfolio Performance**\n💰 Total Realized PnL: ${total_pnl:+.2f} CAD\n🔢 Total Trades: {trade_count}"
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
    watch.add_argument("--confirm", action="store_true", help="submit sell order automatically")
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    raise SystemExit(args.func(args))
