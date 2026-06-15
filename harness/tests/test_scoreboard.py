"""Unit tests for harness.scoreboard — dual-Brier gate math. No network/LLM.

DATABASE_URL is redirected to a temp DB BEFORE importing the DB modules. Each
test rebuilds DB state so it is order-independent.
Run:  python -m harness.tests.test_scoreboard
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_scoreboard_")

from core.calibration import init_db          # noqa: E402
from harness import wallet as paper           # noqa: E402
from harness import scoreboard as SB          # noqa: E402


def _approx(a, b, t=1e-6):
    return a is not None and abs(a - b) < t


def _insert(question, market_id, p, outcome, market_odds):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    brier = (p - outcome) ** 2
    conn.execute(
        "INSERT INTO swarm_forecasts (question, market_id, final_probability, "
        "consensus_score, outcome, brier_score, market_odds) VALUES (?,?,?,?,?,?,?)",
        (question, market_id, p, 0.7, outcome, brier, market_odds))
    conn.commit()
    conn.close()


def _set_wallet(cash, realized):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=? WHERE id=1", (cash, realized))
    conn.commit()
    conn.close()


def _rebuild():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("swarm_forecasts", "forecasts", "baseline_forecasts",
              "paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    init_db()
    paper.init_wallet(1000.0)
    # 55 resolved OPINION markets where the model beats the market:
    #   p=0.70, outcome=1 -> model Brier 0.09 ; odds 0.50 -> market Brier 0.25
    for i in range(55):
        _insert(f"Will candidate {i} win the 2028 election?", f"OPN-{i}", 0.70, 1.0, 0.50)
    # 2 MECHANICAL rows that MUST be excluded from the opinion gate:
    _insert("Will Bitcoin close above $100k?", "MECH-1", 0.30, 0.0, 0.40)
    _insert("Will the Fed cut interest rates?", "MECH-2", 0.20, 0.0, 0.35)
    _set_wallet(1080.0, 80.0)   # bankroll grew (Gate 2 pass)


def test_theme_and_brier_properties_pure():
    assert SB.theme_of("Will the Senate flip?") == "elections"
    assert SB.theme_of("totally unrelated question") == "other"
    ts = SB.ThemeStat("elections", n=2, model_brier_sum=0.18, market_brier_sum=0.50)
    assert _approx(ts.model_brier, 0.09) and _approx(ts.market_brier, 0.25)
    assert SB.ThemeStat("x").model_brier is None   # n==0 guard


def test_counts_and_brier_math():
    _rebuild()
    s = SB.compute()
    assert s["n"] == 55, s["n"]   # 55 opinion only; 2 mechanical excluded
    assert _approx(s["model_brier"], 0.09), s["model_brier"]
    assert _approx(s["market_brier"], 0.25), s["market_brier"]
    assert s["themes"].get("elections", {}).get("n") == 55, list(s["themes"])


def test_gates_both_pass():
    _rebuild()
    s = SB.compute()
    assert s["gate1"]["pass"] is True, s["gate1"]
    assert s["gate2"]["pass"] is True, s["gate2"]
    assert s["both_pass"] is True


def test_gate1_fails_below_min_n():
    _rebuild()
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DELETE FROM swarm_forecasts WHERE market_id LIKE 'OPN-5%' "
                 "OR market_id LIKE 'OPN-4%' OR market_id LIKE 'OPN-3%'")
    conn.commit()
    conn.close()
    s = SB.compute()
    assert s["n"] < 50, s["n"]
    assert s["gate1"]["pass"] is False, s["gate1"]
    assert s["model_brier"] < s["market_brier"]   # still better, just too few


def test_gate2_fails_when_bankroll_shrank():
    _rebuild()
    _set_wallet(940.0, -60.0)
    s = SB.compute()
    assert s["gate2"]["pass"] is False, s["gate2"]


def test_one_row_per_market_dedupe():
    _rebuild()
    # second resolved row for an EXISTING market_id must collapse to one (MAX id).
    _insert("Will candidate 0 win the 2028 election?", "OPN-0", 0.65, 1.0, 0.55)
    s = SB.compute()
    assert s["n"] == 55, s["n"]


def test_render_runs():
    _rebuild()
    SB.render()   # raises on error


TESTS = [
    ("theme_and_brier_properties_pure", test_theme_and_brier_properties_pure),
    ("counts_and_brier_math", test_counts_and_brier_math),
    ("gates_both_pass", test_gates_both_pass),
    ("gate1_fails_below_min_n", test_gate1_fails_below_min_n),
    ("gate2_fails_when_bankroll_shrank", test_gate2_fails_when_bankroll_shrank),
    ("one_row_per_market_dedupe", test_one_row_per_market_dedupe),
    ("render_runs", test_render_runs),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
