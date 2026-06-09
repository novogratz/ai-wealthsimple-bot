Run a full morning scan of Le Grinder, show me the top picks with their scores, futures bias, and the Claude AI analysis. Then tell me the game plan for today (or tomorrow if market is closed). Use the data from kzer_bot/grinder_strategy.py and the logic in scripts/run_grinder.py. Run the actual scan using the venv Python at .venv/Scripts/python.exe.

Steps:
1. Check current time (ET) and whether the market is open
2. Check for any open position in data/open_position.json — show symbol, entry price, current score
3. Import and run the scan using SmartGrinderStrategy (primary tier-0):
   ```python
   import sys; sys.path.insert(0, '.')
   from kzer_bot.grinder_strategy import SmartGrinderStrategy, GrinderMarketData, SmartMarketContext, WATCHLIST, get_futures_bias
   bias, detail = get_futures_bias()
   md = GrinderMarketData()
   ctx = SmartMarketContext.load_or_fetch()
   picks = SmartGrinderStrategy(md, ctx).scan(WATCHLIST[:200])
   ```
4. Show me: futures bias, top 5 picks with scores + reasons, and the full game plan message
5. If there is an open position, show its current rank (score vs top-1 score) and whether the _HOLD_SCORE_GAP = 25 threshold would trigger a rotation
6. Give me a quant's take on the #1 pick — why it should (or shouldn't) work today
