"""Le Grinder — strategy, market data, futures bias."""

from __future__ import annotations

import json
import time
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT           = Path(__file__).resolve().parents[1]
YF_CACHE       = ROOT / "data" / "yfinance_cache"
SNAPSHOT_CACHE = ROOT / "data" / "grinder_snapshot_cache.json"
UNIVERSE_FILE  = ROOT / "data" / "universe.json"
YF_CACHE.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(YF_CACHE))

# ──────────────────────────────────────────────────────────────────────────────
# Universe — dynamically loaded from data/universe.json (built by
# scripts/update_universe.py which pulls TSX + TSXV from the TMX public API).
# Falls back to the 109-ticker hardcoded list if the file is missing.
# ──────────────────────────────────────────────────────────────────────────────

_HARDCODED_WATCHLIST: list[str] = [
    # ── Energy (TSX) ──────────────────────────────────────────────────────────
    "BTE.TO", "ARX.TO", "PEY.TO", "SGY.TO", "TVE.TO", "AAV.TO", "KEL.TO",
    "SDE.TO", "BIR.TO", "FRU.TO", "TOU.TO", "VET.TO", "ATH.TO", "CVE.TO",
    "WCP.TO", "SU.TO", "POU.TO", "HWX.TO", "OBE.TO", "PHX.TO", "SPB.TO",
    "GEI.TO", "PPR.TO", "TBL.TO", "IPCO.TO", "CEU.TO",
    # ── Mining / Metals / Gold (TSX) ──────────────────────────────────────────
    "SVM.TO", "IMG.TO", "OGC.TO", "EDV.TO", "ELD.TO", "MUX.TO", "AGI.TO",
    "AEM.TO", "ABX.TO", "FM.TO", "HBM.TO", "LUN.TO", "CS.TO", "OR.TO",
    "PAAS.TO", "DPM.TO", "WPM.TO", "EQX.TO", "CG.TO", "NG.TO", "SKE.TO",
    "AG.TO", "ERO.TO",
    # ── Cannabis (TSX) ────────────────────────────────────────────────────────
    "ACB.TO", "OGI.TO", "WEED.TO", "CRON.TO",
    # ── Technology / Software (TSX) ───────────────────────────────────────────
    "BB.TO", "LSPD.TO", "REAL.TO", "KXS.TO", "BIPC.TO", "ENGH.TO",
    "CLS.TO", "DCBO.TO", "SHOP.TO", "OTEX.TO", "KEEL.TO", "HUT.TO",
    # ── Financials (TSX) ──────────────────────────────────────────────────────
    "MFC.TO", "SLF.TO", "GWO.TO", "IAG.TO", "EQB.TO", "EFN.TO",
    # ── Industrials / Infrastructure (TSX) ────────────────────────────────────
    "CAE.TO", "WSP.TO", "WCN.TO", "STN.TO", "TIH.TO", "NFI.TO", "RBA.TO",
    # ── Utilities / Renewables (TSX) ──────────────────────────────────────────
    "NPI.TO", "CPX.TO", "ALA.TO", "BEPC.TO", "AQN.TO",
    # ── Consumer / Retail (TSX) ───────────────────────────────────────────────
    "GIL.TO", "ATD.TO", "DOL.TO", "RUS.TO", "PBH.TO", "GURU.TO",
    # ── Healthcare / Biotech (TSX) ────────────────────────────────────────────
    "WELL.TO", "DND.TO",
    # ── Timber / Materials (TSX) ──────────────────────────────────────────────
    "WFG.TO", "IFP.TO", "CFP.TO",
    # ── Junior / Exploration (TSX) ────────────────────────────────────────────
    "NXE.TO", "ALS.TO", "LAM.TO", "TVK.TO", "PXT.TO", "XTC.TO",
    # ── TSXV ──────────────────────────────────────────────────────────────────
    "LIO.V", "GR.V", "PLY.V", "GGO.V", "QCX.V", "BHS.V", "FWZ.V",
]


def _load_watchlist() -> list[str]:
    """Load universe from data/universe.json; fall back to hardcoded list."""
    try:
        if UNIVERSE_FILE.exists():
            data = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            syms = data.get("symbols", [])
            if len(syms) > 0:
                return syms
    except Exception:
        pass
    return _HARDCODED_WATCHLIST


