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
import threading
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
from kzer_bot.market_calendar import is_early_close, is_trading_day
from kzer_bot.quant_research import ShadowLedger, ShadowTrade, decision_id, validate_quote
from kzer_bot.telegram import get_commands, send_message

TZ        = ZoneInfo("America/Toronto")
AUTO      = ROOT / "scripts" / "wealthsimple_auto.py"
# Reuse the interpreter that launched this script. This works for both
# .venv/bin/python on macOS/Linux and .venv\Scripts\python.exe on Windows.
PYTHON    = sys.executable
POS_FILE  = ROOT / "data" / "options_position.json"
LOG_FILE  = ROOT / "data" / "options.log"
AUDIT_FILE = ROOT / "data" / "options_audit.jsonl"
RISK_FILE = ROOT / "data" / "options_daily_risk.json"
STOP_FILE = ROOT / "data" / "options_emergency_stop"
TG_OFFSET_FILE = ROOT / "data" / "telegram_offset.json"
SHADOW_FILE = ROOT / "data" / "options_shadow.jsonl"
SHADOW_POS_FILE = ROOT / "data" / "options_shadow_position.json"
BOT_ID    = "spy-0dte-long-v1"

# Deploy the largest whole-contract amount affordable by the live USD cash.
# With integer contracts this naturally invests 50–100% whenever a contract is
# affordable, while the reviewed debit is still forbidden from exceeding cash.
MIN_DEPLOY_PCT = 0.50
PREMARKET_SCAN_HOUR = 9
PREMARKET_SCAN_MINUTE = 0
PREMARKET_REPORT_SECS = 30 * 60

_DRY_RUN: bool = False
_keepalive_proc: "subprocess.Popen | None" = None
_last_report_t: float = 0.0
REPORT_INTERVAL_SECS = 30 * 60
POSITION_REPORT_SECS = 30 * 60
MAX_DAILY_LOSS_PCT = 0.50
_reporter_stop = threading.Event()


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


def _stop_keepalive() -> None:
    global _keepalive_proc
    if _keepalive_proc is not None and _keepalive_proc.poll() is None:
        _keepalive_proc.terminate()
        try:
            _keepalive_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _keepalive_proc.kill()
    _keepalive_proc = None


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts   = now_et().strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def audit(event: str, **fields) -> None:
    record = {"ts": now_et().isoformat(), "event": event, **fields}
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    log(msg)
    audit("notification", message=msg)
    try:
        send_message(msg)
    except Exception:
        pass


