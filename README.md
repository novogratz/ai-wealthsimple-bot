# Le Grinder 🇨🇦

Autonomous Canadian stock day-trading bot for Wealthsimple.

**Rules:** 1 trade/day · no stop loss · hard exit 3:55 PM ET · target +1.5–3%/day

---

## How it works

1. **5:00 AM** — Scans 164 Canadian tickers (TSX / TSXV / NEO) using an 8-criteria momentum screen, checks US futures (ES=F) for market bias, calls Claude CLI for AI analysis, sends the full game plan to Telegram.
2. **9:15 AM** — Places a pre-market buy order on Wealthsimple (fills at 9:30 open) if futures are GREEN or NEUTRAL.
3. **9:15 AM** (red days) — Sends a "waiting for bounce" message; buys at 11:00 AM instead.
4. **Every 30 min** — Sends price / P&L / time-to-sell update to Telegram.
5. **3:55 PM** — Hard market sell. No exceptions. Records to `trade_history.csv` and `pnl_ledger.json`.
6. **5:00 PM** — Next-day preview scan.

---

## Strategy — 8 criteria

| # | Criterion | Threshold |
|---|---|---|
| 1 | Price | $2.00 – $40.00 |
| 2 | 20-day avg volume | ≥ 300,000 |
| 3 | Yesterday % change | +1.5% to +12% |
| 4 | Relative volume | ≥ 1.5× average |
| 5 | ATR(14) | ≥ 1.5% of price |
| 6 | Close > 20-day EMA | trend filter |
| 7 | Close > 5-day EMA | short-term filter |
| 8 | Close strength | ≥ 40% of day range |

**Score** = `yesterday_pct × rel_volume^1.5 × atr_pct × (1 + close_strength)`

Fallback strategy fires if the main screen finds no candidates (relaxed thresholds, tagged "Fallback Original Strategy" in Telegram).

---

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install msedge
```

**Authenticate Wealthsimple** (first time only):
```powershell
python scripts/wealthsimple_auto.py setup
```
Log in inside the Edge window, navigate to your home page, then press ENTER in the terminal.

After the first login, session expiry is handled **automatically** — the bot reads `WS_EMAIL` / `WS_PASSWORD` from `.env` and re-logs in without stopping.

**Create `.env`** in the project root:
```
TELEGRAM_BOT_TOKEN=<your bot token from BotFather>
TELEGRAM_CHAT_ID=@yourchannel
WS_EMAIL=you@gmail.com
WS_PASSWORD=yourpassword
```

`WS_EMAIL` and `WS_PASSWORD` enable automatic re-login if the Wealthsimple session expires mid-run. The bot detects the login page, fills credentials, and resumes without human intervention. `.env` is gitignored and never leaves your machine.

---

## Run

```powershell
# Normal 24/7 mode
python scripts/run_grinder.py

# Skip overnight wait and buy immediately (debug)
python scripts/run_grinder.py --now

# Override cash (skips live balance fetch)
python scripts/run_grinder.py --balance 95.50
```

---

## Telegram game plan format

Every morning at 5 AM you receive:

```
🌅 Le Grinder — 5 AM Morning Scan

📡 Futures: 🟢 GREEN — ES=F 5,820 pts (+0.35% vs 24h ago)

🔍 Scanned 164 Canadian tickers
   ✅ 3 passed | Strategy: Main Strategy

━━━━━━━━━━━━━━━━━━━━━━━━
🏆 TODAY'S PICK: ERF.TO  ($4.82 CAD)
━━━━━━━━━━━━━━━━━━━━━━━━
WHY THIS STOCK:
  📈 Yesterday: +3.4%  on  2.3× normal volume
  🔥 ATR(14): 2.6% of price
  💪 Closed: 72% of day range
  📊 Trend: EMA5 ✅  EMA20 ✅
  🎯 Score: 47.2  (MEDIUM ✅)

TODAY'S GAME PLAN:
  📋 Main Strategy
  ⏰ Buy at 9:15 AM pre-market (fills at 9:30 open)
  💰 Budget: $100.00 → deploying 90% = $90.00
  🏁 Exit: 3:55 PM ET hard sell (no stop loss)
  🎯 Target: +1.5% to +3%

🤖 AI Analysis (Claude): ...
```

---

## Skills

```
/grinder   — live scan: top picks, futures bias, AI analysis, full game plan
/codex     — full technical overview of Le Grinder
```

---

## CLI tools

```powershell
# Quick scan
python -m kzer_bot scan --cash 100

# Check P&L history
python -m kzer_bot pnl

# Live balance
python -m kzer_bot balance

# Test Telegram
python -m kzer_bot notify --event info --message "test"

# Manual quote
python -m kzer_bot quote --symbol ERF.TO
```

---

## Files that must never be committed

```
.env                   ← credentials (Telegram + Wealthsimple)
data/ws_auth.json      ← browser session token
data/browser_profile/  ← Edge persistent profile
```

All three are gitignored. Double-check with `git status` before any push.

---

## Further reading

- `CLAUDE.md` — project context for Claude Code (what files do what, common tasks)
- `CODEX.md` — full technical reference (module map, score formula, Wealthsimple protocol, error guide)
