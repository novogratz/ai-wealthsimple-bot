# CLAUDE.md — Project context for Claude Code

## What this is

**Le Grinder** is a 24/7 autonomous US stock day-trading bot (NYSE / NASDAQ via Wealthsimple) that:
- Scans ~350 liquid US tickers (hardcoded) or a custom `us_universe.json` via a 4-tier momentum strategy
- Checks US futures (ES=F) to decide *when* to buy (open vs bounce)
- Trades freely between 9:35 AM and 3:30 PM ET — **intraday rotation** after each exit (no fees)
- Exits autonomously: +10% profit target, trailing stop (+2% trigger / 1% trail), 3:55 PM hard close
- AH/PM positions (limit buy outside hours) **always sell at market at 9:35 AM** then rotate — no hold decision
- Sends all updates to Telegram including a **30-min watchlist alert** (top 3 manual picks with reasons)
- Writes all output to `data/grinder.log`

## Key files

| File | Role |
|---|---|
| `scripts/run_grinder.py` | **Main entry point** — 24/7 loop, orchestrates everything |
| `kzer_bot/grinder_strategy.py` | Strategy logic: SmartGrinderStrategy (12-signal, 0–125 pts), fallback, best-effort |
| `kzer_bot/market_data.py` | yfinance data layer (used by hold loop live price checks) |
| `kzer_bot/telegram.py` | Telegram helpers: `send_message()`, `trade_message()` |
| `kzer_bot/cli.py` | CLI commands: `scan`, `paper`, `watch`, `balance`, `pnl` |
| `scripts/wealthsimple_auto.py` | Playwright browser automation (buy, sell, balance, setup) |
| `data/open_position.json` | Live position state (JSON) |
| `data/pnl_ledger.json` | Cumulative P&L ledger |
| `data/trade_history.csv` | Full trade log (CSV) |
| `data/grinder.log` | Persistent log file — all bot output, rotates at 5 MB |
| `data/scan_state.json` | Latest scan picks, shortlist, bias (used by 30-min watchlist alert) |
| `data/smart_context_cache.json` | SPY 5d %, sector returns, trending tickers (2h TTL) |
| `data/earnings_cache.json` | Earnings blackout cache — next earnings date per symbol (12h TTL) |
| `data/short_interest_cache.json` | Short % of float per symbol (24h TTL) |
| `.env` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` + `WS_EMAIL` + `WS_PASSWORD` (never commit) |

## Running the bot

```powershell
# Normal 24/7 mode
python scripts/run_grinder.py

# Skip the overnight wait and buy immediately (debug/test)
python scripts/run_grinder.py --now

# Override cash amount (skips live Wealthsimple fetch)
python scripts/run_grinder.py --balance 95.50

# Buy a specific US ticker immediately
python scripts/run_grinder.py --ticker NVDA
```

## First-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install msedge

# First-time Wealthsimple login (opens Edge, log in manually, press ENTER)
python scripts/wealthsimple_auto.py setup
```

Create `.env` in the project root (never commit this file):
```
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=@yourchannel
WS_EMAIL=you@gmail.com
WS_PASSWORD=yourpassword
```

`WS_EMAIL` / `WS_PASSWORD` power the auto-login recovery: if the session expires mid-run, `wealthsimple_auto.py` detects the login page and re-authenticates automatically.

## Strategy summary (v5.0)

### 12-signal Quant Engine (0–125 pts)
Primary screener synthesizing institutional strategies (IBKR, Minervini, CANSLIM, LangChain):

| Signal | Points | Description |
|---|---|---|
| A | 0–25 | **Momentum cascade** — 1d/5d/20d alignment |
| B | 0–12 | **MACD(12,26,9)** — bullish crossover / above signal |
| C | 0–10 | **RSI(14) zone** — 45–70 momentum, <35 bounce |
| D | 0–12 | **Stage 2 alignment** — Price > SMA50 > SMA150 > SMA200 (Minervini) |
| E | 0–18 | **Volume conviction** — RVOL + trend + 1-year volume breakthrough |
| F | 0–10 | **52-week proximity** — within 20% of 52-week high (CANSLIM "N") |
| G | 0–8 | **Relative strength vs SPY** — outperforms SPY 5d return |
| H | 0–5 | **OBV smart money** — volume-weighted direction |
| I | 0–10 | **Bonuses** — close strength + ATR + Yahoo trending |
| J | 0–5 | **Sector alignment** — stock in top-performing sector (XLK/XLF/XLE/XLV/XLI/XLY) |
| K | hard | **Earnings blackout** — filtered out if earnings within 3 calendar days |
| L | 0–8 | **Short squeeze** — short float >20% + momentum = squeeze setup |

Market regime gate: SPY below SMA200 → all scores × 0.70

### Mandate: 10% Daily Alpha
- **Zero Idle Cash:** Always deployed (PM → Intraday → AH → Overnight)
- **Hard Target:** +10% unrealized profit → sell immediately & rotate
- **Trailing Stop:** Triggered at +2%, 1% trail distance
- **3:55 PM Lock:** Hard close of all daytime positions

