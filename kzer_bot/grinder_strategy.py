"""Le Grinder — strategy, market data, futures bias."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "yfinance_cache"
CACHE.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(CACHE))

# ──────────────────────────────────────────────────────────────────────────────
# Verified Canadian watchlist — TSX (.TO), TSXV (.V)
# All tickers validated to return 60d yfinance data as of May 2026.
# Covers energy, mining, cannabis, tech, financials, industrials, utilities,
# consumer, healthcare, timber, REITs, and junior explorers.
# ──────────────────────────────────────────────────────────────────────────────
WATCHLIST: list[str] = [
    # ── Energy (TSX) ────────────────────────────────────────────────────────
    "BTE.TO", "ARX.TO", "PEY.TO", "SGY.TO", "TVE.TO", "AAV.TO", "KEL.TO",
    "SDE.TO", "BIR.TO", "FRU.TO", "TOU.TO", "VET.TO", "ATH.TO", "CVE.TO",
    "WCP.TO", "SU.TO", "POU.TO", "HWX.TO", "OBE.TO", "PHX.TO", "SPB.TO",
    "GEI.TO", "PPR.TO", "TBL.TO", "IPCO.TO", "CEU.TO",
    # ── Mining / Metals / Gold (TSX) ────────────────────────────────────────
    "SVM.TO", "IMG.TO", "OGC.TO", "EDV.TO", "ELD.TO", "MUX.TO", "AGI.TO",
    "AEM.TO", "ABX.TO", "FM.TO", "HBM.TO", "LUN.TO", "CS.TO", "OR.TO",
    "PAAS.TO", "DPM.TO", "WPM.TO", "EQX.TO", "CG.TO", "NG.TO", "SKE.TO",
    "AG.TO", "ERO.TO",
    # ── Cannabis (TSX) ───────────────────────────────────────────────────────
    "ACB.TO", "OGI.TO", "WEED.TO", "CRON.TO",
    # ── Technology / Software (TSX) ─────────────────────────────────────────
    "BB.TO", "LSPD.TO", "REAL.TO", "KXS.TO", "BIPC.TO", "ENGH.TO",
    "CLS.TO", "DCBO.TO", "SHOP.TO", "OTEX.TO", "HUT.TO", "BITF.TO",
    # ── Financials (TSX) ─────────────────────────────────────────────────────
    "MFC.TO", "SLF.TO", "GWO.TO", "IAG.TO", "EQB.TO", "EFN.TO",
    # ── Industrials / Infrastructure (TSX) ──────────────────────────────────
    "CAE.TO", "WSP.TO", "WCN.TO", "STN.TO", "TIH.TO", "NFI.TO", "RBA.TO",
    # ── Utilities / Renewables (TSX) ────────────────────────────────────────
    "NPI.TO", "CPX.TO", "ALA.TO", "BEPC.TO", "AQN.TO",
    # ── Consumer / Retail (TSX) ─────────────────────────────────────────────
    "GIL.TO", "ATD.TO", "DOL.TO", "RUS.TO", "PBH.TO", "GURU.TO",
    # ── Healthcare / Biotech (TSX) ───────────────────────────────────────────
    "WELL.TO", "DND.TO",
    # ── Timber / Materials (TSX) ─────────────────────────────────────────────
    "WFG.TO", "IFP.TO", "CFP.TO",
    # ── REITs / Real estate (TSX) — yfinance data limited for .UN tickers ──
    # Add manually verified REIT tickers here if needed
    # ── Junior / Exploration (TSX) ───────────────────────────────────────────
    "NXE.TO", "ALS.TO", "LAM.TO", "TVK.TO", "PXT.TO", "XTC.TO",
    # ── TSXV — Venture Exchange ───────────────────────────────────────────────
    "IPT.V",   # Impact Silver
    "LIO.V",   # Lion One Metals
    "GR.V",    # Gold Reserve
    "PLY.V",   # Playfair Mining
    "GGO.V",   # Goldenrod Capital
    "QCX.V",   # QCX Gold
    "BHS.V",   # Bayhorse Silver
    "SPOT.V",  # Sprott Focus Trust
    "FWZ.V",   # Fireweed Metals
]


# ──────────────────────────────────────────────────────────────────────────────
# Data layer
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GrinderSnapshot:
    symbol: str
    last_close: float        # yesterday's completed close
    prev_close: float        # two sessions ago (for yesterday's % change)
    yesterday_high: float    # yesterday's high
    yesterday_low: float     # yesterday's low
    avg_volume_20: float     # 20-day average daily volume
    yesterday_volume: float  # yesterday's actual volume
    atr14: float             # ATR(14) in dollars
    ema5: float              # 5-day EMA of close
    ema20: float             # 20-day EMA of close

    @property
    def yesterday_pct_change(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.last_close - self.prev_close) / self.prev_close * 100

    @property
    def rel_volume(self) -> float:
        return self.yesterday_volume / self.avg_volume_20 if self.avg_volume_20 else 0.0

    @property
    def atr_pct(self) -> float:
        return self.atr14 / self.last_close * 100 if self.last_close else 0.0

    @property
    def close_strength(self) -> float:
        """0 = closed at low, 1 = closed at high."""
        rng = self.yesterday_high - self.yesterday_low
        return (self.last_close - self.yesterday_low) / rng if rng > 0 else 0.5

    @property
    def score(self) -> float:
        """
        Composite momentum edge score.
        Weights volume^1.5 to reward institutional conviction.
        A score ≥ 50 = HIGH; ≥ 20 = MEDIUM; < 20 = LOW confidence.
        """
        return (
            self.yesterday_pct_change
            * (self.rel_volume ** 1.5)
            * self.atr_pct
            * (1.0 + self.close_strength)
        )


def _atr14(df: pd.DataFrame) -> float:
    hi, lo, pc = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([(hi - lo), (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def _ema(series: pd.Series, period: int) -> float:
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


class GrinderMarketData:
    """
    yfinance-backed: fetches OHLCV + computes ATR14, EMA5, EMA20.
    Caches snapshots in memory so main / fallback / best-effort strategies
    all share a single download pass — no duplicate API calls.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Optional[GrinderSnapshot]] = {}

    def snapshot(self, symbol: str) -> Optional[GrinderSnapshot]:
        if symbol in self._cache:
            return self._cache[symbol]
        result = self._fetch(symbol)
        self._cache[symbol] = result
        return result

    def all_snapshots(self) -> list[GrinderSnapshot]:
        """Return all successfully downloaded snapshots (for diagnostics)."""
        return [s for s in self._cache.values() if s is not None]

    def _fetch(self, symbol: str) -> Optional[GrinderSnapshot]:
        try:
            ticker = yf.Ticker(symbol)
            daily = ticker.history(period="60d", interval="1d", auto_adjust=False)
            if daily.empty:
                return None
            daily = daily.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            if len(daily) < 22:
                return None

            last = daily.iloc[-1]
            prev = daily.iloc[-2]
            avg_vol = float(daily["Volume"].tail(20).mean())

            return GrinderSnapshot(
                symbol=symbol,
                last_close=float(last["Close"]),
                prev_close=float(prev["Close"]),
                yesterday_high=float(last["High"]),
                yesterday_low=float(last["Low"]),
                avg_volume_20=avg_vol,
                yesterday_volume=float(last["Volume"]),
                atr14=_atr14(daily),
                ema5=_ema(daily["Close"], 5),
                ema20=_ema(daily["Close"], 20),
            )
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────────────────────
# US Futures bias
# ──────────────────────────────────────────────────────────────────────────────

