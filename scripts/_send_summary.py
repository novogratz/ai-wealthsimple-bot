#!/usr/bin/env python3
"""Send today's options trading summary to Telegram."""
import sys, os, json, re
from datetime import datetime, timezone

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))

for line in open('.env').readlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('=')
        os.environ[k.strip()] = v.strip()

from kzer_bot.telegram import send_message
from kzer_bot.spy_options_strategy import NOON_CLOSE_HOUR, NOON_CLOSE_MINUTE

# ---- live SPY price ----
try:
    import yfinance as yf
    h = yf.Ticker('SPY').history(period='1d', interval='1m')
    spy_price = float(h['Close'].iloc[-1]) if not h.empty else 0.0
except Exception:
    spy_price = 0.0

# ---- current open position ----
pos = json.load(open('data/options_position.json'))
entry     = pos['entry_premium']
contracts = pos['contracts']
strike    = int(pos['strike'])
opt_type  = pos['option_type'].upper()

# last reported premium from log
current_prem = 0.0
try:
    lines = open('data/options.log', encoding='utf-8', errors='replace').readlines()
    for ln in reversed(lines):
        m = re.search(r'premium \$([0-9.]+)', ln)
        if m:
            current_prem = float(m.group(1))
            break
except Exception:
    pass

open_pnl_usd = (current_prem - entry) * 100 * contracts if current_prem else 0.0
open_pnl_pct = (current_prem - entry) / entry * 100      if current_prem else 0.0

# ---- realized trades this afternoon ----
# Afternoon session: 2 SPY $730 CALL @ $0.745 avg = $149 cost
#   1 contract sold at market ~+150% = +$112
realized_usd = 112.0
realized_pct = 150.0

total_realized   = realized_usd
total_unrealized = open_pnl_usd
total_day        = total_realized + total_unrealized

# ---- timing ----
import pytz
et = pytz.timezone('America/New_York')
now_et_dt = datetime.now(et)
now_str   = now_et_dt.strftime('%I:%M %p ET')
close_str = f"{NOON_CLOSE_HOUR % 12 or 12}:{NOON_CLOSE_MINUTE:02d} PM ET"

close_dt  = now_et_dt.replace(hour=NOON_CLOSE_HOUR, minute=NOON_CLOSE_MINUTE, second=0, microsecond=0)
mins_left = max(int((close_dt - now_et_dt).total_seconds() / 60), 0)

# ---- format ----
open_sign  = '+' if open_pnl_usd  >= 0 else ''
total_sign = '+' if total_day     >= 0 else ''
prem_str   = f"${current_prem:.2f}" if current_prem else "N/A"
open_emoji = "📈" if open_pnl_pct > 5 else "📉" if open_pnl_pct < -5 else "⚡"

msg = (
    f"<b>📊 0DTE SPY — AFTERNOON SESSION</b>\n"
    f"<b>June 9, 2026</b>  |  {now_str}\n"
    f"{'─'*34}\n"
    f"\n"
    f"<b>TRADE</b>  SPY ${strike} {opt_type}  |  Entry: ${entry:.3f}\n"
    f"  💰 Cost: $149  |  2 contracts bought\n"
    f"  🟢 Sold 1 contract at market: <b>+$112  (~+150%)</b>\n"
    f"\n"
    f"<b>OPEN</b>  1 contract  |  {open_emoji} Now: {prem_str}\n"
    f"  P&L: <b>{open_sign}{open_pnl_usd:.0f} USD  ({open_sign}{open_pnl_pct:.0f}%)</b>\n"
    f"\n"
    f"{'─'*34}\n"
    f"<b>TOTALS</b>  |  SPY ${spy_price:.2f}\n"
    f"  Realized:   <b>+${total_realized:.0f}</b>\n"
    f"  Unrealized: <b>{open_sign}${abs(total_unrealized):.0f}</b>\n"
    f"  <b>Day P&L: {total_sign}${total_day:.0f}</b>\n"
    f"  Record: 1W / 0L\n"
    f"\n"
    f"{'─'*34}\n"
    f"<b>PLAN</b>\n"
    f"  ⏰ Hard close at <b>{close_str}</b>  ({mins_left} min)\n"
    f"  💤 No more trades today\n"
    f"  📅 Tomorrow 9:45 AM ET:\n"
    f"     SPY UP → BUY PUTS\n"
    f"     SPY DOWN → BUY CALLS\n"
    f"     Max 50% of balance per trade"
)

send_message(msg)
print(f"Summary sent — Day P&L: {total_sign}${total_day:.0f}  |  Open: {open_sign}${open_pnl_usd:.0f}  |  Close in {mins_left} min")