### Extended Hours Rules
- **Pre-Market (7–9:29 AM):** Limit buy stocks < $10 at 5% above current price
- **After-Hours (4–7:57 PM):** Limit buy stocks < $10 at 5% above current price
- **NEVER limit sell** in extended hours — all sells happen at 9:35 AM market open
- AH/PM positions skip the morning hold decision — always sell at 9:35 AM and rotate

### 30-minute watchlist alert
Every 30 minutes during market hours, Telegram sends:
1. Position update (price, P&L, time to sell)
2. Top 3 picks from the last scan the bot would buy if it had more cash — with score + reasons (for manual trading)

## Daily schedule

| Time (ET) | Action |
|---|---|
| Startup | Log balance, send game plan if in market hours |
| 5:00 AM | Futures check + full scan + AI analysis → Telegram game plan with countdown |
| 7:00 AM | Pre-market scan → limit buy top < $10 mover if cash available |
| 9:31 AM | Morning hold decision for regular overnight positions (sell or keep) |
| 9:35 AM | AH/PM positions → market sell + rotate. Regular buys → market order |
| 11:00 AM | Buy window for red-bias days (bounce entry) |
| Every 30 min | Position update + top 3 watchlist alert to Telegram |
| Any time | Profit target hit (+10%) → sell + rotate |
| 3:30 PM | Last entry cutoff for intraday rotation |
| 3:55 PM | Hard sell all daytime positions |
| 4:00 PM | After-hours scan → limit buy top < $10 mover if cash available |
| 5:00 PM | Next-day preview scan |

## Debugging

**Read the log:** `data/grinder.log` contains all bot output with timestamps.

When the main strategy finds 0 candidates, the log shows the top 5 by raw score + filter failures:
```
  NVDA   score=45.0  pct=+0.3%  relvol=0.8x  ->  pct=0.3%<0.5% | relvol=0.8x<1.0x
```

## AI analysis

The bot calls `claude -p "..."` (Claude Code CLI) at each scan for qualitative reasoning on top picks. Silently skipped if claude CLI is not on PATH.

## Skills

```
/grinder   — live scan, top picks, AI analysis, game plan
/codex     — full technical overview of Le Grinder
```

## Common tasks for Claude Code

- **Check bugs:** read `data/grinder.log` — no need to copy-paste terminal output
- **Add a ticker:** edit `_HARDCODED_WATCHLIST` in `kzer_bot/grinder_strategy.py`
- **Add a sector mapping:** edit `_SECTOR_MAP` in `kzer_bot/grinder_strategy.py`
- **Adjust main criteria:** edit class constants in `GrinderStrategy`
- **Adjust fallback criteria:** edit class constants in `FallbackStrategy`
- **Change sell time:** edit `_SELL_HOUR` / `_SELL_MINUTE` in `scripts/run_grinder.py`
- **Debug a scan:** `python -m kzer_bot scan --cash 100`
- **Check PnL:** `python -m kzer_bot pnl`
- **Test Telegram:** `python -m kzer_bot notify --event info --message "test"`

## Architecture

```
run_grinder.py
  ├─ grinder_strategy.py  ← GrinderMarketData (batch yfinance)
  │    ├─ SmartGrinderStrategy  (tier 0: 12-signal composite)
  │    │    ├─ _SECTOR_MAP           (symbol → XLK/XLF/XLE/XLV/XLI/XLY)
  │    │    ├─ _is_earnings_blackout (12h cache, yfinance calendar)
  │    │    └─ _squeeze_bonus        (24h cache, short % of float)
  │    ├─ GrinderStrategy      (tier 1: 8-criteria hard filters)
  │    ├─ FallbackStrategy     (tier 2: relaxed)
  │    └─ BestEffortStrategy   (tier 3: guaranteed pick)
  ├─ telegram.py          ← send_message()
  └─ wealthsimple_auto.py ← Playwright → Edge → Wealthsimple (buy/sell/balance)
```

Data flows: WATCHLIST → scan → earnings/squeeze enrichment → AI analysis → game plan Telegram → buy order → position file → 30-min updates + watchlist alert → sell → P&L ledger + CSV.

## What NOT to do

- Do not commit `.env`, `data/ws_auth.json`, `data/browser_profile/` — all gitignored
- Do not add stop losses — exits are target/trail/time-based only
- Do not change the sell time without testing — 3:55 PM is intentional (5 min before NYSE close)
- Do not add limit sells in extended hours — `_afterhours_sell_limit` and `_premarket_sell_limit` are permanently disabled stubs
- Do not run `run_day.py` and `run_grinder.py` simultaneously (both write to `open_position.json`)
- Do not put credentials directly in code — always use `.env`
- Do not append `.TO` or `.V` to tickers — this bot trades US stocks only
