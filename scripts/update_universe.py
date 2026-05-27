#!/usr/bin/env python3
"""
Fetch the full Canadian stock universe from TSX and TSXV public APIs.
Saves to data/universe.json — loaded automatically by Le Grinder at scan time.

Usage:
    python scripts/update_universe.py          # fetch + save
    python scripts/update_universe.py --stats  # show current file stats only

Run manually or it auto-runs when the file is older than 7 days.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
UNIVERSE_FILE = DATA / "universe.json"

TSX_URL  = "https://www.tsx.com/json/company-directory/search/tsx/**"
TSXV_URL = "https://www.tsx.com/json/company-directory/search/tsxv/**"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; LeGrinder/1.0)"}

# Instrument sub-types to skip — not regular equity / income trust
_SKIP_SUFFIXES = {".U", ".WT", ".WT.A", ".WT.B", ".DB", ".DB.A", ".DB.B",
                  ".DB.C", ".DB.D", ".DB.E", ".R", ".F", ".W"}


def _is_equity(raw_symbol: str) -> bool:
    """Keep common shares and trust units (.UN); drop warrants, debentures, USD units, etc."""
    sym = raw_symbol.upper()
    # Split by dots to check last component
    parts = sym.split(".")
    if len(parts) >= 2:
        last = "." + parts[-1]
        if last in _SKIP_SUFFIXES:
            return False
        # Catch .DB.A style (debenture series)
        if parts[-1].isdigit():
            return False
    return True


def _fetch(url: str, suffix: str) -> list[str]:
    """Fetch all symbols from TSX/TSXV API and convert to yfinance format."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    symbols = []
    for company in r.json().get("results", []):
        # Each company can have multiple instrument classes
        for instr in company.get("instruments", [company]):
            raw = instr.get("symbol", "").strip()
            if raw and _is_equity(raw):
                symbols.append(f"{raw}.{suffix}")
    return symbols


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats", action="store_true", help="Show current file info only")
    args = parser.parse_args()

    if args.stats:
        if UNIVERSE_FILE.exists():
            u = json.loads(UNIVERSE_FILE.read_text())
            print(f"Universe: {u['count']} tickers  |  Updated: {u['updated']}")
        else:
            print("No universe file found — run without --stats to fetch.")
        return

    print("Fetching TSX companies...")
    try:
        tsx = _fetch(TSX_URL, "TO")
        print(f"  TSX: {len(tsx)} equity symbols")
    except Exception as e:
        print(f"  TSX fetch failed: {e}")
        tsx = []

    print("Fetching TSXV companies...")
    try:
        tsxv = _fetch(TSXV_URL, "V")
        print(f"  TSXV: {len(tsxv)} equity symbols")
    except Exception as e:
        print(f"  TSXV fetch failed: {e}")
        tsxv = []

    # Deduplicate (some symbols appear on both exchanges)
    all_symbols = list(dict.fromkeys(tsx + tsxv))

    universe = {
        "updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(all_symbols),
        "tsx_count": len(tsx),
        "tsxv_count": len(tsxv),
        "symbols": all_symbols,
    }
    UNIVERSE_FILE.write_text(json.dumps(universe, indent=2))

    print(f"\nTotal: {len(all_symbols)} unique symbols saved to {UNIVERSE_FILE.name}")
    print("Le Grinder will use this universe at next scan.")


if __name__ == "__main__":
    main()
