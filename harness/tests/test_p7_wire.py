"""P7 WIRE — cold-start + tightening invariants for the wired P7 chain.

P7 wires four already-built modules into the live decision + settlement paths,
ADDITIVELY and GUARDED. The acceptance bar (the guiding principle: bet BETTER,
not MORE) is that every wired change is a pure TIGHTENING or a passive recording,
and cold-start / thin-data behavior is NEVER looser than pre-P7.

These tests prove, with NO network / NO LLM and a temp DB only:

  (1) ADAPTIVE MIN_EDGE — cold start: the min_edge handed to size_bet is EXACTLY
      the caller's floor (DEFAULT_MIN_EDGE, or a higher cfg.min_edge), so sizing is
      unchanged today. FLOOR-ONLY-UP: never below the floor; only RAISES for a
      theme with a real losing track record (>=15 settled, realized_pnl<0).
  (2) EV-AFTER-COSTS HARD GATE — a sized bet whose slippage-worsened fill is
      non-positive-EV is rejected with the EXACT reason 'neg_ev_after_costs',
      while a healthy +edge bet PASSES. The gate can only reject (pure tightening),
      and degrades to ALLOW (== pre-P7) if the module is unavailable.
  (3) SETTLEMENT HOOKS — loop.settle_resolved records a CLV row AND an experiment
      outcome row when a market resolves, best-effort, without breaking settlement.

Run:  python -m harness.tests.test_p7_wire
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_p7wire_")

from harness import sizing                          # noqa: E402
from harness import profitability as PROF           # noqa: E402
from harness import clv as CLV                       # noqa: E402
from harness import experiments as EXP               # noqa: E402
from harness import predict_today as PT              # noqa: E402
from harness import loop as LP                        # noqa: E402
from harness import wallet as W                       # noqa: E402
from harness import challenger                        # noqa: E402
from harness import journal                           # noqa: E402
from core import calibration                          # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────
def _fresh_db():
    """Drop the tables the P7 wiring touches so each test starts clean."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_positions", CLV._TABLE, EXP._EXP_TABLE, EXP._OUT_TABLE):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()


def _seed_settled(theme_question, n, pnl_each):
    """Insert n SETTLED paper_positions for one theme (for adaptive_min_edge)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, question TEXT, side TEXT,
            model_p REAL, market_p REAL, edge REAL,
            stake REAL, fill_price REAL, shares REAL, fee REAL,
            status TEXT DEFAULT 'open',
            outcome REAL, payout REAL, realized_pnl REAL,
            end_date TEXT, opened_at TEXT DEFAULT CURRENT_TIMESTAMP, settled_at TEXT
        )"""
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions (market_id, question, side, stake, "
            "realized_pnl, status) VALUES (?,?,?,?,?, 'settled')",
            (f"M{i}", theme_question, "YES", 10.0, pnl_each),
        )
    conn.commit()
    conn.close()


# ── (1) cold-start adaptive min_edge == the floor, never below it ─────────────────
def test_cold_start_min_edge_equals_default():
    _fresh_db()                              # no settled history -> cold start
    # Default floor (DEFAULT_MIN_EDGE) is passed through UNCHANGED at cold start.
    me = PT._p7_adaptive_min_edge("Will the Democrat win the Senate election?",
                                  sizing.DEFAULT_MIN_EDGE)
    assert me == sizing.DEFAULT_MIN_EDGE, me
    # A HIGHER caller floor (e.g. cfg.min_edge=0.04) is respected, never lowered to 0.02.
    me2 = PT._p7_adaptive_min_edge("Will the Democrat win?", 0.04)
    assert me2 == 0.04, me2
    # An "other"-theme question, same invariant.
    me3 = PT._p7_adaptive_min_edge("Will it rain tomorrow?", sizing.DEFAULT_MIN_EDGE)
    assert me3 == sizing.DEFAULT_MIN_EDGE, me3


