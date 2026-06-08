"""Le Grinder — strategy, market data, futures bias."""

from __future__ import annotations

import json
import time
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT                = Path(__file__).resolve().parents[1]
YF_CACHE            = ROOT / "data" / "yfinance_cache"
SNAPSHOT_CACHE      = ROOT / "data" / "grinder_snapshot_cache.json"
MARKET_CAP_CACHE    = ROOT / "data" / "market_cap_cache.json"
FUTURES_CACHE       = ROOT / "data" / "futures_bias_cache.json"
UNIVERSE_FILE       = ROOT / "data" / "universe.json"
SMART_CONTEXT_CACHE = ROOT / "data" / "smart_context_cache.json"
EARNINGS_CACHE      = ROOT / "data" / "earnings_cache.json"
SHORT_CACHE         = ROOT / "data" / "short_interest_cache.json"
DELIST_CACHE        = ROOT / "data" / "delist_cache.json"

_DELIST_TTL_DAYS = 30   # re-check delisted symbols after 30 days


def _load_delist_cache() -> set[str]:
    """Return set of symbols that have consistently returned no yfinance data."""
    try:
        if not DELIST_CACHE.exists():
            return set()
        from datetime import datetime, timedelta
        data = json.loads(DELIST_CACHE.read_text())
        cutoff = (datetime.now() - timedelta(days=_DELIST_TTL_DAYS)).date().isoformat()
        return {sym for sym, added in data.items() if added >= cutoff}
    except Exception:
        return set()


def _mark_delist(symbols: list[str]) -> None:
    """Record symbols that returned no data so future scans skip them."""
    if not symbols:
        return
    try:
        from datetime import datetime
        today = datetime.now().date().isoformat()
        existing: dict[str, str] = {}
        if DELIST_CACHE.exists():
            existing = json.loads(DELIST_CACHE.read_text())
        for sym in symbols:
            if sym not in existing:
                existing[sym] = today
        DELIST_CACHE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass

_SECTOR_ETFS = ["XLK", "XLV", "XLE", "XLF", "XLI", "XLY"]
YF_CACHE.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(YF_CACHE))

# ──────────────────────────────────────────────────────────────────────────────
# Universe — NYSE / NASDAQ universe loaded from data/us_universe.json.
# Falls back to the hardcoded list of ~350 liquid US tickers if missing.
# ──────────────────────────────────────────────────────────────────────────────

_HARDCODED_WATCHLIST: list[str] = [
    # ── Mega-cap Tech ─────────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA",
    # ── Semiconductors ───────────────────────────────────────────────────────
    "AVGO", "QCOM", "MU", "MRVL", "AMAT", "LRCX", "KLAC", "INTC",
    "TXN", "ON", "MCHP", "WOLF", "SMCI", "ARM", "SLAB",
    # ── Cloud / Enterprise SaaS ───────────────────────────────────────────────
    "CRM", "NOW", "SNOW", "PLTR", "ORCL", "ADBE", "WDAY", "TEAM",
    # ── Cybersecurity ────────────────────────────────────────────────────────
    "PANW", "CRWD", "ZS", "FTNT", "DDOG", "NET", "CYBR", "S",
    # ── Fintech / Crypto ─────────────────────────────────────────────────────
    "V", "MA", "PYPL", "SQ", "COIN", "HOOD", "SOFI", "AFRM",
    "MARA", "RIOT", "CLSK", "HUT", "CIFR",
    # ── Banks / Finance ──────────────────────────────────────────────────────
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BX", "BLK", "SCHW",
    "IBKR", "RJF",
    # ── Energy ───────────────────────────────────────────────────────────────
    "XOM", "CVX", "COP", "OXY", "MRO", "DVN", "FANG", "HES", "EOG",
    "SLB", "HAL", "BKR", "NOG", "SM",
    # ── Healthcare / Biotech ─────────────────────────────────────────────────
    "LLY", "NVO", "ABBV", "MRK", "PFE", "BMY", "AMGN", "GILD",
    "REGN", "VRTX", "MRNA", "BNTX", "BIIB", "ALNY", "EXAS",
    "RXRX", "ACHR",
    # ── Consumer / Retail ────────────────────────────────────────────────────
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "DIS", "NFLX",
    "UBER", "LYFT", "DASH", "BKNG", "ABNB",
    # ── AI / Data / Infra ────────────────────────────────────────────────────
    "DELL", "HPE", "IONQ", "RGTI", "QUBT", "LUNR", "RKLB",
    # ── EVs / Clean Energy ───────────────────────────────────────────────────
    "RIVN", "LCID", "NIO", "LI", "XPEV", "ENPH", "FSLR",
    "F", "GM", "PLUG",
    # ── Industrials / Defense ────────────────────────────────────────────────
    "GE", "CAT", "BA", "RTX", "LMT", "NOC", "DE", "HON",
    # ── Media / Gaming ───────────────────────────────────────────────────────
    "RBLX", "EA", "TTWO", "SPOT", "NFLX",
    # ── High-beta momentum ───────────────────────────────────────────────────
    "GME", "AMC", "UWMC", "CLOV", "SPCE",
    "SNDL", "NKLA", "WKHS",
    # ── S&P 500 high-volume liquid names ─────────────────────────────────────
    "AAON", "ACM", "AES", "AIG", "AIZ", "AJG", "AKAM", "ALB", "ALGN",
    "ALK", "ALL", "ALLE", "ANET", "AON", "APA", "APD", "APH", "APTV",
    "ARE", "ATO", "AVB", "AVGO", "AWK", "AXP", "AZO", "BBY", "BDX",
    "BEN", "BIO", "BK", "BMRN", "BR", "BRKB", "BRO", "BSX", "BXP",
    "CB", "CBOE", "CBRE", "CDW", "CE", "CF", "CHD", "CHRW", "CHTR",
    "CI", "CINF", "CLX", "CMCSA", "CMS", "CNC", "CNP", "COF", "COO",
    "CPRT", "CPT", "CSX", "CTAS", "CTLT", "CTSH", "CTVA", "CVS",
    "D", "DAL", "DE", "DFS", "DG", "DHI", "DHR", "DLR", "DLTR",
    "DOV", "DPZ", "DRI", "DTE", "DUK", "DVA", "EFX", "EG", "EIX",
    "EL", "EMN", "EMR", "EOG", "ES", "ESS", "EW", "EXC", "EXPD",
    "EXPE", "EXR", "FAST", "FDX", "FIS", "FISV", "FLT", "FMC",
    "FOX", "FOXA", "FRC", "FRT", "GD", "GL", "HAS", "HCA", "HII",
    "HLT", "HOLX", "HPQ", "HRL", "HSIC", "HST", "HSY", "HWM",
    "ICE", "IDXX", "IEX", "IFF", "ILMN", "INCY", "IP", "IPG",
    "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J", "JBHT",
    "JCI", "JKHY", "JNJ", "JNPR", "K", "KEY", "KHC", "KIM", "KLAC",
    "KMB", "KMI", "KMX", "KO", "KR", "L", "LDOS", "LEN", "LH",
    "LIN", "LKQ", "LNC", "LNT", "LUV", "LVS", "LYB", "LYV",
    "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK", "MCO", "MDLZ",
    "MDT", "MET", "MGM", "MHK", "MKC", "MKTX", "MLM", "MMC",
    "MNST", "MO", "MOS", "MPC", "MPW", "MPWR", "MRO", "MSCI",
    "MSI", "MTB", "MTCH", "MTD", "MU", "NDAQ", "NEE", "NEM",
    "NFLX", "NI", "NKE", "NLOK", "NLSN", "NRG", "NSC", "NTAP",
    "NTRS", "NUE", "NVAX", "NVR", "NWL", "NWS", "NWSA", "NXPI",
    "O", "OGN", "OKE", "OMC", "OPEN", "OPK", "ORLY", "OTIS",
    "PARA", "PAYC", "PAYX", "PEG", "PEP", "PFG", "PGR", "PH",
    "PHM", "PKG", "PKI", "PLD", "PM", "PNC", "PNR", "PNW", "POOL",
    "PPG", "PPL", "PRU", "PSA", "PSX", "PTC", "PVH", "PWR",
    "QRVO", "RCL", "RE", "REG", "REGN", "RF", "RHI", "RJF",
    "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX",
    "SBAC", "SBUX", "SEE", "SHW", "SIVB", "SJM", "SNA", "SNPS",
    "SO", "SPG", "SPGI", "SRE", "STT", "STX", "STZ", "SWK",
    "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY", "TEL",
    "TER", "TFC", "TFX", "TGT", "TJX", "TMO", "TMUS", "TPR",
    "TRMB", "TROW", "TRV", "TSCO", "TT", "TTWO", "TWTR", "TXN",
    "TXT", "TYL", "UAL", "UDR", "UHS", "ULTA", "UNH", "UNP",
    "UPS", "URI", "USB", "VFC", "VLO", "VMC", "VNO", "VNT",
    "VRSK", "VRSN", "VRTX", "VTR", "VZ", "WAB", "WAT", "WBA",
    "WBD", "WDC", "WEC", "WELL", "WHR", "WM", "WMB", "WRB",
    "WRK", "WST", "WTW", "WY", "WYNN", "XEL", "XYL", "YUM",
    "ZBH", "ZBRA", "ZION", "ZTS",
]


