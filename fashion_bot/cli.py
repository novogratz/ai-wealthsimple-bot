from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from .config import load_settings, load_universe
from .market_data import YFinanceMarketData
from .paper import PaperBroker
from .runner import run_paper_once
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

    args = parser.parse_args()
    raise SystemExit(args.func(args))
