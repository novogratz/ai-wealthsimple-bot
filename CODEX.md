# CODEX.md — SPY 0DTE technical reference

## Active system

The active entry point is `scripts/run_grinder.py`, which immediately delegates to
`scripts/run_spy_options.py`. The old multi-ticker stock grinder remains in the file for
history but is unreachable after delegation and must not be described as production logic.

Read these sources before changing behavior:

1. `kzer_bot/spy_options_strategy.py` — directional factors, contract universe, exits.
2. `scripts/run_spy_options.py` — orchestration, sizing, broker review and reporting.
3. `kzer_bot/quant_research.py` — quote gates, shadow ledger and walk-forward metrics.
4. `scripts/wealthsimple_auto.py` — fragile Wealthsimple/Chrome UI integration.
5. `kzer_bot/market_calendar.py` and `kzer_bot/telegram.py` — schedule and controls.
6. `.codex/skills/spy-0dte-operator/references/strategy.md` — full equations.
7. `.codex/skills/spy-0dte-operator/references/operations.md` — operating procedures.

## Non-negotiable invariants

- SPY only; exact current ET expiration only.
- Long calls or puts only; never open a short option.
- A sell ticket must match the bot-owned ledger by expiry, type, strike, and quantity.
- Contract distance must remain 4–5 SPY points OTM unless tests and documentation change.
- Expected debit must never exceed observed USD cash.
- Live browser workflows stop at final review; do not add `--confirm` to automated calls.
- Preserve the independent append-only shadow and audit trails.
- Do not commit `.env`, auth state, browser profiles, screenshots, or runtime ledgers.
- Keep macOS interpreter paths based on `sys.executable`; never restore `.venv/Scripts/python.exe`.

## Verification

```bash
source .venv/bin/activate
python -m py_compile kzer_bot/*.py scripts/run_spy_options.py scripts/wealthsimple_auto.py
python -m unittest discover -s tests -v
git diff --check
```

For strategy explanations, distinguish the directional score from the contract score. Neither
is a calibrated probability. State known limitations and never imply profitability.