def _load_risk_state() -> dict:
    today = now_et().date().isoformat()
    try:
        state = json.loads(RISK_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("date") != today:
        state = {"date": today, "entries": 0, "realized_pnl": 0.0, "starting_balance": None}
    return state


def _save_risk_state(state: dict) -> None:
    RISK_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _entry_allowed(balance: float) -> tuple[bool, str]:
    state = _load_risk_state()
    if int(state.get("entries", 0)) >= 1:
        return False, "one-trade-per-day lockout is active"
    starting = float(state.get("starting_balance") or balance or 0)
    if starting > 0 and float(state.get("realized_pnl", 0)) <= -starting * MAX_DAILY_LOSS_PCT:
        return False, "daily loss lockout is active"
    return True, ""


def _mark_entry(balance: float) -> None:
    state = _load_risk_state()
    state["entries"] = int(state.get("entries", 0)) + 1
    state["starting_balance"] = state.get("starting_balance") or balance
    _save_risk_state(state)
    audit("entry_lock", state=state)


def _mark_realized_pnl(pnl: float) -> None:
    state = _load_risk_state()
    state["realized_pnl"] = float(state.get("realized_pnl", 0)) + pnl
    _save_risk_state(state)
    audit("realized_pnl", pnl=pnl, state=state)


def _poll_control_commands(position: OptionsPosition | None = None) -> str | None:
    try:
        offset = json.loads(TG_OFFSET_FILE.read_text()).get("offset", 0) if TG_OFFSET_FILE.exists() else 0
        commands, next_offset = get_commands(int(offset))
        TG_OFFSET_FILE.write_text(json.dumps({"offset": next_offset}), encoding="utf-8")
    except Exception:
        return "stop" if STOP_FILE.exists() else None
    action = None
    for command in commands:
        if command == "/stop":
            STOP_FILE.write_text(now_et().isoformat(), encoding="utf-8")
            action = "stop"
            notify("🛑 <b>EMERGENCY STOP ENABLED</b> — no new entries; bot-owned position will close")
        elif command == "/resume":
            STOP_FILE.unlink(missing_ok=True)
            action = "resume"
            notify("▶️ <b>Emergency stop cleared</b>")
        elif command == "/status":
            status = "IN POSITION" if position else "FLAT"
            notify(f"ℹ️ <b>SPY bot status:</b> {status} | stop={'ON' if STOP_FILE.exists() else 'OFF'}")
    return action or ("stop" if STOP_FILE.exists() else None)


# ── Position persistence ──────────────────────────────────────────────────────

def save_position(pos: OptionsPosition) -> None:
    data = {
        "bot_id":          BOT_ID,
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
        "reconciled":      pos.reconciled,
    }
    POS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_position() -> None:
    POS_FILE.unlink(missing_ok=True)


def load_position() -> OptionsPosition | None:
    if not POS_FILE.exists():
        return None
    try:
        d = json.loads(POS_FILE.read_text(encoding="utf-8"))
        if d.get("bot_id") != BOT_ID or d.get("symbol") != "SPY":
            log("[SAFETY] Position ledger is not owned by this SPY 0DTE bot")
            return None
        contract = OptionContract(
            expiry=d["expiry"], strike=float(d["strike"]), option_type=d["option_type"],
            last_price=d.get("entry_premium", 0.0), bid=0.0, ask=0.0,
            mid=d.get("entry_premium", 0.0), iv=0.0, volume=0, open_interest=0,
        )
        return OptionsPosition(
            contract=contract, contracts=int(d["contracts"]),
            entry_premium=float(d["entry_premium"]),
            entry_time=datetime.fromisoformat(d["entry_time"]),
            entry_spy_price=float(d["entry_spy_price"]),
            partial_closed=bool(d.get("partial_closed", False)),
            cost_basis=float(d.get("cost_basis", 0.0)),
            reconciled=bool(d.get("reconciled", False)),
        )
    except Exception as e:
        log(f"[WARN] Could not load position file: {e}")
        return None


def _save_shadow_position(pos: OptionsPosition, model_score: float) -> None:
    SHADOW_POS_FILE.write_text(json.dumps({
        "expiry": pos.contract.expiry, "option_type": pos.contract.option_type,
        "strike": pos.contract.strike, "contracts": pos.contracts,
        "entry_premium": pos.entry_premium, "entry_time": pos.entry_time.isoformat(),
        "entry_spy_price": pos.entry_spy_price, "cost_basis": pos.cost_basis,
        "bid": pos.contract.bid, "ask": pos.contract.ask, "mid": pos.contract.mid,
        "iv": pos.contract.iv, "volume": pos.contract.volume,
        "open_interest": pos.contract.open_interest, "model_score": model_score,
    }, indent=2), encoding="utf-8")


def _load_shadow_position() -> tuple[OptionsPosition, float] | None:
    try:
        data = json.loads(SHADOW_POS_FILE.read_text(encoding="utf-8"))
        contract = OptionContract(
            expiry=data["expiry"], strike=float(data["strike"]),
            option_type=data["option_type"], last_price=float(data["mid"]),
            bid=float(data["bid"]), ask=float(data["ask"]), mid=float(data["mid"]),
            iv=float(data["iv"]), volume=int(data["volume"]),
            open_interest=int(data["open_interest"]),
        )
        return OptionsPosition(
            contract=contract, contracts=int(data["contracts"]),
            entry_premium=float(data["entry_premium"]),
            entry_time=datetime.fromisoformat(data["entry_time"]),
            entry_spy_price=float(data["entry_spy_price"]),
            cost_basis=float(data["cost_basis"]), reconciled=True,
        ), float(data["model_score"])
    except Exception:
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
    """Buy the maximum whole contracts without exceeding the USD cash balance."""
    if ask_price <= 0 or balance <= 0:
        return 0
    usable   = balance
    cost_per = ask_price * 100
    n        = int(usable // cost_per)
    return max(n, 0)


def _broker_option_quote(contract: OptionContract) -> float:
    """Prefer Wealthsimple bid/mid for exits; return 0 to trigger Yahoo fallback."""
    try:
        result = subprocess.run([
            PYTHON, str(AUTO), "option-quote", "--symbol", "SPY",
            "--option-type", contract.option_type, "--strike", str(int(contract.strike)),
            "--expiry", contract.expiry, "--contracts", "1",
        ], capture_output=True, text=True, timeout=120)
        match = re.search(r"OPTION_QUOTE_JSON:(\{.*\})", result.stdout + result.stderr)
        if match:
            quote = json.loads(match.group(1))
            audit("broker_quote", contract=contract.strike, quote=quote)
            return float(quote.get("bid") or quote.get("mid") or 0)
    except Exception as exc:
        log(f"[WARN] Broker quote failed: {exc}")
    return 0.0


def _reconcile_position(pos: OptionsPosition, attempts: int = 6) -> OptionsPosition:
    """Replace estimated entry/quantity with broker-confirmed fill details."""
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run([
                PYTHON, str(AUTO), "option-position", "--symbol", "SPY",
                "--option-type", pos.contract.option_type,
                "--strike", str(int(pos.contract.strike)), "--expiry", pos.contract.expiry,
                "--contracts", str(pos.contracts),
            ], capture_output=True, text=True, timeout=120)
            match = re.search(r"OPTION_POSITION_JSON:(\{.*\})", result.stdout + result.stderr)
            if match:
                fill = json.loads(match.group(1))
                pos.contracts = int(fill["contracts"])
                pos.entry_premium = float(fill["fill_price"])
                pos.cost_basis = float(fill["fill_value"])
                pos.reconciled = True
                save_position(pos)
                audit("fill_reconciled", attempt=attempt, fill=fill)
                notify(
                    f"✅ <b>FILL CONFIRMED</b> | {pos.contracts}x SPY "
                    f"${pos.contract.strike:.0f} {pos.contract.option_type.upper()} @ ${pos.entry_premium:.2f}"
                )
                return pos
        except Exception as exc:
            log(f"[WARN] Fill reconciliation attempt {attempt}: {exc}")
        if attempt < attempts:
            time.sleep(10)
    audit("fill_unreconciled", strike=pos.contract.strike, contracts=pos.contracts)
    notify("⚠️ Fill not yet visible at broker; keeping provisional ledger and retrying during monitoring")
    return pos


def _cancel_pending_option(pos: OptionsPosition) -> bool:
    try:
        result = subprocess.run([
            PYTHON, str(AUTO), "cancel-option", "--symbol", "SPY",
            "--option-type", pos.contract.option_type,
            "--strike", str(int(pos.contract.strike)), "--expiry", pos.contract.expiry,
            "--contracts", str(pos.contracts),
        ], capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0 and "OPTION_CANCELLED:" in result.stdout
        audit("pending_cancel", success=ok, output=(result.stdout + result.stderr)[-500:])
        return ok
    except Exception as exc:
        audit("pending_cancel", success=False, error=str(exc))
        return False


# ── AI contract picker ────────────────────────────────────────────────────────

def _contract_quant_score(contract: OptionContract, spy_price: float) -> tuple[float, dict[str, float]]:
    """Score a bounded 0DTE contract on execution quality and convexity (0–100)."""
    ask = contract.ask if contract.ask > 0 else contract.mid
    spread = max(contract.ask - contract.bid, 0.0) if contract.ask > 0 and contract.bid > 0 else ask
    spread_pct = spread / ask if ask > 0 else 1.0
    distance = abs(contract.strike - spy_price)

    # Tight spreads and real activity matter most for executable 0DTE orders.
    spread_score = max(0.0, 30.0 * (1.0 - min(spread_pct, 1.0)))
    volume_score = min(contract.volume / 10_000, 1.0) * 20.0
    oi_score = min(contract.open_interest / 5_000, 1.0) * 12.0
    premium_score = max(0.0, 18.0 - abs(ask - TARGET_PREMIUM_MID) / 0.25 * 18.0)
    distance_score = max(0.0, 15.0 - abs(distance - 4.5) / 3.5 * 15.0)
    iv_score = 5.0 if 0.10 <= contract.iv <= 1.50 else 1.0
    total = max(0.0, min(100.0, spread_score + volume_score + oi_score + premium_score + distance_score + iv_score))
    return total, {
        "spread": spread_score, "volume": volume_score, "oi": oi_score,
        "premium": premium_score, "distance": distance_score, "iv": iv_score,
    }


def _rank_contracts(candidates: list[OptionContract], spy_price: float) -> list[tuple[OptionContract, float]]:
    return sorted(
        ((c, _contract_quant_score(c, spy_price)[0]) for c in candidates),
        key=lambda item: item[1],
        reverse=True,
    )


def _contract_leaderboard(candidates: list[OptionContract], spy_price: float, limit: int = 3) -> str:
    rows = []
    for rank, (c, score) in enumerate(_rank_contracts(candidates, spy_price)[:limit], 1):
        ask = c.ask if c.ask > 0 else c.mid
        spread = max(c.ask - c.bid, 0.0)
        rows.append(
            f"#{rank} ${c.strike:.0f} {c.option_type.upper()} | score {score:.1f}/100 | "
            f"ask ${ask:.2f} | spread ${spread:.2f} | {abs(c.strike-spy_price):.1f}pt OTM | "
            f"vol {c.volume:,} OI {c.open_interest:,}"
        )
        audit(
            "contract_score", rank=rank, strike=c.strike, option_type=c.option_type,
            score=score, bid=c.bid, ask=c.ask, spread=spread,
            distance=abs(c.strike-spy_price), volume=c.volume,
            open_interest=c.open_interest, iv=c.iv, spy_price=spy_price,
        )
    return "\n".join(rows)


def _ai_pick_contract(
    bias: PreMarketBias,
    candidates: list[OptionContract],
    spy_price: float,
) -> OptionContract:
    """
    Pick the highest deterministic quant score. The score is auditable and does
    not depend on an LLM response at trade time.
    """
    if not candidates:
        raise ValueError("No candidates to pick from")

    ranked = _rank_contracts(candidates, spy_price)
    chosen, score = ranked[0]
    log("[QUANT] Contract leaderboard:\n" + _contract_leaderboard(candidates, spy_price))
    log(f"[QUANT] PLAN: buy ${chosen.strike:.0f} {chosen.option_type.upper()} at market | score {score:.1f}/100")
    return chosen


# ── Order execution ───────────────────────────────────────────────────────────

def execute_buy_option(contract: OptionContract, n_contracts: int, max_debit: float) -> str:
    """Prepare the exact ticket; securities submission requires human confirmation."""
    expected_cost = contract.ask * n_contracts * 100
    if n_contracts < 1 or expected_cost > max_debit:
        log(f"[SAFETY] Refusing buy: {n_contracts} contract(s), expected cost ${expected_cost:.2f}")
        return "failed"
    if _DRY_RUN:
        log(
            f"[DRY] BUY {n_contracts}x SPY {contract.expiry} "
            f"${contract.strike:.0f} {contract.option_type.upper()} @ ${contract.ask:.2f}"
        )
        return "dry"
    result = subprocess.run(
        [
            PYTHON, str(AUTO), "buy-option",
            "--symbol",      "SPY",
            "--option-type", contract.option_type,
            "--strike",      str(int(contract.strike)),
            "--expiry",      contract.expiry,
            "--contracts",   str(n_contracts),
            "--max-cost",    f"{max_debit:.2f}",
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = (result.stdout + result.stderr).strip()
    log(f"[buy-option] {output[:800]}")
    if result.returncode == 0 and "ORDER_RESULT_JSON:" in output:
        return "review"
    return "failed"


def execute_sell_option(contract: OptionContract, n_contracts: int) -> str:
    owned = load_position()
    if (
        owned is None
        or owned.contract.expiry != contract.expiry
        or owned.contract.option_type != contract.option_type
        or owned.contract.strike != contract.strike
        or n_contracts < 1
        or n_contracts > owned.contracts
    ):
        log("[SAFETY] Refusing sell: contract/quantity is not owned in the bot ledger")
        return "failed"
    if _DRY_RUN:
        log(
            f"[DRY] SELL {n_contracts}x SPY {contract.expiry} "
            f"${contract.strike:.0f} {contract.option_type.upper()}"
        )
        return "dry"
    result = subprocess.run(
        [
            PYTHON, str(AUTO), "sell-option",
            "--symbol",      "SPY",
            "--option-type", contract.option_type,
            "--strike",      str(int(contract.strike)),
            "--expiry",      contract.expiry,
            "--contracts",   str(n_contracts),
        ],
        capture_output=True, text=True, timeout=300,
    )
    output = (result.stdout + result.stderr).strip()
    log(f"[sell-option] {output[:800]}")
    return "review" if result.returncode == 0 and "ORDER_RESULT_JSON:" in output else "failed"


# ── Telegram report messages ──────────────────────────────────────────────────

def _plan_report_msg(bias: "PreMarketBias", today: str, mins_to_entry: int) -> str:
    direction_emoji = "🔴" if bias.fade_with == "put" else "🟢"
    from kzer_bot.spy_options_strategy import REGIME_BIAS, TARGET_PREMIUM_MIN, TARGET_PREMIUM_MAX
    regime_label = "bearish" if REGIME_BIAS < 0 else "bullish" if REGIME_BIAS > 0 else "neutral"
    lines = [
        f"📊 <b>0DTE SPY OPTIONS | {today} | {'DRY RUN' if _DRY_RUN else 'ORDER REVIEW'}</b>",
        f"{direction_emoji} <b>Playing {bias.fade_with.upper()}S today</b> (gap-fade + {regime_label} regime)",
        f"   SPY PM: {bias.pm_pct:+.2f}%  VIX: {bias.vix:.1f}  ES 1h: {bias.es_pct:+.2f}%",
        f"   Strike range: 4–5 SPY points OTM | ${TARGET_PREMIUM_MIN:.2f}–${TARGET_PREMIUM_MAX:.2f} ask",
        f"   Sizing: maximum whole contracts within USD cash (target 50–100% deployed)",
        f"   Safety: reviewed debit cannot exceed cash | no naked sells",
        f"   Entry window: 9:45–10:00 AM ET",
        f"   Exits: +500% full close | 3:25 PM time close | 3:45 PM nuclear",
    ]
    if mins_to_entry > 0:
        lines.append(f"   ⏰ {mins_to_entry} min to entry window")
    return "\n".join(lines)


def _scored_plan_report(bias: "PreMarketBias", today: str, mins_to_entry: int) -> str:
    """Build a directional plan with an auditable factor and contract board."""
    base = _plan_report_msg(bias, today, mins_to_entry)
    factor_lines = [r.strip() for r in bias.reasons if r.strip()]
    factors = "\n".join(f"   {line}" for line in factor_lines)
    if factors:
        base += "\n\n<b>Directional factor model</b>\n" + factors
    spy_price = get_spy_price()
    if spy_price <= 0 or bias.fade_with not in {"call", "put"}:
        return base + "\n   Contract board: waiting for a valid SPY quote"
    candidates = get_otm_contracts_in_range(
        bias.fade_with, spy_price, expiry=now_et().date().isoformat()
    )
    if not candidates:
        return base + "\n   Contract board: exact 0DTE quotes unavailable pre-open"
    board = _contract_leaderboard(candidates, spy_price)
    planned, score = _rank_contracts(candidates, spy_price)[0]
    plan = (
        f"PLAN → MARKET BUY ${planned.strike:.0f} {planned.option_type.upper()} "
        f"at entry confirmation | score {score:.1f}/100"
    )
    chosen_parts = _contract_quant_score(planned, spy_price)[1]
    breakdown = (
        f"Execution score → spread {chosen_parts['spread']:.1f}/30 | "
        f"volume {chosen_parts['volume']:.1f}/20 | OI {chosen_parts['oi']:.1f}/12 | "
        f"premium fit {chosen_parts['premium']:.1f}/18 | distance {chosen_parts['distance']:.1f}/15 | "
        f"IV sanity {chosen_parts['iv']:.1f}/5"
    )
    log("[30-MIN QUANT PLAN]\n" + board + "\n" + breakdown + "\n" + plan)
    return base + "\n\n<b>Quant contract board</b>\n" + board + "\n" + breakdown + "\n<b>" + plan + "</b>"


def _half_hour_report() -> None:
    """Send current SPY state at an exact :00/:30 ET boundary."""
    position = load_position()
    if position and position.contract.expiry == now_et().date().isoformat():
        quote = _broker_option_quote(position.contract) or get_option_mid(position.contract)
        if quote > 0:
            notify(_position_report_msg(position, quote))
            return

    shadow = _load_shadow_position()
    if shadow and shadow[0].contract.expiry == now_et().date().isoformat():
        shadow_pos, model_score = shadow
        quote = get_option_mid(shadow_pos.contract)
        if quote > 0:
            action, reason = check_exit(shadow_pos, quote)
            pnl = (quote - shadow_pos.entry_premium) * shadow_pos.contracts * 100
            notify("🧪 <b>SHADOW</b> | " + _position_report_msg(shadow_pos, quote) + f"\n   Model score: {model_score:.1f} | {reason}")
            if action != "hold":
                audit("shadow_exit", action=action, pnl=pnl, reason=reason)
                with SHADOW_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"timestamp": now_et().isoformat(), "event": "exit", "pnl": pnl, "reason": reason}) + "\n")
                SHADOW_POS_FILE.unlink(missing_ok=True)
            return

    bias = get_premarket_bias()
    n = now_et()
    trading = is_trading_day(n.date())
    session = "MARKET CLOSED"
    if trading and (n.hour, n.minute) < (9, 30):
        session = "PREMARKET"
    elif trading and (n.hour, n.minute) < (16, 0):
        session = "REGULAR SESSION"
    elif trading:
        session = "AFTER HOURS"
    report = _scored_plan_report(bias, n.date().isoformat(), 0)
    notify(f"🕒 <b>HALF-HOUR QUANT UPDATE | {n:%H:%M} ET | {session}</b>\n" + report)


def _half_hour_reporter_loop() -> None:
    """Publish once at startup, then align reports to wall-clock :00/:30 ET."""
    try:
        _half_hour_report()
    except Exception as exc:
        log(f"[reporter] Startup report failed: {exc}")

    while not _reporter_stop.is_set():
        n = now_et()
        minute_target = 30 if n.minute < 30 else 60
        wait = (minute_target - n.minute) * 60 - n.second - n.microsecond / 1_000_000
        if _reporter_stop.wait(max(wait, 0.1)):
            return
        try:
            _half_hour_report()
        except Exception as exc:
            log(f"[reporter] Half-hour report failed: {exc}")


def _position_report_msg(pos: "OptionsPosition", current_mid: float) -> str:
    from kzer_bot.spy_options_strategy import (
        PROFIT_TARGET_PCT, PARTIAL_CLOSE_PCT, NOON_CLOSE_HOUR, NOON_CLOSE_MINUTE,
    )
    n   = now_et()
    pnl_pct = (current_mid - pos.entry_premium) / pos.entry_premium * 100 if pos.entry_premium > 0 else 0.0
    pnl_usd = (current_mid - pos.entry_premium) * pos.contracts * 100
    close_dt = n.replace(hour=NOON_CLOSE_HOUR, minute=NOON_CLOSE_MINUTE, second=0, microsecond=0)
    mins_to_close = max(int((close_dt - n).total_seconds() / 60), 0)
    close_label = f"{NOON_CLOSE_HOUR % 12 or 12}:{NOON_CLOSE_MINUTE:02d} PM"
    trend_emoji = "📈" if pnl_pct > 5 else "📉" if pnl_pct < -5 else "⚡"
    direction_emoji = "🔴" if pos.contract.option_type == "put" else "🟢"
    lines = [
        f"{direction_emoji}{trend_emoji} <b>SPY ${int(pos.contract.strike)} {pos.contract.option_type.upper()} 0DTE</b>",
        f"   Entry: ${pos.entry_premium:.2f}  Now: ${current_mid:.2f}",
        f"   P&L: <b>{pnl_pct:+.0f}%</b> / ${pnl_usd:+.0f}",
        f"   Contracts: {pos.contracts}  Cost basis: ${pos.cost_basis:.0f}",
    ]
    if pos.partial_closed:
        lines.append(f"   ✅ Partial close taken")
        lines.append(f"   Next target: +{PROFIT_TARGET_PCT:.0f}% (${pos.entry_premium * (1 + PROFIT_TARGET_PCT / 100):.2f}/contract)")
    else:
        lines.append(f"   Exit plan: full close at +{PROFIT_TARGET_PCT:.0f}% or mandatory time close")
    lines.append(f"   ⏰ Hard close {close_label} ET  ({mins_to_close} min)")
    lines.append(f"   📅 Tomorrow 9:45 AM: fade PM gap (50% allocation)")
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


def premarket_plan_loop(today: str) -> PreMarketBias:
    """Re-scan and publish a fresh SPY 0DTE plan every five minutes until 9:30 ET."""
    global _last_report_t
    latest: PreMarketBias | None = None
    while True:
        if _poll_control_commands() == "stop":
            return PreMarketBias(
                direction="skip", fade_with="skip", pm_pct=0.0, vix=0.0,
                spy_prev_close=0.0, spy_pm_price=0.0, es_pct=0.0,
                skip_reason="Telegram emergency stop is active",
            )
        n = now_et()
        if n.hour > 9 or (n.hour == 9 and n.minute >= 30):
            break

        latest = get_premarket_bias()
        for reason in latest.reasons:
            log(f"  {reason}")
        mins_to_open = max(int((n.replace(hour=9, minute=30, second=0, microsecond=0) - n).total_seconds() / 60), 0)
        if latest.fade_with == "skip":
            log(f"PREMARKET no-trade plan: {latest.skip_reason} | {mins_to_open} min to open")
        else:
            log(_scored_plan_report(latest, today, mins_to_open))
        _last_report_t = time.time()

        # Align scans to the next five-minute wall-clock boundary.
        wait = PREMARKET_REPORT_SECS - (n.minute % 5) * 60 - n.second
        time.sleep(max(1, min(wait, 300)))

    # Always recalculate at/after the opening bell so the entry uses fresh data.
    latest = get_premarket_bias()
    return latest


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

        emergency = _poll_control_commands(pos)

        if not pos.reconciled:
            pos = _reconcile_position(pos, attempts=1)

        current_mid = _broker_option_quote(pos.contract)
        price_source = "Wealthsimple bid"
        if current_mid <= 0:
            current_mid = get_option_mid(pos.contract)
            price_source = "Yahoo fallback"
        if current_mid <= 0:
            log("Could not refresh option mid — retrying next cycle")
            continue

        action, reason = check_exit(pos, current_mid)
        if emergency == "stop":
            action, reason = "close_all", "TELEGRAM EMERGENCY STOP"
        pnl_pct = (current_mid - pos.entry_premium) / pos.entry_premium * 100
        pnl_usd = (current_mid - pos.entry_premium) * pos.contracts * 100

        if action == "hold":
            log(reason)
            continue

        if action == "close_half":
            if pos.contracts < 2:
                action = "close_all"
                reason = f"PROFIT EXIT (single contract) — {reason}"
            else:
                half = pos.contracts // 2
                sell_state = execute_sell_option(pos.contract, half)
                if sell_state == "review":
                    notify(f"👀 PARTIAL EXIT READY FOR MANUAL REVIEW | {half} contract(s) | {reason}")
                    return
                if sell_state == "dry":
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
            sell_state = execute_sell_option(pos.contract, pos.contracts)
            if sell_state == "review":
                notify(f"👀 FULL EXIT READY FOR MANUAL REVIEW | {pos.contracts} contract(s) | {reason}")
                return
            if sell_state == "dry":
                result_emoji = "🚀" if pnl_pct > 100 else "✅" if pnl_pct > 0 else "🔻"
                notify(
                    f"{result_emoji} <b>CLOSED</b> | SPY ${int(pos.contract.strike)} "
                    f"{pos.contract.option_type.upper()} | "
                    f"{pos.contracts} contract(s) @ ${current_mid:.2f} | "
                    f"<b>P&L: {pnl_pct:+.0f}% / ${pnl_usd:+.0f}</b> | {reason}"
                )
                clear_position()
                _mark_realized_pnl(pnl_usd)
                audit("position_closed", strike=pos.contract.strike, option_type=pos.contract.option_type,
                      contracts=pos.contracts, exit_mid=current_mid, pnl_usd=pnl_usd, reason=reason)
                return
            else:
                log(f"[WARN] Full sell failed for: {reason} — retrying next cycle")


# ── Main run ──────────────────────────────────────────────────────────────────

def run_today(now_flag: bool = False, balance_override: float | None = None) -> None:
    today = now_et().date().isoformat()
    notify(f"=== 0DTE SPY OPTIONS BOT | {today} | {'DRY RUN' if _DRY_RUN else 'ORDER REVIEW'} ===")
    if STOP_FILE.exists():
        notify("NO TRADE: emergency stop is active. Send /resume to clear it.")
        return

    # ── Resume open position from today ──────────────────────────────────────
    existing = load_position()
    if existing and existing.contract.expiry == today:
        notify(
            f"Resuming open position: SPY ${existing.contract.strike:.0f} "
            f"{existing.contract.option_type.upper()} | entry ${existing.entry_premium:.2f} | "
            f"{existing.contracts} contract(s)"
        )
        if not existing.reconciled:
            existing = _reconcile_position(existing)
            if not existing.reconciled:
                cancelled = _cancel_pending_option(existing)
                notify(f"Unconfirmed pending order {'cancelled' if cancelled else 'requires manual cancellation'}")
                if cancelled:
                    clear_position()
                return
        hold_loop(existing)
        return
    elif existing:
        log(f"Stale position file from {existing.contract.expiry} — clearing")
        clear_position()

    # ── Five-minute pre-market planning ──────────────────────────────────────
    log("Detecting pre-market bias...")
    bias = get_premarket_bias() if now_flag or now_et().hour >= 9 and now_et().minute >= 30 else premarket_plan_loop(today)
    for r in bias.reasons:
        log(f"  {r}")

    if bias.fade_with == "skip":
        notify(f"SKIP TODAY: {bias.skip_reason}")
        return

    # Send initial game plan to Telegram
    notify(_scored_plan_report(bias, today, 0))

    # ── Wait for 9:45 AM entry window; reporter publishes at :00/:30 ────────
    if not now_flag:
        _sleep_until(
            ENTRY_HOUR, ENTRY_MINUTE_START,
            "entry window 9:45 AM",
        )

    if not now_flag and _past_cutoff():
        notify(f"MISSED ENTRY WINDOW ({now_et().strftime('%H:%M')} ET) — skip today")
        return

    # ── Reversal confirmation ─────────────────────────────────────────────────
    confirmed = False
    rev_msg = ""
    while not confirmed and not _past_cutoff():
        confirmed, rev_msg = check_reversal_starting(bias)
        log(f"Reversal check: {rev_msg}")
        if not confirmed:
            time.sleep(60)
    if not confirmed:
        notify(f"NO TRADE: reversal was not confirmed by 10:00 ET ({rev_msg})")
        return

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

    # ── Deterministic quant score picks the best bounded contract ─────────────
    log("Scoring eligible contracts...")
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
            log("[WARN] Balance fetch failed — retrying once...")
            time.sleep(15)
            balance = get_available_balance()
        if balance <= 0:
            notify("ERROR: Could not fetch balance from Wealthsimple — aborting. Use --balance X to override.")
            return

    allowed, lock_reason = _entry_allowed(balance)
    if not allowed:
        notify(f"NO TRADE: {lock_reason}")
        return

    n_contracts = calc_max_contracts(entry_ask, balance)
    if n_contracts == 0:
        notify(
            f"NO TRADE: SPY contract costs ${entry_ask * 100:.2f}, exceeding "
            f"available USD cash ${balance:.2f}."
        )
        return
    cost_total  = entry_ask * n_contracts * 100
    deploy_pct = cost_total / balance if balance > 0 else 0.0
    if deploy_pct < MIN_DEPLOY_PCT:
        notify(
            f"NO TRADE: max affordable whole-contract sizing would deploy only "
            f"{deploy_pct:.0%} of ${balance:.2f}; minimum is {MIN_DEPLOY_PCT:.0%}."
        )
        return

    notify(
        f"CONTRACT SELECTED: SPY ${int(contract.strike)} {contract.option_type.upper()} 0DTE | "
        f"ask=${entry_ask:.2f} | IV={contract.iv:.0%} | vol={contract.volume:,} | "
        f"balance=${balance:.0f} → {n_contracts} contract(s) @ ${cost_total:.0f} total"
    )

    # ── Place buy order ───────────────────────────────────────────────────────
    quality = validate_quote(
        bid=contract.bid, ask=contract.ask, volume=contract.volume,
        open_interest=contract.open_interest,
    )
    if not quality.valid:
        notify("NO TRADE: contract failed execution-quality gates — " + "; ".join(quality.reasons))
        audit("contract_quality_rejected", strike=contract.strike, reasons=quality.reasons)
        return

    model_score = _contract_quant_score(contract, spy_price)[0]
    ShadowLedger(SHADOW_FILE).append(ShadowTrade(
        timestamp=now_et().isoformat(),
        decision_id=decision_id(now_et(), contract.option_type, contract.strike),
        expiry=contract.expiry, option_type=contract.option_type, strike=contract.strike,
        contracts=n_contracts, entry_bid=contract.bid, entry_ask=contract.ask,
        assumed_entry=contract.ask, model_score=model_score,
        live_mode="dry" if _DRY_RUN else "review",
    ))
    audit("shadow_entry", strike=contract.strike, contracts=n_contracts, score=model_score)

    order_state = execute_buy_option(contract, n_contracts, max_debit=balance)
    if order_state == "failed":
        notify("BUY FAILED — check data/options.log and data/screen_*.png for details")
        return
    if order_state == "review":
        _save_shadow_position(OptionsPosition(
            contract=contract, contracts=n_contracts, entry_premium=entry_ask,
            entry_time=now_et(), entry_spy_price=spy_price, cost_basis=cost_total,
            reconciled=True,
        ), model_score)
        notify(
            f"👀 <b>ORDER READY FOR MANUAL REVIEW</b> | SPY ${contract.strike:.0f} "
            f"{contract.option_type.upper()} 0DTE | {n_contracts} contract(s). "
            "Verify the live debit and confirm in Wealthsimple if you choose to proceed."
        )
        audit("order_review_ready", strike=contract.strike, contracts=n_contracts)
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
    _mark_entry(balance)
    audit("position_recorded", strike=contract.strike, option_type=contract.option_type,
          contracts=n_contracts, entry_premium=entry_ask, cost_basis=cost_total)

    # ── Hold loop until exit condition ────────────────────────────────────────
    pos = _reconcile_position(pos)
    if not pos.reconciled:
        cancelled = _cancel_pending_option(pos)
        notify(f"Unconfirmed order {'cancelled automatically' if cancelled else 'must be checked manually in Activity'}")
        if cancelled:
            clear_position()
        return
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

    # The normal runner is a persistent service: half-hour reports continue on
    # nights, weekends, and holidays while trading remains NYSE-session-only.
    reporter = threading.Thread(target=_half_hour_reporter_loop, name="spy-quant-reporter", daemon=True)
    reporter.start()
    _start_keepalive()
    try:
        if args.now:
            run_today(now_flag=True, balance_override=args.balance)
            return

        completed_date: date | None = None
        while True:
            n = now_et()
            if not is_trading_day(n.date()):
                if completed_date != n.date():
                    log("NYSE is closed today — reports continue; no orders")
                    completed_date = n.date()
                _poll_control_commands()
                time.sleep(60)
                continue

            if completed_date == n.date():
                _poll_control_commands()
                time.sleep(60)
                continue

            if is_early_close(n.date()):
                log("NYSE early-close session detected — mandatory exit moves to 12:45 ET")
            _sleep_until(PREMARKET_SCAN_HOUR, PREMARKET_SCAN_MINUTE, "SPY pre-market scan start")
            run_today(now_flag=False, balance_override=args.balance)
            completed_date = n.date()
    finally:
        _reporter_stop.set()
        reporter.join(timeout=2)
        _stop_keepalive()


if __name__ == "__main__":
    main()
