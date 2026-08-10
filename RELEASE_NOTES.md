# v3.5.1 — Immediate next-session estimate

- Publishes the live target or next-session theoretical estimate immediately at startup
- Does not wait for the slower Wealthsimple balance fetch before showing the plan
- Keeps live-chain replacement, reversal, liquidity, cash, and market-hour order gates intact

# v3.5.0 — Half-hour live and dry-run balances

- Every `:00`/`:30` Telegram update shows Wealthsimple available USD cash
- Added persistent dry-run equity starting at exactly `$10,000.00 USD`
- Dry-run P&L includes all modeled exits plus the current marked shadow position
- Equity is rebuilt from the append-only shadow ledger, so restarts preserve performance
- Failed broker balance reads are labeled unavailable instead of being treated as zero cash

# v3.4.0 — Fully autonomous Wealthsimple execution

- New `execution_mode` in `config/spy_0dte.toml` (schema v2):
  - `auto` (default): the prepared order ticket is submitted automatically
  - `review`: fills the ticket and stops at the final review screen (previous behavior)
  - `shadow`: never opens a Wealthsimple ticket (simulation only)
- Buy and sell subprocesses pass `--confirm` in auto mode and now return `submitted`
- `_parse_order_result`/`_order_state` decode the broker `ORDER_RESULT_JSON` payload
- New `BUY SUBMITTED`/`SELL SUBMITTED` notifications, partial and full closes
- Auto mode never auto-cancels a real submitted order; sells are blocked until the fill reconciles
- Mode labels surfaced in the session banner, plan reports and startup health

# v3.3.0 — Five-minute named contract targets

- Sends a named SPY contract target and rationale every five minutes
- Keeps full quant reports at exact `:00` and `:30` without duplicate messages
- Uses live eligible exact-0DTE quotes whenever available
- Adds next-NYSE-session Black–Scholes previews when the live chain is closed
- Labels every theoretical preview non-actionable and replaces it with the live chain
- Includes assumed SPY, VIX-derived IV, strike, expiry and theoretical premium

# v3.2.2 — macOS virtualenv bootstrap repair

- Detects the project virtualenv with `sys.prefix` instead of resolved symlink paths
- Reliably re-executes `python3 scripts/watchdog.py` under `.venv/bin/python`
- Adds `--check-runtime` interpreter diagnostics

# v3.2.1 — macOS Wealthsimple reconnect repair

- Detects Wealthsimple login by URL, password chooser, email field or password field
- Supports the current “Log in with a password” step before filling credentials
- Removes the false “session active” result on email/passkey-first login pages
- Prevents watchdog from launching the bot until authentication is positively confirmed
- Exits failed auto-login checks nonzero and leaves manual login/2FA visible

# v3.2.0 — Professional research controls

- Replaced fixed strike distance with strict OTM $0.25–$0.70 premium eligibility
- Rebalanced contract scoring around a $0.475 premium center
- Added validated TOML configuration and audit configuration hashes
- Added quote source/retrieval timestamps and maximum quote-age checks
- Added operator-maintained high-impact economic-event blackouts
- Added startup health reporting and single-instance locks
- Added empirical probability calibration and formal promotion gates
- Added shadow MFE, MAE, half-hour marks and alternative exit observations

# v3.1.3 — Deterministic Wealthsimple startup refresh

- Refreshes Wealthsimple synchronously before the startup SPY scan
- Confirms the browser session and logs the startup refresh result
- Continues the existing background refresh every two minutes

# v3.1.2 — Eight-point contracts and resilient macOS launch

- Moved the SPY contract band to 7–8 points OTM, capped at eight points
- Models maximum affordable whole-contract sizing up to 100% of available USD cash
- Watchdog automatically restarts itself under `.venv/bin/python` when launched with `python3`
- Watchdog starts the active SPY runner directly instead of importing the legacy stock engine

# v3.1.1 — Documentation and operator skill

- Replaced all stale stock-grinder and Windows documentation with the active SPY 0DTE system
- Added complete directional, contract-scoring, sizing, exit, shadow, and validation reference
- Added macOS setup, launch, Telegram, state, incident, and release runbook
- Added validated `spy-0dte-operator` Codex skill with UI metadata and progressive references
- Documented the manual broker-confirmation boundary and known model limitations consistently

# v3.1.0 — Quant research and shadow execution

- Independent append-only shadow ledger alongside each eligible decision
- Half-hour shadow marking and simulated exit lifecycle
- Hard two-sided quote, spread, volume, and open-interest quality gates
- Chronological walk-forward evaluator with out-of-sample performance metrics
- Broker tickets stop at final review for explicit human confirmation
- Twenty automated tests covering research, risk, ownership, and scheduling

# v3.0.1 — Immediate startup quant plan

- Runs a SPY scan immediately when the service starts
- Sends the complete directional rationale and proposed contract plan to Telegram
- Continues detailed updates at exact `:00` and `:30` ET boundaries, 24/7

# v3.0.0 — SPY 0DTE production hardening

- Broker-confirmed option fill reconciliation
- Wealthsimple-first exit quotes
- Exact pending-order cancellation and bot-owned sell protection
- NYSE holiday and early-close calendar
- Persistent one-trade and daily-loss lockouts
- Telegram emergency stop, resume, and status commands
- Automatic Chrome recovery and two-minute session keepalive
- Structured JSONL score and lifecycle audit trail
- Persistent 24/7 Telegram reporting at exact half-hour boundaries
- Detailed directional factors and 4–5 point OTM contract score decomposition
- Market entry with maximum affordable whole-contract sizing

0DTE options can lose the full premium. This release improves operational controls;
it does not guarantee profitability.
