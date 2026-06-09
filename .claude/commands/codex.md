Give me a complete technical overview of the ai-wealthsimple-bot project as it stands right now. Read CLAUDE.md for the reference, then verify the actual current state of the code by checking:

1. Which strategy files exist and what their criteria are (kzer_bot/grinder_strategy.py)
2. What the main entry points are (scripts/run_grinder.py)
3. What data files are present in data/ (open_position, pnl_ledger, trade_history)
4. Current universe size: count tickers in data/us_universe.json
5. Any recent git changes (git log --oneline -10)

Then give me:
- A concise architecture diagram (text)
- Current strategy parameters (all thresholds — especially _HOLD_SCORE_GAP, _PROFIT_TARGET_PCT, _TRAILING_STOP_TRIGGER_PCT)
- Rank-based exit logic summary (hold threshold, intraday rotation, morning check timing)
- Outstanding issues or things that could be improved
- The exact command to run the bot right now