WATCHLIST: list[str] = _load_watchlist()


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
        A score >= 50 = HIGH; >= 20 = MEDIUM; < 20 = LOW confidence.
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


def _build_snapshot(symbol: str, df: pd.DataFrame) -> Optional["GrinderSnapshot"]:
    """Build a GrinderSnapshot from a cleaned OHLCV DataFrame."""
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < 22:
        return None
    last    = df.iloc[-1]
    prev    = df.iloc[-2]
    avg_vol = float(df["Volume"].tail(20).mean())
    if avg_vol == 0:
        return None
    return GrinderSnapshot(
        symbol          = symbol,
        last_close      = float(last["Close"]),
        prev_close      = float(prev["Close"]),
        yesterday_high  = float(last["High"]),
        yesterday_low   = float(last["Low"]),
        avg_volume_20   = avg_vol,
        yesterday_volume= float(last["Volume"]),
        atr14           = _atr14(df),
        ema5            = _ema(df["Close"], 5),
        ema20           = _ema(df["Close"], 20),
    )


def _extract_ticker_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        if symbol in raw.columns.get_level_values(0):
            return raw[symbol].copy()
        return pd.DataFrame()
    return raw.copy()


def _snapshot_to_dict(snapshot: GrinderSnapshot) -> dict:
    return {
        "symbol": snapshot.symbol,
        "last_close": snapshot.last_close,
        "prev_close": snapshot.prev_close,
        "yesterday_high": snapshot.yesterday_high,
        "yesterday_low": snapshot.yesterday_low,
        "avg_volume_20": snapshot.avg_volume_20,
        "yesterday_volume": snapshot.yesterday_volume,
        "atr14": snapshot.atr14,
        "ema5": snapshot.ema5,
        "ema20": snapshot.ema20,
    }


def _snapshot_from_dict(data: dict) -> Optional[GrinderSnapshot]:
    try:
        return GrinderSnapshot(
            symbol=data["symbol"],
            last_close=float(data["last_close"]),
            prev_close=float(data["prev_close"]),
            yesterday_high=float(data["yesterday_high"]),
            yesterday_low=float(data["yesterday_low"]),
            avg_volume_20=float(data["avg_volume_20"]),
            yesterday_volume=float(data["yesterday_volume"]),
            atr14=float(data["atr14"]),
            ema5=float(data["ema5"]),
            ema20=float(data["ema20"]),
        )
    except Exception:
        return None


def _history_with_retry(symbol: str, *, period: str, interval: str,
                        attempts: int = 3, pause: float = 1.5) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            frame = yf.Ticker(symbol).history(
                period=period, interval=interval, auto_adjust=False
            )
            if not frame.empty:
                return frame
        except Exception as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(pause * attempt)
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


