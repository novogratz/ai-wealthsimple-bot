# SPY 0DTE Quant Research Service

This macOS service researches one intraday SPY option setup per NYSE trading day,
publishes its reasoning to Telegram and can operate as a report-only contract scanner. In
the default report mode it never creates an entry, shadow position, or Wealthsimple ticket.

The default report model is a deterministic intraday trend-confirmation score. It combines
the five-minute opening range, session VWAP displacement and five-minute momentum, then
separately ranks executable exact-0DTE contracts. Its weights require walk-forward validation
and do not constitute proven positive expectancy.

## Strategy in one minute

1. Publish the highest-quality eligible SPY 0DTE contract every 10 minutes during market hours.
2. From 9:00 ET, calculate a directional score using the SPY opening gap, RSI(14),
   five-session extension, one-hour ES move, VIX level, and configured regime bias.
3. Require aligned opening-range breakout, VWAP position and five-minute momentum; weak or
   conflicting evidence is `NO TRADE`.
4. Positive scores select calls and negative scores select puts.
5. Rank strictly OTM exact-0DTE contracts with asks between $0.50 and $3.00.
6. Reject contracts without a valid two-sided quote, spread ≤25%, volume ≥100, and open
   interest ≥250.
7. Size the largest affordable whole-contract quantity, using as close to 100% of available
   USD cash as indivisible contracts permit, without exceeding cash.
8. Maintain a separate shadow position and evaluate +500% and mandatory time exits.

The complete equations and lifecycle are in [strategy.md](.codex/skills/spy-0dte-operator/references/strategy.md).

## macOS setup

```bash
cd /Users/benoitfloch/ai-wealthsimple-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python scripts/wealthsimple_auto.py setup
```

Use the Chrome window opened by setup, sign into Wealthsimple, and leave it open. Store
credentials locally in `.env`; this file is gitignored:

```dotenv
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_CHAT_ID=<numeric group id>
WS_EMAIL=<Wealthsimple email>
WS_PASSWORD=<Wealthsimple password>
```

Rotate any token that has appeared in chat, terminal history, screenshots, or logs.

On a new Mac, the bot-specific Chrome profile does not inherit the Wealthsimple session from
Windows or from normal Chrome. Complete login and any 2FA once in the window opened by
`wealthsimple_auto.py setup`. The reconnect detector recognizes Wealthsimple's current
passkey/password chooser and legacy password form. If neither a valid session nor local
credentials exist, watchdog stays stopped and retries instead of reporting a false success.

## Commands

Safe research mode:

```bash
source .venv/bin/activate
python scripts/run_spy_options.py --dry
```

Persistent service (auto-executes by default; see `execution_mode` in `config/spy_0dte.toml`):

```bash
source .venv/bin/activate
python scripts/watchdog.py
```

Run only one watchdog. The normal service stays alive overnight, on weekends, and on
holidays; recurring reports continue, while order consideration remains restricted to valid
NYSE sessions.

At startup, watchdog synchronously opens or refreshes Wealthsimple home and verifies the session
before launching the first SPY scan. The bot then refreshes Wealthsimple every two minutes.

Walk-forward evaluation of exported outcomes:

```bash
python scripts/run_walk_forward.py data/spy_outcomes.csv --train 60 --test 20
```

The CSV requires `timestamp`, `score`, and `return` columns.

## Directional score

Positive contributions lean toward calls; negative contributions lean toward puts.

| Factor | Rule |
|---|---|
| Opening gap/intraday move | `-25 × SPY move %` |
| RSI(14) | −20 to +20 at overbought/oversold bands |
| Five-session return | −15 to +15 for directional extension |
| ES one-hour move | `-5 × ES move %` |
| VIX | +5 below 12; −5 above 20; −10 above 25; skip above 40 |
| Regime bias | Configured additive constant; currently 0 |

The 9:45 reversal gate applies to red-open calls and prevents entry while SPY remains pinned
at the opening low. Flatish/green puts use the 9:31 path. Missing opening data, invalid quotes,
or failed safety gates always fail closed.

## Contract score

Eligible contracts receive a 0–100 execution/convexity score:

| Component | Maximum |
|---|---:|
| Relative bid/ask tightness | 30 |
| Same-day volume | 20 |
| Open interest | 12 |
| Premium execution band | 18 |
| Near-the-money exposure | 15 |
| IV sanity range | 5 |

The leaderboard explains every component in Telegram and `data/options_audit.jsonl`.
The score ranks contracts; it is not a calibrated probability of profit.

## Timing and exits

