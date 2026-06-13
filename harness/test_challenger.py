"""P4 challenger verification — storage/resolve/Brier + scoreboard A/B. No LLM.
Run: ./.venv/Scripts/python.exe -m harness.test_challenger  (from polyswarm/)"""
import os, tempfile, sys
TEST_DB = os.path.join(tempfile.gettempdir(), "polyswarm_challenger_test.db")
if os.path.exists(TEST_DB): os.remove(TEST_DB)
os.environ["DATABASE_URL"] = TEST_DB

from core.calibration import init_db, save_swarm_forecast, resolve_forecast   # noqa: E402
from harness import challenger as CH                                          # noqa: E402
from harness import scoreboard as SB                                          # noqa: E402

ok = True
def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
def approx(a, b, t=1e-6): return a is not None and abs(a - b) < t

init_db(); CH.init_baseline_db()
# Swarm p=0.70 (Brier 0.09 on YES); single-LLM p=0.60 (Brier 0.16); market 0.50 (Brier 0.25)
for i in range(3):
    mid = f"OPN-{i}"; q = f"Will candidate {i} win the 2028 election?"
    save_swarm_forecast(q, 0.70, 0.7, market_odds=0.50, market_id=mid)
    CH.save_baseline(mid, q, 0.60, 0.50)
    resolve_forecast(q, 1.0, market_id=mid)
    CH.resolve_baseline(1.0, mid)

print("== challenger storage + resolve + Brier ==")
check("get_baseline_brier = (0.6-1)^2 = 0.16", approx(CH.get_baseline_brier(), 0.16), f"{CH.get_baseline_brier()}")

print("\n== scoreboard A/B integration ==")
s = SB.compute()
check("baseline_n == 3", s["baseline_n"] == 3, str(s["baseline_n"]))
check("baseline_brier = 0.16", approx(s["baseline_brier"], 0.16), str(s["baseline_brier"]))
check("model_brier = 0.09", approx(s["model_brier"], 0.09), str(s["model_brier"]))
check("ordering: swarm(0.09) < single-LLM(0.16) < market(0.25)",
      s["model_brier"] < s["baseline_brier"] < s["market_brier"])

print()
SB.render()

print("\n" + ("P4 CHALLENGER: ALL PASSED" if ok else "P4 CHALLENGER: FAILURES ABOVE"))
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
