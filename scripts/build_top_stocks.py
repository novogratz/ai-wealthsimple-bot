"""One-time: fetch market caps, build top-150 watchlist for _HARDCODED_WATCHLIST."""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
UNIVERSE_FILE = DATA / "universe.json"
CACHE_FILE = DATA / "market_cap_cache.json"

MAX_WORKERS = 15
RATE_LIMIT_DELAY = 0.1  # seconds between each ticker


def load_symbols() -> list[str]:
    if UNIVERSE_FILE.exists():
        data = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
        syms = data.get("symbols", [])
        if syms:
            return syms
    from kzer_bot.grinder_strategy import _HARDCODED_WATCHLIST
    return list(_HARDCODED_WATCHLIST)


def fetch_mc(symbol: str) -> tuple[str, float | None]:
    try:
        t = yf.Ticker(symbol)
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            try:
                mc = fi.get("market_cap") or fi.get("marketCap")
                if mc:
                    return symbol, float(mc)
            except Exception:
                pass
        info = t.get_info()
        mc = info.get("marketCap") or info.get("market_cap")
        return symbol, float(mc) if mc else None
    except Exception:
        return symbol, None


def main():
    symbols = load_symbols()
    print(f"Loaded {len(symbols)} tickers")

    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            print(f"Loaded {len(cache)} cached entries")
        except Exception:
            pass

    to_fetch = [s for s in symbols if s not in cache]
    print(f"Need to fetch: {len(to_fetch)}")

    if to_fetch:
        done = len(cache)
        total = len(symbols)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {}
            for s in to_fetch:
                futs[pool.submit(fetch_mc, s)] = s
                time.sleep(RATE_LIMIT_DELAY)

            for fut in as_completed(futs):
                sym, mc = fut.result()
                cache[sym] = mc
                done += 1
                lbl = f"${mc/1e9:.1f}B" if mc else "N/A"
                print(f"  [{done}/{total}] {sym:12s}  {lbl}")
                if done % 100 == 0:
                    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                    print(f"  (saved {len(cache)} to cache)")

        CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    scored = [(s, cache[s]) for s in symbols if cache.get(s) is not None]
    scored.sort(key=lambda x: x[1], reverse=True)

    print(f"\nTickers with market cap data: {len(scored)}")
    top = scored[:150]
    top_syms = [s for s, _ in top]

    print(f"\nTop 150 by market cap:")
    for i, (s, mc) in enumerate(top, 1):
        print(f"  {i:>3}. {s:12s}  ${mc/1e9:.2f}B")

    out_txt = ROOT / "data" / "top_150_watchlist.txt"
    out_txt.write_text("\n".join(top_syms), encoding="utf-8")
    print(f"\nWritten to {out_txt}")

    print("\n--- Python list (copy into _HARDCODED_WATCHLIST) ---")
    for i in range(0, len(top_syms), 6):
        chunk = top_syms[i:i+6]
        print("    " + ", ".join(f'"{s}"' for s in chunk) + ",")


if __name__ == "__main__":
    main()
