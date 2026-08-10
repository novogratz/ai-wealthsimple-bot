# CLAUDE.md — Project context

This repository now runs a persistent macOS SPY 0DTE quant research and Wealthsimple
order-review service. `scripts/watchdog.py` starts `scripts/run_spy_options.py` directly;
`scripts/run_grinder.py` remains a compatibility delegate. Its legacy stock-rotation
implementation below the early delegation is inactive.

## Required context

- User-facing overview: `README.md`
- Full strategy equations: `.codex/skills/spy-0dte-operator/references/strategy.md`
- Operations/runbook: `.codex/skills/spy-0dte-operator/references/operations.md`
- Agent workflow: `.codex/skills/spy-0dte-operator/SKILL.md`
- Release history: `RELEASE_NOTES.md`

## Current behavior

- Immediate Telegram plan, concise named contract target every five minutes, and full reports
  at exact `:00` and `:30` ET, 24/7.
- Closed-market targets are theoretical next-session previews and never order inputs.
- One potential SPY call/put ticket per NYSE day during 9:45–10:00 ET.
- Contrarian directional score: SPY gap, RSI, weekly extension, ES, VIX, regime bias.
- Reversal confirmation of 0.05% away from the opening extreme.
- Exact 0DTE, strictly OTM, $0.25–$0.70 ask; no fixed strike-distance filter.
- Contract ranking by spread, volume, OI, premium fit and IV.
- Hard liquidity gates: two-sided quote, ≤25% spread, volume ≥100, OI ≥250.
- Largest affordable whole-contract modeled sizing and parallel shadow ledger.
- Wealthsimple buy and sell tickets stop at final review for manual confirmation.
- Versioned TOML configuration, event blackouts, quote provenance/freshness,
  duplicate-instance locks, startup health, calibration and promotion gates.

## Start and test

```bash
source .venv/bin/activate
python scripts/run_spy_options.py --dry   # no broker order
python scripts/watchdog.py                # persistent order-review service
python -m unittest discover -s tests -v
```

Never expose or commit tokens, passwords, browser profiles, auth JSON, or runtime data. When
changing signal or exit parameters, update README, the skill strategy reference, tests, and
release notes in the same commit.
