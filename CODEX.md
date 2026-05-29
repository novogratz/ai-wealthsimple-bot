# CODEX.md — Technical Reference

## Module map

```
ai-wealthsimple-bot/
├── kzer_bot/
│   ├── grinder_strategy.py   ← Le Grinder strategy (MAIN)
│   │     GrinderSnapshot      — 10-field frozen dataclass w/ computed properties
│   │     GrinderMarketData    — yfinance wrapper w/ in-memory snapshot + frame cache
│   │       .prefetch()        — batch yf.download (200/batch, 3-5x faster)
│   │       .get_frame(sym)    — returns raw OHLCV DataFrame for SmartStrategy
│   │       .market_cap(sym)   — fetches/caches market cap (7-day TTL)
│   │     FuturesBias          — Enum: GREEN / RED / NEUTRAL
│   │     get_futures_bias()   — ES=F 1h bars, compare to 24h ago
│   │     GrinderPick          — frozen result dataclass w/ .confidence property
│   │     SmartSignals         — 13-field frozen dataclass (pct_5d, pct_20d, rsi14,
│   │                            macd_diff, macd_crossed, sma50/150/200, obv_score, ...)
│   │     SmartMarketContext   — SPY 5d %, sector ETF returns, trending tickers (2h cache)
│   │       .load_or_fetch()   — returns cached or fresh context
│   │       .regime_multiplier — 1.0 (bull) / 0.85 (below SMA50) / 0.70 (below SMA200)
│   │     _SECTOR_MAP          — 350+ symbols mapped to XLK/XLF/XLE/XLV/XLI/XLY
│   │     _is_earnings_blackout(sym) — True if earnings within 3 days (12h disk cache)
│   │     _squeeze_bonus(sym, pct)   — 0–8 pts for high short float + momentum (24h cache)
│   │     SmartGrinderStrategy — tier 0: 12-signal composite 0–125 pts
│   │     GrinderStrategy      — tier 1: 8-criteria hard-filter scanner
│   │     FallbackStrategy     — tier 2: relaxed fallback (rel vol ≥ 1.0x)
│   │     BestEffortStrategy   — tier 3: guaranteed pick, no filters, highest score
│   │     WATCHLIST            — loaded from data/us_universe.json
│   │                            falls back to ~350-ticker hardcoded US list
│   │
│   ├── market_data.py         ← Live price data layer (used by hold loop)
│   │     YFinanceMarketData   — 1m intraday for live price in watch loop
│   │
│   ├── telegram.py            ← Telegram integration
│   │     send_message()       — HTTP POST to Telegram Bot API (HTML parse mode)
│   │
│   └── cli.py                 ← CLI commands (scan / paper / watch / balance / pnl)
│
├── scripts/
│   ├── run_grinder.py         ← Le Grinder orchestrator (MAIN ENTRY POINT)
│   │     refresh_universe_if_stale() — logs US watchlist size (no-op, no TMX fetch)
│   │     run_scan()           — futures + 4-tier strategy (smart/main/fallback/best-effort)
│   │     build_watchlist_alert()     — top 3 cached picks with score + reasons
│   │     build_update_message()      — 30-min position update (price / P&L / time-to-sell)
│   │     build_sell_message()        — trade result (% return, entry/exit, all-time record)
│   │     build_daily_report()        — EOD report (per-trade %, day W/L, all-time W/L)
│   │     execute_buy()        — calls wealthsimple_auto.py buy --max-dollars
│   │     _morning_hold_decision()    — 9:31 AM: hold or rotate (EMA20 + smart score ≥ 20)
│   │                                   NOT called for AH/PM positions (always sell at 9:35)
│   │     hold_and_sell()      — autonomous: profit target +10%, trailing stop, 3:55 PM lock
│   │                            AH/PM positions: sell at 9:35 AM + rotate (no hold decision)
│   │     _run_afterhours_strategy()  — scan < $10 movers, place limit buy (4–7:57 PM)
│   │     _run_premarket_strategy()   — scan < $10 movers, place limit buy (7–9:29 AM)
│   │     _afterhours_sell_limit()    — DISABLED stub (never limit sell in extended hours)
│   │     _premarket_sell_limit()     — DISABLED stub (never limit sell in extended hours)
│   │     wait_overnight()     — fires 5 AM and 5 PM scans; AH scan if cash available
│   │     main()               — intraday rotation loop: sell → re-scan → buy → repeat
│   │
│   └── wealthsimple_auto.py   ← Playwright browser automation
│         cmd_buy()            — navigates to stock, fills Dollars→Max, submits
│         cmd_sell()           — sell all shares of position (market order)
│         get_live_balance()   — scrapes USD balance; fallback converts CAD via CADUSD=X
│         cmd_setup()          — first-time: opens Edge for manual login
│         ORDER_RESULT_JSON:   — stdout line with fill details (parsed by caller)
│
└── data/                      ← All runtime state (gitignored)
    ├── open_position.json     ← {symbol, buyPrice, shares, estimatedCost, afterHours, ...}
    ├── pnl_ledger.json        ← [{symbol, buyCost, sellValue, realizedPnl, time}]
    ├── trade_history.csv      ← timestamp,symbol,side,price,shares,cost,pnl,strategy
    ├── session_info.json      ← {startingBalance, startTime}
    ├── scan_state.json        ← {picks[10], shortlist[150], bias, updated, ...}
    ├── smart_context_cache.json ← {spy_5d_pct, sector_returns, trending, spy_sma50/200}
    ├── earnings_cache.json    ← {symbol: {next_earnings, ts}} — 12h TTL
    ├── short_interest_cache.json ← {symbol: {short_pct, ts}} — 24h TTL
    ├── grinder_snapshot_cache.json ← OHLCV snapshots — 18h TTL
    ├── grinder.log            ← Persistent bot log (all output, rotates at 5 MB)
    ├── ws_auth.json           ← Wealthsimple browser session (NEVER commit)
    └── browser_profile/       ← Edge persistent profile (NEVER commit)
```

