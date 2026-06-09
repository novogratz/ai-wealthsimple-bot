"""
0DTE SPY Options — Contrarian Gap-Fade Strategy

Logic: SPY gaps up pre-market → buy OTM puts at 9:45 AM (fade the green)
       SPY gaps down pre-market → buy OTM calls at 9:45 AM (fade the red)

Big money sells into the retail FOMO open; bots and algos fade the gap.
OTM options are cheap — small account, large leverage, defined risk.

Exit rules (in priority order):
  1. +100% on premium → close all (doubled the money)
  2. Partial at +50% → sell half, let rest run to +150%
  3. -50% on premium → stop loss
  4. 12:00 PM hard close (theta crushes OTM after noon)
  5. 3:45 PM nuclear close (0DTE expiry protection)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf

TZ = ZoneInfo("America/Toronto")


def now_et() -> datetime:
    return datetime.now(tz=TZ)


# ── Strategy parameters ──────────────────────────────────────────────────────
TARGET_PREMIUM_MIN   = 0.10   # minimum ask price
TARGET_PREMIUM_MAX   = 0.60   # maximum ask price — AI picks best in this range
TARGET_PREMIUM_MID   = 0.35   # fallback target when no contract is exactly in range
MIN_OTM_STRIKES      = 3      # minimum strikes from ATM before starting the search
MAX_OTM_STRIKES      = 8      # maximum strikes from ATM — never go further OTM than this
MIN_PM_PCT           = 0.15   # minimum pre-market move to trade (skip flat days)
PROFIT_TARGET_PCT    = 500.0  # +500% → close all (6x bagger — 0DTE can do 1000%+)
PARTIAL_CLOSE_PCT    = 200.0  # +200% → sell half, let remaining half run free
PARTIAL_TARGET_PCT   = 500.0  # second target for remaining half
# NO stop loss — 0DTE deep OTM options can go -80% before reversing violently.
# Time-based exits (noon + 3:45 PM) are the only hard protection.
NOON_CLOSE_HOUR      = 12     # hard close at noon (theta kills OTM after this)
NOON_CLOSE_MINUTE    = 0
HARD_CLOSE_HOUR      = 15     # nuclear close 3:45 PM
HARD_CLOSE_MINUTE    = 45
ENTRY_HOUR           = 9      # entry window: 9:45–10:00 AM ET
ENTRY_MINUTE_START   = 45
ENTRY_MINUTE_END     = 60     # if no entry by 10:00 → skip today
MAX_VIX              = 40.0   # skip if market is panic-mode (VIX > 40)
REVERSAL_CONFIRM_PCT = 0.05   # SPY must be pulling back this much from the open high/low to confirm fade

# Regime bias: negative = lean bearish (prefer puts), positive = lean bullish (prefer calls).
# Applied as an additive score offset. On flat/ambiguous gap days this is the deciding factor.
# Magnitude guide: ±10 is a gentle tilt; ±20 overrides all but the largest gap signals.
# Current: -12 → bearish regime (market overextended to the upside).
REGIME_BIAS          = -12


@dataclass
class PreMarketBias:
    direction: str       # "green", "red", or "flat" (flat = no trade)
    fade_with: str       # "put" (fade green), "call" (fade red), or "skip"
    pm_pct: float        # SPY % change from yesterday close to pre-market
    vix: float
    spy_prev_close: float
    spy_pm_price: float
    es_pct: float        # ES futures 1h trend
    reasons: list[str]   = field(default_factory=list)
    skip_reason: str     = ""


@dataclass
class OptionContract:
    expiry: str          # "2026-06-09"
    strike: float        # e.g. 542.0
    option_type: str     # "call" or "put"
    last_price: float
    bid: float
    ask: float
    mid: float           # (bid + ask) / 2 — used as entry price reference
    iv: float
    volume: int
    open_interest: int


@dataclass
class OptionsPosition:
    contract: OptionContract
    contracts: int        # number of contracts
    entry_premium: float  # mid-price at entry (per share, so x100 per contract)
    entry_time: datetime
    entry_spy_price: float
    partial_closed: bool = False   # True once 50% partial has been taken
    cost_basis: float    = 0.0     # total dollars spent (entry_premium * contracts * 100)


# ── Pre-market direction ─────────────────────────────────────────────────────

def _get_spy_rsi_daily() -> float:
    """SPY 14-day RSI on daily closes. Returns 50 on failure (neutral)."""
    try:
        hist = yf.Ticker("SPY").history(period="30d", interval="1d")
        if len(hist) < 15:
            return 50.0
        closes = hist["Close"].values.astype(float)
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-14:]]
        losses = [max(-d, 0) for d in deltas[-14:]]
        avg_g  = sum(gains) / 14
        avg_l  = sum(losses) / 14
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1 + rs))
    except Exception:
        return 50.0


def _get_spy_week_return(spy_prev_close: float) -> float:
    """SPY 5-trading-day return (% from 5 sessions ago to yesterday close)."""
    try:
        hist = yf.Ticker("SPY").history(period="10d", interval="1d")
        if len(hist) < 6:
            return 0.0
        close_5d_ago = float(hist["Close"].iloc[-6])
        if close_5d_ago <= 0:
            return 0.0
        # Use spy_prev_close as yesterday close; fallback to last bar
        yesterday = spy_prev_close if spy_prev_close > 0 else float(hist["Close"].iloc[-1])
        return (yesterday - close_5d_ago) / close_5d_ago * 100
    except Exception:
        return 0.0


def get_premarket_bias() -> PreMarketBias:
    """
    Multi-factor scoring to decide direction (puts vs calls) for today's 0DTE trade.

    Score components (positive = lean calls/bullish, negative = lean puts/bearish):
      1. Gap fade   — green gap → negative (fade up = puts); red gap → positive (fade down = calls)
      2. RSI daily  — overbought (>65) → more negative; oversold (<35) → more positive
      3. Weekly ext — SPY up big 5 days → more negative; down big → more positive
      4. ES futures — fade the 1h ES trend too
      5. VIX level  — high VIX → lean puts (fear = puts outperform)
      6. REGIME_BIAS — user-set constant (currently bearish = -12)

    Positive total → buy CALLS  |  Negative total → buy PUTS
    On flat gaps (< MIN_PM_PCT), regime + technicals decide.
    """
    reasons: list[str] = []
    score          = 0.0
    spy_pm_pct     = 0.0
    es_pct         = 0.0
    vix            = 16.0
    spy_prev_close = 0.0
    spy_pm_price   = 0.0

    # ── SPY pre-market vs yesterday close ────────────────────────────────────
    try:
        hist = yf.Ticker("SPY").history(period="3d", interval="5m", prepost=True)
        if not hist.empty:
            rh = hist[hist.index.map(
                lambda x: 9 <= x.hour < 16 if hasattr(x, "hour") else False
            )]
            if len(rh) >= 1:
                spy_prev_close = float(rh["Close"].iloc[-1])

            pm = hist[hist.index.map(
                lambda x: (x.hour < 9 or (x.hour == 9 and x.minute < 30))
                if hasattr(x, "hour") else False
            )]
            if not pm.empty and spy_prev_close > 0:
                spy_pm_price = float(pm["Close"].iloc[-1])
                spy_pm_pct   = (spy_pm_price - spy_prev_close) / spy_prev_close * 100
                reasons.append(f"SPY pre-market: {spy_pm_pct:+.2f}%  (${spy_prev_close:.2f} -> ${spy_pm_price:.2f})")
    except Exception as e:
        reasons.append(f"SPY PM data error: {e}")

    # ── ES futures 1-hour trend ───────────────────────────────────────────────
    try:
        es_hist = yf.Ticker("ES=F").history(period="2d", interval="5m", prepost=True)
        if len(es_hist) >= 12:
            es_now  = float(es_hist["Close"].iloc[-1])
            es_1h   = float(es_hist["Close"].iloc[-12])
            es_pct  = (es_now - es_1h) / es_1h * 100
            reasons.append(f"ES futures 1h: {es_pct:+.2f}%")
    except Exception as e:
        reasons.append(f"ES data error: {e}")

    # ── VIX ───────────────────────────────────────────────────────────────────
    try:
        vix_hist = yf.Ticker("^VIX").history(period="2d", interval="5m", prepost=True)
        if not vix_hist.empty:
            vix = float(vix_hist["Close"].iloc[-1])
            reasons.append(f"VIX: {vix:.1f}")
    except Exception as e:
        reasons.append(f"VIX error: {e}")

    # ── Hard gates ────────────────────────────────────────────────────────────
    if vix > MAX_VIX:
        return PreMarketBias(
            direction="skip", fade_with="skip",
            pm_pct=spy_pm_pct, vix=vix,
            spy_prev_close=spy_prev_close, spy_pm_price=spy_pm_price,
            es_pct=es_pct, reasons=reasons,
            skip_reason=f"VIX {vix:.1f} > {MAX_VIX} — market in panic, skip today",
        )

    # ── Scoring: 1. Gap fade (primary signal, max ~±30 pts) ──────────────────
    # Green gap → market likely to pull back → puts → negative contribution
    gap_pts = -spy_pm_pct * 25
    score  += gap_pts
    if abs(spy_pm_pct) >= 0.05:
        reasons.append(f"  Gap fade: {gap_pts:+.1f} pts (PM {spy_pm_pct:+.2f}%)")

    # ── Scoring: 2. RSI daily (max ±20 pts) ──────────────────────────────────
    rsi = _get_spy_rsi_daily()
    if rsi > 70:
        rsi_pts = -20
    elif rsi > 65:
        rsi_pts = -12
    elif rsi > 60:
        rsi_pts = -6
    elif rsi < 30:
        rsi_pts = +20
    elif rsi < 35:
        rsi_pts = +12
    elif rsi < 40:
        rsi_pts = +6
    else:
        rsi_pts = 0
    score += rsi_pts
    reasons.append(f"  RSI(14): {rsi:.0f} -> {rsi_pts:+.0f} pts")

    # ── Scoring: 3. Weekly extension (max ±15 pts) ───────────────────────────
    week_ret = _get_spy_week_return(spy_prev_close)
    if week_ret > 4.0:
        wk_pts = -15
    elif week_ret > 2.5:
        wk_pts = -10
    elif week_ret > 1.5:
        wk_pts = -5
    elif week_ret < -4.0:
        wk_pts = +15
    elif week_ret < -2.5:
        wk_pts = +10
    elif week_ret < -1.5:
        wk_pts = +5
    else:
        wk_pts = 0
    score += wk_pts
    reasons.append(f"  Week return: {week_ret:+.1f}% -> {wk_pts:+.0f} pts")

    # ── Scoring: 4. ES futures fade (max ±10 pts) ────────────────────────────
    es_pts = -es_pct * 5
    score += es_pts
    if abs(es_pts) >= 1.0:
        reasons.append(f"  ES fade: {es_pts:+.1f} pts")

    # ── Scoring: 5. VIX level (max ±10 pts) ──────────────────────────────────
    if vix > 25:
        vix_pts = -10
    elif vix > 20:
        vix_pts = -5
    elif vix < 12:
        vix_pts = +5
    else:
        vix_pts = 0
    score += vix_pts
    if vix_pts != 0:
        reasons.append(f"  VIX {vix:.1f}: {vix_pts:+.0f} pts")

    # ── Scoring: 6. Regime bias constant ─────────────────────────────────────
    score += REGIME_BIAS
    regime_label = "bearish" if REGIME_BIAS < 0 else "bullish" if REGIME_BIAS > 0 else "neutral"
    reasons.append(f"  Regime bias: {REGIME_BIAS:+.0f} pts ({regime_label})")

    reasons.append(f"  TOTAL SCORE: {score:+.1f}  ({'PUTS' if score < 0 else 'CALLS'})")

    # ── Skip flat days (score near zero AND tiny gap AND no strong regime bias) ─
    abs_pm = abs(spy_pm_pct)
    if abs_pm < MIN_PM_PCT and abs(score) < 5 and abs(REGIME_BIAS) < 8:
        return PreMarketBias(
            direction="flat", fade_with="skip",
            pm_pct=spy_pm_pct, vix=vix,
            spy_prev_close=spy_prev_close, spy_pm_price=spy_pm_price,
            es_pct=es_pct, reasons=reasons,
            skip_reason=f"Score {score:+.1f} + PM {abs_pm:.2f}% — too ambiguous, skip today",
        )

    # ── Direction decision ────────────────────────────────────────────────────
    fade_with = "put" if score < 0 else "call"
    direction = "green" if spy_pm_pct > 0 else "red" if spy_pm_pct < 0 else "flat"
    reasons.append(
        f"-> {'GREEN' if spy_pm_pct >= 0 else 'RED'} PM + score {score:+.1f} "
        f"-> {'OTM PUTS' if fade_with == 'put' else 'OTM CALLS'}"
    )

    return PreMarketBias(
        direction=direction, fade_with=fade_with,
        pm_pct=spy_pm_pct, vix=vix,
        spy_prev_close=spy_prev_close, spy_pm_price=spy_pm_price,
        es_pct=es_pct, reasons=reasons,
    )


# ── Live price + reversal check ──────────────────────────────────────────────

def get_spy_price() -> float:
    """Current SPY price from 1-min data (no pre-market)."""
    try:
        hist = yf.Ticker("SPY").history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 0.0


def check_reversal_starting(bias: PreMarketBias) -> tuple[bool, str]:
    """
    At 9:45 AM, confirm the fade direction is actually starting.
    Checks that SPY is already pulling back from the open high (green day)
    or bouncing off the open low (red day).

    Returns (confirmed, message).
    """
    try:
        hist = yf.Ticker("SPY").history(period="1d", interval="1m", prepost=False)
        if len(hist) < 5:
            return True, "Not enough bars — entering on bias alone"

        open_price   = float(hist["Open"].iloc[0])
        current      = float(hist["Close"].iloc[-1])
        session_high = float(hist["High"].max())
        session_low  = float(hist["Low"].min())

        if bias.fade_with == "put":
            # Fading green: SPY should be pulling back from session high
            pullback = (session_high - current) / session_high * 100
            if pullback >= REVERSAL_CONFIRM_PCT:
                return True, f"Pullback confirmed: SPY {pullback:.2f}% off session high ${session_high:.2f}"
            else:
                return True, f"SPY still near high (${current:.2f} vs high ${session_high:.2f}) — entering anyway"
        else:
            # Fading red: SPY should be bouncing off session low
            bounce = (current - session_low) / session_low * 100
            if bounce >= REVERSAL_CONFIRM_PCT:
                return True, f"Bounce confirmed: SPY +{bounce:.2f}% off session low ${session_low:.2f}"
            else:
                return True, f"SPY near session low (${current:.2f} vs low ${session_low:.2f}) — entering anyway"

    except Exception as e:
        return True, f"Reversal check failed ({e}) — entering on bias"


# ── Options chain ─────────────────────────────────────────────────────────────

def get_otm_contract(
    option_type: str,
    spy_price: float,
    expiry: Optional[str] = None,
) -> Optional[OptionContract]:
    """
    Pull today's SPY 0DTE chain from yfinance and find the contract whose ask
    price falls in [TARGET_PREMIUM_MIN, TARGET_PREMIUM_MAX] ($0.20–$0.40).

    We start at least MIN_OTM_STRIKES away from ATM and scan further OTM until
    we find a contract in the target range. If none found, we return the contract
    closest to TARGET_PREMIUM_MID ($0.30) as a best-effort pick.

    option_type: "call" or "put"
    """
    if expiry is None:
        expiry = date.today().strftime("%Y-%m-%d")

    try:
        spy  = yf.Ticker("SPY")
        exps = spy.options
        if expiry not in exps:
            from datetime import datetime as dt
            target = dt.strptime(expiry, "%Y-%m-%d")
            expiry = min(exps, key=lambda d: abs((dt.strptime(d, "%Y-%m-%d") - target).days))

        chain = spy.option_chain(expiry)
        df    = (chain.calls if option_type == "call" else chain.puts).copy()

        # ATM strike = nearest strike to current SPY price
        atm_idx = (df["strike"] - spy_price).abs().argsort().iloc[0]
        atm     = float(df.iloc[atm_idx]["strike"])

        # Filter to OTM only (put = below ATM, call = above ATM), skip ATM and close-to-money
        if option_type == "put":
            df = df[(df["strike"] <= atm - MIN_OTM_STRIKES) & (df["strike"] >= atm - MAX_OTM_STRIKES)].sort_values("strike", ascending=False)
        else:
            df = df[(df["strike"] >= atm + MIN_OTM_STRIKES) & (df["strike"] <= atm + MAX_OTM_STRIKES)].sort_values("strike", ascending=True)

        if df.empty:
            return None

        # Compute ask price for each row (use last if bid/ask spread is 0)
        def _ask(row) -> float:
            a = float(row.get("ask", 0) or 0)
            b = float(row.get("bid", 0) or 0)
            l = float(row.get("lastPrice", 0) or 0)
            return a if a > 0 else (l if l > 0 else b)

        df = df.copy()
        df["_ask"] = df.apply(_ask, axis=1)

        # Prefer contracts with ask in the $0.20–$0.40 target window
        in_range = df[(df["_ask"] >= TARGET_PREMIUM_MIN) & (df["_ask"] <= TARGET_PREMIUM_MAX)]
        if not in_range.empty:
            # Among in-range contracts, pick the one closest to TARGET_PREMIUM_MID ($0.30)
            row = in_range.iloc[(in_range["_ask"] - TARGET_PREMIUM_MID).abs().argsort().iloc[0]]
        else:
            # Best-effort: pick the contract whose ask is closest to $0.30
            row = df.iloc[(df["_ask"] - TARGET_PREMIUM_MID).abs().argsort().iloc[0]]

        bid  = float(row.get("bid", 0) or 0)
        ask  = float(row.get("ask", 0) or 0)
        last = float(row.get("lastPrice", 0) or 0)
        mid  = (bid + ask) / 2 if (bid > 0 and ask > 0) else last

        return OptionContract(
            expiry=expiry,
            strike=float(row["strike"]),
            option_type=option_type,
            last_price=last,
            bid=bid,
            ask=ask,
            mid=mid,
            iv=float(row.get("impliedVolatility", 0) or 0),
            volume=int(row.get("volume", 0) or 0),
            open_interest=int(row.get("openInterest", 0) or 0),
        )
    except Exception:
        return None


def get_otm_contracts_in_range(
    option_type: str,
    spy_price: float,
    expiry: Optional[str] = None,
    n: int = 6,
) -> list[OptionContract]:
    """
    Return up to `n` OTM contracts in the $TARGET_PREMIUM_MIN–$TARGET_PREMIUM_MAX range,
    sorted by ask price (closest to TARGET_PREMIUM_MID first). Used by the LLM picker.
    Falls back to closest-to-mid contracts if fewer than n are in range.
    """
    if expiry is None:
        expiry = date.today().strftime("%Y-%m-%d")
    try:
        spy  = yf.Ticker("SPY")
        exps = spy.options
        if expiry not in exps:
            from datetime import datetime as dt
            target = dt.strptime(expiry, "%Y-%m-%d")
            expiry = min(exps, key=lambda d: abs((dt.strptime(d, "%Y-%m-%d") - target).days))

        chain = spy.option_chain(expiry)
        df    = (chain.calls if option_type == "call" else chain.puts).copy()

        atm_idx = (df["strike"] - spy_price).abs().argsort().iloc[0]
        atm     = float(df.iloc[atm_idx]["strike"])

        if option_type == "put":
            df = df[(df["strike"] <= atm - MIN_OTM_STRIKES) & (df["strike"] >= atm - MAX_OTM_STRIKES)].sort_values("strike", ascending=False)
        else:
            df = df[(df["strike"] >= atm + MIN_OTM_STRIKES) & (df["strike"] <= atm + MAX_OTM_STRIKES)].sort_values("strike", ascending=True)

        def _ask(row) -> float:
            a = float(row.get("ask", 0) or 0)
            b = float(row.get("bid", 0) or 0)
            l = float(row.get("lastPrice", 0) or 0)
            return a if a > 0 else (l if l > 0 else b)

        df = df.copy()
        df["_ask"] = df.apply(_ask, axis=1)
        df         = df[df["_ask"] > 0]

        # Candidates in range, sorted by closeness to mid
        in_range = df[(df["_ask"] >= TARGET_PREMIUM_MIN) & (df["_ask"] <= TARGET_PREMIUM_MAX)]
        if in_range.empty:
            in_range = df

        in_range = in_range.copy()
        in_range["_dist"] = (in_range["_ask"] - TARGET_PREMIUM_MID).abs()
        top = in_range.nsmallest(n, "_dist")

        contracts = []
        for _, row in top.iterrows():
            bid  = float(row.get("bid", 0) or 0)
            ask  = float(row.get("ask", 0) or 0)
            last = float(row.get("lastPrice", 0) or 0)
            mid  = (bid + ask) / 2 if (bid > 0 and ask > 0) else last
            contracts.append(OptionContract(
                expiry=expiry,
                strike=float(row["strike"]),
                option_type=option_type,
                last_price=last,
                bid=bid,
                ask=ask,
                mid=mid,
                iv=float(row.get("impliedVolatility", 0) or 0),
                volume=int(row.get("volume", 0) or 0),
                open_interest=int(row.get("openInterest", 0) or 0),
            ))
        return contracts
    except Exception:
        return []


def get_option_mid(contract: OptionContract) -> float:
    """Refresh the mid-price of an open contract. Used in the hold loop."""
    try:
        chain = yf.Ticker("SPY").option_chain(contract.expiry)
        df    = chain.calls if contract.option_type == "call" else chain.puts
        row   = df[abs(df["strike"] - contract.strike) < 0.01]
        if row.empty:
            return 0.0
        r   = row.iloc[0]
        bid = float(r.get("bid", 0) or 0)
        ask = float(r.get("ask", 0) or 0)
        last = float(r.get("lastPrice", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return last
    except Exception:
        return 0.0


# ── Exit logic ────────────────────────────────────────────────────────────────

def check_exit(
    position: OptionsPosition,
    current_premium: float,
) -> tuple[str, str]:
    """
    Returns (action, reason):
      "hold"       — keep riding
      "close_all"  — sell everything
      "close_half" — sell half (lock partial profit, let rest run)

    No stop loss — 0DTE deep OTM can drop 80% before ripping 1000%+.
    Time exits (noon, 3:45 PM) are the only hard protection.
    """
    if position.entry_premium <= 0 or current_premium <= 0:
        return "hold", ""

    pnl_pct = (current_premium - position.entry_premium) / position.entry_premium * 100
    now     = now_et()

    # 1. Full profit target (+500% — close all, 6x bagger)
    if pnl_pct >= PROFIT_TARGET_PCT:
        return "close_all", f"PROFIT TARGET +{pnl_pct:.0f}% — massive winner, closing all"

    # 2. Partial at +200% (doubled twice) — sell half, let remaining ride to +500%
    if not position.partial_closed and pnl_pct >= PARTIAL_CLOSE_PCT:
        return "close_half", f"PARTIAL CLOSE +{pnl_pct:.0f}% — locking half, rest runs free"

    # 3. After partial, close remaining at +500%
    if position.partial_closed and pnl_pct >= PARTIAL_TARGET_PCT:
        return "close_all", f"SECOND TARGET +{pnl_pct:.0f}% — closing remaining half"

    # 4. Noon hard close — theta destroys deep OTM after 12 PM, no point holding
    noon = now.replace(hour=NOON_CLOSE_HOUR, minute=NOON_CLOSE_MINUTE, second=0, microsecond=0)
    if now >= noon:
        return "close_all", f"NOON CLOSE ({pnl_pct:+.0f}%) — theta kills OTM after 12 PM"

    # 5. Nuclear close 3:45 PM — never hold 0DTE into expiry
    hard = now.replace(hour=HARD_CLOSE_HOUR, minute=HARD_CLOSE_MINUTE, second=0, microsecond=0)
    if now >= hard:
        return "close_all", "HARD CLOSE 3:45 PM — 0DTE expiry protection"

    return "hold", f"Holding  |  P&L: {pnl_pct:+.1f}%  |  premium ${current_premium:.2f}"
