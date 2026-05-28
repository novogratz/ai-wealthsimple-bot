# CODEX.md — Technical Reference

## Module map

```
ai-wealthsimple-bot/
├── kzer_bot/
│   ├── grinder_strategy.py   ← Le Grinder strategy (MAIN)
│   │     GrinderSnapshot      — 10-field frozen dataclass w/ computed properties
│   │     GrinderMarketData    — yfinance wrapper w/ in-memory snapshot + frame cache
│   │       .prefetch()        — batch yf.download (200/batch, threaded, 3-5x faster)
│   │       .get_frame(sym)    — returns raw OHLCV DataFrame for SmartStrategy signals
│   │       .all_snapshots()   — returns all cached snapshots (for diagnostics)
│   │     FuturesBias          — Enum: GREEN / RED / NEUTRAL
│   │     get_futures_bias()   — ES=F 1h bars, compare to 24h ago
│   │     GrinderPick          — frozen result dataclass w/ .confidence property
│   │     SmartSignals         — frozen dataclass: pct_5d, pct_20d, high_20d, vol_trend, obv_score
│   │     SmartMarketContext   — TSX + sector ETF returns, trending tickers (cached 2h)
│   │       .load_or_fetch()   — returns cached or fresh context
│   │     SmartGrinderStrategy — tier 0: composite 0-100 score, no hard cap filters
│   │     GrinderStrategy      — tier 1: 8-criteria scanner, sorted by score
│   │     FallbackStrategy     — tier 2: relaxed fallback (rel vol ≥ 1.0x)
│   │     BestEffortStrategy   — tier 3: guaranteed pick, no filters, highest score wins
│   │     WATCHLIST            — loaded from data/universe.json (4052 TSX/TSXV tickers)
│   │                            falls back to 100-ticker hardcoded list if file missing
│   │
│   ├── market_data.py         ← Old data layer (used by hold loop)
│   │     Snapshot             — 7-field dataclass (last_price, avg_volume, etc.)
│   │     YFinanceMarketData   — 1m intraday for live price in watch loop
│   │
│   ├── telegram.py            ← Telegram integration
│   │     TelegramConfig       — bot_token + chat_id, loaded from .env
│   │     send_message()       — HTTP POST to Telegram Bot API
│   │     trade_message()      — formats structured event messages
│   │     load_dotenv()        — minimal .env parser (no dependency)
│   │
│   ├── cli.py                 ← CLI commands (scan / paper / watch / balance / pnl)
│   │     _get_total_pnl()     — reads pnl_ledger.json
│   │     _record_and_get_total_pnl() — appends to pnl_ledger.json
│   │     cmd_watch()          — hold loop (price check every 60s, sell at force_exit)
│   │
│   ├── paper.py               ← PaperBroker — simulated trades to CSV
│   ├── runner.py              ← run_paper_once() — one paper cycle
│   ├── schedule.py            ← is_weekday / is_market_session / should_force_exit
│   └── config.py              ← Settings / TradingSettings / RiskSettings (TOML)
│
├── scripts/
│   ├── run_grinder.py         ← Le Grinder orchestrator (MAIN ENTRY POINT)
│   │     refresh_universe_if_stale() — auto-refresh data/universe.json if >7 days old
│   │     run_scan()           — futures + 4-tier strategy (smart/main/fallback/best-effort)
│   │     _log_scan_diagnostics() — logs top-5 by score + exact filter failures
│   │     _buy_timing_line()   — "Buying in 11h 20min (9:35 AM tomorrow)"
│   │     _day_label()         — "TODAY" / "TOMORROW" / "MONDAY"
│   │     get_ai_analysis()    — calls `claude -p "..."` subprocess
│   │     wait_for_buy_window() — 9:35 AM or 11:00 AM depending on bias
│   │     execute_buy()        — calls wealthsimple_auto.py buy --max-dollars
│   │     _morning_hold_decision() — at 9:31 AM: hold or rotate (EMA20 + smart score ≥ 20)
│   │                                honours forceSell flag in position file
│   │     hold_and_sell()      — autonomous: profit target +5%, 3:55 PM lock +2%, overnight
│   │     wait_overnight()     — fires 5 AM and 5 PM scans, sleeps between
│   │     main()               — intraday rotation loop: sell → re-scan → buy → repeat until 3:30 PM
│   │
│   ├── update_universe.py     ← Fetches full TSX/TSXV listing from TMX public API
│   │     _fetch(url, suffix)  — GET tsx.com/json/company-directory → yfinance symbols
│   │     main()               — saves to data/universe.json (4052 tickers)
│   │
│   ├── wealthsimple_auto.py   ← Playwright browser automation
│   │     cmd_buy()            — navigates to stock, fills Dollars→Max, submits
│   │     cmd_sell()           — sell all shares of position
│   │     cmd_balance()        — scrapes Available-to-trade balance
│   │     cmd_setup()          — opens browser for manual login
│   │     open_browser()       — launches Edge with persistent profile
│   │     ORDER_RESULT_JSON:   — stdout line with fill details (parsed by caller)
│   │     LIVE_BALANCE_CAD:    — stdout line with balance (parsed by caller)
│   │
│   └── run_day.py             ← Old kzer orchestrator (unchanged, still works)
│
├── config/
│   ├── settings.toml          ← Risk/timing for OLD kzer strategy
│   └── universe.csv           ← Old ticker universe (not used by grinder)
│
└── data/                      ← All runtime state (gitignored)
    ├── open_position.json     ← {symbol, buyPrice, shares, estimatedCost, ...}
    ├── pnl_ledger.json        ← [{symbol, buyCost, sellValue, realizedPnl, time}]
    ├── trade_history.csv      ← Full log: timestamp,symbol,side,price,shares,cost,pnl,strategy
    ├── session_info.json      ← {startingBalance, startTime}
    ├── grinder.log            ← Persistent bot log (all output, rotates at 5 MB)
    ├── universe.json          ← TSX/TSXV ticker universe (auto-generated by update_universe.py)
    ├── ws_auth.json           ← Wealthsimple browser session (NEVER commit)
    ├── browser_profile/       ← Edge persistent profile (NEVER commit)
    └── yfinance_cache/        ← yfinance tz cache
```

