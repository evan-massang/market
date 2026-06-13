"""
P4 scoreboard verification. No network/LLM; throwaway DB.
Run:  ./.venv/Scripts/python.exe -m harness.test_scoreboard   (from polyswarm/)
"""
import os, sqlite3, tempfile, sys
TEST_DB = os.path.join(tempfile.gettempdir(), "polyswarm_scoreboard_test.db")
if os.path.exists(TEST_DB): os.remove(TEST_DB)
os.environ["DATABASE_URL"] = TEST_DB

from core.calibration import init_db                # noqa: E402
from harness import wallet as paper                 # noqa: E402
from harness import scoreboard as SB                # noqa: E402

ok = True
def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
def approx(a, b, t=1e-6): return a is not None and abs(a - b) < t

def insert(question, market_id, p, outcome, market_odds):
    conn = sqlite3.connect(TEST_DB)
    brier = (p - outcome) ** 2
    conn.execute(
        "INSERT INTO swarm_forecasts (question, market_id, final_probability, consensus_score, outcome, brier_score, market_odds) "
        "VALUES (?,?,?,?,?,?,?)", (question, market_id, p, 0.7, outcome, brier, market_odds))
    conn.commit(); conn.close()

init_db()
paper.init_wallet(1000.0)

# 55 resolved OPINION markets where the model BEATS the market:
#   model p=0.70, outcome=1 -> model Brier 0.09 ; market 0.50 -> market Brier 0.25
for i in range(55):
    insert(f"Will candidate {i} win the 2028 election?", f"OPN-{i}", 0.70, 1.0, 0.50)
# 2 MECHANICAL resolved rows that MUST be excluded from the opinion gate:
insert("Will Bitcoin close above $100k?", "MECH-1", 0.30, 0.0, 0.40)
insert("Will the Fed cut interest rates?", "MECH-2", 0.20, 0.0, 0.35)

# Make the paper bankroll grow (Gate 2): realized_pnl > 0 and equity >= starting.
conn = sqlite3.connect(TEST_DB)
conn.execute("UPDATE paper_wallet SET cash=1080.0, realized_pnl=80.0 WHERE id=1")
conn.commit(); conn.close()

s = SB.compute()
print("== counts + Brier math ==")
check("n counts only the 55 OPINION rows (mechanical excluded)", s["n"] == 55, f"n={s['n']}")
check("model Brier = 0.09", approx(s["model_brier"], 0.09), f"{s['model_brier']}")
check("market Brier = 0.25", approx(s["market_brier"], 0.25), f"{s['market_brier']}")
check("theme 'elections' present with n=55", s["themes"].get("elections", {}).get("n") == 55, str(list(s["themes"])))

print("\n== GATE logic (both should PASS) ==")
check("GATE 1 PASS (model<market, n>=50)", s["gate1"]["pass"] is True)
check("GATE 2 PASS (bankroll grew)", s["gate2"]["pass"] is True, str(s["gate2"]))
check("both_pass True", s["both_pass"] is True)

print("\n== GATE 1 must FAIL when n < 50 (even if model better) ==")
conn = sqlite3.connect(TEST_DB)
conn.execute("DELETE FROM swarm_forecasts WHERE market_id LIKE 'OPN-5%' OR market_id LIKE 'OPN-4%' OR market_id LIKE 'OPN-3%'")
conn.commit()
remaining = conn.execute("SELECT COUNT(*) FROM swarm_forecasts WHERE market_id LIKE 'OPN-%'").fetchone()[0]
conn.close()
s2 = SB.compute()
check(f"n now {remaining} (<50) -> GATE 1 FAIL despite model better", s2["gate1"]["pass"] is False, f"n={s2['n']}")
check("model still better than market on the smaller sample", s2["model_brier"] < s2["market_brier"])

print("\n== GATE 2 must FAIL when bankroll shrank ==")
conn = sqlite3.connect(TEST_DB)
conn.execute("UPDATE paper_wallet SET cash=940.0, realized_pnl=-60.0 WHERE id=1")
conn.commit(); conn.close()
s3 = SB.compute()
check("bankroll shrank -> GATE 2 FAIL", s3["gate2"]["pass"] is False, str(s3["gate2"]))

print("\n== render() runs without error ==")
try:
    SB.render(); rok = True
except Exception as e:
    rok = False; print("  render error:", e)
check("render ok", rok)

print("\n" + ("P4 SCOREBOARD: ALL PASSED" if ok else "P4 SCOREBOARD: FAILURES ABOVE"))
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
