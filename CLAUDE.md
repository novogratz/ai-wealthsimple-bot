# CLAUDE.md — Project context for Claude Code

## What this is

**ai-wealthsimple-bot** is a 24/7 autonomous Canadian stock day-trading bot that:
- Scans 164 Canadian tickers (TSX / TSXV / NEO) each morning using a momentum strategy
- Checks US futures (ES=F) to decide *when* to buy (open vs bounce)
- Places one trade per day via Wealthsimple browser automation (Playwright + Edge)
- Sends all updates to Telegram
- Uses Claude CLI for AI-powered pick analysis every morning

## Key files

| File | Role |
|---|---|
| `scripts/run_grinder.py` | **Main entry point** — 24/7 loop, orchestrates everything |
| `kzer_bot/grinder_strategy.py` | Strategy logic: 8-criteria scan, fallback, futures bias, watchlist |
| `kzer_bot/market_data.py` | Old yfinance data layer (used by watch loop price checks) |
| `kzer_bot/telegram.py` | Telegram helpers: `send_message()`, `trade_message()`, `load_dotenv()` |
| `kzer_bot/cli.py` | CLI commands: `scan`, `paper`, `watch`, `balance`, `pnl` |
| `scripts/wealthsimple_auto.py` | Playwright browser automation (buy, sell, balance, setup) |
| `scripts/run_day.py` | **Old kzer strategy** — still works, uses config/universe.csv |
| `config/settings.toml` | Risk/timing config for the OLD kzer strategy |
| `config/universe.csv` | Old universe (not used by grinder — watchlist is hardcoded) |
| `data/open_position.json` | Live position state (JSON) |
| `data/pnl_ledger.json` | Cumulative P&L ledger |
| `data/trade_history.csv` | Full trade log (CSV) |
| `.env` | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (never commit) |

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

# Authenticate Wealthsimple (saves session to data/ws_auth.json)
python scripts/wealthsimple_auto.py setup

# Create .env
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=@yourchannel
```

## Strategy summary (v3.2)

**8 criteria (main):** price $2–$40 | avg vol ≥ 300k | yesterday +1.5–+12% | rel vol ≥ 1.5× | ATR14 ≥ 1.5% | above EMA20 | above EMA5 | close strength ≥ 0.40

**Score:** `yesterday_pct × rel_volume^1.5 × atr_pct × (1 + close_strength)`

**Fallback:** relaxed criteria fires if main finds nothing → tagged "Fallback Original Strategy"

**Futures bias (ES=F):**
- ≥ +0.3% → GREEN → buy at 9:15 AM
- ≤ -0.3% → RED   → wait, buy 11:00–12:00 PM
- else     → NEUTRAL → buy at 9:15 AM

**Exit:** hard market sell at 3:55 PM ET. No stop loss. One trade per day.

## Daily schedule

| Time (ET) | Action |
|---|---|
| 5:00 AM | Futures check + full scan + AI analysis → Telegram game plan |
| 9:15 AM | Buy (green/neutral) — pre-market order, fills at 9:30 open |
| 9:15 AM | Red days: "Waiting for bounce" Telegram message |
| 11:00 AM | Buy (red bias) — bounce window |
| Every 30 min | Position update to Telegram (price / P&L / time-to-sell) |
| 3:55 PM | Hard sell everything |
| 5:00 PM | Next-day preview scan |

## AI analysis

The bot calls `claude -p "..."` (Claude Code CLI) at each scan to get qualitative reasoning on the top picks. If claude CLI is not on PATH, this step is silently skipped.

## Common tasks for Claude Code

- **Add a ticker:** edit `WATCHLIST` in `kzer_bot/grinder_strategy.py`
- **Adjust criteria:** edit class constants in `GrinderStrategy` or `FallbackStrategy`
- **Change sell time:** edit `_SELL_HOUR` / `_SELL_MINUTE` in `scripts/run_grinder.py`
- **Debug a scan:** `python -m kzer_bot scan --cash 100`
- **Check PnL:** `python -m kzer_bot pnl`
- **Test Telegram:** `python -m kzer_bot notify --event info --message "test"`

## Architecture

```
run_grinder.py
  ├─ grinder_strategy.py  ← GrinderMarketData (yfinance) + GrinderStrategy + FallbackStrategy
  ├─ telegram.py          ← send_message()
  └─ wealthsimple_auto.py ← Playwright → Edge → Wealthsimple (buy/sell/balance)
```

Data flows: scan → AI analysis → game plan Telegram → buy order → position file → 30-min updates → sell → P&L ledger + CSV.

## What NOT to do

- Do not commit `.env`, `data/ws_auth.json`, or `data/browser_profile/`
- Do not add stop losses — the strategy is time-based exit only
- Do not change the sell time without testing — 3:55 PM is intentional (5 min before TSX close)
- Do not run `run_day.py` and `run_grinder.py` simultaneously (both write to `open_position.json`)
