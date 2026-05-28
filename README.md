# Le Grinder Quant v4.0

**Autonomous 24/7 Multi-Stage Momentum Rotation Engine for Wealthsimple US/CA.**

Le Grinder is a professional-grade quantitative trading system designed for high-frequency autonomous rotation in the US and Canadian equity markets. It leverages a sophisticated 9-signal composite scoring engine and a 24/7 execution cycle to achieve consistent high-alpha returns.

## Core Mandate: 10%+ Daily Alpha
The system is engineered for a target return of **10% per day** through:
- **Zero Idle Cash:** 100% capital deployment across pre-market, intraday, and after-hours windows.
- **Autonomous Rotation:** Real-time profit taking at +10% with immediate re-scanning and redeployment into the next high-conviction mover.
- **Gain Protection:** Intelligent trailing stops (activated at +2%, 1% trail) to lock in profits and protect the downside.

## Strategic Framework (The 9-Signal Alpha Engine)
Le Grinder's scoring engine synthesizes concepts from premier quantitative repositories and institutional strategies:
1. **Momentum Cascade (IBKR):** Triple-timeframe alignment (1d/5d/20d) for high-probability trend continuation.
2. **Stage 2 Alignment (Minervini):** Price > SMA50 > SMA150 > SMA200 trend validation.
3. **Volume Conviction:** Relative volume (RVOL) + 5d/20d volume trend analysis.
4. **Institutional Flow (OBV):** Volume-weighted price action to detect smart money accumulation.
5. **Breakout Proximity (CANSLIM):** Scoring based on closeness to 52-week and 20-day highs.
6. **Relative Strength (Alpha):** Performance benchmarking against S&P 500 (^GSPC) and TSX (^GSPTSE).
7. **MACD Divergence:** Bullish crossover detection for optimal entry timing.
8. **RSI Momentum:** Identifying the 'Goldilocks' zone (RSI 45-70) for sustained moves.
9. **Market Regime Gate:** Dynamic score scaling based on broad market health (SPY vs SMA200).

## Execution Cycle (24/7 Autonomous Operation)
| Window | Timing (ET) | Strategic Action |
|---|---|---|
| **Pre-Market** | 7:00 AM – 9:29 AM | Alpha scan on top movers + Limit Buy + Intraday monitoring (+2% target) |
| **Morning Decision** | 9:31 AM | Automated hold/rotate check on overnight positions based on fresh Smart Score |
| **Intraday Alpha** | 9:35 AM – 3:55 PM | **The Grinder Loop:** Buy at 10% target → Sell → Re-scan → Re-buy |
| **Market Lock** | 3:55 PM | Hard sell of all daytime positions to capture realized gains |
| **After-Hours** | 4:00 PM – 8:00 PM | Extended-hours momentum rotation with 60s price monitoring (+3% target) |

## Tech Stack & Compliance
- **Execution:** Playwright-based browser automation for Wealthsimple Trade.
- **Data:** High-frequency yfinance wrapper with multi-threaded prefetching.
- **Intelligence:** Claude 3.5 Sonnet (via Claude Code) for qualitative pick analysis.
- **Reporting:** Professional Telegram integration with real-time P&L, trade logs, and EOD Quant Summaries.

## Installation & Deployment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install msedge

# Configuration
# Create .env with WS_EMAIL, WS_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 24/7 Operation
python scripts/run_grinder.py
```

## Disclaimer
This is a high-risk quantitative trading tool. Past performance is not indicative of future results. Aiming for 10% daily alpha involves significant leverage (of time and capital rotation) and market exposure.
