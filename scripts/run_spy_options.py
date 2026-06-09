#!/usr/bin/env python3
"""
0DTE SPY Options Bot — contrarian gap-fade strategy.

Pre-market green → buy OTM puts  at 9:45 AM (fade the gap up)
Pre-market red   → buy OTM calls at 9:45 AM (fade the gap down)

Usage:
  python scripts/run_spy_options.py              # normal mode — waits for 9:45 AM
  python scripts/run_spy_options.py --now        # skip wait, enter immediately
  python scripts/run_spy_options.py --dry        # paper mode — no real orders placed
  python scripts/run_spy_options.py --balance 150  # override cash (skips WS fetch)
"""
from __future__ import annotations

import argparse
import json
import re
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
    TARGET_PREMIUM_MAX,
    TARGET_PREMIUM_MID,
    TARGET_PREMIUM_MIN,
    OptionContract,
    OptionsPosition,
    PreMarketBias,
    check_exit,
    check_reversal_starting,
    get_option_mid,
    get_otm_contracts_in_range,
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

_DRY_RUN: bool = False
_keepalive_proc: "subprocess.Popen | None" = None
_last_report_t: float = 0.0
REPORT_INTERVAL_SECS = 30 * 60


# ── Keepalive ─────────────────────────────────────────────────────────────────