## Score formula (v5.0)

### 12-Signal Quant Engine (Primary) — 0–125 pts
```
Signal A: Momentum cascade   (0–25 pts) — 1d pct × 1.2 + 5d confirmed + 20d confirmed
Signal B: MACD(12,26,9)      (0–12 pts) — crossover = 12, above signal = 6
Signal C: RSI(14) zone       (0–10 pts) — 45-70 = 10, 35-45 = 5, 30-35 = 3
Signal D: Stage 2 MA align   (0–12 pts) — price>SMA50>SMA150>SMA200 = 12 (Minervini)
Signal E: Volume conviction  (0–18 pts) — rel_vol×2 + vol_trend bonus + 1yr record
Signal F: 52w high proximity (0–10 pts) — ≤2% from high=10, ≤10%=7, ≤20%=4, ≤30%=1
Signal G: Rel strength SPY   (0–8 pts)  — outperforms SPY 5d by >5%=8, >2%=5, >0%=3
Signal H: OBV smart money    (0–5 pts)  — volume-weighted up/down ratio
Signal I: Bonuses            (0–10 pts) — close_strength×3.5 + ATR bonus + trending×4
Signal J: Sector alignment   (0–5 pts)  — stock's sector 5d return ≥4%=5, ≥2%=3, ≥0%=1
Signal K: Earnings blackout  (FILTER)   — skip if earnings within 3 calendar days
Signal L: Short squeeze      (0–8 pts)  — short_float≥30%+momentum=8, ≥20%=5, ≥10%=2

Regime gate: SPY below SMA200 → score × 0.70 | SPY below SMA50 → × 0.85
```

**Base filters (SmartGrinderStrategy):** price $1–$1000 | avg vol ≥ 100k | yesterday ≥ +0.5% | above EMA20

