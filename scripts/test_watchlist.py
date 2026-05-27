"""Quick script to test which watchlist tickers have valid yfinance data."""
import sys
sys.path.insert(0, ".")
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

from kzer_bot.grinder_strategy import WATCHLIST

bad = []
good = []
for t in WATCHLIST:
    data = yf.Ticker(t).history(period="60d", interval="1d", auto_adjust=False)
    if data.empty or len(data) < 22:
        bad.append(t)
    else:
        good.append(t)

print(f"GOOD: {len(good)}")
print(f"BAD:  {len(bad)}")
print()
print("BAD TICKERS:")
for b in bad:
    print(" ", b)
