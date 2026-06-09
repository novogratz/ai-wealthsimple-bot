#!/usr/bin/env python3
"""
0DTE SPY Options Bot — contrarian gap-fade strategy.

Pre-market green → buy OTM puts  at 9:45 AM (fade the gap up)
Pre-market red   → buy OTM calls at 9:45 AM (fade the gap down)

Usage:
  python scripts/run_spy_options.py              # normal mode
  python scripts/run_spy_options.py --now        # skip wait, enter immediately
  python scripts/run_spy_options.py --dry        # paper mode — no real orders
  python scripts/run_spy_options.py --contracts 2
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from kzer_bot.spy_options_strategy import (
    ENTRY_HOUR,
    ENTRY_MINUTE_END,
    ENTRY_MINUTE_START,
    HARD_CLOSE_HOUR,
    HARD_CLOSE_MINUTE,
    NOON_CLOSE_HOUR,
    NOON_CLOSE_MINUTE,
    OptionContract,
    OptionsPosition,
    PreMarketBias,
    check_exit,
    check_reversal_starting,
    get_option_mid,
    get_otm_contract,
    get_premarket_bias,
    get_spy_price,
    now_et,
)
from kzer_bot.telegram import send_message

TZ        = ZoneInfo("America/Toronto")
AUTO      = ROOT / "scripts" / "wealthsimple_auto.py"
PYTHON    = str(ROOT / ".venv" / "Scripts" / "python.exe")
POS_FILE  = ROOT / "data" / "options_position.json"
LOG_FILE  = ROOT / "data" / "options.log"

_CONTRACTS: int  = 1
_DRY_RUN:  bool = False


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts   = now_et().strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    log(msg)
    try:
        send_message(msg)
    except Exception:
        pass


# ── Position persistence ──────────────────────────────────────────────────────

def save_position(pos: OptionsPosition) -> None:
    data = {
        "symbol":         "SPY",
        "option_type":    pos.contract.option_type,
        "strike":         pos.contract.strike,
        "expiry":         pos.contract.expiry,
        "contracts":      pos.contracts,
        "entry_premium":  pos.entry_premium,
        "entry_time":     pos.entry_time.isoformat(),
        "entry_spy_price": pos.entry_spy_price,
        "partial_closed": pos.partial_closed,
        "cost_basis":     pos.cost_basis,
    }
    POS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_position() -> None:
    POS_FILE.unlink(missing_ok=True)


def load_position() -> OptionsPosition | None:
    if not POS_FILE.exists():
        return None
    try:
        d = json.loads(POS_FILE.read_text(encoding="utf-8"))
        contract = OptionContract(
            expiry=d["expiry"],
            strike=float(d["strike"]),
            option_type=d["option_type"],
            last_price=d.get("entry_premium", 0.0),
            bid=0.0, ask=0.0,
            mid=d.get("entry_premium", 0.0),
            iv=0.0, volume=0, open_interest=0,
        )
        return OptionsPosition(
            contract=contract,
            contracts=int(d["contracts"]),
            entry_premium=float(d["entry_premium"]),
            entry_time=datetime.fromisoformat(d["entry_time"]),
            entry_spy_price=float(d["entry_spy_price"]),
            partial_closed=bool(d.get("partial_closed", False)),
            cost_basis=float(d.get("cost_basis", 0.0)),
        )
    except Exception as e:
        log(f"[WARN] Could not load position file: {e}")
        return None


# ── Order execution ───────────────────────────────────────────────────────────

def execute_buy_option(contract: OptionContract, n_contracts: int) -> bool:
    if _DRY_RUN:
        log(
            f"[DRY] BUY {n_contracts}x SPY {contract.expiry} "
            f"${contract.strike:.0f} {contract.option_type.upper()} @ ${contract.mid:.2f}"
        )
        return True
    result = subprocess.run(
        [
            PYTHON, str(AUTO), "buy-option",
            "--symbol",      "SPY",
            "--option-type", contract.option_type,
            "--strike",      str(int(contract.strike)),
            "--expiry",      contract.expiry,
            "--contracts",   str(n_contracts),
            "--confirm",
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = (result.stdout + result.stderr).strip()
    log(f"[buy-option stdout] {output[:800]}")
    return result.returncode == 0 and "ORDER_RESULT" in output


def execute_sell_option(contract: OptionContract, n_contracts: int) -> bool:
    if _DRY_RUN:
        log(
            f"[DRY] SELL {n_contracts}x SPY {contract.expiry} "
            f"${contract.strike:.0f} {contract.option_type.upper()}"
        )
        return True
    result = subprocess.run(
        [
            PYTHON, str(AUTO), "sell-option",
            "--symbol",      "SPY",
            "--option-type", contract.option_type,
            "--strike",      str(int(contract.strike)),
            "--expiry",      contract.expiry,
            "--contracts",   str(n_contracts),
            "--confirm",
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = (result.stdout + result.stderr).strip()
    log(f"[sell-option stdout] {output[:800]}")
    return result.returncode == 0


# ── Timing helpers ────────────────────────────────────────────────────────────

def _sleep_until(hour: int, minute: int, label: str) -> None:
    while True:
        n = now_et()
        if n.hour > hour or (n.hour == hour and n.minute >= minute):
            return
        target = n.replace(hour=hour, minute=minute, second=0, microsecond=0)
        secs   = max((target - n).total_seconds(), 1)
        log(f"Waiting for {hour:02d}:{minute:02d} ET ({label}) — {int(secs / 60)} min left")
        time.sleep(min(secs, 300))


def _past_cutoff() -> bool:
    n = now_et()
    return n.hour > ENTRY_HOUR or (n.hour == ENTRY_HOUR and n.minute >= ENTRY_MINUTE_END)


def _is_noon_or_later() -> bool:
    n = now_et()
    return n.hour >= NOON_CLOSE_HOUR


# ── Hold loop ─────────────────────────────────────────────────────────────────

def hold_loop(pos: OptionsPosition) -> None:
    notify(
        f"POSITION OPEN | SPY ${pos.contract.strike:.0f} "
        f"{pos.contract.option_type.upper()} 0DTE | "
        f"entry ${pos.entry_premium:.2f} | "
        f"{pos.contracts} contract(s) | cost ${pos.cost_basis:.0f}"
    )

    while True:
        time.sleep(60)

        current_mid = get_option_mid(pos.contract)
        if current_mid <= 0:
            log("Could not refresh option mid — retrying next cycle")
            continue

        action, reason = check_exit(pos, current_mid)
        pnl_pct = (current_mid - pos.entry_premium) / pos.entry_premium * 100
        pnl_usd = (current_mid - pos.entry_premium) * pos.contracts * 100

        if action == "hold":
            log(reason)
            continue

        if action == "close_half":
            half = max(1, pos.contracts // 2)
            ok   = execute_sell_option(pos.contract, half)
            if ok:
                pos.partial_closed  = True
                remaining           = pos.contracts - half
                pos.cost_basis     *= remaining / pos.contracts
                pos.contracts       = remaining
                save_position(pos)
                notify(
                    f"PARTIAL CLOSE | {half} contract(s) @ ${current_mid:.2f} | "
                    f"P&L: {pnl_pct:+.0f}% / ${pnl_usd:+.0f} | "
                    f"{remaining} remaining | {reason}"
                )
            else:
                log("[WARN] Partial sell failed — retrying next cycle")
            continue

        if action == "close_all":
            ok = execute_sell_option(pos.contract, pos.contracts)
            if ok:
                notify(
                    f"CLOSED | {pos.contracts} contract(s) @ ${current_mid:.2f} | "
                    f"P&L: {pnl_pct:+.0f}% / ${pnl_usd:+.0f} | {reason}"
                )
                clear_position()
                return
            else:
                log(f"[WARN] Full sell failed for: {reason} — retrying next cycle")


# ── Main run ──────────────────────────────────────────────────────────────────

def run_today(now_flag: bool = False) -> None:
    today = date.today().strftime("%Y-%m-%d")
    notify(f"=== 0DTE SPY OPTIONS BOT | {today} | {'DRY RUN' if _DRY_RUN else 'LIVE'} ===")

    # ── Resume existing position from today ──────────────────────────────────
    existing = load_position()
    if existing and existing.contract.expiry == today:
        notify(
            f"Resuming open position: SPY ${existing.contract.strike:.0f} "
            f"{existing.contract.option_type.upper()} | entry ${existing.entry_premium:.2f}"
        )
        hold_loop(existing)
        return
    elif existing:
        log(f"Stale position file from {existing.contract.expiry} — clearing")
        clear_position()

    # ── Pre-market bias ───────────────────────────────────────────────────────
    log("Detecting pre-market bias...")
    bias = get_premarket_bias()
    for r in bias.reasons:
        log(f"  {r}")

    if bias.fade_with == "skip":
        notify(f"SKIP TODAY: {bias.skip_reason}")
        return

    notify(
        f"PRE-MARKET BIAS: {bias.direction.upper()} ({bias.pm_pct:+.2f}%) "
        f"→ fade with {bias.fade_with.upper()}S | "
        f"VIX {bias.vix:.1f} | ES {bias.es_pct:+.2f}%"
    )

    # ── Wait for 9:45 AM entry window ─────────────────────────────────────────
    if not now_flag:
        _sleep_until(ENTRY_HOUR, ENTRY_MINUTE_START, "entry window 9:45 AM")

    if _past_cutoff():
        notify(f"MISSED ENTRY WINDOW ({now_et().strftime('%H:%M')} ET) — skip today")
        return

    # ── Reversal confirmation ─────────────────────────────────────────────────
    confirmed, rev_msg = check_reversal_starting(bias)
    log(f"Reversal check: {rev_msg}")

    # ── Live SPY price ────────────────────────────────────────────────────────
    spy_price = get_spy_price()
    if spy_price <= 0:
        notify("ERROR: Could not fetch SPY live price — aborting today")
        return
    log(f"SPY live: ${spy_price:.2f}")

    # ── Find OTM contract ─────────────────────────────────────────────────────
    contract = get_otm_contract(bias.fade_with, spy_price, expiry=today)
    if contract is None:
        notify(f"ERROR: No SPY {bias.fade_with.upper()} contract found for {today} — aborting")
        return
    if contract.mid <= 0.01:
        notify(
            f"ERROR: SPY ${contract.strike:.0f} {contract.option_type.upper()} mid=${contract.mid:.2f} "
            f"(too cheap / illiquid) — aborting"
        )
        return

    cost_total = contract.mid * _CONTRACTS * 100
    notify(
        f"TARGET CONTRACT: SPY {today} ${contract.strike:.0f} "
        f"{contract.option_type.upper()} | mid=${contract.mid:.2f} | "
        f"IV={contract.iv:.0%} | vol={contract.volume:,} | OI={contract.open_interest:,} | "
        f"{_CONTRACTS} contract(s) = ${cost_total:.0f} cost"
    )

    # ── Place buy order ───────────────────────────────────────────────────────
    ok = execute_buy_option(contract, _CONTRACTS)
    if not ok:
        notify("BUY FAILED — check data/options.log and data/screen_*.png for details")
        return

    pos = OptionsPosition(
        contract=contract,
        contracts=_CONTRACTS,
        entry_premium=contract.mid,
        entry_time=now_et(),
        entry_spy_price=spy_price,
        cost_basis=cost_total,
    )
    save_position(pos)

    # ── Hold loop until exit condition ────────────────────────────────────────
    hold_loop(pos)
    notify(f"=== 0DTE SESSION DONE | {today} ===")


def main() -> None:
    global _DRY_RUN, _CONTRACTS

    ap = argparse.ArgumentParser(description="0DTE SPY options — contrarian gap-fade bot")
    ap.add_argument("--now",       action="store_true",     help="Enter immediately (skip wait)")
    ap.add_argument("--dry",       action="store_true",     help="Paper mode — no real orders")
    ap.add_argument("--contracts", type=int, default=1,     help="Number of contracts (default 1)")
    args = ap.parse_args()

    _DRY_RUN   = args.dry
    _CONTRACTS = args.contracts

    if _DRY_RUN:
        log("[DRY RUN] No orders will be placed — strategy logic only")

    # Wait until 9:00 AM before bias detection (pre-market data available)
    if not args.now:
        _sleep_until(9, 0, "pre-market open")

    run_today(now_flag=args.now)


if __name__ == "__main__":
    main()