def _start_keepalive() -> None:
    """Launch wealthsimple_auto.py keepalive as a background daemon.
    Refreshes the WS Edge session every 2 min so it never expires mid-session."""
    global _keepalive_proc
    if _DRY_RUN:
        return
    try:
        _keepalive_proc = subprocess.Popen(
            [PYTHON, str(AUTO), "keepalive"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"[keepalive] Started (PID {_keepalive_proc.pid}) — refreshing WS session every 2 min")
    except Exception as exc:
        log(f"[keepalive] Failed to start: {exc}")


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
        "symbol":          "SPY",
        "option_type":     pos.contract.option_type,
        "strike":          pos.contract.strike,
        "expiry":          pos.contract.expiry,
        "contracts":       pos.contracts,
        "entry_premium":   pos.entry_premium,
        "entry_time":      pos.entry_time.isoformat(),
        "entry_spy_price": pos.entry_spy_price,
        "partial_closed":  pos.partial_closed,
        "cost_basis":      pos.cost_basis,
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


# ── Balance ───────────────────────────────────────────────────────────────────

def get_available_balance() -> float:
    """Fetch live USD balance from Wealthsimple. Returns 0.0 on failure."""
    try:
        result = subprocess.run(
            [PYTHON, str(AUTO), "balance"],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        m = re.search(r"LIVE_BALANCE_USD:([\d.]+)", output)
        if m:
            return float(m.group(1))
    except Exception as e:
        log(f"[WARN] Balance fetch failed: {e}")
    return 0.0


def calc_max_contracts(ask_price: float, balance: float) -> int:
    """How many contracts can we buy with the available balance? Each contract = ask × 100."""
    if ask_price <= 0 or balance <= 0:
        return 1
    # Use 95% of balance as usable cash (leave a small buffer for fees)
    usable   = balance * 0.95
    cost_per = ask_price * 100
    n        = int(usable // cost_per)
    return max(n, 1)


# ── AI contract picker ────────────────────────────────────────────────────────

def _ai_pick_contract(
    bias: PreMarketBias,
    candidates: list[OptionContract],
    spy_price: float,
) -> OptionContract:
    """
    Ask Claude to pick the best contract from the candidate list.
    Falls back to the candidate closest to TARGET_PREMIUM_MID on failure.
    """
    if not candidates:
        raise ValueError("No candidates to pick from")

    chain_lines = "\n".join(
        f"  ${int(c.strike)} {c.option_type.upper()}: ask=${c.ask:.2f}  bid=${c.bid:.2f}"
        f"  IV={c.iv:.0%}  vol={c.volume:,}  OI={c.open_interest:,}"
        for c in candidates
    )

    prompt = (
        f"0DTE SPY contrarian gap-fade trade. Pick the single best options contract to buy.\n\n"
        f"Market context:\n"
        f"- SPY pre-market: {bias.pm_pct:+.2f}% ({bias.direction}) → fading with {bias.fade_with.upper()}S\n"
        f"- SPY live price: ${spy_price:.2f}\n"
        f"- VIX: {bias.vix:.1f}\n"
        f"- ES futures 1h: {bias.es_pct:+.2f}%\n\n"
        f"Available 0DTE {bias.fade_with.upper()} contracts (ask ${TARGET_PREMIUM_MIN:.2f}–${TARGET_PREMIUM_MAX:.2f} range):\n"
        f"{chain_lines}\n\n"
        f"Criteria: best risk/reward, sufficient liquidity (volume > 1000 preferred), "
        f"reasonable IV, not too deep OTM.\n"
        f"Reply with ONLY the strike number (e.g. '726') on the first line, "
        f"then one sentence explaining why."
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        log(f"[AI] {output[:300]}")

        m = re.search(r'\b(\d{3,4})\b', output)
        if m:
            ai_strike = int(m.group(1))
            for c in candidates:
                if int(c.strike) == ai_strike:
                    log(f"[AI] Picked ${ai_strike}")
                    return c
        log("[AI] Could not parse strike from response — using algorithm fallback")
    except Exception as e:
        log(f"[AI] Analysis failed ({e}) — using algorithm fallback")

    # Algorithm fallback: pick the candidate closest to TARGET_PREMIUM_MID
    ask_price = lambda c: c.ask if c.ask > 0 else c.mid
    return min(candidates, key=lambda c: abs(ask_price(c) - TARGET_PREMIUM_MID))


# ── Order execution ───────────────────────────────────────────────────────────

def execute_buy_option(contract: OptionContract, n_contracts: int) -> bool:
    if _DRY_RUN:
        log(
            f"[DRY] BUY {n_contracts}x SPY {contract.expiry} "
            f"${contract.strike:.0f} {contract.option_type.upper()} @ ${contract.ask:.2f}"
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
    log(f"[buy-option] {output[:800]}")
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
    log(f"[sell-option] {output[:800]}")
    return result.returncode == 0


# ── Telegram report messages ──────────────────────────────────────────────────

def _plan_report_msg(bias: "PreMarketBias", today: str, mins_to_entry: int) -> str:
    direction_emoji = "🔴" if bias.fade_with == "put" else "🟢"
    from kzer_bot.spy_options_strategy import REGIME_BIAS, TARGET_PREMIUM_MIN, TARGET_PREMIUM_MAX
    regime_label = "bearish" if REGIME_BIAS < 0 else "bullish" if REGIME_BIAS > 0 else "neutral"
    lines = [
        f"📊 <b>0DTE SPY OPTIONS | {today} | {'DRY RUN' if _DRY_RUN else 'LIVE'}</b>",
        f"{direction_emoji} <b>Playing {bias.fade_with.upper()}S today</b> (gap-fade + {regime_label} regime)",
        f"   SPY PM: {bias.pm_pct:+.2f}%  VIX: {bias.vix:.1f}  ES 1h: {bias.es_pct:+.2f}%",
        f"   Strike range: ${TARGET_PREMIUM_MIN:.2f}–${TARGET_PREMIUM_MAX:.2f} ask | 3+ strikes OTM",
        f"   Entry window: 9:45–10:00 AM ET",
        f"   Exits: +200% half close → +500% full | Noon hard close | 3:45 PM nuclear",
    ]
    if mins_to_entry > 0:
        lines.append(f"   ⏰ {mins_to_entry} min to entry window")
    return "\n".join(lines)


def _position_report_msg(pos: "OptionsPosition", current_mid: float) -> str:
    n   = now_et()
    pnl_pct = (current_mid - pos.entry_premium) / pos.entry_premium * 100 if pos.entry_premium > 0 else 0.0
    pnl_usd = (current_mid - pos.entry_premium) * pos.contracts * 100
    noon    = n.replace(hour=12, minute=0, second=0, microsecond=0)
    mins_to_noon = max(int((noon - n).total_seconds() / 60), 0)
    trend_emoji = "📈" if pnl_pct > 5 else "📉" if pnl_pct < -5 else "⚡"
    direction_emoji = "🔴" if pos.contract.option_type == "put" else "🟢"
    from kzer_bot.spy_options_strategy import PROFIT_TARGET_PCT, PARTIAL_CLOSE_PCT
    lines = [
        f"{direction_emoji}{trend_emoji} <b>SPY ${int(pos.contract.strike)} {pos.contract.option_type.upper()} 0DTE</b>",
        f"   Entry: ${pos.entry_premium:.2f}  Now: ${current_mid:.2f}",
        f"   P&L: <b>{pnl_pct:+.0f}%</b> / ${pnl_usd:+.0f}",
        f"   Contracts: {pos.contracts}  Cost basis: ${pos.cost_basis:.0f}",
    ]
    if pos.partial_closed:
        lines.append(f"   ✅ Partial close taken at +{PARTIAL_CLOSE_PCT:.0f}%")
        lines.append(f"   Next target: +{PROFIT_TARGET_PCT:.0f}% (${pos.entry_premium * (1 + PROFIT_TARGET_PCT / 100):.2f}/contract)")
    else:
        lines.append(f"   Partial at +{PARTIAL_CLOSE_PCT:.0f}% → full at +{PROFIT_TARGET_PCT:.0f}%")
    lines.append(f"   ⏰ Noon close in {mins_to_noon} min")
    return "\n".join(lines)


# ── Timing helpers ────────────────────────────────────────────────────────────

def _sleep_until(hour: int, minute: int, label: str, period_fn=None) -> None:
    """Sleep until hour:minute ET. Calls period_fn(mins_left) every REPORT_INTERVAL_SECS if provided."""
    global _last_report_t
    while True:
        n = now_et()
        if n.hour > hour or (n.hour == hour and n.minute >= minute):
            return
        target = n.replace(hour=hour, minute=minute, second=0, microsecond=0)
        secs   = max((target - n).total_seconds(), 1)
        mins   = int(secs / 60)

        if period_fn and time.time() - _last_report_t >= REPORT_INTERVAL_SECS:
            try:
                period_fn(mins)
            except Exception:
                pass
            _last_report_t = time.time()

        log(f"Waiting for {hour:02d}:{minute:02d} ET ({label}) — {mins} min left")
        time.sleep(min(secs, 60))


def _past_cutoff() -> bool:
    n = now_et()
    return n.hour > ENTRY_HOUR or (n.hour == ENTRY_HOUR and n.minute >= ENTRY_MINUTE_END)


# ── Hold loop ─────────────────────────────────────────────────────────────────

def hold_loop(pos: OptionsPosition) -> None:
    global _last_report_t
    direction_emoji = "🔴" if pos.contract.option_type == "put" else "🟢"
    notify(
        f"{direction_emoji} <b>BOUGHT</b> | SPY ${int(pos.contract.strike)} "
        f"{pos.contract.option_type.upper()} 0DTE | "
        f"entry ${pos.entry_premium:.2f} | "
        f"{pos.contracts} contract(s) | cost ${pos.cost_basis:.0f}"
    )
    _last_report_t = time.time()

    while True:
        time.sleep(60)

        current_mid = get_option_mid(pos.contract)
        if current_mid <= 0:
            log("Could not refresh option mid — retrying next cycle")
            continue

        # 30-min position update to Telegram
        if time.time() - _last_report_t >= REPORT_INTERVAL_SECS:
            notify(_position_report_msg(pos, current_mid))
            _last_report_t = time.time()

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
                remaining       = pos.contracts - half
                pos.cost_basis *= remaining / pos.contracts
                pos.contracts   = remaining
                pos.partial_closed = True
                save_position(pos)
                notify(
                    f"✅ <b>PARTIAL CLOSE</b> | SPY ${int(pos.contract.strike)} "
                    f"{pos.contract.option_type.upper()} | "
                    f"{half} contract(s) @ ${current_mid:.2f} | "
                    f"<b>P&L: {pnl_pct:+.0f}% / ${pnl_usd:+.0f}</b> | "
                    f"{remaining} contracts riding free | {reason}"
                )
                _last_report_t = time.time()
            else:
                log("[WARN] Partial sell failed — retrying next cycle")
            continue

        if action == "close_all":
            ok = execute_sell_option(pos.contract, pos.contracts)
            if ok:
                result_emoji = "🚀" if pnl_pct > 100 else "✅" if pnl_pct > 0 else "🔻"
                notify(
                    f"{result_emoji} <b>CLOSED</b> | SPY ${int(pos.contract.strike)} "
                    f"{pos.contract.option_type.upper()} | "
                    f"{pos.contracts} contract(s) @ ${current_mid:.2f} | "
                    f"<b>P&L: {pnl_pct:+.0f}% / ${pnl_usd:+.0f}</b> | {reason}"
                )
                clear_position()
                return
            else:
                log(f"[WARN] Full sell failed for: {reason} — retrying next cycle")


# ── Main run ──────────────────────────────────────────────────────────────────

def run_today(now_flag: bool = False, balance_override: float | None = None) -> None:
    today = date.today().strftime("%Y-%m-%d")
    notify(f"=== 0DTE SPY OPTIONS BOT | {today} | {'DRY RUN' if _DRY_RUN else 'LIVE'} ===")

    # ── Resume open position from today ──────────────────────────────────────
    existing = load_position()
    if existing and existing.contract.expiry == today:
        notify(
            f"Resuming open position: SPY ${existing.contract.strike:.0f} "
            f"{existing.contract.option_type.upper()} | entry ${existing.entry_premium:.2f} | "
            f"{existing.contracts} contract(s)"
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

    # Send initial game plan to Telegram
    notify(_plan_report_msg(bias, today, 0))

    # ── Wait for 9:45 AM entry window — resend plan every 30 min ─────────────
    if not now_flag:
        _sleep_until(
            ENTRY_HOUR, ENTRY_MINUTE_START,
            "entry window 9:45 AM",
            period_fn=lambda mins: notify(_plan_report_msg(bias, today, mins)),
        )

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

    # ── Get contract candidates in $0.30–$0.60 range ─────────────────────────
    candidates = get_otm_contracts_in_range(bias.fade_with, spy_price, expiry=today)
    if not candidates:
        notify(f"ERROR: No SPY {bias.fade_with.upper()} candidates found for {today} — aborting")
        return

    log(f"Found {len(candidates)} candidate contracts:")
    for c in candidates:
        log(
            f"  ${int(c.strike)} {c.option_type.upper()}: "
            f"ask=${c.ask:.2f}  bid=${c.bid:.2f}  mid=${c.mid:.2f}  "
            f"IV={c.iv:.0%}  vol={c.volume:,}  OI={c.open_interest:,}"
        )

    # ── AI picks the best contract ────────────────────────────────────────────
    log("Asking AI to pick the best contract...")
    contract = _ai_pick_contract(bias, candidates, spy_price)
    entry_ask = contract.ask if contract.ask > 0 else contract.mid

    if entry_ask < 0.01:
        notify(
            f"ERROR: Selected contract ${int(contract.strike)} ask=${entry_ask:.2f} "
            f"— market may not be open yet. Aborting."
        )
        return

    # ── Position sizing — buy as many as possible ────────────────────────────
    if balance_override is not None:
        balance = balance_override
        log(f"Using balance override: ${balance:.2f}")
    else:
        log("Fetching live balance from Wealthsimple...")
        balance = get_available_balance()
        if balance <= 0:
            log("[WARN] Balance fetch failed — defaulting to 1 contract")
            balance = entry_ask * 100  # enough for exactly 1

    n_contracts = calc_max_contracts(entry_ask, balance)
    cost_total  = entry_ask * n_contracts * 100

    notify(
        f"CONTRACT SELECTED: SPY ${int(contract.strike)} {contract.option_type.upper()} 0DTE | "
        f"ask=${entry_ask:.2f} | IV={contract.iv:.0%} | vol={contract.volume:,} | "
        f"balance=${balance:.0f} → {n_contracts} contract(s) @ ${cost_total:.0f} total"
    )

    # ── Place buy order ───────────────────────────────────────────────────────
    ok = execute_buy_option(contract, n_contracts)
    if not ok:
        notify("BUY FAILED — check data/options.log and data/screen_*.png for details")
        return

    pos = OptionsPosition(
        contract=contract,
        contracts=n_contracts,
        entry_premium=entry_ask,
        entry_time=now_et(),
        entry_spy_price=spy_price,
        cost_basis=cost_total,
    )
    save_position(pos)

    # ── Hold loop until exit condition ────────────────────────────────────────
    hold_loop(pos)
    notify(f"=== 0DTE SESSION DONE | {today} ===")


def main() -> None:
    global _DRY_RUN

    ap = argparse.ArgumentParser(description="0DTE SPY options — contrarian gap-fade bot")
    ap.add_argument("--now",     action="store_true",  help="Enter immediately (skip 9:45 AM wait)")
    ap.add_argument("--dry",     action="store_true",  help="Paper mode — no real orders")
    ap.add_argument("--balance", type=float,           help="Override available cash (skips WS fetch)")
    args = ap.parse_args()

    _DRY_RUN = args.dry
    if _DRY_RUN:
        log("[DRY RUN] No orders will be placed — strategy logic only")

    # Keep the WS browser session alive throughout the entire options session
    _start_keepalive()

    # Wait for 9:00 AM before starting bias detection
    if not args.now:
        _sleep_until(9, 0, "pre-market open")

    run_today(now_flag=args.now, balance_override=args.balance)


if __name__ == "__main__":
    main()