## Score formula (v4.0)

### 9-Signal Quant Engine (Primary) — 0–110 pts
Synthesized from IBKR, Minervini, CANSLIM, and LangChain concepts:
```
Signal A: Momentum alignment (0–25 pts) — 1d/5d/20d alignment
Signal B: MACD Bullish      (0–12 pts) — Crossover + Signal alignment
Signal C: RSI Momentum      (0–10 pts) — Goldilocks zone (45-70)
Signal D: Stage 2 Alignment (0–12 pts) — Price>SMA50>SMA150>SMA200
Signal E: Volume Conviction (0–18 pts) — RVOL + Trend + Breakthrough
Signal F: High Proximity    (0–10 pts) — Within 20% of 52-week high
Signal G: Relative Strength (0–8 pts)  — Performance vs S&P 500
Signal H: OBV Smart Money   (0–5 pts)  — Direction-weighted volume
Signal I: Bonuses           (0–10 pts) — Close Quality + ATR + Trending
```

**Risk Management & Exits:**
- **Daily Target:** +10.0% (Hard target for intraday rotation)
- **Trailing Stop:** Trigger +2.0%, Trail 1.0% (Activated after trigger)
- **Market Close:** 3:55 PM (Hard sell of all daytime positions)
- **Extended Hours:** PM (+2.0% target), AH (+3.0% target)

## Wealthsimple automation protocol

```
wealthsimple_auto.py buy --symbol ERF.TO --max-dollars
  → connects to Edge via CDP (localhost:9222)
  → if login page detected → try_auto_login() → fills WS_EMAIL/WS_PASSWORD → Log In
  → navigates to Wealthsimple stock page
  → clicks Buy → switches to Dollars mode → clicks Max
  → reads review page → prints ORDER_RESULT_JSON:{...}
  → prints LIVE_BALANCE_CAD:xxx if balance visible
  → exits 0 on success

wealthsimple_auto.py sell --symbol ERF.TO --sell-all
  → same flow but Sell side → Max shares
  → prints ORDER_RESULT_JSON:{submitted: true, ...}

wealthsimple_auto.py balance
  → goes to home → auto-login if session expired
  → scrapes Non-registered/Unregistered account balance
  → prints LIVE_BALANCE_CAD:xxx
  → exits 0 on success

wealthsimple_auto.py setup
  → first-time only: launches Edge with persistent profile at data/browser_profile
  → user logs in manually → session persists across bot restarts
  → subsequent session expiries handled automatically via try_auto_login()
```