| Time ET | Behavior |
|---|---|
| Startup | Immediate live target or clearly labeled next-session theoretical estimate |
| 9:30 AM–4:00 PM | Report-only best-contract signal every 10 minutes |
| Outside market hours | Non-actionable status every 30 minutes |
| 9:00 | Begin premarket planning loop |
| 9:31 | Flatish/green-open put path; live chain and all execution gates required |
| 9:45–10:00 | Red-open call reversal and potential ticket preparation |
| 3:25 | Mandatory modeled close |
| 3:45 | Nuclear fallback close |

On NYSE early-close sessions, the mandatory close moves to 12:45 ET. The service observes
weekends and its built-in NYSE holiday calendar.

## Safety and state

- `data/options_position.json`: broker-reconciled, bot-owned position ledger.
- `data/options_shadow.jsonl`: append-only shadow decisions and exits.
- `data/options_shadow_position.json`: currently open shadow position.
- `data/options_shadow_marks.jsonl`: periodic P&L, MFE, MAE and exit levels.
- `data/options_daily_risk.json`: one-entry/day and daily-loss state.
- `data/options_audit.jsonl`: structured decisions and lifecycle events.
- `data/options.log`: human-readable runtime log.
- `data/browser_profile/`: persistent Chrome profile; never commit it.
- `data/options_emergency_stop`: local emergency-stop flag.
- `data/options_runner.lock` / `data/watchdog.lock`: duplicate-instance protection.

Every periodic Telegram report rebuilds the dry-run equity curve from the append-only shadow
ledger. It starts at `$10,000.00 USD` and includes realized modeled exits plus the current
marked shadow position. This simulated balance is separate from Wealthsimple available cash.

Telegram accepts `/status`, `/stop`, and `/resume` only from the configured chat. `/stop`
blocks new entries and prepares an exact close ticket for a reconciled bot-owned position.
Execution is controlled by `execution_mode` in `config/spy_0dte.toml`: `report` (default)
only publishes the best quant-ranked contract; `review` stops at final review; `shadow`
models without a broker ticket; and `auto` is the legacy automatic execution mode.

Outside market hours, the target is a Black–Scholes preview for the next NYSE session using
the last SPY price, current VIX as volatility, a 9:45 ET entry assumption and unchanged market
inputs. It includes expiry, call/put, strike and theoretical premium. It is always labeled
`NOT ACTIONABLE`; the live exact-0DTE chain replaces it before any ticket can be prepared.

## Architecture

```text
watchdog.py
  ├─ Chrome health/recovery + caffeinate
  └─ run_spy_options.py
            ├─ spy_options_strategy.py   directional signal and exits
            ├─ quant_research.py         quality gates, shadow ledger, metrics
            ├─ market_calendar.py        NYSE sessions and early closes
            ├─ telegram.py               reports and control commands
            ├─ yfinance                  SPY/ES/VIX and option-chain research data
            └─ wealthsimple_auto.py      balance, quotes, reconciliation, review tickets
```

## Validation status

Run:

```bash
python -m unittest discover -s tests -v
```

Unit tests cover scheduling, ownership checks, affordability, contract scoring, quote
quality, Telegram authorization, shadow persistence, and chronological walk-forward logic.
Browser selectors and actual broker fills remain integration risks because Wealthsimple has
no stable public trading API in this project.

## Limitations

- The directional weights and +500% exit are hypotheses requiring historical and forward
  validation.
- Yahoo/yfinance quotes can be delayed, stale, incomplete, or unavailable.
- Premium-based selection still does not normalize exposure by delta or volatility.
- Market orders have uncertain execution prices; always inspect the final broker debit.
- Full-account 0DTE sizing can lose the entire premium in one session.
- Browser UI automation can break when Wealthsimple changes its interface.

This repository is an options research and order-review tool, not a promise of returns or
personalized financial advice.

## Professional research controls

`config/spy_0dte.toml` is the versioned source for premium, liquidity, signal, schedule,
exit, event and promotion parameters. Every audit record includes its configuration hash.

`config/market_events.json` accepts ISO-8601 ET timestamps for operator-selected high-impact
events. Tickets are blocked during the configured window. An empty calendar is reported and
does not imply event risk was checked online.

Quotes carry source and retrieval time. Startup health reports Chrome/CDP, Telegram state,
emergency stop, event count, configuration hash and mode. File locks prevent duplicate
watchdogs and runners.

Probability remains uncalibrated until `data/spy_outcomes.csv` has at least 60 outcomes.
Walk-forward output includes a promotion decision based on trade count, profitable windows,
profit factor and drawdown. Shadow marks record P&L, MFE, MAE and alternative exit levels.
