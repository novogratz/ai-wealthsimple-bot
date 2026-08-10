# Operations and incident reference

## Contents

1. Setup
2. Launch modes
3. Expected schedule
4. Telegram controls
5. Runtime state
6. Diagnostics
7. Incident response
8. Release procedure

## 1. Setup

```bash
cd /Users/benoitfloch/ai-wealthsimple-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/wealthsimple_auto.py setup
```

Use Chrome or Edge discovered by `wealthsimple_auto.py`. The persistent profile is
`data/browser_profile`, and CDP listens on port 9222. Keep secrets only in gitignored `.env`.
Sessions are profile-specific and do not migrate from Windows or normal Chrome. Complete
login/2FA once in the bot-profile window. Watchdog must not launch the scanner until the
Wealthsimple home session is positively confirmed.

## 2. Launch modes

No-order research:

```bash
source .venv/bin/activate
python scripts/run_spy_options.py --dry
```

Persistent service:

```bash
source .venv/bin/activate
python scripts/watchdog.py
```

Execution behavior is selected by `execution_mode` in `config/spy_0dte.toml`:

- `auto` (default): buys and sells are submitted automatically; no human clicks required.
- `review`: tickets are filled and stop at the final review screen for a human confirm.
- `shadow`: the bot models the trade but never opens a Wealthsimple ticket.

`watchdog.py` enables `caffeinate`, restores Chrome/CDP, synchronously refreshes Wealthsimple
and verifies the session, starts the SPY runner, and restarts it after abnormal exit. The
runner's background keepalive refreshes Wealthsimple every two minutes. Run one instance only.

`--now` bypasses timing for diagnostics and should not be used as a routine launch mode.

## 3. Expected schedule

- Startup: immediate live target or next-session theoretical estimate; the balance/full-state
  report may complete afterward without blocking the target.
- 16:00–09:00: compact cash, paper-equity and target/position snapshots every 30 minutes.
- 09:00–09:30 and 10:00–16:00: compact snapshots every 15 minutes.
- 09:30–10:00: five-minute opening and entry-gate monitoring.
- Orders, fills, skips, exits, errors and emergency controls: immediate alerts.
- 09:00: trading-day planning begins.
- 09:31: flatish/green-open put path and potential ticket after all execution gates.
- 09:45–10:00: red-open call reversal and potential ticket.
- 15:25: regular modeled time exit.
- 15:45: fallback exit.
- Nights/weekends/holidays: reporting continues, ordering remains disabled.

## 4. Telegram controls

- `/status`: report flat/in-position and emergency-stop state.
- `/stop`: create `data/options_emergency_stop`, prevent entry, and prepare an exact close
  ticket for a reconciled bot-owned position (submitted automatically in auto mode).
- `/resume`: remove the stop flag.

Commands from any chat other than `TELEGRAM_CHAT_ID` are ignored. Telegram delivery failure
is logged but does not authorize bypassing safety gates.

Balance checks never open an order surface. If confirmed USD cash is hidden on the read-only
home page, the check returns unavailable and entry fails closed. Option workflows reject any
Shares ticket and require the exact intended strike/type on final review.

## 5. Runtime state

| Path | Purpose |
|---|---|
| `data/options.log` | Timestamped operator log |
| `data/options_audit.jsonl` | Structured notifications, scores and lifecycle events |
| `data/options_position.json` | Broker-reconciled bot-owned position |
| `data/options_shadow.jsonl` | Append-only simulated decisions/exits |
| `data/options_shadow_position.json` | Open simulated position |
| `data/options_shadow_marks.jsonl` | Shadow marks, excursions and exit levels |
| `data/options_daily_risk.json` | Daily entry/loss lockout |
| `data/options_daily_bias.json` | Optional date-scoped operator call/put override |
| `data/options_emergency_stop` | Stop flag |
| `data/options_runner.lock`, `data/watchdog.lock` | Single-instance locks |
| `data/telegram_offset.json` | Telegram polling cursor |
| `data/browser_profile/` | Persistent trusted browser profile |

Never commit runtime state, screenshots, credentials, cookies, `.env`, or authentication JSON.

Strategy parameters live in `config/spy_0dte.toml`; high-impact event timestamps live in
`config/market_events.json`. Parameter changes require tests, documentation, a version
increment and review of the new configuration hash.

## 6. Diagnostics

```bash
python scripts/wealthsimple_auto.py keepalive --once
python -m unittest discover -s tests -v
tail -n 100 data/options.log
tail -n 20 data/options_audit.jsonl
git status --short --branch
```

Confirm one watchdog process:

```bash
pgrep -af 'scripts/watchdog.py|scripts/run_spy_options.py'
```

Do not print `.env` while debugging. Redact any Telegram token exposed outside the local file
and rotate it through BotFather.

## 7. Incident response

### Prevent new activity

Send `/stop` from the configured Telegram group or create the local stop file. Inspect
Wealthsimple directly for actual orders and positions; local ledgers are not broker truth.

### Browser disconnected

Leave the persistent profile intact. Let watchdog relaunch Chrome. If login recovery fails,
run `python scripts/wealthsimple_auto.py setup` and authenticate in the opened window.
The login detector must check the `/login` URL and the current “Log in with a password”
chooser; checking only for a visible password field is insufficient.

### Suspected duplicate process

Inspect PIDs before stopping anything. Stop only the explicit duplicate PID; do not use broad
process-kill patterns that could terminate unrelated Chrome or Python work.

### Uncertain fill

Treat Wealthsimple Activity/Holdings as authoritative. Do not infer a fill from a clicked
button, local screenshot, Yahoo quote, or shadow record.

## 8. Release procedure

1. Update code, tests, `README.md`, `RELEASE_NOTES.md`, and affected skill references.
2. Run compilation, unit tests, skill validation, and `git diff --check`.
3. Verify no secret or runtime file is staged.
4. Commit and push `main` through the configured SSH remote.
5. Create an annotated semantic-version tag and push it.
6. Publish the GitHub Release only from an authenticated session.

Do not move an existing public tag; increment the patch/minor version instead.
