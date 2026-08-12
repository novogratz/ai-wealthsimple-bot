"""SPY 0DTE research signals and exact-expiry contract selection.

The report model uses observable opening-window price/volume confirmation. It does not
assume that a green open must reverse or that a red open must bounce.

Exit rules (in priority order):
  1. +500% on premium → close all
  2. 3:25 PM time close
  3. 3:45 PM nuclear close (0DTE expiry protection)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import yfinance as yf
from .market_calendar import market_close_time
from .strategy_config import load_strategy_config

TZ = ZoneInfo("America/Toronto")
CONFIG = load_strategy_config()


def now_et() -> datetime:
    return datetime.now(tz=TZ)


# ── Strategy parameters ──────────────────────────────────────────────────────
TARGET_PREMIUM_MIN   = float(CONFIG.get("contract", "premium_min"))
TARGET_PREMIUM_MAX   = float(CONFIG.get("contract", "premium_max"))
TARGET_PREMIUM_MID   = float(CONFIG.get("contract", "premium_mid"))
MIN_PM_PCT           = float(CONFIG.get("signal", "minimum_spy_move_pct"))
PROFIT_TARGET_PCT    = float(CONFIG.get("exit", "profit_target_pct"))
PARTIAL_CLOSE_PCT    = PROFIT_TARGET_PCT
PARTIAL_TARGET_PCT   = PROFIT_TARGET_PCT
# NO stop loss — 0DTE deep OTM options can go -80% before reversing violently.
# Time-based exits (noon + 3:45 PM) are the only hard protection.
NOON_CLOSE_HOUR      = int(CONFIG.get("schedule", "time_close_hour"))
NOON_CLOSE_MINUTE    = int(CONFIG.get("schedule", "time_close_minute"))
HARD_CLOSE_HOUR      = int(CONFIG.get("schedule", "hard_close_hour"))
HARD_CLOSE_MINUTE    = int(CONFIG.get("schedule", "hard_close_minute"))
ENTRY_HOUR           = int(CONFIG.get("schedule", "entry_hour"))
EARLY_PUT_ENTRY_MINUTE = int(CONFIG.get("schedule", "early_put_entry_minute"))
ENTRY_MINUTE_START   = int(CONFIG.get("schedule", "entry_minute_start"))
ENTRY_MINUTE_END     = int(CONFIG.get("schedule", "entry_minute_end"))
MAX_VIX              = float(CONFIG.get("signal", "maximum_vix"))
REVERSAL_CONFIRM_PCT = float(CONFIG.get("signal", "reversal_confirmation_pct"))

# Regime bias: negative = lean bearish (prefer puts), positive = lean bullish (prefer calls).
# Applied as an additive score offset. On flat/ambiguous gap days this is the deciding factor.
# Magnitude guide: ±10 is a gentle tilt; ±20 overrides all but the largest gap signals.
# Current: 0 → neutral (direction driven entirely by intraday SPY move vs open).
REGIME_BIAS          = float(CONFIG.get("signal", "regime_bias"))
OPENING_RANGE_MINUTES = int(CONFIG.get("signal", "opening_range_minutes"))
MIN_INTRADAY_SCORE = float(CONFIG.get("signal", "minimum_intraday_score"))


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
    quote_time: datetime | None = None
    quote_source: str = "unknown"


@dataclass
class OptionsPosition:
    contract: OptionContract
    contracts: int        # number of contracts
    entry_premium: float  # mid-price at entry (per share, so x100 per contract)
    entry_time: datetime
    entry_spy_price: float
    partial_closed: bool = False   # True once 50% partial has been taken
    cost_basis: float    = 0.0     # total dollars spent (entry_premium * contracts * 100)
    reconciled: bool     = False   # True after broker confirms actual fill details


@dataclass(frozen=True)
class OpeningSignal:
    option_type: str
    score: float
    state: str
    reason: str
    spy_price: float = 0.0
    vwap: float = 0.0
    opening_high: float = 0.0
    opening_low: float = 0.0


def get_opening_signal() -> OpeningSignal:
    """Score opening-range, VWAP and short-horizon momentum; abstain on conflict."""
    n = now_et()
    if (n.hour, n.minute) < (9, 30) or (n.hour, n.minute) >= (16, 0):
        return OpeningSignal("skip", 0.0, "CLOSED", "signal window is 09:30–16:00 ET")
    try:
        bars = yf.Ticker("SPY").history(period="2d", interval="1m", prepost=False)
        today = bars[bars.index.map(lambda x: x.date() == n.date())]
        if len(today) <= OPENING_RANGE_MINUTES:
            return OpeningSignal("skip", 0.0, "WAIT", f"need {OPENING_RANGE_MINUTES} completed one-minute bars")
        opening = today.iloc[:OPENING_RANGE_MINUTES]
        current = float(today["Close"].iloc[-1])
        opening_high = float(opening["High"].max())
        opening_low = float(opening["Low"].min())
        opening_range = max(opening_high - opening_low, current * 0.0002)
        volume = today["Volume"].astype(float)
        typical = (today["High"] + today["Low"] + today["Close"]) / 3
        total_volume = float(volume.sum())
        if current <= 0 or total_volume <= 0:
            raise ValueError("invalid price or volume")
        vwap = float((typical * volume).sum() / total_volume)
        momentum = (current / float(today["Close"].iloc[-6]) - 1) * 100
        score = 0.0
        evidence: list[str] = []
        if current > opening_high:
            score += min(40.0, 25.0 + (current - opening_high) / opening_range * 15.0)
            evidence.append("above 5m range")
        elif current < opening_low:
            score -= min(40.0, 25.0 + (opening_low - current) / opening_range * 15.0)
            evidence.append("below 5m range")
        else:
            evidence.append("inside 5m range")
        vwap_distance = (current / vwap - 1) * 100
        score += max(-25.0, min(25.0, vwap_distance * 250.0))
        score += max(-25.0, min(25.0, momentum * 125.0))
        score = max(-100.0, min(100.0, score))
        evidence.extend([f"VWAP {vwap_distance:+.2f}%", f"5m momentum {momentum:+.2f}%"])
        if abs(score) < MIN_INTRADAY_SCORE:
            return OpeningSignal("skip", score, "NO TRADE", "; ".join(evidence), current, vwap, opening_high, opening_low)
        option_type = "call" if score > 0 else "put"
        return OpeningSignal(option_type, score, "CONFIRMED", "; ".join(evidence), current, vwap, opening_high, opening_low)
    except Exception as exc:
        return OpeningSignal("skip", 0.0, "NO DATA", f"opening signal unavailable: {exc}")


def is_strike_within_otm_bounds(option_type: str, strike: float, spy_price: float) -> bool:
    """Compatibility helper: require strictly OTM; premium now defines eligibility."""
    return strike < spy_price if option_type == "put" else strike > spy_price


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

    Execution direction is asymmetric: flatish/green opens select puts at 9:31;
    clearly red opens select calls after the standard reversal gate. The score
    remains an audited research signal and does not override this opening rule.
    """
    reasons: list[str] = []
    score          = 0.0
    spy_pm_pct     = 0.0
    es_pct         = 0.0
    vix            = 16.0
    spy_prev_close = 0.0
    spy_pm_price   = 0.0

    # ── SPY direction: open-gap (morning) or intraday (afternoon) ────────────
    # Morning  9:30–10:00 AM: compare today's open vs yesterday's close
    #   → market gapped UP   → puts  (fade the gap)
    #   → market gapped DOWN → calls (fade the gap)
    # Afternoon 10:00 AM +: compare current price vs today's open
    #   → market DOWN from open → calls (fade the drop)
    #   → market UP   from open → puts  (fade the rally)
    try:
        hist = yf.Ticker("SPY").history(period="3d", interval="1m", prepost=False)
        if not hist.empty:
            today_str = date.today().isoformat()
            today_bars = hist[hist.index.map(
                lambda x: x.date().isoformat() == today_str if hasattr(x, "date") else False
            )]
            prev_bars = hist[hist.index.map(
                lambda x: x.date().isoformat() < today_str if hasattr(x, "date") else False
            )]
            if len(today_bars) >= 2:
                spy_open     = float(today_bars["Open"].iloc[0])
                spy_current  = float(today_bars["Close"].iloc[-1])
                n            = now_et()
                morning_entry = n.hour < 10 or (n.hour == 9 and n.minute < 60)
                if morning_entry and len(prev_bars) >= 1:
                    # Morning: gap = how much did SPY open vs yesterday close
                    spy_prev_close = float(prev_bars["Close"].iloc[-1])
                    spy_pm_price   = spy_open
                    spy_pm_pct     = (spy_open - spy_prev_close) / spy_prev_close * 100
                    reasons.append(
                        f"SPY open gap: {spy_pm_pct:+.2f}%  "
                        f"(yesterday ${spy_prev_close:.2f} → open ${spy_open:.2f})"
                    )
                else:
                    # Afternoon: intraday = how much has SPY moved from today's open
                    spy_prev_close = spy_open
                    spy_pm_price   = spy_current
                    spy_pm_pct     = (spy_current - spy_open) / spy_open * 100
                    reasons.append(
                        f"SPY intraday: {spy_pm_pct:+.2f}%  "
                        f"(open ${spy_open:.2f} → now ${spy_current:.2f})"
                    )
    except Exception as e:
        reasons.append(f"SPY data error: {e}")

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

    if spy_prev_close <= 0 or spy_pm_price <= 0:
        return PreMarketBias(
            direction="skip", fade_with="skip",
            pm_pct=spy_pm_pct, vix=vix,
            spy_prev_close=spy_prev_close, spy_pm_price=spy_pm_price,
            es_pct=es_pct, reasons=reasons,
            skip_reason="Opening gap unavailable — refusing to infer a 9:31 direction",
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

    # ── Asymmetric opening rule ───────────────────────────────────────────────
    fade_with, entry_style = select_opening_play(spy_pm_pct)
    direction = "green" if spy_pm_pct > 0 else "red" if spy_pm_pct < 0 else "flat"
    reasons.append(
        f"-> gap {spy_pm_pct:+.2f}% -> {fade_with.upper()} via {entry_style} entry"
    )

    return PreMarketBias(
        direction=direction, fade_with=fade_with,
        pm_pct=spy_pm_pct, vix=vix,
        spy_prev_close=spy_prev_close, spy_pm_price=spy_pm_price,
        es_pct=es_pct, reasons=reasons,
    )


def select_opening_play(spy_open_gap_pct: float) -> tuple[str, str]:
    """Map the opening gap to the configured asymmetric entry path."""
    if spy_open_gap_pct >= -MIN_PM_PCT:
        return "put", "9:31"
    return "call", "9:45 reversal"


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
            return False, "Not enough one-minute bars to confirm reversal"

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
                return False, f"SPY still near high (${current:.2f} vs high ${session_high:.2f}) — waiting"
        else:
            # Fading red: SPY should be bouncing off session low
            bounce = (current - session_low) / session_low * 100
            if bounce >= REVERSAL_CONFIRM_PCT:
                return True, f"Bounce confirmed: SPY +{bounce:.2f}% off session low ${session_low:.2f}"
            else:
                return False, f"SPY near session low (${current:.2f} vs low ${session_low:.2f}) — waiting"

    except Exception as e:
        return False, f"Reversal check failed ({e}) — waiting"


# ── Options chain ─────────────────────────────────────────────────────────────

def get_otm_contract(
    option_type: str,
    spy_price: float,
    expiry: Optional[str] = None,
) -> Optional[OptionContract]:
    """
    Pull today's SPY 0DTE chain from yfinance and find the contract whose ask
    price falls strictly in [TARGET_PREMIUM_MIN, TARGET_PREMIUM_MAX].

    option_type: "call" or "put"
    """
    if expiry is None:
        expiry = date.today().strftime("%Y-%m-%d")

    try:
        spy  = yf.Ticker("SPY")
        exps = spy.options
        if expiry not in exps:
            return None

        chain = spy.option_chain(expiry)
        df    = (chain.calls if option_type == "call" else chain.puts).copy()

        if option_type == "put":
            df = df[df["strike"] < spy_price].sort_values("strike", ascending=False)
        else:
            df = df[df["strike"] > spy_price].sort_values("strike", ascending=True)

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

        in_range = df[(df["_ask"] >= TARGET_PREMIUM_MIN) & (df["_ask"] <= TARGET_PREMIUM_MAX)]
        if in_range.empty:
            return None
        row = in_range.iloc[(in_range["_ask"] - TARGET_PREMIUM_MID).abs().argsort().iloc[0]]

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
            quote_time=now_et(), quote_source="yfinance",
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
    sorted by ask price (closest to TARGET_PREMIUM_MID first). No out-of-band fallback.
    """
    if expiry is None:
        expiry = date.today().strftime("%Y-%m-%d")
    try:
        spy  = yf.Ticker("SPY")
        exps = spy.options
        if expiry not in exps:
            return []

        chain = spy.option_chain(expiry)
        df    = (chain.calls if option_type == "call" else chain.puts).copy()

        if option_type == "put":
            df = df[df["strike"] < spy_price].sort_values("strike", ascending=False)
        else:
            df = df[df["strike"] > spy_price].sort_values("strike", ascending=True)

        def _ask(row) -> float:
            a = float(row.get("ask", 0) or 0)
            b = float(row.get("bid", 0) or 0)
            l = float(row.get("lastPrice", 0) or 0)
            return a if a > 0 else (l if l > 0 else b)

        df = df.copy()
        df["_ask"] = df.apply(_ask, axis=1)
        df         = df[df["_ask"] > 0]

        in_range = df[(df["_ask"] >= TARGET_PREMIUM_MIN) & (df["_ask"] <= TARGET_PREMIUM_MAX)]
        if in_range.empty:
            return []

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
                quote_time=now_et(), quote_source="yfinance",
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

    # 2. Hard time close. No profit-taking occurs below +500%.
    close_hour, _ = market_close_time(now.date())
    time_exit_hour, time_exit_minute = (12, 45) if close_hour == 13 else (NOON_CLOSE_HOUR, NOON_CLOSE_MINUTE)
    noon = now.replace(hour=time_exit_hour, minute=time_exit_minute, second=0, microsecond=0)
    if now >= noon:
        return "close_all", f"TIME CLOSE {time_exit_hour:02d}:{time_exit_minute:02d} ({pnl_pct:+.0f}%) — expiry risk"

    # 3. Nuclear close 3:45 PM — never hold 0DTE into expiry
    hard = now.replace(hour=HARD_CLOSE_HOUR, minute=HARD_CLOSE_MINUTE, second=0, microsecond=0)
    if now >= hard:
        return "close_all", "HARD CLOSE 3:45 PM — 0DTE expiry protection"

    return "hold", f"Holding  |  P&L: {pnl_pct:+.1f}%  |  premium ${current_premium:.2f}"
