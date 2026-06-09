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

# ---- live SPY price ----
try:
    import yfinance as yf
    h = yf.Ticker('SPY').history(period='1d', interval='1m')
    spy_price = float(h['Close'].iloc[-1]) if not h.empty else 0.0
except Exception:
    spy_price = 0.0

# ---- current open position ----
pos = json.load(open('data/options_position.json'))
entry    = pos['entry_premium']
contracts = pos['contracts']

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

# ---- known realized trades today ----
# Session 1: 11 SPY $749 CALL (09:46 AM)
#   partial close 11 contracts: +$803 (+365%)
#   remaining 11 noon close:    -$83  (-75%)
s1_partial =  803.0
s1_full    =  -83.0
s1_net     = s1_partial + s1_full    # +720

# Session 2: 2 SPY $730 CALL (12:44 PM, entry $0.745)
#   1 contract sold at market ~+150%: +$112
s2_partial =  112.0                  # realized on sold contract
s2_open    = open_pnl_usd            # unrealized on remaining 1 contract

total_realized   = s1_net + s2_partial
total_unrealized = s2_open
total_day        = total_realized + total_unrealized

wins  = 2  # session 1 partial + session 2 partial
losses = 1  # session 1 noon close

# ---- format message ----
sign = '+' if total_day >= 0 else ''
open_sign = '+' if open_pnl_usd >= 0 else ''
now_et = datetime.now(timezone.utc).strftime('%I:%M %p ET')

msg = (
    f"<b>📊 0DTE SPY OPTIONS — DAILY SUMMARY</b>\n"
    f"<b>June 9, 2026</b> | {now_et}\n"
    f"{'─'*32}\n"
    f"\n"
    f"<b>SESSION 1</b>  |  SPY $749 CALL  |  09:46 AM\n"
    f"  🟢 Partial close (11 contracts): <b>+$803</b> (+365%)\n"
    f"  🔴 Noon hard close (11 contracts): <b>-$83</b> (-75%)\n"
    f"  Session net: <b>+$720</b>\n"
    f"\n"
    f"<b>SESSION 2</b>  |  SPY $730 CALL  |  12:44 PM\n"
    f"  Entry: $0.745 avg  |  2 contracts  |  Cost: $149\n"
    f"  🟢 Partial close (1 contract): <b>+$112</b> (~+150%)\n"
    f"  ⏳ Open (1 contract): now ${current_prem:.2f}  "
    f"<b>{open_sign}{open_pnl_usd:.0f}</b> ({open_sign}{open_pnl_pct:.0f}%)\n"
    f"\n"
    f"{'─'*32}\n"
    f"<b>TODAY</b>  |  SPY ${spy_price:.2f}\n"
    f"  Realized P&L:   <b>+${total_realized:.0f}</b>\n"
    f"  Unrealized P&L: <b>{open_sign}${abs(total_unrealized):.0f}</b>\n"
    f"  <b>Total day:  {sign}${total_day:.0f}</b>\n"
    f"  Record:  {wins}W / {losses}L  (win rate {wins/(wins+losses)*100:.0f}%)\n"
    f"\n"
    f"{'─'*32}\n"
    f"<b>PLAN</b>\n"
    f"  🕓 Hold $730 CALL → hard close 3:25 PM ET\n"
    f"  💤 No more trades today after close\n"
    f"  📅 Tomorrow 9:45 AM: fade PM gap\n"
    f"     SPY UP → buy PUTS | SPY DOWN → buy CALLS\n"
    f"     Allocation: 50% of balance per bet"
)

send_message(msg)
print("Telegram summary sent.")
print(f"Realized: +${total_realized:.0f}  |  Open: {open_sign}${open_pnl_usd:.0f}  |  Day total: {sign}${total_day:.0f}")