def _load_watchlist() -> list[str]:
    """Load US stock universe from data/us_universe.json; fall back to hardcoded list."""
    try:
        us_file = ROOT / "data" / "us_universe.json"
        if us_file.exists():
            data = json.loads(us_file.read_text(encoding="utf-8"))
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
        self._frames: dict[str, pd.DataFrame] = {}
        self._market_cap_cache: dict[str, Optional[float]] = {}
        self._market_cap_disk_loaded = False
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

    def _load_market_cap_cache(self) -> None:
        if self._market_cap_disk_loaded:
            return
        self._market_cap_disk_loaded = True
        if not MARKET_CAP_CACHE.exists():
            return
        try:
            age = time.time() - MARKET_CAP_CACHE.stat().st_mtime
            if age > 7 * 86400:
                return
            raw = json.loads(MARKET_CAP_CACHE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for symbol, value in raw.items():
                    if value is None:
                        self._market_cap_cache[symbol] = None
                    else:
                        self._market_cap_cache[symbol] = float(value)
        except Exception:
            return

    def _persist_market_cap_cache(self) -> None:
        try:
            MARKET_CAP_CACHE.write_text(
                json.dumps(self._market_cap_cache, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def get_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        """Return the raw OHLCV DataFrame for a symbol (populated after prefetch)."""
        return self._frames.get(symbol)

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
        # Skip symbols that have persistently returned no data (delisted / bad tickers)
        delist = _load_delist_cache()
        symbols = [s for s in symbols if s not in delist]
        # Also re-fetch symbols whose frames are missing (needed by SmartGrinderStrategy)
        to_fetch = [s for s in symbols if s not in self._cache or s not in self._frames]
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
                        period       = "1y",
                        interval     = "1d",
                        auto_adjust  = False,
                        progress     = False,
                        group_by     = "ticker",
                        threads      = False,
                        timeout      = 30,
                    )
                    if not raw.empty:
                        break
                except Exception:
                    raw = pd.DataFrame()
                if attempt < 3:
                    time.sleep(1.5 * attempt)

            for sym in batch:
                if sym in self._cache and sym in self._frames:
                    continue
                try:
                    df = _extract_ticker_frame(raw, sym)
                    if df.empty:
                        if sym not in self._cache:
                            self._cache[sym] = self._fetch_one(sym)
                    else:
                        self._frames[sym] = df
                        self._cache[sym] = _build_snapshot(sym, df)
                except Exception:
                    if sym not in self._cache:
                        self._cache[sym] = None

            done += len(batch)
            if progress_cb:
                progress_cb(done, total)

        # Mark any symbol that still has no data as persistently failing
        newly_failed = [s for s in to_fetch if self._cache.get(s) is None]
        if newly_failed:
            _mark_delist(newly_failed)

        self._persist_cache()

    def all_snapshots(self) -> list[GrinderSnapshot]:
        """Return all successfully downloaded snapshots (for diagnostics)."""
        return [s for s in self._cache.values() if s is not None]

    def market_cap(self, symbol: str) -> Optional[float]:
        self._load_market_cap_cache()
        if symbol in self._market_cap_cache:
            return self._market_cap_cache[symbol]
        value = self._fetch_market_cap(symbol)
        self._market_cap_cache[symbol] = value
        self._persist_market_cap_cache()
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
            daily = _history_with_retry(symbol, period="1y", interval="1d")
            if daily.empty:
                return None
            self._frames[symbol] = daily
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


def _load_cached_futures_bias() -> tuple[FuturesBias, str] | None:
    if not FUTURES_CACHE.exists():
        return None
    try:
        age = time.time() - FUTURES_CACHE.stat().st_mtime
        if age > 18 * 3600:
            return None
        raw = json.loads(FUTURES_CACHE.read_text(encoding="utf-8"))
        bias = FuturesBias(raw.get("bias", "neutral"))
        detail = str(raw.get("detail", ""))
        if detail:
            detail += " (cached)"
        else:
            detail = "cached futures bias"
        return bias, detail
    except Exception:
        return None


def _save_cached_futures_bias(bias: FuturesBias, detail: str) -> None:
    try:
        FUTURES_CACHE.write_text(
            json.dumps({
                "bias": bias.value,
                "detail": detail,
                "updated": time.time(),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


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
            bias = FuturesBias.GREEN
        elif pct <= -0.3:
            bias = FuturesBias.RED
        else:
            bias = FuturesBias.NEUTRAL
        _save_cached_futures_bias(bias, detail)
        return bias, detail
    except Exception as exc:
        cached = _load_cached_futures_bias()
        if cached is not None:
            return cached
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
    premarket_gap_pct: float = 0.0  # live pre-market gap from previous close

    @property
    def confidence(self) -> str:
        if self.score >= 80:
            return "HIGH"
        elif self.score >= 45:
            return "MEDIUM"
        else:
            return "LOW"

    @property
    def confidence_emoji(self) -> str:
        if self.score >= 80:
            return "HIGH (fire)"
        elif self.score >= 45:
            return "MEDIUM (ok)"
        else:
            return "LOW (warn)"


# ──────────────────────────────────────────────────────────────────────────────
# Main strategy — 8 criteria
# ──────────────────────────────────────────────────────────────────────────────

class GrinderStrategy:
    """
    8-criteria main strategy (US stocks / NYSE / NASDAQ):
      1. Price $1.00-$1000            - covers penny to large cap
      2. 20-day avg volume >= 500,000 - liquid enough to enter/exit
      3. Yesterday % change +1.5-+15% - real momentum, not noise
      4. Rel. volume >= 1.5x          - elevated = institutional conviction
      5. ATR(14) >= 1.0% of price     - room to run 1-3 %
      6. Close > 20-day EMA           - confirmed medium-term uptrend
      7. Close > 5-day EMA            - short-term trend intact
      8. Close strength >= 0.40       - closed in upper 60 % of day range

    Score = yesterday_pct x rel_volume^1.5 x atr_pct x (1 + close_strength)
    """

    MIN_PRICE     = 1.00
    MAX_PRICE     = 1000.00
    MIN_MARKET_CAP = 100_000_000
    MIN_AVG_VOL   = 500_000
    MIN_YDAY_VOL  = 200_000
    MIN_PCT_CHG   = 1.5
    MAX_PCT_CHG   = 15.0
    MIN_REL_VOL   = 1.5
    MIN_ATR_PCT   = 1.0
    MIN_CLOSE_STR = 0.40
    MARKET_CAP_SCAN_LIMIT = 120

    def __init__(self, market_data: Optional[GrinderMarketData] = None) -> None:
        self.market_data = market_data or GrinderMarketData()

    def scan(self, watchlist: list[str]) -> list[GrinderPick]:
        picks: list[GrinderPick] = []
        for symbol in watchlist:
            snap = self.market_data.snapshot(symbol)
            if snap is None:
                continue
            pick = self._qualify_core(snap)
            if pick is not None:
                picks.append(pick)
        picks.sort(key=lambda p: p.score, reverse=True)
        return self._filter_market_cap(picks)

    def _qualify_core(self, snap: GrinderSnapshot) -> Optional[GrinderPick]:
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

    def _filter_market_cap(self, picks: list[GrinderPick]) -> list[GrinderPick]:
        filtered: list[GrinderPick] = []
        for pick in picks[: self.MARKET_CAP_SCAN_LIMIT]:
            market_cap = self.market_data.market_cap(pick.symbol)
            if market_cap is None or market_cap < self.MIN_MARKET_CAP:
                continue
            filtered.append(pick)
        return filtered


# ──────────────────────────────────────────────────────────────────────────────
# Fallback — fires when main finds nothing
# ──────────────────────────────────────────────────────────────────────────────

class FallbackStrategy:
    """
    Relaxed momentum fallback (US stocks):
      1. Price $1.00-$1000
      2. 20-day avg vol >= 200,000
      3. Yesterday % change +1.0-+20%
      4. Rel. volume >= 1.0x
      5. Close > 20-day EMA
    Score = yesterday_pct x rel_volume x (1 + close_strength)
    """

    MIN_PRICE   = 1.00
    MAX_PRICE   = 1000.00
    MIN_MARKET_CAP = 100_000_000
    MIN_AVG_VOL = 200_000
    MIN_YDAY_VOL = 100_000
    MIN_PCT_CHG = 1.0
    MAX_PCT_CHG = 20.0
    MIN_REL_VOL = 1.0
    MARKET_CAP_SCAN_LIMIT = 120

    def __init__(self, market_data: Optional[GrinderMarketData] = None) -> None:
        self.market_data = market_data or GrinderMarketData()

    def scan(self, watchlist: list[str]) -> list[GrinderPick]:
        picks: list[GrinderPick] = []
        for symbol in watchlist:
            snap = self.market_data.snapshot(symbol)
            if snap is None:
                continue
            pick = self._qualify_core(snap)
            if pick is not None:
                picks.append(pick)
        picks.sort(key=lambda p: p.score, reverse=True)
        return self._filter_market_cap(picks)

    def _qualify_core(self, snap: GrinderSnapshot) -> Optional[GrinderPick]:
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

    def _filter_market_cap(self, picks: list[GrinderPick]) -> list[GrinderPick]:
        filtered: list[GrinderPick] = []
        for pick in picks[: self.MARKET_CAP_SCAN_LIMIT]:
            market_cap = self.market_data.market_cap(pick.symbol)
            if market_cap is None or market_cap < self.MIN_MARKET_CAP:
                continue
            filtered.append(pick)
        return filtered


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
            if snap.yesterday_volume < 200_000:
                continue
            market_cap = self.market_data.market_cap(symbol)
            if market_cap is None or market_cap < 100_000_000:
                continue
            candidates.append(snap)

        if not candidates:
            return []

        positives = [s for s in candidates if s.yesterday_pct_change > 0]
        pool = positives if positives else candidates
        if not pool:
            return []
        best = max(pool, key=lambda s: s.score)
        # Refuse to deploy into a flat/negative market — cash is better
        if best.yesterday_pct_change <= 0:
            return []

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


# ──────────────────────────────────────────────────────────────────────────────
# Smart Strategy — composite 5-signal screener (primary tier)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SmartSignals:
    """Per-ticker signals from 1-year OHLCV history — 9 indicators from 4 quant repos."""
    pct_5d:       float   # 5-day price return %
    pct_20d:      float   # 20-day price return %
    high_20d:     float   # 20-day high (breakout reference)
    vol_trend:    float   # 5d avg vol / 20d avg vol (>1 = rising)
    obv_score:    float   # volume-weighted up/down, -1 to +1
    # ── New: from Minervini + IBKR + CANSLIM research ─────────────────────
    rsi14:        float   # RSI(14) — 0-100
    macd_diff:    float   # MACD line − signal line (>0 = bullish)
    macd_crossed: bool    # MACD crossed above signal in last 3 bars
    sma50:        float   # 50-day SMA
    sma150:       float   # 150-day SMA (Minervini Stage 2)
    sma200:       float   # 200-day SMA (Minervini Stage 2)
    high_52w:     float   # 52-week high (CANSLIM "N" — leadership proxy)
    vol_1yr_ratio: float  # yesterday_vol / 1yr max vol (>1.0 = 1yr volume record)
    consec_green:  int    # consecutive up-close days (2–3 = sweet spot, 6+ = extended)


def _calc_rsi(closes: "np.ndarray", period: int = 14) -> float:
    """RSI(period) — returns 50.0 if insufficient data."""
    import numpy as np
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 10):])
    gains  = np.where(deltas > 0, deltas,  0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return float(100 - 100 / (1 + rs))


def _calc_macd(closes: "np.ndarray") -> tuple[float, bool]:
    """MACD(12,26,9) → (macd_diff, crossed_bullish_last_3_bars)."""
    import pandas as pd
    if len(closes) < 35:
        return 0.0, False
    s = pd.Series(closes)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    diff  = macd - sig
    diff_arr = diff.values
    macd_diff   = float(diff_arr[-1])
    # Bullish crossover = diff went from negative to positive in last 3 bars
    crossed = any(
        diff_arr[-(i+2)] <= 0 and diff_arr[-(i+1)] > 0
        for i in range(min(3, len(diff_arr) - 1))
    )
    return macd_diff, crossed


def _build_smart_signals(df: pd.DataFrame) -> SmartSignals:
    import numpy as np
    df  = df.dropna(subset=["Close", "High", "Volume"])
    n   = len(df)
    cls = df["Close"].values.astype(float)
    hig = df["High"].values.astype(float)
    vol = df["Volume"].values.astype(float)

    # ── Core momentum (existing) ───────────────────────────────────────────
    pct_5d   = float((cls[-1] - cls[-6])  / cls[-6]  * 100) if n >= 7  else 0.0
    pct_20d  = float((cls[-1] - cls[-21]) / cls[-21] * 100) if n >= 22 else 0.0
    high_20d = float(hig[-20:].max())  if n >= 20 else float(cls[-1])

    vol_5d    = float(vol[-5:].mean())  if n >= 5  else float(vol.mean())
    vol_20d   = float(vol[-20:].mean()) if n >= 20 else float(vol.mean())
    vol_trend = vol_5d / vol_20d if vol_20d > 0 else 1.0

    days = min(10, n - 1)
    vw_sum = vw_tot = 0.0
    for i in range(-days, 0):
        direction = 1 if cls[i] > cls[i - 1] else (-1 if cls[i] < cls[i - 1] else 0)
        vw_sum += direction * vol[i]
        vw_tot += vol[i]
    obv_score = vw_sum / vw_tot if vw_tot > 0 else 0.0

    # ── New indicators from 4-repo research ───────────────────────────────
    rsi14       = _calc_rsi(cls)
    macd_diff, macd_crossed = _calc_macd(cls)

    sma50  = float(cls[-50:].mean())  if n >= 50  else float(cls.mean())
    sma150 = float(cls[-150:].mean()) if n >= 150 else float(cls.mean())
    sma200 = float(cls[-200:].mean()) if n >= 200 else float(cls.mean())

    high_52w = float(hig[-252:].max()) if n >= 60 else float(hig.max())

    vol_252   = float(vol[-252:].max()) if n >= 60 else float(vol.max())
    vol_1yr_ratio = float(vol[-1]) / vol_252 if vol_252 > 0 else 0.0

    # Consecutive up-close days (extension detector)
    consec_green = 0
    for i in range(-1, -min(10, n), -1):
        if cls[i] > cls[i - 1]:
            consec_green += 1
        else:
            break

    return SmartSignals(
        pct_5d=pct_5d, pct_20d=pct_20d, high_20d=high_20d,
        vol_trend=vol_trend, obv_score=obv_score,
        rsi14=rsi14, macd_diff=macd_diff, macd_crossed=macd_crossed,
        sma50=sma50, sma150=sma150, sma200=sma200,
        high_52w=high_52w, vol_1yr_ratio=vol_1yr_ratio,
        consec_green=consec_green,
    )


def _fetch_yahoo_trending() -> set:
    """Try to fetch trending US tickers from Yahoo Finance screener (best-effort)."""
    trending: set = set()
    urls = [
        ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
         "?formatted=true&lang=en-US&region=US&scrIds=day_gainers&count=25"),
        ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
         "?formatted=true&lang=en-US&region=US&scrIds=most_actives&count=25"),
    ]
    hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json",
    }
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                for q in (data.get("finance", {})
                              .get("result", [{}])[0]
                              .get("quotes", [])):
                    sym = str(q.get("symbol", ""))
                    # US stocks — no exchange suffix
                    if sym and "." not in sym:
                        trending.add(sym)
        except Exception:
            pass
    return trending


