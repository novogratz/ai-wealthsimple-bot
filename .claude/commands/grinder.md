Run a full morning scan of Le Grinder, show me the top picks with their scores, futures bias, and the Claude AI analysis. Then tell me the game plan for today (or tomorrow if market is closed). Use the data from kzer_bot/grinder_strategy.py and the logic in scripts/run_grinder.py. Run the actual scan using the venv Python at .venv/Scripts/python.exe.

Steps:
1. Check current time (ET) and whether the market is open
2. Import and run the scan:
   ```python
   import sys; sys.path.insert(0, '.')
   from kzer_bot.grinder_strategy import GrinderStrategy, FallbackStrategy, GrinderMarketData, WATCHLIST, get_futures_bias
   bias, detail = get_futures_bias()
   md = GrinderMarketData()
   picks = GrinderStrategy(md).scan(WATCHLIST)
   if not picks:
       picks = FallbackStrategy(md).scan(WATCHLIST)
   ```
3. Show me: futures bias, top 5 picks with scores, and the full game plan message
4. Give me a quant's take on the #1 pick — why it should (or shouldn't) work today
