#!/usr/bin/env python3
"""Send tonight's game plan to Telegram."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kzer_bot.telegram import send_message

msg = """🌙 <b>Le Grinder — Tonight's Game Plan</b>

📡 Futures: <b>⚪ NEUTRAL</b>  —  ES=F 7,540 pts (+0.02%)

━━━━━━━━━━━━━━━━━━━━━━━━
🔄 <b>TOMORROW MORNING:</b>

  1️⃣  <b>9:31 AM ET</b> — Sell <code>GPH.V</code> (overnight position)
  2️⃣  <b>9:35 AM ET</b> — Buy new pick
  3️⃣  Autonomous exit — +5% target anytime  |  +2% lock at 3:55 PM
  ♻️  <b>NEW: Intraday rotation</b> — after exit, re-scan &amp; buy next mover if before 3:30 PM

━━━━━━━━━━━━━━━━━━━━━━━━
🏆 <b>TOMORROW'S TOP PICKS (Smart Strategy, 150-ticker scan)</b>

  #1 <code>AIS.V</code>   $0.12  score=93.9  yday=+20.0%  vol=5.2x  closestr=100%  [HIGH]
  #2 <code>COCO.V</code>  $0.29  score=91.3  yday=+9.3%   vol=2.5x  closestr=83%   [HIGH]
  #3 <code>AMM.V</code>   $0.34  score=89.6  yday=+13.3%  vol=3.8x  closestr=100%  [HIGH]
  #4 <code>IPT.V</code>   $0.39  score=89.2  yday=+11.4%  vol=3.1x  closestr=75%   [HIGH]
  #5 <code>FTG.TO</code>  $24.24 score=86.5  yday=+12.6%  vol=2.7x  closestr=99%   [HIGH]

━━━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>LEADING PICK: <code>AIS.V</code> @ $0.12</b>

  📈 +20% yesterday on <b>5.2x normal volume</b>
  💪 Closed at <b>100%</b> of day range (pure strength)
  📊 ATR: 6.0%  |  Above EMA5 ✅  EMA20 ✅
  🎯 Composite score: <b>93.9/100</b>  [HIGH]

⚠️ Micro-cap — will re-scan at 5 AM for confirmation

━━━━━━━━━━━━━━━━━━━━━━━━
🤖 <b>Strategy upgrade: INTRADAY ROTATION</b>
No trading fees → after selling, immediately find next mover
Bot will rotate through winners all day (9:31 AM → 3:30 PM cutoff)"""

send_message(msg)
print("Telegram sent!")
