import sys
sys.path.insert(0, ".")
from scripts.run_grinder import _combined_report, _daily_sent

print("--- Testing combined report (top picks + rapport) ---")
_combined_report("test_slot", "16h00 ET")
print("Done. Sent:", _daily_sent)
