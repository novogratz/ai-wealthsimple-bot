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
