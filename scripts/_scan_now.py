#!/usr/bin/env python3
"""Quick scanner — tonight's pick from shortlist."""
import sys, json, traceback
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TZ = ZoneInfo("America/Toronto")
print("Time ET:", datetime.now(TZ).strftime("%Y-%m-%d %H:%M"))

state_file = Path("data/scan_state.json")
if state_file.exists():
    state = json.loads(state_file.read_text())
    shortlist = state.get("shortlist", [])
    print(f"Shortlist: {len(shortlist)} tickers, last scan: {state.get('updated', '?')}")
else:
    shortlist = []
    print("No scan_state.json — using WATCHLIST")

from kzer_bot.grinder_strategy import (
    GrinderMarketData, SmartGrinderStrategy, SmartMarketContext,
    GrinderStrategy, FallbackStrategy, BestEffortStrategy, WATCHLIST, get_futures_bias,
)

scan_syms = shortlist[:150] if shortlist else WATCHLIST[:100]
print(f"Scanning {len(scan_syms)} tickers...")

bias, detail = get_futures_bias()
print(f"Futures: {bias.value.upper()} | {detail}")

md = GrinderMarketData()
md.prefetch(scan_syms)
snaps = md.all_snapshots()
print(f"Got data for {len(snaps)} tickers")

try:
    ctx = SmartMarketContext.load_or_fetch()
    print(f"TSX 5d: {ctx.tsx_5d_pct:+.2f}%  |  Trending: {len(ctx.trending)} stocks")
except Exception as e:
    ctx = None
    print(f"No ctx: {e}")

picks = SmartGrinderStrategy(md, ctx).scan(scan_syms)
strat = "Smart Strategy"
print(f"Smart picks: {len(picks)}")

if not picks:
    picks = GrinderStrategy(md).scan(scan_syms)
    strat = "Main (8-criteria)"
    print(f"Main picks: {len(picks)}")

if not picks:
    picks = FallbackStrategy(md).scan(scan_syms)
    strat = "Fallback"
    print(f"Fallback picks: {len(picks)}")

if not picks:
    picks = BestEffortStrategy(md).scan(scan_syms)
    strat = "Best Available"
    print(f"BestEffort picks: {len(picks)}")

print()
print(f"=== TONIGHT'S TOP PICKS ({strat}) ===")
for i, p in enumerate(picks[:8], 1):
    print(
        f"#{i} {p.symbol:12s}  ${p.last_close:.2f}"
        f"  score={p.score:.1f}  yday={p.yesterday_pct:+.1f}%"
        f"  relvol={p.rel_volume:.1f}x  atr={p.atr_pct:.1f}%"
        f"  closestr={p.close_strength:.0%}  EMA5={p.above_ema5}  EMA20={p.above_ema20}"
        f"  [{p.confidence}]"
    )

if picks:
    top = picks[0]
    print()
    print(">>> TOMORROW'S PICK <<<")
    print(f"  {top.symbol}  @${top.last_close:.2f}  score={top.score:.1f}  [{strat}]")
    print(f"  Yesterday: {top.yesterday_pct:+.1f}%  on  {top.rel_volume:.1f}x normal volume")
    print(f"  ATR: {top.atr_pct:.1f}%  |  Close strength: {top.close_strength:.0%}")