@dataclass
class SmartMarketContext:
    """SPY + sector + VIX context fetched once per scan — includes regime gate."""
    spy_5d_pct:       float
    sector_returns:   dict   # ETF symbol → 5d return %
    trending:         set    # Yahoo Finance trending US symbols
    spy_above_sma50:  bool   # SPY healthy short-term (Minervini regime gate)
    spy_above_sma200: bool   # SPY in long-term uptrend
    spy_sma50:        float
    spy_sma200:       float
    spy_price:        float
    vix_level:        float = 0.0  # CBOE VIX — volatility regime gate

    @property
    def regime_multiplier(self) -> float:
        """Combined SPY trend + VIX volatility regime multiplier."""
        # SPY trend regime (Minervini)
        if self.spy_above_sma50 and self.spy_above_sma200:
            spy_mult = 1.0
        elif self.spy_above_sma200:
            spy_mult = 0.85
        else:
            spy_mult = 0.70

        # VIX volatility regime — high VIX = whipsaw, kills intraday +10% targets
        vix = self.vix_level
        if vix >= 30:
            vix_mult = 0.55   # panic/crisis — extremely selective
        elif vix >= 25:
            vix_mult = 0.70   # elevated fear — hard to hit +10% cleanly
        elif vix >= 20:
            vix_mult = 0.88   # mild anxiety — slight caution
        elif 0 < vix <= 14:
            vix_mult = 1.12   # complacency — momentum runs clean
        else:
            vix_mult = 1.0    # normal (VIX 14-20)

        return spy_mult * vix_mult

    @classmethod
    def load_or_fetch(cls, max_age: int = 7_200) -> "SmartMarketContext":
        if SMART_CONTEXT_CACHE.exists():
            try:
                if time.time() - SMART_CONTEXT_CACHE.stat().st_mtime < max_age:
                    raw = json.loads(SMART_CONTEXT_CACHE.read_text(encoding="utf-8"))
                    return cls(
                        spy_5d_pct=float(raw["spy_5d_pct"]),
                        sector_returns={k: float(v) for k, v in raw["sector_returns"].items()},
                        trending=set(raw.get("trending", [])),
                        spy_above_sma50=bool(raw.get("spy_above_sma50", True)),
                        spy_above_sma200=bool(raw.get("spy_above_sma200", True)),
                        spy_sma50=float(raw.get("spy_sma50", 0)),
                        spy_sma200=float(raw.get("spy_sma200", 0)),
                        spy_price=float(raw.get("spy_price", 0)),
                        vix_level=float(raw.get("vix_level", 0.0)),
                    )
            except Exception:
                pass
        return cls._fetch()

    @classmethod
    def _fetch(cls) -> "SmartMarketContext":
        spy_pct = 0.0
        sectors: dict = {}
        trending: set = set()
        spy_above_sma50 = True
        spy_above_sma200 = True
        spy_sma50 = spy_sma200 = spy_price = 0.0
        vix_level = 0.0

        syms = ["SPY"] + _SECTOR_ETFS
        try:
            raw = yf.download(
                syms, period="1y", interval="1d",
                auto_adjust=False, progress=False,
                group_by="ticker", threads=False, timeout=20,
            )
            for sym in syms:
                try:
                    df = _extract_ticker_frame(raw, sym).dropna(subset=["Close"])
                    if len(df) < 6:
                        continue
                    c = df["Close"].values.astype(float)
                    pct = float((c[-1] - c[-6]) / c[-6] * 100)
                    if sym == "SPY":
                        spy_pct = pct
                        spy_price  = float(c[-1])
                        spy_sma50  = float(c[-50:].mean())  if len(c) >= 50  else spy_price
                        spy_sma200 = float(c[-200:].mean()) if len(c) >= 200 else spy_price
                        spy_above_sma50  = spy_price > spy_sma50
                        spy_above_sma200 = spy_price > spy_sma200
                    else:
                        sectors[sym] = pct
                except Exception:
                    pass
        except Exception:
            pass

        # VIX — volatility regime gate
        try:
            vix_raw = yf.download(
                "^VIX", period="5d", interval="1d",
                auto_adjust=False, progress=False, timeout=10,
            )
            if not vix_raw.empty:
                vix_close = vix_raw["Close"] if "Close" in vix_raw.columns else vix_raw.iloc[:, 0]
                vix_level = float(vix_close.dropna().iloc[-1])
        except Exception:
            pass

        try:
            trending = _fetch_yahoo_trending()
        except Exception:
            pass

        ctx = cls(
            spy_5d_pct=spy_pct, sector_returns=sectors, trending=trending,
            spy_above_sma50=spy_above_sma50, spy_above_sma200=spy_above_sma200,
            spy_sma50=spy_sma50, spy_sma200=spy_sma200, spy_price=spy_price,
            vix_level=vix_level,
        )
        try:
            SMART_CONTEXT_CACHE.write_text(json.dumps({
                "spy_5d_pct": spy_pct,
                "sector_returns": sectors,
                "trending": list(trending),
                "spy_above_sma50":  spy_above_sma50,
                "spy_above_sma200": spy_above_sma200,
                "spy_sma50":   spy_sma50,
                "spy_sma200":  spy_sma200,
                "spy_price":   spy_price,
                "vix_level":   vix_level,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass
        return ctx


# ──────────────────────────────────────────────────────────────────────────────
# Sector map — symbol → sector ETF (XLK/XLF/XLE/XLV/XLI/XLY)
# Used by SmartGrinderStrategy to boost picks in hot sectors.
# ──────────────────────────────────────────────────────────────────────────────

_SECTOR_MAP: dict[str, str] = {
    **{s: "XLK" for s in [
        "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "AVGO", "QCOM",
        "MU", "MRVL", "AMAT", "LRCX", "KLAC", "INTC", "TXN", "ON", "MCHP",
        "SMCI", "ARM", "SLAB", "CRM", "NOW", "SNOW", "PLTR", "ORCL", "ADBE",
        "WDAY", "TEAM", "PANW", "CRWD", "ZS", "FTNT", "DDOG", "NET", "CYBR",
        "S", "DELL", "HPE", "IONQ", "RGTI", "QUBT", "SNPS", "CDNS", "TER",
        "MPWR", "NXPI", "SWKS", "QRVO", "AKAM", "JNPR", "NTAP", "STX", "WDC",
    ]},
    **{s: "XLF" for s in [
        "V", "MA", "PYPL", "SQ", "JPM", "BAC", "GS", "MS", "C", "WFC",
        "BX", "BLK", "SCHW", "IBKR", "RJF", "COIN", "HOOD", "SOFI", "AFRM",
        "MARA", "RIOT", "CLSK", "HUT", "CIFR", "COF", "AXP", "DFS", "SYF",
        "BK", "STT", "NTRS", "TROW", "IVZ", "BEN", "AIG", "MET", "PRU",
        "AFL", "ALL", "TRV", "CB", "PGR", "CINF", "EG", "RE", "WRB",
    ]},
    **{s: "XLE" for s in [
        "XOM", "CVX", "COP", "OXY", "MRO", "DVN", "FANG", "HES", "EOG",
        "SLB", "HAL", "BKR", "NOG", "SM", "PLUG", "NRG", "AES", "NEE",
        "DUK", "SO", "D", "EXC", "XEL", "SRE", "ES", "ATO", "LNT",
        "PNW", "CNP", "NI", "EIX", "PPL", "PEG", "WEC", "CMS",
    ]},
    **{s: "XLV" for s in [
        "LLY", "NVO", "ABBV", "MRK", "PFE", "BMY", "AMGN", "GILD", "REGN",
        "VRTX", "MRNA", "BNTX", "BIIB", "ALNY", "EXAS", "RXRX", "ACHR",
        "JNJ", "ABT", "MDT", "BSX", "EW", "SYK", "ISRG", "BDX", "DHR",
        "TMO", "IQV", "CNC", "UNH", "HCA", "CVS", "MCK", "CI", "ANTM",
        "HOLX", "PKI", "IDXX", "MTD", "BMRN", "BIIB", "INCY", "REGN",
    ]},
    **{s: "XLI" for s in [
        "GE", "CAT", "BA", "RTX", "LMT", "NOC", "DE", "HON", "MMM",
        "EMR", "ITW", "ROK", "PH", "ETN", "IR", "OTIS", "CARR",
        "LUNR", "RKLB", "F", "GM", "RIVN", "LCID", "NIO", "LI", "XPEV",
        "ENPH", "FSLR", "UPS", "FDX", "CSX", "UNP", "NSC", "WAB",
        "JBHT", "CHRW", "EXPD", "XYL", "ROP", "FAST", "GD", "HII",
        "LDOS", "ACM", "PWR", "VMC", "MLM", "SWK", "TXT", "HWM",
    ]},
    **{s: "XLY" for s in [
        "TSLA", "WMT", "COST", "TGT", "HD", "LOW", "NKE", "DIS",
        "NFLX", "UBER", "LYFT", "DASH", "BKNG", "ABNB", "RBLX", "EA",
        "TTWO", "SPOT", "GME", "AMC", "SPCE", "WKHS", "BBBY",
        "MCD", "SBUX", "YUM", "DPZ", "DRI", "CHTR", "CMCSA",
        "NWS", "FOXA", "PARA", "WBD", "DIS", "NFLX", "TTWO",
        "RL", "PVH", "TPR", "VFC", "HAS", "MAT", "NKE",
        "ULTA", "TJX", "ROST", "KMX", "AN", "AZO", "ORLY",
        "LVS", "WYNN", "MGM", "CZR", "RCL", "CCL", "NCLH", "MAR", "HLT",
    ]},
}


# ──────────────────────────────────────────────────────────────────────────────
# Earnings blackout — skip stocks with earnings within next 3 days
# ──────────────────────────────────────────────────────────────────────────────

_earnings_mem: dict = {}   # in-memory cache (lives for process lifetime)
_earnings_mem_loaded = False

def _load_earnings_mem() -> None:
    global _earnings_mem, _earnings_mem_loaded
    if _earnings_mem_loaded:
        return
    _earnings_mem_loaded = True
    if EARNINGS_CACHE.exists():
        try:
            if time.time() - EARNINGS_CACHE.stat().st_mtime < 12 * 3600:
                _earnings_mem = json.loads(EARNINGS_CACHE.read_text())
        except Exception:
            pass

def _save_earnings_mem() -> None:
    try:
        EARNINGS_CACHE.write_text(json.dumps(_earnings_mem, indent=2))
    except Exception:
        pass

def _is_earnings_blackout(symbol: str, window_days: int = 3) -> bool:
    """Return True if earnings are within the next window_days calendar days."""
    from datetime import date as _date
    _load_earnings_mem()
    entry = _earnings_mem.get(symbol, {})
    if entry and time.time() - entry.get("ts", 0) < 12 * 3600:
        raw = entry.get("next_earnings")
        if not raw:
            return False
        try:
            delta = (_date.fromisoformat(raw) - _date.today()).days
            return 0 <= delta <= window_days
        except Exception:
            return False

    next_earnings = None
    try:
        dates_df = yf.Ticker(symbol).get_earnings_dates(limit=4)
        if dates_df is not None and not dates_df.empty:
            today = _date.today()
            for dt_idx in sorted(dates_df.index):
                try:
                    d = dt_idx.date() if hasattr(dt_idx, "date") else \
                        __import__("pandas").Timestamp(dt_idx).date()
                    if d >= today:
                        next_earnings = d.isoformat()
                        break
                except Exception:
                    continue
    except Exception:
        pass

    _earnings_mem[symbol] = {"next_earnings": next_earnings, "ts": time.time()}
    _save_earnings_mem()
    if not next_earnings:
        return False
    try:
        delta = (_date.fromisoformat(next_earnings) - _date.today()).days
        return 0 <= delta <= window_days
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Short squeeze radar — short % of float + momentum = squeeze setup
# ──────────────────────────────────────────────────────────────────────────────

_short_mem: dict = {}
_short_mem_loaded = False

def _load_short_mem() -> None:
    global _short_mem, _short_mem_loaded
    if _short_mem_loaded:
        return
    _short_mem_loaded = True
    if SHORT_CACHE.exists():
        try:
            if time.time() - SHORT_CACHE.stat().st_mtime < 24 * 3600:
                _short_mem = json.loads(SHORT_CACHE.read_text())
        except Exception:
            pass

def _save_short_mem() -> None:
    try:
        SHORT_CACHE.write_text(json.dumps(_short_mem, indent=2))
    except Exception:
        pass

def _get_float_info(symbol: str) -> tuple[float, float]:
    """Return (short_pct, float_shares). short_pct is 0.0–1.0. Cached 24h."""
    _load_short_mem()
    entry = _short_mem.get(symbol, {})
    if entry and time.time() - entry.get("ts", 0) < 24 * 3600:
        return float(entry.get("short_pct", 0.0)), float(entry.get("float_shares", 0.0))

    short_pct = 0.0
    float_shares = 0.0
    try:
        info = yf.Ticker(symbol).get_info()
        val = info.get("shortPercentOfFloat")
        if val is not None:
            short_pct = float(val)
        fv = info.get("floatShares")
        if fv is not None:
            float_shares = float(fv)
    except Exception:
        pass

    _short_mem[symbol] = {"short_pct": short_pct, "float_shares": float_shares, "ts": time.time()}
    _save_short_mem()
    return short_pct, float_shares


def _squeeze_bonus(symbol: str, yesterday_pct: float) -> float:
    """Score bonus: high short interest + small float + momentum = explosive squeeze."""
    if yesterday_pct < 2.0:
        return 0.0
    sp, float_shares = _get_float_info(symbol)
    if sp >= 0.30:   base = 8.0   # >30% short float = prime squeeze
    elif sp >= 0.20: base = 5.0   # >20% short float
    elif sp >= 0.10: base = 2.0   # modest short interest
    else:            return 0.0
    # Small float amplifies squeeze violence
    if 0 < float_shares <= 10_000_000:
        return base * 2.0   # tiny float: max pain for shorts
    if float_shares <= 50_000_000:
        return base * 1.3   # small float: meaningful squeeze potential
    return base


class SmartGrinderStrategy:
    """
    Primary screener — 14-signal composite score (0-~140 pts).

    Signals (from 4 quant repos — IBKR, Minervini, CANSLIM, LangChain):
      A. Momentum alignment  — 1d/5d/20d alignment                    (0-25 pts)
      B. MACD(12,26,9)       — bullish crossover / above signal        (0-12 pts)
      C. RSI(14) zone        — 45-70 = momentum, <35 = bounce         (0-10 pts)
      D. Stage 2 MA align    — Price>SMA50>SMA150>SMA200 [Minervini]  (0-12 pts)
      E. Volume conviction   — rel vol + trend + 1yr breakthrough      (0-18 pts)
      F. 52-week proximity   — within 20% of 52-week high [CANSLIM]   (0-10 pts)
      G. Rel strength SPY    — outperforms SPY 5d return              (0-8 pts)
      H. OBV smart money     — volume-weighted up/down                (0-5 pts)
      I. Bonuses             — close quality + ATR + trending          (0-10 pts)
      J. Sector alignment    — stock in top-performing sector          (0-5 pts)
      K. Earnings blackout   — hard filter: skip if earnings ≤3 days  (filter)
      L. Short squeeze       — float-adjusted short interest bonus     (0-16 pts)
      M. Live gap            — pre-market/intraday gap vs yesterday    (-15 to +12 pts)
      N. Consecutive days    — 2-3 green = sweet spot, 6+ = filtered  (-8 to +6 pts)

    Regime gate: SPY×VIX combined multiplier (0.55–1.12)
    Hard filters: price $1–$1000 | avg vol ≥100k | yesterday ≥+0.5% | above EMA20
                  6+ consecutive green days = extended (skipped)
    Confidence: HIGH ≥80 | MEDIUM ≥45 | LOW <45
    """

    MIN_PRICE      = 1.00
    MAX_PRICE      = 1000.00
    MIN_AVG_VOL    = 100_000  # US stocks are liquid — low bar, score handles quality
    MIN_YDAY_VOL   = 50_000
    MIN_PCT_CHG    = 0.5
    SCAN_LIMIT     = 200

    def __init__(
        self,
        market_data: Optional[GrinderMarketData] = None,
        ctx: Optional[SmartMarketContext] = None,
    ) -> None:
        self.md  = market_data or GrinderMarketData()
        self.ctx = ctx or SmartMarketContext.load_or_fetch()

    def scan(self, watchlist: list) -> list:
        scored: list = []
        for sym in watchlist:
            snap = self.md.snapshot(sym)
            if snap is None or not self._base_ok(snap):
                continue
            df = self.md.get_frame(sym)
            if df is None or len(df) < 7:
                continue
            sig = _build_smart_signals(df)
            if sig.consec_green >= 6:
                continue  # hard filter: 6+ consecutive green days = extended
            score = self._score(snap, sig)
            if score > 0:
                scored.append((score, snap, sig))

        scored.sort(key=lambda x: x[0], reverse=True)

        # ── Earnings blackout + float-adjusted squeeze (top 50 only) ──────
        enriched: list = []
        for score, snap, sig in scored[:50]:
            if _is_earnings_blackout(snap.symbol):
                continue  # hard filter: skip earnings week
            score += _squeeze_bonus(snap.symbol, snap.yesterday_pct_change)
            enriched.append((score, snap, sig))
        # Append remainder (rank 51+) without enrichment as fallback pool
        combined = sorted(
            enriched + scored[50:],
            key=lambda x: x[0], reverse=True,
        )

        # ── Live/pre-market gap enrichment (all candidates — parallel prepost history) ─
        def _fetch_gap(sym: str, lc: float) -> tuple[str, float | None]:
            try:
                df = yf.Ticker(sym).history(period="1d", interval="5m", prepost=True)
                if df.empty:
                    return sym, None
                live = float(df["Close"].iloc[-1])
                if live > 0 and lc > 0:
                    return sym, (live - lc) / lc * 100
            except Exception:
                pass
            return sym, None

        gaps: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            fut_map = {
                ex.submit(_fetch_gap, snap.symbol, snap.last_close): snap.symbol
                for _, snap, _ in combined
            }
            for f in as_completed(fut_map):
                try:
                    sym, gap_val = f.result()
                    if gap_val is not None:
                        gaps[sym] = gap_val
                except Exception:
                    pass

        gap_adjusted: list = []
        for score, snap, sig in combined:
            gap_pct = gaps.get(snap.symbol)
            if gap_pct is not None:
                if   gap_pct >= 5.0:
                    score += 20.0   # explosive intraday momentum
                elif gap_pct >= 3.0:
                    score += 14.0   # strong continuation
                elif gap_pct >= 1.5:
                    score += 9.0    # moderate intraday mover
                elif gap_pct >= 0.5:
                    score += 5.0    # slight positive drift
                elif gap_pct <= -1.0:
                    score = max(0.0, score - 18.0)  # momentum reversed — penalise
                elif gap_pct <= -0.5:
                    score = max(0.0, score - 10.0)  # slight negative drift
            gap_adjusted.append((score, snap, sig, gap_pct or 0.0))
        combined = sorted(gap_adjusted, key=lambda x: x[0], reverse=True)

        out: list = []
        for score, snap, _sig, gap_pct in combined[:self.SCAN_LIMIT]:
            out.append(GrinderPick(
                symbol           = snap.symbol,
                last_close       = snap.last_close,
                score            = round(score, 2),
                yesterday_pct    = snap.yesterday_pct_change,
                rel_volume       = snap.rel_volume,
                atr_pct          = snap.atr_pct,
                close_strength   = snap.close_strength,
                above_ema5       = snap.last_close > snap.ema5,
                above_ema20      = True,
                strategy_name    = "Smart Strategy",
                premarket_gap_pct= round(gap_pct, 2),
            ))
        return out

    def _base_ok(self, snap: GrinderSnapshot) -> bool:
        return (
            self.MIN_PRICE <= snap.last_close <= self.MAX_PRICE
            and snap.avg_volume_20 >= self.MIN_AVG_VOL
            and snap.yesterday_volume >= self.MIN_YDAY_VOL
            and snap.yesterday_pct_change >= self.MIN_PCT_CHG
            and snap.last_close > snap.ema20
        )

    def _score(self, snap: GrinderSnapshot, sig: SmartSignals) -> float:
        s = 0.0
        price = snap.last_close

        # A — Momentum cascade (0-25) ─────────────────────────────────────
        s += min(12.0, snap.yesterday_pct_change * 1.2)    # 1-day (max 12 @ +10%)
        if sig.pct_5d  > 0: s += 7.0                       # 5-day confirmed
        if sig.pct_20d > 0: s += 6.0                       # 20-day confirmed

        # B — MACD(12,26,9) (0-12) ────────────────────────────────────────
        if sig.macd_crossed:
            s += 12.0                                       # fresh bullish crossover
        elif sig.macd_diff > 0:
            s += 6.0                                        # above signal, no cross yet

        # C — RSI(14) zone (0-10) ─────────────────────────────────────────
        rsi = sig.rsi14
        if   45 <= rsi <= 70: s += 10.0                    # momentum zone (not overbought)
        elif 35 <= rsi <  45: s +=  5.0                    # building momentum
        elif 30 <= rsi <  35: s +=  3.0                    # oversold bounce candidate
        # >70 = overbought caution → 0 pts; <30 deep oversold → 0 pts

        # D — Minervini Stage 2 MA alignment (0-12) ───────────────────────
        if sig.sma50 > 0 and sig.sma150 > 0 and sig.sma200 > 0:
            if price > sig.sma50 > sig.sma150 > sig.sma200:
                s += 12.0                                   # full Stage 2 (best setup)
            elif price > sig.sma50 > sig.sma200:
                s +=  7.0                                   # partial alignment
            elif price > sig.sma50:
                s +=  3.0                                   # short-term trend intact

        # E — Volume conviction (0-18) ────────────────────────────────────
        s += min(8.0, snap.rel_volume * 2.0)                # rel vol (4x = 8 pts)
        s += min(5.0, max(0.0, (sig.vol_trend - 1.0) * 5.0))  # rising vol trend
        if sig.vol_1yr_ratio >= 1.0:
            s += 5.0                                        # 1-year volume record!

        # F — 52-week high proximity / CANSLIM "N" (0-10) ────────────────
        if sig.high_52w > 0 and price > 0:
            pct_from_high = (sig.high_52w - price) / sig.high_52w
            if   pct_from_high <= 0.02: s += 10.0          # at/breaking 52w high
            elif pct_from_high <= 0.10: s +=  7.0          # within 10% of high
            elif pct_from_high <= 0.20: s +=  4.0          # within 20% of high
            elif pct_from_high <= 0.30: s +=  1.0          # still in striking range

        # G — Relative strength vs SPY (0-8) ─────────────────────────────
        rs = sig.pct_5d - self.ctx.spy_5d_pct
        if   rs > 5: s += 8.0
        elif rs > 2: s += 5.0
        elif rs > 0: s += 3.0

        # H — OBV smart money (0-5) ───────────────────────────────────────
        s += max(0.0, sig.obv_score * 5.0)

        # I — Bonuses: close quality + ATR + trending (0-10) ─────────────
        s += snap.close_strength * 3.5                      # max 3.5
        s += min(2.5, snap.atr_pct * 0.5)
        if snap.symbol in self.ctx.trending:
            s += 4.0

        # J — Sector alignment: stock in a hot sector (0-5) ──────────────
        sector = _SECTOR_MAP.get(snap.symbol)
        if sector:
            sector_ret = self.ctx.sector_returns.get(sector, 0.0)
            if   sector_ret >= 4.0: s += 5.0   # sector on fire this week
            elif sector_ret >= 2.0: s += 3.0   # sector trending well
            elif sector_ret >= 0.0: s += 1.0   # sector positive

        # N — Consecutive green days: sweet spot 2-3, penalise 5+ (extended)
        cg = sig.consec_green
        if   cg == 2: s += 4.0   # early in move — ideal entry
        elif cg == 3: s += 6.0   # confirmed 3-day trend — sweet spot
        elif cg == 4: s -= 3.0   # getting extended
        elif cg >= 5: s -= 8.0   # overextended — likely to reverse

        # Market regime gate (Minervini + VIX): never fight the tape ─────
        s *= self.ctx.regime_multiplier

        return s


# ──────────────────────────────────────────────────────────────────────────────
# PennyExplosiveStrategy — intraday 100%+ potential scanner (FYI only)
# ──────────────────────────────────────────────────────────────────────────────

class PennyExplosiveStrategy:
    """
    Explosive penny/small-cap scanner — 10-signal composite (0-100 pts).
    Finds the best intraday or 1-3 day explosive candidate for manual trading.
    FYI only — bot will NOT auto-buy these.

    Signals:
      A. Yesterday momentum  — tiered by % gain                    (0-25 pts)
      B. Volume conviction   — tiered RVOL + vol_trend bonus       (0-20 pts)
      C. Short squeeze fuel  — short float % + momentum catalyst   (0-15 pts)
      D. MACD signal         — fresh crossover or bullish          (0-10 pts)
      E. Consecutive green   — multi-day accumulation pattern      (0-10 pts)
      F. Close strength      — buyers held the close (no fade)     (0-8 pts)
      G. RS vs SPY 5d        — outperforming the market this week  (0-5 pts)
      H. ATR quality         — wide daily range = real movement    (0-4 pts)
      I. OBV smart money     — volume-weighted up/down direction   (0-2 pts)
      J. Yahoo trending      — retail/algo attention catalyst      (0-1 pt)
    Short squeeze enrichment on top 20 candidates.
    VIX gate: skip when VIX >= 28 (panic mode destroys penny plays).
    """

    MIN_PRICE         = 0.30
    MAX_PRICE         = 20.0   # extended to catch small caps up to $20
    MIN_AVG_VOL       = 100_000
    MIN_YDAY_VOL      = 50_000
    MIN_PCT_CHG       = 3.0    # lowered to cast wider net; score sorts quality
    MIN_CLOSE_STR     = 0.35
    MAX_VIX           = 28.0

    def __init__(
        self,
        market_data: Optional[GrinderMarketData] = None,
        ctx: Optional[SmartMarketContext] = None,
    ) -> None:
        self.md  = market_data or GrinderMarketData()
        self.ctx = ctx or SmartMarketContext.load_or_fetch()

    def scan(self, watchlist: list) -> "dict | None":
        """Return the best explosive pick as a dict, or None."""
        if self.ctx.vix_level >= self.MAX_VIX:
            return None

        candidates: list = []
        for sym in watchlist:
            snap = self.md.snapshot(sym)
            if snap is None or not self._base_ok(snap):
                continue
            df = self.md.get_frame(sym)
            if df is None or len(df) < 7:
                continue
            sig = _build_smart_signals(df)
            score = self._score(snap, sig)
            if score > 0:
                candidates.append((score, snap, sig))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)

        # Short squeeze enrichment + earnings blackout (top 20 only)
        enriched: list = []
        for score, snap, sig in candidates[:20]:
            if _is_earnings_blackout(snap.symbol):
                continue
            score += _squeeze_bonus(snap.symbol, snap.yesterday_pct_change)
            enriched.append((score, snap, sig))

        if not enriched:
            enriched = candidates[:3]

        enriched.sort(key=lambda x: x[0], reverse=True)
        score, snap, sig = enriched[0]

        price = snap.last_close
        t1    = round(price * 1.20, 2)   # Target 1: +20%
        t2    = round(price * 1.50, 2)   # Target 2: +50%
        stop  = round(price * 0.90, 2)   # Stop: -10%

        # Dynamic hold note based on signal strength
        sq_pct, _ = _get_float_info(snap.symbol)
        is_squeeze  = sq_pct >= 0.20
        is_momentum = sig.consec_green >= 2 and snap.rel_volume >= 3.0
        if is_squeeze and is_momentum:
            hold_note = "🔥 Multi-day squeeze setup — hold 1-3 days. Trail stop 15% from peak once up +20%."
        elif sig.macd_crossed and sig.pct_5d > self.ctx.spy_5d_pct + 2:
            hold_note = "📅 Swing 1-3 days — MACD crossover + RS strength. Exit on +30% or loss of momentum."
        else:
            hold_note = "⚡ Day trade — exit by 3 PM ET. Trail stop 15% from peak once up +15%."

        signal_parts = []
        if sig.macd_crossed:
            signal_parts.append("MACD crossover 🔔")
        elif sig.macd_diff > 0:
            signal_parts.append("MACD bullish")
        if sig.consec_green >= 2:
            signal_parts.append(f"{sig.consec_green} green days")
        if sig.obv_score > 0.1:
            signal_parts.append("OBV accumulation")
        if is_squeeze:
            signal_parts.append(f"{sq_pct*100:.0f}% short float → squeeze risk")
        if snap.close_strength >= 0.80:
            signal_parts.append("closed at highs")
        rs = round(sig.pct_5d - self.ctx.spy_5d_pct, 1)
        if rs > 0:
            signal_parts.append(f"RS vs SPY +{rs:.1f}%")
        if snap.symbol in self.ctx.trending:
            signal_parts.append("Yahoo trending")
        if not signal_parts:
            signal_parts.append("volume + momentum surge")

        return {
            "symbol":        snap.symbol,
            "last_close":    round(price, 4),
            "atr_pct":       round(snap.atr_pct, 1),
            "rel_volume":    round(snap.rel_volume, 1),
            "yesterday_pct": round(snap.yesterday_pct_change, 1),
            "close_strength":round(snap.close_strength, 2),
            "consec_green":  sig.consec_green,
            "macd_crossed":  sig.macd_crossed,
            "rsi14":         round(sig.rsi14, 1),
            "score":         round(score, 1),
            "signals":       "  ·  ".join(signal_parts),
            "entry_note":    "9:31 AM ET open (day trade) or pullback to EMA5 (swing)",
            "entry_price":   price,
            "target1_pct":   20.0,
            "target1_price": t1,
            "target2_pct":   50.0,
            "target2_price": t2,
            "stop_pct":      -10.0,
            "stop_price":    stop,
            "hold_note":     hold_note,
        }

    def _base_ok(self, snap: GrinderSnapshot) -> bool:
        return (
            self.MIN_PRICE <= snap.last_close <= self.MAX_PRICE
            and snap.avg_volume_20 >= self.MIN_AVG_VOL
            and snap.yesterday_volume >= self.MIN_YDAY_VOL
            and snap.yesterday_pct_change >= self.MIN_PCT_CHG
            and snap.close_strength >= self.MIN_CLOSE_STR
        )

    def _score(self, snap: GrinderSnapshot, sig: SmartSignals) -> float:
        s = 0.0

        # A — Yesterday momentum (0-25) ──────────────────────────────────────
        pct = snap.yesterday_pct_change
        if   pct >= 100: s += 25.0
        elif pct >=  50: s += 21.0 + (pct -  50) /  50 * 4.0
        elif pct >=  25: s += 16.0 + (pct -  25) /  25 * 5.0
        elif pct >=  10: s += 10.0 + (pct -  10) /  15 * 6.0
        elif pct >=   5: s +=  5.0 + (pct -   5) /   5 * 5.0
        else:            s +=        (pct -   3.0)/   2 * 5.0

        # B — Volume conviction (0-20) ───────────────────────────────────────
        rv = snap.rel_volume
        if   rv >= 10: s += 20.0
        elif rv >=  5: s += 16.0
        elif rv >=  3: s += 11.0
        elif rv >=  2: s +=  6.0
        elif rv >= 1.5: s += 2.0
        if sig.vol_trend >= 1.5: s += 2.0
        elif sig.vol_trend >= 1.2: s += 1.0

        # C — Short squeeze fuel (0-15) ──────────────────────────────────────
        # Applied post-scan via _squeeze_bonus; pre-score gets 0 here
        # (keeps _score() pure; squeeze enrichment happens in scan())

        # D — MACD signal (0-10) ─────────────────────────────────────────────
        if sig.macd_crossed: s += 10.0
        elif sig.macd_diff > 0: s += 5.0

        # E — Consecutive green days (0-10) ──────────────────────────────────
        cg = sig.consec_green
        if   cg >= 4: s += 10.0
        elif cg == 3: s +=  8.0
        elif cg == 2: s +=  5.0
        elif cg == 1: s +=  2.0

        # F — Close strength (0-8) ───────────────────────────────────────────
        s += snap.close_strength * 8.0

        # G — RS vs SPY 5d (0-5) ─────────────────────────────────────────────
        rs = sig.pct_5d - self.ctx.spy_5d_pct
        if   rs >= 10: s += 5.0
        elif rs >=  5: s += 3.5
        elif rs >=  0: s += 1.5

        # H — ATR quality (0-4): wide range = real movement potential ────────
        atr = snap.atr_pct
        if   atr >= 20: s += 4.0
        elif atr >= 10: s += 3.5
        elif atr >=  5: s += 2.5
        elif atr >=  3: s += 1.0

        # I — OBV smart money (0-2) ──────────────────────────────────────────
        s += max(0.0, min(sig.obv_score * 2.0, 2.0))

        # J — Yahoo trending (0-1) ───────────────────────────────────────────
        if snap.symbol in self.ctx.trending:
            s += 1.0

        return s