# ── (1b) FLOOR-ONLY-UP: a losing theme RAISES min_edge, never drops below the floor ─
def test_losing_theme_raises_min_edge_only_up():
    _fresh_db()
    # 15 settled losing "elections" bets -> demand MORE edge there (>= floor, tighter).
    _seed_settled("Will the Democrat win the presidential election?", 15, -2.0)
    me = PT._p7_adaptive_min_edge("Will the Democrat win the presidential election?",
                                  sizing.DEFAULT_MIN_EDGE)
    assert me >= sizing.DEFAULT_MIN_EDGE, me          # NEVER below the floor
    assert me > sizing.DEFAULT_MIN_EDGE, me           # losing theme -> tightened
    # An UNSEEDED theme is untouched (floor exactly) -> tightening is theme-local.
    me_other = PT._p7_adaptive_min_edge("Will the movie win an Oscar?",
                                        sizing.DEFAULT_MIN_EDGE)
    assert me_other == sizing.DEFAULT_MIN_EDGE, me_other
    # Even with a high caller floor, a losing theme never drops below it.
    me_hi = PT._p7_adaptive_min_edge("Will the Democrat win the presidential election?", 0.09)
    assert me_hi >= 0.09, me_hi


# ── (2) EV-after-costs gate rejects a -EV sized bet, passes a healthy +edge bet ──
def test_ev_gate_rejects_neg_ev_passes_healthy():
    # Thin edge (0.008) clears a thin min_edge but the +0.01 slippage flips EV negative:
    #   YES fill = 0.50 + 0.01 = 0.51 ; ev_per_share = 0.508 - 0.51 = -0.002 < 0.
    sz = sizing.size_bet(0.508, 0.50, 1000.0, min_edge=0.005)
    assert sz.side == "YES" and sz.stake > 0, sz       # sizing WOULD place it
    ok, reason = PT._p7_ev_gate(0.508, 0.50, "YES")
    assert ok is False, (ok, reason)
    assert reason == PROF.REJECT_REASON == "neg_ev_after_costs", reason
    # Healthy edge (model 0.55 vs market 0.50): fill 0.51, ev +0.04 -> PASSES.
    ok2, reason2 = PT._p7_ev_gate(0.55, 0.50, "YES")
    assert ok2 is True, (ok2, reason2)
    # NO side, thin edge: market 0.50, model 0.492 -> NO fill 0.51, ev (1-.492)-.51<0.
    ok3, reason3 = PT._p7_ev_gate(0.492, 0.50, "NO")
    assert ok3 is False and reason3 == "neg_ev_after_costs", (ok3, reason3)


# ── (2b) Plan 1 FAIL-CLOSED: an unavailable EV module BLOCKS the bet ─────────────
def test_ev_gate_unavailable_fails_closed():
    """Plan 1: the EV gate is a MONEY GATE. If the profitability module is
    unavailable it BLOCKS the bet (never assumes positive EV on a fault)."""
    from harness import safety_gate as SG
    with patched(PT, "_profitability", None):
        ok, reason = PT._p7_ev_gate(0.508, 0.50, "YES")     # would reject anyway
    assert ok is False and reason == SG.EV_UNAVAILABLE, (ok, reason)
    # A degenerate side is rejected as INVALID (conservative, fail-closed).
    okd, rd = PT._p7_ev_gate(0.5, 0.5, "MAYBE")
    assert okd is False and rd == SG.EV_INVALID, (okd, rd)


# ── (2c) the active experiment tag is the baseline (a numeric no-op) at cold start ─
def test_experiment_tag_is_baseline():
    _fresh_db()
    key = PT._p7_experiment_tag()
    assert key == EXP.BASELINE_KEY == "baseline", key
    # baseline params ARE the live defaults (running under it is a no-op).
    exp = EXP.active_experiment()
    assert exp["params"]["min_edge"] == sizing.DEFAULT_MIN_EDGE, exp


# ── (3) settlement records a CLV row + an experiment-outcome row ──────────────────
def _rebuild_settle_db():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("swarm_forecasts", "forecasts", "baseline_forecasts",
              "paper_wallet", "paper_positions", CLV._TABLE,
              EXP._EXP_TABLE, EXP._OUT_TABLE):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    calibration.init_db()
    W.init_wallet(1000.0)
    challenger.init_baseline_db()
    journal.init_journal()