class FuturesBias(Enum):
    GREEN   = "green"
    RED     = "red"
    NEUTRAL = "neutral"


def get_futures_bias() -> tuple[FuturesBias, str]:
    """
    Checks ES=F (S&P 500 futures) vs 24h ago.
    ≥ +0.3% → GREEN  → buy at open (9:15 AM)
    ≤ -0.3% → RED    → wait for bounce (11:00 AM window)
    else    → NEUTRAL → buy at open
    """
    try:
        data = yf.Ticker("ES=F").history(period="5d", interval="1h", auto_adjust=False)
        data = data.dropna(subset=["Close"])
        if len(data) < 2:
            return FuturesBias.NEUTRAL, "ES=F: insufficient data"

        last = float(data["Close"].iloc[-1])
        ref  = float(data["Close"].iloc[-24] if len(data) >= 24 else data["Close"].iloc[0])
        if ref == 0:
            return FuturesBias.NEUTRAL, "ES=F: invalid reference"

        pct    = (last - ref) / ref * 100
        detail = f"ES=F {last:,.0f} pts  ({pct:+.2f}% vs 24h ago)"

        if pct >= 0.3:
            return FuturesBias.GREEN,   detail
        elif pct <= -0.3:
            return FuturesBias.RED,     detail
        else:
            return FuturesBias.NEUTRAL, detail
    except Exception as exc:
        return FuturesBias.NEUTRAL, f"ES=F: error — {exc}"


