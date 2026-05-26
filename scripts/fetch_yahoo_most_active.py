#!/usr/bin/env python3
"""Fetch top Canadian stocks by volume from Yahoo Finance screener API."""
import json
import re
import sys
import time

import requests

SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
)
SESSION.headers["Origin"] = "https://ca.finance.yahoo.com"
SESSION.headers["Referer"] = "https://ca.finance.yahoo.com/"


def get_crumb() -> str:
    r = SESSION.get("https://fc.yahoo.com/", timeout=15)
    m = re.search(r'crumb":"([^"]+)"', r.text)
    if m:
        return m.group(1)
    r2 = SESSION.get(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
    )
    return r2.text.strip().strip('"')


def fetch_all_symbols() -> list[str]:
    crumb = get_crumb()
    print(f"Crumb: {crumb}", file=sys.stderr)

    all_symbols = []
    seen = set()

    for page in range(8):
        offset = page * 100
        print(f"Fetching page {page+1} (offset={offset})...", file=sys.stderr)

        url = (
            f"https://query1.finance.yahoo.com/v1/finance/screener/"
            f"predefined/saved?crumb={crumb}"
            f"&formatted=true&lang=en-CA&region=CA"
            f"&scrIds=most_actives_ca&count=100&start={offset}"
        )

        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                break

            j = r.json()
            result = j.get("finance", {}).get("result")
            if not result:
                print(f"  No result: {j}", file=sys.stderr)
                break

            quotes = result[0].get("quotes", [])
            if not quotes:
                print(f"  No quotes on page {page+1}", file=sys.stderr)
                break

            for q in quotes:
                sym = q.get("symbol", "")
                if sym and sym not in seen:
                    seen.add(sym)
                    all_symbols.append(sym)

            print(f"  Got {len(quotes)} symbols (total: {len(all_symbols)})", file=sys.stderr)

            if len(quotes) < 100:
                print("  Last page reached", file=sys.stderr)
                break

            time.sleep(1)
        except Exception as e:
            print(f"  Error on page {page+1}: {e}", file=sys.stderr)
            break

    return all_symbols


def main():
    symbols = fetch_all_symbols()
    print(f"\nTotal: {len(symbols)} symbols")
    for s in symbols:
        print(s)


if __name__ == "__main__":
    main()
