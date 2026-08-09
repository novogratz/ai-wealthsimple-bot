# Le Grinder v5.0

**Autonomous 24/7 momentum rotation bot for Wealthsimple US (NYSE / NASDAQ).**

Le Grinder scans ~350 liquid US tickers, picks the highest-conviction momentum setup, buys it, and rotates on profit or at market close — all without manual intervention. Every 30 minutes it also sends a watchlist alert with the top 3 picks you can trade manually in your own account.

---

## What it does

- **Always deployed** — buys pre-market, intraday, or after-hours. Never sits in cash during market or extended hours.
- **Rotates aggressively** — sells at +10% profit target or trailing stop, immediately re-scans and re-buys. No fees on Wealthsimple.
- **Hard exits** — 3:55 PM sell for all daytime positions. AH/PM positions always sell at 9:35 AM market open.
- **Watchlist for you** — every 30 min, Telegram sends the top 3 picks the bot would buy with more cash, with score and reasons, so you can trade them manually.

---

## The 12-Signal Engine (0–125 pts)

Synthesized from IBKR, Minervini, CANSLIM, and LangChain quant strategies:

| # | Signal | Max pts |
|---|---|---|
| A | Momentum cascade — 1d/5d/20d alignment | 25 |
| B | MACD(12,26,9) — bullish crossover | 12 |
| C | RSI(14) — momentum zone 45–70 | 10 |
| D | Stage 2 MA alignment — Price > SMA50 > SMA150 > SMA200 | 12 |
| E | Volume conviction — RVOL + trend + 1-year volume record | 18 |
| F | 52-week high proximity — within 20% of high (CANSLIM "N") | 10 |
| G | Relative strength vs SPY — outperforms 5d return | 8 |
| H | OBV smart money — volume-weighted direction | 5 |
| I | Bonuses — close quality + ATR + Yahoo trending | 10 |
| J | Sector alignment — stock in top-performing sector (XLK/XLF/XLE…) | 5 |
| K | **Earnings blackout** — hard filter: skip if earnings within 3 days | — |
| L | **Short squeeze radar** — short float > 20% + momentum | 8 |

**Market regime gate:** SPY below SMA200 → all scores × 0.70

---

## Daily Schedule (ET)

| Time | Action |
|---|---|
| 5:00 AM | Futures check + full scan + AI analysis → Telegram game plan |
| 7:00 AM | Pre-market scan → limit buy top < $10 mover |
| 9:31 AM | Morning hold-or-rotate decision (regular overnight positions only) |
| **9:35 AM** | AH/PM positions: market sell + rotate. Regular buy: market order |
| Every 30 min | Position update + **top 3 watchlist picks** (for manual trading) |
| 3:30 PM | Last intraday entry cutoff |
| **3:55 PM** | Hard sell all daytime positions |
| 4:00 PM | After-hours scan → limit buy top < $10 mover |
| 5:00 PM | Next-day preview scan |

---

## Extended Hours Rules

- **Pre-market (7–9:29 AM) / After-hours (4–7:57 PM):** limit buy only, stocks under $10, price set 5% above current to ensure fill
- **NEVER limit sell** outside market hours — all sells happen at 9:35 AM market open
- AH/PM positions skip the morning hold decision and always sell at 9:35 AM then rotate

---

## Installation (macOS)

You can use your normal **Google Chrome** or **Microsoft Edge** installation. Chrome is
recommended if it is already installed; no special Playwright browser download is needed.

```bash
cd /path/to/ai-wealthsimple-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# First-time login (opens Chrome or Edge)
python scripts/wealthsimple_auto.py setup
```

Log in to Wealthsimple in the browser window that opens, return to Terminal, and press
Enter. Keep that browser window open while the bot runs.

Create `.env` in the project root:

```dotenv
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=@yourchannel
WS_EMAIL=you@gmail.com
WS_PASSWORD=yourpassword
```

Run the bot:

```bash
source .venv/bin/activate
python scripts/run_grinder.py

# Optional: watchdog keeps the Mac awake and restarts the browser/bot if needed
python scripts/watchdog.py
```

### Current SPY 0DTE behavior

`run_grinder.py` delegates to the SPY 0DTE bot. On weekdays it:

- refreshes the Wealthsimple Chrome session every two minutes;
- stays running across nights, weekends, and holidays and sends a detailed SPY quant update at every exact `:00` and `:30` ET boundary;
- shows the directional factor contributions and a contract execution-score breakdown for spread, volume, open interest, premium fit, strike distance, and IV;
- considers one autonomous long call or long put entry from 9:45–10:00 AM ET;
- buys the maximum affordable whole contracts, targeting 50–100% of available USD cash;
- refuses any expiry other than the current ET date and any strike outside 4–5 SPY points OTM; and
- only submits sell-to-close orders matching its own local position ledger.

Production safeguards in v3.0.0:

- confirms actual fills from Wealthsimple before trusting entry premium or quantity;
- uses Wealthsimple's displayed bid for exits, with Yahoo only as a fallback;
- cancels an unconfirmed pending bot order automatically;
- enforces one entry per trading day plus a persistent daily-loss lockout;
- handles NYSE holidays and 1:00 PM early closes;
- relaunches Chrome automatically with the persistent trusted profile;
- writes every contract score and lifecycle decision to `data/options_audit.jsonl`; and
- accepts Telegram `/status`, `/stop`, and `/resume` from the configured chat.

`/stop` prevents new entries. If the bot owns a reconciled position, it submits a
sell-to-close for that exact contract and quantity. It never issues a naked sell.

Safe paper-mode launch (no orders):

```bash
python scripts/run_grinder.py --dry
```

## Installation (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install msedge

# First-time login (opens Edge, log in manually, press ENTER)
python scripts/wealthsimple_auto.py setup
```

Create `.env` in the project root:
```
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=@yourchannel
WS_EMAIL=you@gmail.com
WS_PASSWORD=yourpassword
```

```powershell
# Run 24/7
python scripts/run_grinder.py

# Debug: skip overnight wait and buy immediately
python scripts/run_grinder.py --now

# Override balance
python scripts/run_grinder.py --balance 95.50
```

---

## Stack

- **Execution:** Playwright → Edge → Wealthsimple Trade (no API, browser automation)
- **Data:** yfinance batch download with in-memory + disk cache
- **Signals:** Custom quant engine (12 signals) + earnings/short-interest enrichment
- **Intelligence:** Claude Code CLI for qualitative pick analysis at each scan
- **Reporting:** Telegram (position updates, watchlist alerts, trade results, daily report)

---

## Disclaimer

High-risk autonomous trading tool. Past performance is not indicative of future results.
