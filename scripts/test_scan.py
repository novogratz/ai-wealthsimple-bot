"""Quick end-to-end scan test — main, fallback, best-effort."""
import sys
sys.path.insert(0, ".")
from kzer_bot.grinder_strategy import (
    BestEffortStrategy, FallbackStrategy, GrinderMarketData,
    GrinderStrategy, WATCHLIST, get_futures_bias,
)

print(f"Watchlist: {len(WATCHLIST)} tickers")
print("Checking futures bias...")
bias, detail = get_futures_bias()
print(f"  Bias: {bias.value.upper()}  |  {detail}")

md = GrinderMarketData()

print("\nRunning main strategy...")
picks = GrinderStrategy(md).scan(WATCHLIST)
print(f"  Main: {len(picks)} picks")
for p in picks[:3]:
    print(f"    {p.symbol}  ${p.last_close:.2f}  score={p.score:.1f}  {p.confidence}")

if not picks:
    print("\nRunning fallback strategy...")
    picks = FallbackStrategy(md).scan(WATCHLIST)
    print(f"  Fallback: {len(picks)} picks")
    for p in picks[:3]:
        print(f"    {p.symbol}  ${p.last_close:.2f}  score={p.score:.1f}  {p.confidence}")

if not picks:
    print("\nRunning best-effort strategy...")
    picks = BestEffortStrategy(md).scan(WATCHLIST)
    print(f"  Best-effort: {len(picks)} picks")
    for p in picks:
        print(f"    {p.symbol}  ${p.last_close:.2f}  score={p.score:.1f}  {p.confidence}  [{p.strategy_name}]")

if picks:
    top = picks[0]
    print(f"\n=== TODAY'S PICK: {top.symbol} ===")
    print(f"  Price: ${top.last_close:.2f}")
    print(f"  Yesterday: {top.yesterday_pct:+.2f}%  |  RelVol: {top.rel_volume:.1f}x")
    print(f"  ATR%: {top.atr_pct:.2f}%  |  CloseStr: {top.close_strength:.0%}")
    print(f"  EMA5: {'above' if top.above_ema5 else 'below'}  EMA20: {'above' if top.above_ema20 else 'below'}")
    print(f"  Strategy: {top.strategy_name}  |  Score: {top.score:.1f}  ({top.confidence})")
else:
    print("\n=== NO PICK — check internet connection ===")