### Auto-login recovery (`try_auto_login`)

Triggered whenever `input[type="password"]` is detected after navigating to WS_HOME.

```python
try_auto_login(page):
  1. Loads .env to read WS_EMAIL / WS_PASSWORD
  2. Fills email input with WS_EMAIL
  3. Fills password input with WS_PASSWORD
  4. Clicks "Log In" button
  5. Waits 8s for redirect
  6. Returns True if login page is gone, False otherwise
  # Screenshots saved to data/screen_login_page.png and data/screen_after_auto_login.png
```

## Telegram message contract

All messages are sent as HTML via `send_message()`. Key events:

| Event | When | Key content |
|---|---|---|
| Startup | Bot launch | Balance, watchlist size, strategy summary |
| Resume | Restart with open position | Symbol, entry, cost, autonomous exit rules |
| Game plan | 5 AM / startup scan | Pick, score, why, plan, AI analysis |
| Red waiting | 9:35 AM (red days) | Why we wait, what to watch |
| Buying now | 9:35 AM or 11 AM | Edge reasoning, AI view |
| Fill confirmed | 9:31 AM (pre-market buys) | Live balance after fill |
| Morning decision | 9:31 AM | Hold another day or rotate to new pick |
| 30-min update | Every 30 min in session | Price, P&L, time to sell |
| Profit target | Any time (after 10:30 AM) | +5% hit — selling now |
| 3:55 PM lock | 3:55 PM if ≥ +2% | Lock in gain |
| Overnight hold | 3:55 PM if < +2% | Holding — morning decision tomorrow |
| Intraday rotation | After any sell before 3:30 PM | New pick found — buying immediately |
| Sold | After sell executes | Trade P&L, all-time PnL, record |
| 5 PM preview | 5:00 PM | Next day's top pick + plan |

## AI analysis flow

```python
get_ai_analysis(picks, bias, futures_detail, balance)
  # builds a 120-word-max prompt with pick metrics
  # calls: subprocess.run(["claude", "-p", prompt])
  # returns: plain text response (empty str if claude not on PATH)
  # appended to game plan and buy messages on Telegram
```

## Position file schema

```json
{
  "symbol": "ERF.TO",
  "buyPrice": 4.82,
  "shares": 18.5831,
  "estimatedCost": 89.59,
  "sellAll": true,
  "strategyName": "Smart Strategy",
  "time": "2026-05-25T09:35:32.123456-04:00",
  "forceSell": false
}
```

Set `forceSell: true` to guarantee the position sells at the next 9:31 AM morning decision, bypassing the hold/rotate check.

## Environment variables

```
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=@channel_or_numeric_id
WS_EMAIL=<Wealthsimple login email>
WS_PASSWORD=<Wealthsimple password>
ANTHROPIC_API_KEY=<not needed — using claude CLI subprocess>
```

All vars are loaded from `.env` at runtime. `.env` is gitignored — never committed.

## Common errors and fixes

| Error | Fix |
|---|---|
| Session expires mid-run | Auto-handled — `try_auto_login()` fires automatically using WS_EMAIL/WS_PASSWORD |
| Auto-login fails (wrong creds) | Check WS_EMAIL / WS_PASSWORD in `.env`; re-run `setup` if needed |
| `claude: command not found` | AI analysis skipped silently — install Claude Code CLI |
| `No candidates passed` | Market was closed or all tickers failed yfinance; check internet |
| `UnicodeEncodeError` | PowerShell encoding issue — run in Windows Terminal with UTF-8 |
| Balance returns None | Wealthsimple page layout changed — check wealthsimple_auto.py scraper |

## Adding new tickers

Edit `WATCHLIST` in `kzer_bot/grinder_strategy.py`:
- TSX: use `.TO` suffix (e.g. `"ERF.TO"`)
- TSXV: use `.V` suffix (e.g. `"GGD.V"`)
- NEO: use `.NE` suffix (e.g. `"CBLT.NE"`)

Tickers that fail to download or don't pass criteria are silently skipped.
