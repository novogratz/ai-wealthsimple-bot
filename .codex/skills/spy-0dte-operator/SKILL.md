---
name: spy-0dte-operator
description: Inspect, explain, test, document, or safely operate this repository's macOS SPY 0DTE quant research and Wealthsimple order-review service. Use for strategy/scoring questions, Telegram plans, dry runs, watchdog startup, broker-session diagnosis, shadow or walk-forward analysis, option lifecycle changes, release checks, and incident response.
---

# SPY 0DTE Operator

Treat the implementation as the source of truth. The legacy stock grinder below the early
delegation in `scripts/run_grinder.py` is inactive.

## Route the task

- Read [references/strategy.md](references/strategy.md) for signals, scoring, gates, sizing,
  exits, shadow evaluation, or research changes.
- Read [references/operations.md](references/operations.md) for setup, launch, Telegram,
  Chrome/Wealthsimple recovery, runtime state, diagnostics, or releases.
- Read both for changes spanning strategy and production operation.

## Inspect before acting

1. Read the relevant reference.
2. Inspect the corresponding implementation and tests; do not trust stale logs or prose.
3. Check `git status --short --branch` and preserve unrelated user changes.
4. Redact credentials and never print `.env`, browser auth, cookies, or tokens.

## Preserve invariants

- Keep the universe limited to SPY and the exact current ET expiration.
- Permit long calls or puts only; never create an uncovered short option.
- Require sell tickets to match bot-owned expiry, type, strike, and quantity.
- Keep expected debit at or below observed USD cash.
- Keep the browser at final order review; never automate the final securities confirmation.
- Maintain independent audit and shadow ledgers.
- Fail closed on missing, crossed, stale-looking, wide, or illiquid quotes.
- Keep NYSE holiday and early-close behavior intact.

## Validate changes

Run:

```bash
source .venv/bin/activate
python -m py_compile kzer_bot/*.py scripts/run_spy_options.py scripts/wealthsimple_auto.py
python -m unittest discover -s tests -v
git diff --check
```

For browser changes, run only non-submitting diagnostics unless the user explicitly performs
the final broker confirmation. For documentation changes, search every Markdown file for
stale Windows paths, stock-grinder claims, automated-submission claims, and inconsistent
version numbers.

## Communicate accurately

Separate:

- directional score (call versus put hypothesis);
- contract score (relative execution-quality ranking);
- quote-quality gates (eligibility);
- shadow results (simulated observations); and
- broker state (observed Wealthsimple position/order state).

Never call either score a probability, claim the model is proven, or promise profit. State
that 0DTE premium can be lost completely and browser/data sources can fail.