class GrinderMarketData:
    """
    yfinance-backed market data with in-memory snapshot cache.

    Individual snapshot() calls are used for single lookups.
    prefetch(symbols) batch-downloads up to N symbols at a time for full
    universe scans — much faster than sequential individual calls.
    All three strategies (main / fallback / best-effort) share one cache
    instance so each ticker is only ever downloaded once per scan.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Optional[GrinderSnapshot]] = {}
        self._market_cap_cache: dict[str, Optional[float]] = {}
        self._disk_loaded = False

    def _load_disk_cache(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        if not SNAPSHOT_CACHE.exists():
            return
        try:
            age = time.time() - SNAPSHOT_CACHE.stat().st_mtime
            if age > 18 * 60 * 60:
                return
            raw = json.loads(SNAPSHOT_CACHE.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for symbol, payload in raw.items():
                if isinstance(payload, dict):
                    snap = _snapshot_from_dict(payload)
                    if snap is not None:
                        self._cache[symbol] = snap
        except Exception:
            return

    def _persist_cache(self) -> None:
        try:
            data = {sym: _snapshot_to_dict(snap) for sym, snap in self._cache.items() if snap is not None}
            SNAPSHOT_CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def snapshot(self, symbol: str) -> Optional[GrinderSnapshot]:
        self._load_disk_cache()
        if symbol in self._cache:
            return self._cache[symbol]
        result = self._fetch_one(symbol)
        self._cache[symbol] = result
        if result is not None:
            self._persist_cache()
        return result

    def prefetch(self, symbols: list[str], batch_size: int = 200,
                 progress_cb=None) -> None:
        """
        Batch-download all symbols not yet in cache.
        Uses yf.download(group_by='ticker', threads=True) for parallelism —
        roughly 3-5x faster than sequential individual calls for large universes.
        progress_cb(done, total) called after each batch if provided.
        """
        self._load_disk_cache()
        to_fetch = [s for s in symbols if s not in self._cache]
        total    = len(to_fetch)
        done     = 0

        if batch_size <= 0:
            batch_size = 25

        for i in range(0, total, batch_size):
            batch = to_fetch[i : i + batch_size]
            if len(batch) == 1:
                self._cache[batch[0]] = self._fetch_one(batch[0])
                done += 1
                if progress_cb:
                    progress_cb(done, total)
                continue

            raw = pd.DataFrame()
            for attempt in range(1, 4):
                try:
                    raw = yf.download(
                        batch,
                        period       = "60d",
                        interval     = "1d",
                        auto_adjust  = False,
                        progress     = False,
                        group_by     = "ticker",
                        threads      = False,
                        timeout      = 20,
                    )
                    if not raw.empty:
                        break
                except Exception:
                    raw = pd.DataFrame()
                if attempt < 3:
                    time.sleep(1.5 * attempt)

            for sym in batch:
                if sym in self._cache:
                    continue
                try:
                    df = _extract_ticker_frame(raw, sym)
                    if df.empty:
                        self._cache[sym] = self._fetch_one(sym)
                    else:
                        self._cache[sym] = _build_snapshot(sym, df)
                except Exception:
                    self._cache[sym] = None

            done += len(batch)
            if progress_cb:
                progress_cb(done, total)

        self._persist_cache()

    def all_snapshots(self) -> list[GrinderSnapshot]:
        """Return all successfully downloaded snapshots (for diagnostics)."""
        return [s for s in self._cache.values() if s is not None]

    def market_cap(self, symbol: str) -> Optional[float]:
        if symbol in self._market_cap_cache:
            return self._market_cap_cache[symbol]
        value = self._fetch_market_cap(symbol)
        self._market_cap_cache[symbol] = value
        return value

    def _fetch_market_cap(self, symbol: str) -> Optional[float]:
        try:
            ticker = yf.Ticker(symbol)
            fast_info = getattr(ticker, "fast_info", None)
            if fast_info is not None:
                for key in ("market_cap", "marketCap"):
                    try:
                        value = fast_info.get(key) if hasattr(fast_info, "get") else None
                    except Exception:
                        value = None
                    if value:
                        return float(value)

            info = ticker.get_info()
            for key in ("marketCap", "market_cap"):
                value = info.get(key)
                if value:
                    return float(value)
        except Exception:
            return None
        return None

    def _fetch_one(self, symbol: str) -> Optional[GrinderSnapshot]:
        try:
            daily = _history_with_retry(symbol, period="60d", interval="1d")
            if daily.empty:
                return None
            return _build_snapshot(symbol, daily)
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
    >= +0.3% -> GREEN  -> buy at open (9:15 AM)
    <= -0.3% -> RED    -> wait for bounce (11:00 AM window)
    else     -> NEUTRAL -> buy at open
    """
    try:
        data = _history_with_retry("ES=F", period="5d", interval="1h")
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
        return FuturesBias.NEUTRAL, f"ES=F: error - {exc}"


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
    close_strength: float   # 0-1, where 1 = closed at the day high
    above_ema5: bool
    above_ema20: bool
    strategy_name: str      # "Main Strategy", "Fallback", or "Best Available"

    @property
    def confidence(self) -> str:
        if self.score >= 50:
            return "HIGH"
        elif self.score >= 20:
            return "MEDIUM"
        else:
            return "LOW"

    @property
    def confidence_emoji(self) -> str:
        if self.score >= 50:
            return "HIGH (fire)"
        elif self.score >= 20:
            return "MEDIUM (ok)"
        else:
            return "LOW (warn)"


