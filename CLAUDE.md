# CLAUDE.md — Project context for Claude Code

## What this is

**Le Grinder** is a 24/7 autonomous Canadian stock day-trading bot that:
- Scans the full TSX + TSXV universe (~4,000 tickers) each morning via a 3-tier momentum strategy
- Checks US futures (ES=F) to decide *when* to buy (open vs bounce)
- Places one trade per day via Wealthsimple browser automation (Playwright + Edge)
- Sends all updates to Telegram with smart timing ("Buying in 11h 20min — tomorrow")
- Uses Claude CLI for AI-powered pick analysis every morning
- Writes all output to `data/grinder.log` for easy debugging

## Key files

| File | Role |
|---|---|
| `scripts/run_grinder.py` | **Main entry point** — 24/7 loop, orchestrates everything |
| `kzer_bot/grinder_strategy.py` | Strategy logic: 8-criteria scan, fallback, best-effort, futures bias, watchlist |
| `scripts/update_universe.py` | Fetches full TSX/TSXV ticker list from TMX API → `data/universe.json` |
| `kzer_bot/market_data.py` | Old yfinance data layer (used by hold loop live price checks) |
| `kzer_bot/telegram.py` | Telegram helpers: `send_message()`, `trade_message()`, `load_dotenv()` |
| `kzer_bot/cli.py` | CLI commands: `scan`, `paper`, `watch`, `balance`, `pnl` |
| `scripts/wealthsimple_auto.py` | Playwright browser automation (buy, sell, balance, setup) |
| `scripts/run_day.py` | **Old kzer strategy** — still works, uses config/universe.csv |
| `config/settings.toml` | Risk/timing config for the OLD kzer strategy |
| `data/open_position.json` | Live position state (JSON) |
| `data/pnl_ledger.json` | Cumulative P&L ledger |
| `data/trade_history.csv` | Full trade log (CSV) |
| `data/grinder.log` | Persistent log file — all bot output, rotates at 5 MB |
| `data/universe.json` | Full TSX/TSXV ticker universe (auto-generated, gitignored) |
| `.env` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` + `WS_EMAIL` + `WS_PASSWORD` (never commit) |

## Running the bot

```powershell
# Normal 24/7 mode
python scripts/run_grinder.py

# Skip the overnight wait and buy immediately (debug/test)
python scripts/run_grinder.py --now

# Override cash amount (skips live Wealthsimple fetch)
python scripts/run_grinder.py --balance 95.50
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

## Strategy summary (v3.3)

### 3-tier picks (never skips a day)

**Tier 1 — Main (8 criteria):**  
price $2–$40 | avg vol ≥ 300k | yesterday +1.5–+12% | rel vol ≥ 1.5× | ATR14 ≥ 1.5% | above EMA20 | above EMA5 | close strength ≥ 0.40

**Tier 2 — Fallback (relaxed):**  
Fires when main finds nothing. price $1–$40 | avg vol ≥ 100k | pct +1–15% | rel vol ≥ 1.0× | above EMA20

**Tier 3 — Best Available (guaranteed):**  
Fires when both above fail. No filters — picks highest-scoring ticker with positive momentum from full universe. Tagged "Best Available" with warning in Telegram.

**Score:** `yesterday_pct × rel_volume^1.5 × atr_pct × (1 + close_strength)`

**Futures bias (ES=F):**
- ≥ +0.3% → GREEN → buy at 9:15 AM
- ≤ -0.3% → RED   → wait, buy 11:00–12:00 PM
- else     → NEUTRAL → buy at 9:15 AM

**Exit:** hard market sell at 3:55 PM ET. No stop loss. One trade per day.

## Daily schedule

| Time (ET) | Action |
|---|---|
| Startup | Auto-refresh universe if >7 days old |
| 5:00 AM | Futures check + full scan + AI analysis → Telegram game plan with countdown |
| 9:15 AM | Buy (green/neutral) — pre-market order, fills at 9:30 open |
| 9:15 AM | Red days: "Waiting for bounce" Telegram message |
| 11:00 AM | Buy (red bias) — bounce window |
| Every 30 min | Position update to Telegram (price / P&L / time-to-sell) |
| 3:55 PM | Hard sell everything |
| 5:00 PM | Next-day preview scan |

## Universe management

The screener uses the **full TSX + TSXV listing** (~4,000 tickers) loaded from `data/universe.json`.

- Auto-refreshed on startup if the file is >7 days old
- Fetched from the TMX public API (`tsx.com/json/company-directory`)
- Hardcoded fallback list of ~100 verified tickers used if the file is missing

```powershell
# Manual refresh
python scripts/update_universe.py

# Check current stats
python scripts/update_universe.py --stats
```

## Debugging

**Read the log:** `data/grinder.log` contains all bot output with timestamps.  
When main strategy finds 0 candidates, the log shows the top 5 by raw score + exact filter failures:
```
  NG.TO   score=76.0  pct=+6.5%  relvol=1.0x  ->  relvol=1.0x<1.5x | below_ema20
```

## AI analysis

The bot calls `claude -p "..."` (Claude Code CLI) at each scan to get qualitative reasoning on the top picks. If claude CLI is not on PATH, this step is silently skipped.

## Skills

```
/grinder   — live scan, top picks, AI analysis, game plan
/codex     — full technical overview of Le Grinder
```

## Common tasks for Claude Code

- **Check bugs:** read `data/grinder.log` directly — no need to copy-paste terminal output
- **Refresh universe:** run `python scripts/update_universe.py`
- **Add a specific ticker:** edit `_HARDCODED_WATCHLIST` in `kzer_bot/grinder_strategy.py`
- **Adjust main criteria:** edit class constants in `GrinderStrategy`
- **Adjust fallback criteria:** edit class constants in `FallbackStrategy`
- **Change sell time:** edit `_SELL_HOUR` / `_SELL_MINUTE` in `scripts/run_grinder.py`
- **Debug a scan:** `python -m kzer_bot scan --cash 100`
- **Check PnL:** `python -m kzer_bot pnl`
- **Test Telegram:** `python -m kzer_bot notify --event info --message "test"`

## Architecture

```
run_grinder.py
  ├─ update_universe.py   ← TMX API → data/universe.json (weekly)
  ├─ grinder_strategy.py  ← GrinderMarketData (batch yfinance)
  │    ├─ GrinderStrategy     (main 8-criteria)
  │    ├─ FallbackStrategy    (relaxed)
  │    └─ BestEffortStrategy  (guaranteed pick)
  ├─ telegram.py          ← send_message()
  └─ wealthsimple_auto.py ← Playwright → Edge → Wealthsimple (buy/sell/balance)
```

Data flows: universe.json → scan → AI analysis → game plan Telegram (with countdown) → buy order → position file → 30-min updates → sell → P&L ledger + CSV.

## What NOT to do

- Do not commit `.env`, `data/ws_auth.json`, `data/browser_profile/`, or `data/universe.json` — all gitignored
- Do not add stop losses — the strategy is time-based exit only
- Do not change the sell time without testing — 3:55 PM is intentional (5 min before TSX close)
- Do not run `run_day.py` and `run_grinder.py` simultaneously (both write to `open_position.json`)
- Do not put credentials directly in code — always use `.env`
