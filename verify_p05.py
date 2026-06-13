"""
P0.5 verification — market_id keying. Zero LLM, zero network. Proves:
  1. fresh init_db() creates market_id columns on both tables
  2. an OLD (pre-P0.5) DB is migrated in place (column added, rows preserved)
  3. resolve keyed by market_id works even when the question TEXT has drifted
  4. the model Brier written matches the hand-computed value
  5. get_open_market_ids() dedupe reflects open/resolved state
  6. backward-compat: resolve by question text still works (market_id=None)
  7. two markets sharing identical question text resolve INDEPENDENTLY by id
Run from the polyswarm dir:  ./.venv/Scripts/python.exe verify_p05.py
"""
import os, sqlite3, tempfile, sys

# Point the calibration module at a throwaway DB BEFORE importing it.
TEST_DB = os.path.join(tempfile.gettempdir(), "polyswarm_p05_test.db")
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_URL"] = TEST_DB

from core import calibration as C  # noqa: E402

ok = True
def check(label, cond, detail=""):
    global ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        ok = False
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))

def cols(table):
    conn = sqlite3.connect(TEST_DB)
    c = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.close()
    return c

print("\n== 1. fresh init creates market_id columns ==")
C.init_db()
check("forecasts has market_id", "market_id" in cols("forecasts"), str(cols("forecasts")))
check("swarm_forecasts has market_id", "market_id" in cols("swarm_forecasts"), str(cols("swarm_forecasts")))

print("\n== 2. migration of an OLD (pre-P0.5) DB preserves rows + adds column ==")
os.remove(TEST_DB)
old = sqlite3.connect(TEST_DB)
old.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, agent_id TEXT, probability REAL, outcome REAL, brier_score REAL, created_at TEXT, resolved_at TEXT)")
old.execute("CREATE TABLE swarm_forecasts (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, final_probability REAL, consensus_score REAL, outcome REAL, brier_score REAL, market_odds REAL, created_at TEXT, resolved_at TEXT)")
old.execute("INSERT INTO swarm_forecasts (question, final_probability, consensus_score, market_odds) VALUES ('legacy Q', 0.5, 0.6, 0.4)")
old.commit(); old.close()
C.init_db()  # should ALTER both tables, not drop the legacy row
check("legacy DB migrated: swarm_forecasts.market_id added", "market_id" in cols("swarm_forecasts"))
conn = sqlite3.connect(TEST_DB)
legacy = conn.execute("SELECT question, final_probability, market_id FROM swarm_forecasts").fetchone()
conn.close()
check("legacy row preserved", legacy and legacy[0] == "legacy Q" and legacy[1] == 0.5, str(legacy))
check("legacy row market_id is NULL", legacy and legacy[2] is None)

print("\n== 3+4. resolve keyed by market_id despite drifted question text; Brier correct ==")
C.save_swarm_forecast("Will Country X hold an election in 2026?", 0.30, 0.72, market_odds=0.45, market_id="MKT-AAA")
C.save_forecast("Will Country X hold an election in 2026?", "macro", 0.28, market_id="MKT-AAA")
# Resolve with DIFFERENT wording but the SAME market id:
n = C.resolve_forecast("X 2026 election (reworded headline)", outcome=1.0, market_id="MKT-AAA")
conn = sqlite3.connect(TEST_DB); conn.row_factory = sqlite3.Row
sw = dict(conn.execute("SELECT * FROM swarm_forecasts WHERE market_id='MKT-AAA'").fetchone())
ag = dict(conn.execute("SELECT * FROM forecasts WHERE market_id='MKT-AAA'").fetchone())
conn.close()
check("resolve_forecast returned rows-resolved count >=2", n >= 2, f"n={n}")
check("swarm resolved despite text drift", sw["outcome"] == 1.0)
check("swarm model Brier = (0.30-1)^2 = 0.49", abs(sw["brier_score"] - 0.49) < 1e-9, f"got {sw['brier_score']}")
check("agent Brier = (0.28-1)^2 = 0.5184", abs(ag["brier_score"] - 0.5184) < 1e-9, f"got {ag['brier_score']}")

print("\n== 5. get_open_market_ids dedupe ==")
C.save_swarm_forecast("Another market", 0.6, 0.7, market_odds=0.5, market_id="MKT-BBB")
open_ids = C.get_open_market_ids()
check("MKT-BBB is open (unresolved)", "MKT-BBB" in open_ids, str(open_ids))
check("MKT-AAA NOT open (already resolved)", "MKT-AAA" not in open_ids, str(open_ids))

print("\n== 6. backward-compat: resolve by question text (market_id=None) ==")
C.save_swarm_forecast("Legacy-style question keyed by text", 0.2, 0.7, market_odds=0.35)  # no market_id
m = C.resolve_forecast("Legacy-style question keyed by text", outcome=0.0)
conn = sqlite3.connect(TEST_DB); conn.row_factory = sqlite3.Row
lt = dict(conn.execute("SELECT * FROM swarm_forecasts WHERE question='Legacy-style question keyed by text'").fetchone())
conn.close()
check("question-text resolve still works", m >= 1 and lt["outcome"] == 0.0 and abs(lt["brier_score"] - 0.04) < 1e-9, f"n={m} brier={lt['brier_score']}")

print("\n== 7. two markets, IDENTICAL question text, resolve INDEPENDENTLY by id ==")
C.save_swarm_forecast("SAME WORDING market", 0.10, 0.7, market_odds=0.5, market_id="DUP-1")
C.save_swarm_forecast("SAME WORDING market", 0.90, 0.7, market_odds=0.5, market_id="DUP-2")
C.resolve_forecast("SAME WORDING market", outcome=1.0, market_id="DUP-1")  # resolve only DUP-1
conn = sqlite3.connect(TEST_DB); conn.row_factory = sqlite3.Row
d1 = dict(conn.execute("SELECT * FROM swarm_forecasts WHERE market_id='DUP-1'").fetchone())
d2 = dict(conn.execute("SELECT * FROM swarm_forecasts WHERE market_id='DUP-2'").fetchone())
conn.close()
check("DUP-1 resolved", d1["outcome"] == 1.0 and abs(d1["brier_score"] - 0.81) < 1e-9, f"brier={d1['brier_score']}")
check("DUP-2 still OPEN (not collided)", d2["outcome"] is None, f"outcome={d2['outcome']}")

print("\n" + ("ALL P0.5 CHECKS PASSED" if ok else "SOME P0.5 CHECKS FAILED"))
os.remove(TEST_DB)
sys.exit(0 if ok else 1)
