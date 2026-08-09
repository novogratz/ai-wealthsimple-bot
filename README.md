# SPY 0DTE Quant Research Service

This macOS service researches one intraday SPY option setup per NYSE trading day,
publishes its reasoning to Telegram, maintains an independent shadow ledger, and prepares
an exact Wealthsimple order ticket for manual review. It never sells an option that is not
represented by its bot-owned position ledger.

The current model is a contrarian opening-gap fade. It is deterministic and auditable, but
its hand-set weights and exit thresholds do not constitute proven positive expectancy.

## Strategy in one minute

1. At startup and every `:00`/`:30` ET, refresh the SPY plan and Telegram report.
2. From 9:00 ET, calculate a directional score using the SPY opening gap, RSI(14),
   five-session extension, one-hour ES move, VIX level, and configured regime bias.
3. A negative score proposes puts; a positive score proposes calls. Ambiguous flat sessions
   are skipped.
4. Between 9:45 and 10:00 ET, require a 0.05% reversal away from the opening extreme.
5. Rank exact-0DTE contracts 7–8 SPY points OTM with asks between $0.10 and $0.60.
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

## Commands

Safe research mode:

```bash
source .venv/bin/activate
python scripts/run_spy_options.py --dry
```

Persistent order-review service with browser recovery and macOS sleep prevention:

```bash
source .venv/bin/activate
python scripts/watchdog.py
```

Run only one watchdog. The normal service stays alive overnight, on weekends, and on
holidays; recurring reports continue, while order consideration remains restricted to valid
NYSE sessions.

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

The 9:45 reversal gate prevents an entry proposal while SPY remains pinned at the opening
high for puts or opening low for calls. When market data cannot validate that reversal, the
system waits rather than assuming success.

## Contract score

Eligible contracts receive a 0–100 execution/convexity score:

| Component | Maximum |
|---|---:|
| Relative bid/ask tightness | 30 |
| Same-day volume | 20 |
| Open interest | 12 |
| Fit to $0.35 target premium | 18 |
| Fit to 7.5-point OTM center | 15 |
| IV sanity range | 5 |

The leaderboard explains every component in Telegram and `data/options_audit.jsonl`.
The score ranks contracts; it is not a calibrated probability of profit.

## Timing and exits

| Time ET | Behavior |
|---|---|
| Startup | Immediate quant scan and Telegram plan |
| Every `:00`/`:30` | Plan, market state, or position/shadow update |
| 9:00 | Begin premarket planning loop |
| 9:45–10:00 | Reversal confirmation and potential ticket preparation |
| 3:25 | Mandatory modeled close |
| 3:45 | Nuclear fallback close |

On NYSE early-close sessions, the mandatory close moves to 12:45 ET. The service observes
weekends and its built-in NYSE holiday calendar.

## Safety and state

- `data/options_position.json`: broker-reconciled, bot-owned position ledger.
- `data/options_shadow.jsonl`: append-only shadow decisions and exits.
- `data/options_shadow_position.json`: currently open shadow position.
- `data/options_daily_risk.json`: one-entry/day and daily-loss state.
- `data/options_audit.jsonl`: structured decisions and lifecycle events.
- `data/options.log`: human-readable runtime log.
- `data/browser_profile/`: persistent Chrome profile; never commit it.
- `data/options_emergency_stop`: local emergency-stop flag.

Telegram accepts `/status`, `/stop`, and `/resume` only from the configured chat. `/stop`
blocks new entries and prepares an exact close ticket for a reconciled bot-owned position.
All securities tickets require final human confirmation in Wealthsimple.

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
- A fixed 7–8-point strike distance does not normalize exposure by delta or volatility.
- Market orders have uncertain execution prices; always inspect the final broker debit.
- Full-account 0DTE sizing can lose the entire premium in one session.
- Browser UI automation can break when Wealthsimple changes its interface.

This repository is an options research and order-review tool, not a promise of returns or
personalized financial advice.