Enrichment (top 50 only): earnings blackout check (yfinance `get_earnings_dates`) + squeeze bonus (yfinance `get_info` → `shortPercentOfFloat`). Both disk-cached to avoid repeated slow calls.

### Confidence tiers
- **HIGH (≥80 pts):** Strong momentum, volume, trend all aligned
- **MEDIUM (45–79 pts):** Good setup, some signals missing
- **LOW (<45 pts):** Weak — BestEffort fallback territory

## Extended hours rules

| Window | Buy? | Sell? | Price |
|---|---|---|---|
| Pre-market (7–9:29 AM) | ✅ Limit buy < $10 stocks | ❌ Never | 5% above current |
| Market hours (9:35 AM–3:30 PM) | ✅ Market order | ✅ Market order | Market |
| After-hours (4–7:57 PM) | ✅ Limit buy < $10 stocks | ❌ Never | 5% above current |

AH/PM positions (identified by `afterHours: true` or strategy name "After-Hours Limit"/"Pre-Market Limit") bypass the morning hold decision and always sell at market at **9:35 AM** then rotate immediately.

## Telegram message contract

| Event | When | Key content |
|---|---|---|
| Startup | Bot launch | Balance, watchlist size, strategy, all-time PnL |
| Game plan | 5 AM / startup scan | Pick, score, why, AI analysis, countdown to buy |
| AH window hold | Restart in AH with position | Holding until 9:35 AM, no limit sells |
| Buying now | 9:35 AM or 11 AM | Ticker, price, shares, cost, targets |
| AH/PM sell + rotate | 9:35 AM | Market sell + new scan message |
| Morning decision | 9:31 AM (regular overnight) | Hold another day or rotate to new pick |
| 30-min update | Every 30 min | Price, P&L, trailing stop, time to sell |
| Watchlist alert | Every 30 min | Top 3 manual picks — score + 1-line reasons |
| Profit target | Any time | +10% hit — selling + rotating |
| Trailing stop | Any time after +2% | Stop price hit — protecting gains |
| 3:55 PM lock | 3:55 PM | Selling all daytime positions |
| Overnight hold | 3:55 PM if < threshold | Holding — 9:31 AM morning decision |
| Intraday rotation | After sell before 3:30 PM | New pick found — buying immediately |
| Sold | After sell executes | % return, entry/exit, all-time record + win rate |
| Daily report | 3:55 PM / market close | Per-trade %, day W/L, all-time W/L + win rate |
| 5 PM preview | 5:00 PM | Next day's top pick + plan |

## Position file schema

```json
{
  "symbol": "NVDA",
  "buyPrice": 131.50,
  "shares": 17.0,
  "estimatedCost": 79.56,
  "sellAll": true,
  "strategyName": "After-Hours Limit",
  "afterHours": true,
  "time": "2026-05-28T18:33:58.418127-04:00"
}
```

Set `forceSell: true` to guarantee the position sells at the next morning decision, bypassing the hold/rotate check. `afterHours: true` means the position was entered via AH/PM limit buy and will always sell at 9:35 AM.

## Common errors and fixes

| Error | Fix |
|---|---|
| Session expires mid-run | Auto-handled — `try_auto_login()` fires using WS_EMAIL/WS_PASSWORD |
| Balance returns CAD as USD | CADUSD=X conversion applied in `get_live_balance()` fallback |
| `shares_est == 0` (price > balance) | Bot skips pick and tries next — add to `failed_buys_today` |
| `claude: command not found` | AI analysis skipped silently — install Claude Code CLI |
| Earnings blackout slow on first run | 12h cache warms up after first scan — fast on repeat |
| Short interest slow on first run | 24h cache warms up for top-50 candidates after first scan |

## Environment variables

```
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHAT_ID=@channel_or_numeric_id
WS_EMAIL=<Wealthsimple login email>
WS_PASSWORD=<Wealthsimple password>
```

All vars loaded from `.env` at runtime. Never committed.