# ──────────────────────────────────────────────────────────────────────────────
# Pick dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GrinderPick:
    symbol: str
    last_close: float
    score: float
    yesterday_pct: float
    rel_volume: float
    atr_pct: float
    close_strength: float   # 0–1, where 1 = closed at the day high
    above_ema5: bool
    above_ema20: bool
    strategy_name: str      # "Main Strategy", "Fallback Original Strategy", or "Best Available"

    @property
    def confidence(self) -> str:
        if self.score >= 50:
            return "HIGH 🔥"
        elif self.score >= 20:
            return "MEDIUM ✅"
        else:
            return "LOW ⚠️"


# ──────────────────────────────────────────────────────────────────────────────
# Main strategy — 8 criteria, tuned for 1–3 % daily momentum trades
# ──────────────────────────────────────────────────────────────────────────────

class GrinderStrategy:
    """
    8-criteria main strategy:
      1. Price $2.00–$40.00          — volatile sweet spot (higher = less % move)
      2. 20-day avg volume ≥ 300,000 — enough liquidity to enter/exit
      3. Yesterday % change +1.5–+12% — real momentum, not noise
      4. Rel. volume ≥ 1.5×           — elevated = institutional conviction
      5. ATR(14) ≥ 1.5% of price      — stock needs room to run 1–3 %
      6. Close > 20-day EMA           — confirmed medium-term uptrend
      7. Close > 5-day EMA            — short-term trend intact
      8. Close strength ≥ 0.40        — closed in upper 60 % of day's range

    Score = yesterday_pct × rel_volume^1.5 × atr_pct × (1 + close_strength)
    """

    MIN_PRICE     = 2.00
    MAX_PRICE     = 40.00
    MIN_AVG_VOL   = 300_000
    MIN_PCT_CHG   = 1.5
    MAX_PCT_CHG   = 12.0
    MIN_REL_VOL   = 1.5
    MIN_ATR_PCT   = 1.5
    MIN_CLOSE_STR = 0.40

    def __init__(self, market_data: Optional[GrinderMarketData] = None) -> None:
        self.market_data = market_data or GrinderMarketData()

    def scan(self, watchlist: list[str]) -> list[GrinderPick]:
        picks: list[GrinderPick] = []
        for symbol in watchlist:
            snap = self.market_data.snapshot(symbol)
            if snap is None:
                continue
            pick = self._qualify(snap)
            if pick is not None:
                picks.append(pick)
        return sorted(picks, key=lambda p: p.score, reverse=True)

    def _qualify(self, snap: GrinderSnapshot) -> Optional[GrinderPick]:
        if not (self.MIN_PRICE <= snap.last_close <= self.MAX_PRICE):
            return None
        if snap.avg_volume_20 < self.MIN_AVG_VOL:
            return None
        pct = snap.yesterday_pct_change
        if not (self.MIN_PCT_CHG <= pct <= self.MAX_PCT_CHG):
            return None
        if snap.rel_volume < self.MIN_REL_VOL:
            return None
        if snap.atr_pct < self.MIN_ATR_PCT:
            return None
        if snap.last_close <= snap.ema20:
            return None
        if snap.last_close <= snap.ema5:
            return None
        if snap.close_strength < self.MIN_CLOSE_STR:
            return None
        return GrinderPick(
            symbol=snap.symbol,
            last_close=snap.last_close,
            score=snap.score,
            yesterday_pct=pct,
            rel_volume=snap.rel_volume,
            atr_pct=snap.atr_pct,
            close_strength=snap.close_strength,
            above_ema5=True,
            above_ema20=True,
            strategy_name="Main Strategy",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fallback — fires only when main finds nothing
# ──────────────────────────────────────────────────────────────────────────────

class FallbackStrategy:
    """
    Relaxed momentum fallback:
      1. Price $1.00–$40.00
      2. 20-day avg vol ≥ 100,000
      3. Yesterday % change +1.0–+15%
      4. Rel. volume ≥ 1.2×
      5. Close > 20-day EMA
    Score = yesterday_pct × rel_volume × (1 + close_strength)
    """

    MIN_PRICE   = 1.00
    MAX_PRICE   = 40.00
    MIN_AVG_VOL = 100_000
    MIN_PCT_CHG = 1.0
    MAX_PCT_CHG = 15.0
    MIN_REL_VOL = 1.2

    def __init__(self, market_data: Optional[GrinderMarketData] = None) -> None:
        self.market_data = market_data or GrinderMarketData()

    def scan(self, watchlist: list[str]) -> list[GrinderPick]:
        picks: list[GrinderPick] = []
        for symbol in watchlist:
            snap = self.market_data.snapshot(symbol)
            if snap is None:
                continue
            pick = self._qualify(snap)
            if pick is not None:
                picks.append(pick)
        return sorted(picks, key=lambda p: p.score, reverse=True)

    def _qualify(self, snap: GrinderSnapshot) -> Optional[GrinderPick]:
        if not (self.MIN_PRICE <= snap.last_close <= self.MAX_PRICE):
            return None
        if snap.avg_volume_20 < self.MIN_AVG_VOL:
            return None
        pct = snap.yesterday_pct_change
        if not (self.MIN_PCT_CHG <= pct <= self.MAX_PCT_CHG):
            return None
        if snap.rel_volume < self.MIN_REL_VOL:
            return None
        if snap.last_close <= snap.ema20:
            return None
        score = pct * snap.rel_volume * (1.0 + snap.close_strength)
        return GrinderPick(
            symbol=snap.symbol,
            last_close=snap.last_close,
            score=score,
            yesterday_pct=pct,
            rel_volume=snap.rel_volume,
            atr_pct=snap.atr_pct,
            close_strength=snap.close_strength,
            above_ema5=(snap.last_close > snap.ema5),
            above_ema20=True,
            strategy_name="Fallback Original Strategy",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Best Effort — guaranteed pick when both strategies find nothing
# ──────────────────────────────────────────────────────────────────────────────

class BestEffortStrategy:
    """
    Last-resort guaranteed pick — no filters, pure momentum ranking.
    Fires only when GrinderStrategy and FallbackStrategy both return nothing.
    Always returns exactly 1 pick: the highest-scoring ticker with valid data.
    Prefers positive yesterday_pct (up-day momentum) over all others.
    Tagged "Best Available" in strategy_name.

    This ensures the bot never skips a day due to filter misses on a flat market.
    The Telegram game plan clearly labels it as a best-effort pick.
    """

    def __init__(self, market_data: Optional[GrinderMarketData] = None) -> None:
        self.market_data = market_data or GrinderMarketData()

    def scan(self, watchlist: list[str]) -> list[GrinderPick]:
        candidates: list[GrinderSnapshot] = []
        for symbol in watchlist:
            snap = self.market_data.snapshot(symbol)
            if snap is None:
                continue
            if snap.last_close <= 0 or snap.avg_volume_20 <= 0:
                continue
            candidates.append(snap)

        if not candidates:
            return []

        # Prefer stocks that were up yesterday (positive momentum direction)
        positives = [s for s in candidates if s.yesterday_pct_change > 0]
        pool = positives if positives else candidates

        best = max(pool, key=lambda s: s.score)
        return [GrinderPick(
            symbol=best.symbol,
            last_close=best.last_close,
            score=best.score,
            yesterday_pct=best.yesterday_pct_change,
            rel_volume=best.rel_volume,
            atr_pct=best.atr_pct,
            close_strength=best.close_strength,
            above_ema5=(best.last_close > best.ema5),
            above_ema20=(best.last_close > best.ema20),
            strategy_name="Best Available",
        )]