# ──────────────────────────────────────────────────────────────────────────────
# Main strategy — 8 criteria
# ──────────────────────────────────────────────────────────────────────────────

class GrinderStrategy:
    """
    8-criteria main strategy:
      1. Price $2.00-$40.00          - volatile sweet spot
      2. 20-day avg volume >= 300,000 - enough liquidity to enter/exit
      3. Yesterday % change +1.5-+12% - real momentum, not noise
      4. Rel. volume >= 1.5x          - elevated = institutional conviction
      5. ATR(14) >= 1.5% of price     - stock needs room to run 1-3 %
      6. Close > 20-day EMA           - confirmed medium-term uptrend
      7. Close > 5-day EMA            - short-term trend intact
      8. Close strength >= 0.40       - closed in upper 60 % of day range

    Score = yesterday_pct x rel_volume^1.5 x atr_pct x (1 + close_strength)
    """

    MIN_PRICE     = 2.00
    MAX_PRICE     = 40.00
    MIN_MARKET_CAP = 25_000_000
    MIN_AVG_VOL   = 300_000
    MIN_YDAY_VOL  = 100_000
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
        if snap.yesterday_volume < self.MIN_YDAY_VOL:
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
        market_cap = self.market_data.market_cap(snap.symbol)
        if market_cap is None or market_cap < self.MIN_MARKET_CAP:
            return None
        return GrinderPick(
            symbol        = snap.symbol,
            last_close    = snap.last_close,
            score         = snap.score,
            yesterday_pct = pct,
            rel_volume    = snap.rel_volume,
            atr_pct       = snap.atr_pct,
            close_strength= snap.close_strength,
            above_ema5    = True,
            above_ema20   = True,
            strategy_name = "Main Strategy",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fallback — fires when main finds nothing
# ──────────────────────────────────────────────────────────────────────────────

class FallbackStrategy:
    """
    Relaxed momentum fallback:
      1. Price $1.00-$40.00
      2. 20-day avg vol >= 100,000
      3. Yesterday % change +1.0-+15%
      4. Rel. volume >= 1.0x  (lowered from 1.2x — catches quiet-volume days)
      5. Close > 20-day EMA
    Score = yesterday_pct x rel_volume x (1 + close_strength)
    """

    MIN_PRICE   = 1.00
    MAX_PRICE   = 40.00
    MIN_MARKET_CAP = 25_000_000
    MIN_AVG_VOL = 100_000
    MIN_YDAY_VOL = 100_000
    MIN_PCT_CHG = 1.0
    MAX_PCT_CHG = 15.0
    MIN_REL_VOL = 1.0  # lowered: catches stocks up on quiet volume

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
        if snap.yesterday_volume < self.MIN_YDAY_VOL:
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
        market_cap = self.market_data.market_cap(snap.symbol)
        if market_cap is None or market_cap < self.MIN_MARKET_CAP:
            return None
        score = pct * snap.rel_volume * (1.0 + snap.close_strength)
        return GrinderPick(
            symbol        = snap.symbol,
            last_close    = snap.last_close,
            score         = score,
            yesterday_pct = pct,
            rel_volume    = snap.rel_volume,
            atr_pct       = snap.atr_pct,
            close_strength= snap.close_strength,
            above_ema5    = (snap.last_close > snap.ema5),
            above_ema20   = True,
            strategy_name = "Fallback",
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
            if snap.yesterday_volume < 100_000:
                continue
            market_cap = self.market_data.market_cap(symbol)
            if market_cap is None or market_cap < 25_000_000:
                continue
            candidates.append(snap)

        if not candidates:
            return []

        positives = [s for s in candidates if s.yesterday_pct_change > 0]
        pool = positives if positives else candidates
        best = max(pool, key=lambda s: s.score)

        return [GrinderPick(
            symbol        = best.symbol,
            last_close    = best.last_close,
            score         = best.score,
            yesterday_pct = best.yesterday_pct_change,
            rel_volume    = best.rel_volume,
            atr_pct       = best.atr_pct,
            close_strength= best.close_strength,
            above_ema5    = (best.last_close > best.ema5),
            above_ema20   = (best.last_close > best.ema20),
            strategy_name = "Best Available",
        )]