def test_settle_records_clv_and_experiment_outcome():
    _rebuild_settle_db()
    mid, q = "CID-P7", "Will the Democrat win the Senate election?"
    # A real open paper position (carries side / fill_price / market_p for CLV).
    W.open_position(mid, q, "YES", 0.70, 0.50, 0.20, 10.0)
    # A swarm forecast so latest_forecast_pq -> (model_p, market_p) for the Brier.
    calibration.save_swarm_forecast(q, 0.70, 0.80, 0.50, market_id=mid)
    challenger.save_baseline(mid, q, 0.65, 0.50)

    closed_yes = {"raw": {"closed": True}, "outcomes": ["Yes", "No"],
                  "outcome_prices": [0.99, 0.01]}
    with patched(LP.gamma, "fetch_market_by_condition_id", lambda mid, **k: closed_yes):
        res = LP.settle_resolved(LP.LoopConfig())
    assert len(res) == 1 and res[0]["outcome"] == 1.0, res

    # CLV row recorded for this market (entry=fill_price 0.51, closing-proxy=market_p 0.50).
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    clv_rows = conn.execute(
        f"SELECT * FROM {CLV._TABLE} WHERE market_id=?", (mid,)).fetchall()
    out_rows = conn.execute(
        f"SELECT * FROM {EXP._OUT_TABLE} WHERE market_id=?", (mid,)).fetchall()
    conn.close()
    assert len(clv_rows) == 1, [dict(r) for r in clv_rows]
    assert clv_rows[0]["side"] == "YES", dict(clv_rows[0])
    # experiment outcome attributed to the active (baseline) experiment with the Brier.
    assert len(out_rows) == 1, [dict(r) for r in out_rows]
    r = out_rows[0]
    assert r["exp_key"] == EXP.BASELINE_KEY, dict(r)
    assert abs(r["model_brier"] - (0.70 - 1.0) ** 2) < 1e-9, dict(r)   # 0.09
    assert abs(r["market_brier"] - (0.50 - 1.0) ** 2) < 1e-9, dict(r)  # 0.25


def test_settle_p7_hook_never_breaks_settlement():
    """Even if the experiment recorder blows up, settlement still completes and the
    position still settles (the P7 hook is wrapped best-effort)."""
    _rebuild_settle_db()
    mid, q = "CID-P7B", "Will X happen?"
    W.open_position(mid, q, "YES", 0.70, 0.50, 0.20, 10.0)
    calibration.save_swarm_forecast(q, 0.70, 0.80, 0.50, market_id=mid)
    closed_yes = {"raw": {"closed": True}, "outcomes": ["Yes", "No"],
                  "outcome_prices": [0.99, 0.01]}

    def _boom(*a, **k):
        raise RuntimeError("experiment recorder exploded")

    with patched(EXP, "record_experiment_outcome", _boom):
        with patched(LP.gamma, "fetch_market_by_condition_id", lambda mid, **k: closed_yes):
            res = LP.settle_resolved(LP.LoopConfig())
    assert len(res) == 1 and res[0]["outcome"] == 1.0, res     # settlement unbroken
    assert W.get_open_positions() == [], "position must still settle"


TESTS = [
    ("cold_start_min_edge_equals_default", test_cold_start_min_edge_equals_default),
    ("losing_theme_raises_min_edge_only_up", test_losing_theme_raises_min_edge_only_up),
    ("ev_gate_rejects_neg_ev_passes_healthy", test_ev_gate_rejects_neg_ev_passes_healthy),
    ("ev_gate_unavailable_fails_closed", test_ev_gate_unavailable_fails_closed),
    ("experiment_tag_is_baseline", test_experiment_tag_is_baseline),
    ("settle_records_clv_and_experiment_outcome", test_settle_records_clv_and_experiment_outcome),
    ("settle_p7_hook_never_breaks_settlement", test_settle_p7_hook_never_breaks_settlement),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
