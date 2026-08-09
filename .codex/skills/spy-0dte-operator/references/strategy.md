# Strategy and algorithm reference

## Contents

1. Objective and boundaries
2. Daily state machine
3. Directional model
4. Reversal gate
5. Contract universe and quality gates
6. Contract ranking
7. Sizing
8. Exit model
9. Shadow and validation model
10. Known limitations

## 1. Objective and boundaries

The system evaluates at most one long SPY call or put setup per NYSE trading day. The option
must expire on the current ET date. The implementation does not open short-option positions.
It prepares Wealthsimple tickets for manual confirmation and independently records the
hypothetical decision in a shadow ledger.

Primary sources:

- `kzer_bot/spy_options_strategy.py`
- `scripts/run_spy_options.py`
- `kzer_bot/quant_research.py`

## 2. Daily state machine

```text
startup
  → immediate Telegram scan
  → wait/report through nights, weekends and holidays
  → valid NYSE day at 09:00
  → directional plan
  → 09:45–10:00 reversal confirmation
  → exact-0DTE candidate collection
  → quote/liquidity gates
  → contract ranking and affordability
  → append shadow entry
  → prepare broker ticket for manual review
  → shadow marking at each half hour
  → modeled profit/time exit
```

The emergency-stop flag prevents new tickets. The daily risk file limits the model to one
entry per day and retains realized-loss state.

## 3. Directional model

Sign convention: positive selects calls; negative selects puts.

### SPY opening gap or intraday displacement

Before 10:00, measure today’s open against the prior close. Later reports measure the current
price against today’s open. Contribution:

```text
gap_points = -25 × SPY_move_percent
```

A positive SPY move therefore contributes toward puts; a negative move contributes toward
calls. This encodes the contrarian fade hypothesis.

### RSI(14)

| RSI | Points |
|---:|---:|
| >70 | −20 |
| 65–70 | −12 |
| 60–65 | −6 |
| 40–60 | 0 |
| 35–40 | +6 |
| 30–35 | +12 |
| <30 | +20 |

### Five-session extension

| SPY return | Points |
|---:|---:|
| >+4.0% | −15 |
| +2.5% to +4.0% | −10 |
| +1.5% to +2.5% | −5 |
| −1.5% to +1.5% | 0 |
| −2.5% to −1.5% | +5 |
| −4.0% to −2.5% | +10 |
| <−4.0% | +15 |

### ES and VIX

```text
ES_points = -5 × ES_one_hour_move_percent
```

VIX contributes +5 below 12, −5 above 20, and −10 above 25. VIX above 40 is a hard skip.
`REGIME_BIAS` is an additive operator-controlled constant and is currently neutral at zero.

If the absolute SPY move is below 0.15%, the absolute total score is below 5, and regime bias
is weak, the system skips the day. Otherwise total <0 selects puts and total ≥0 selects calls.

These weights are hand-set hypotheses, not fitted or calibrated probabilities.

## 4. Reversal gate

From 9:45 to 10:00:

- Put plan: current SPY must be at least 0.05% below the session high.
- Call plan: current SPY must be at least 0.05% above the session low.

Missing data fails closed. No confirmation by 10:00 means no ticket that day.

## 5. Contract universe and quality gates

Candidate requirements:

- SPY only.
- Expiration equals today in ET.
- Put strike is 7–8 points below SPY; call strike is 7–8 points above SPY.
- Ask is normally $0.10–$0.60, centered on $0.35.
- Bid and ask are positive and not crossed.
- Relative spread `(ask − bid) / ask` is at most 25%.
- Same-day volume is at least 100.
- Open interest is at least 250.

## 6. Contract ranking

Eligible candidates receive a bounded 0–100 score:

```text
spread    = 30 × (1 − min(relative_spread, 1))
volume    = 20 × min(volume / 10,000, 1)
OI        = 12 × min(open_interest / 5,000, 1)
premium   = max(0, 18 − abs(ask − 0.35) / 0.25 × 18)
distance  = max(0, 15 − abs(OTM_distance − 7.5) / 3.5 × 15)
IV        = 5 when 0.10 ≤ IV ≤ 1.50, otherwise 1
total     = clamp(sum, 0, 100)
```

The highest score wins. The score measures relative contract desirability and execution
quality; it does not estimate probability of profit.

## 7. Sizing

```text
contracts = floor(USD_cash / (ask × 100))
```

The expected debit cannot exceed observed USD cash. The model selects the maximum affordable
whole-contract count, so it uses as close to 100% as the contract multiplier permits. Exact
100% utilization is often impossible. This concentration can lose its entire premium.

## 8. Exit model

The shadow/bot-owned position is monitored using the Wealthsimple bid when available and a
Yahoo midpoint fallback otherwise.

- Full modeled profit exit: +500% relative to reconciled entry premium.
- Mandatory exit: 15:25 ET on regular sessions.
- Nuclear fallback: 15:45 ET.
- Early-close mandatory exit: 12:45 ET.

There is no premium stop-loss. That choice materially increases drawdown and requires
out-of-sample validation. Live close tickets require manual broker confirmation.

## 9. Shadow and validation model

Every eligible decision is appended to `data/options_shadow.jsonl`; the current simulated
position lives in `data/options_shadow_position.json`. Half-hour reports mark the shadow
position and append a modeled exit when an exit rule fires.

`scripts/run_walk_forward.py` consumes chronological outcome rows. For each window it:

1. Uses only the preceding training window.
2. Tests candidate absolute-score thresholds with minimum sample coverage.
3. Locks the best training expectancy threshold.
4. Reports performance on the following unseen test window.

Metrics include trade count, win rate, expectancy, profit factor, maximum additive drawdown,
and annualized trade-level Sharpe estimate. Historical data must use contemporaneous quotes;
using later-known or end-of-bar values introduces lookahead bias.

## 10. Known limitations

- Hand-selected thresholds are not statistically validated edge.
- Yahoo option data may be delayed or incomplete.
- Fixed-dollar strike distance ignores delta and changing implied volatility.
- The +500% target and absence of loss stop create a tail-heavy payoff distribution.
- Open/close periods contain elevated jump and execution risk.
- Browser selectors and text parsing are fragile broker integrations.
- A shadow fill at the ask is not proof that a broker order would fill identically.
