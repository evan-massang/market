"""P10 — no-network tests for harness.metrics (read-only consolidated metrics)."""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_metrics_")

from harness import wallet            # noqa: E402
import core.calibration as CAL        # noqa: E402
from harness import metrics as M      # noqa: E402


def _approx(a, b, tol=1e-4):
    return a is not None and abs(a - b) <= tol


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions", "swarm_forecasts", "forecasts"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    wallet.init_wallet(1000.0)
    CAL.init_db()


def _swarm(p, y, market_id):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO swarm_forecasts (question, market_id, final_probability, consensus_score, "
        "market_odds, outcome) VALUES (?,?,?,?,?,?)",
        (f"Q-{market_id}", market_id, p, 0.7, 0.5, y),
    )
    conn.commit()
    conn.close()


def _settled(pnl, stake, market_id, settled_at):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, realized_pnl, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'settled', ?, ?)",
        (market_id, f"Q-{market_id}", "YES", 0.6, 0.5, 0.1, stake, 0.51, stake / 0.51, 0.0, pnl, settled_at),
    )
    conn.commit()
    conn.close()


# ── log loss ──────────────────────────────────────────────────────────────────
def test_log_loss_hand_computed():
    import math
    _reset()
    _swarm(0.9, 1.0, "A")   # -ln(0.9) = 0.10536
    _swarm(0.2, 0.0, "B")   # -ln(0.8) = 0.22314
    ll = M.log_loss()
    assert ll is not None and ll["n"] == 2, ll
    expected = (-math.log(0.9) - math.log(0.8)) / 2
    assert _approx(ll["log_loss"], expected), (ll, expected)


def test_log_loss_none_below_min_n():
    _reset()
    _swarm(0.7, 1.0, "A")
    assert M.log_loss(min_n=5) is None


def test_log_loss_confident_miss_is_finite():
    _reset()
    _swarm(1.0, 0.0, "A")   # fully confident, wrong -> clamped, not inf
    ll = M.log_loss()
    assert ll is not None and ll["log_loss"] < 100 and ll["log_loss"] > 0, ll


# ── paper metrics ──────────────────────────────────────────────────────────────
def test_paper_metrics_hand_computed():
    _reset()
    # pnls +5, -3, +2, -10 (stakes 10 each)
    _settled(+5.0, 10.0, "P1", "2026-06-10T00:00:00")
    _settled(-3.0, 10.0, "P2", "2026-06-11T00:00:00")
    _settled(+2.0, 10.0, "P3", "2026-06-12T00:00:00")
    _settled(-10.0, 10.0, "P4", "2026-06-13T00:00:00")
    pm = M.paper_metrics()
    assert pm["n"] == 4
    assert _approx(pm["roi"], -6.0 / 40.0), pm                 # -0.15
    assert _approx(pm["hit_rate"], 0.5), pm
    assert _approx(pm["profit_factor"], 7.0 / 13.0), pm        # gross 7 / 13
    assert _approx(pm["max_drawdown"], 11.0), pm               # peak 5 -> trough -6
    assert _approx(pm["realized_pnl"], -6.0), pm


def _closed(pnl, stake, market_id, settled_at):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, realized_pnl, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'closed', ?, ?)",
        (market_id, f"Q-{market_id}", "YES", 0.6, 0.5, 0.1, stake, 0.51, stake / 0.51, 0.0, pnl, settled_at),
    )
    conn.commit(); conn.close()


def test_paper_metrics_includes_cashed_out_closed():
    # audit #8/#19: cashed-out ('closed') trades must count in P&L metrics so they
    # reconcile with the wallet realized_pnl that Gate 2 reads.
    _reset()
    _settled(-10.0, 10.0, "S1", "2026-06-10T00:00:00")
    _closed(-8.0, 10.0, "C1", "2026-06-11T00:00:00")
    pm = M.paper_metrics()
    assert pm["n"] == 2, pm                       # both counted
    assert _approx(pm["realized_pnl"], -18.0), pm  # -10 settled + -8 closed


def test_paper_metrics_empty_book():
    _reset()
    pm = M.paper_metrics()
    assert pm["n"] == 0 and pm["profit_factor"] is None and pm["roi"] == 0.0


def test_profit_factor_none_when_no_losses():
    _reset()
    _settled(+5.0, 10.0, "W1", "2026-06-10T00:00:00")
    _settled(+3.0, 10.0, "W2", "2026-06-11T00:00:00")
    assert M.paper_metrics()["profit_factor"] is None   # no losses


# ── consolidated report ─────────────────────────────────────────────────────────
def test_gate_report_shape_and_not_faked():
    _reset()
    r = M.gate_report()
    # gate booleans come from scoreboard; with no resolved opinion markets -> FAIL
    assert "gate1" in r and "gate2" in r
    assert r["both_pass"] is False                     # honest: nothing passes cold
    assert isinstance(r["paper"], dict)


def test_full_report_never_raises():
    _reset()
    r = M.full_report()
    assert isinstance(r, dict)
    for k in ("gate1", "gate2", "paper", "calibration", "theme_pnl", "drawdown"):
        assert k in r, (k, list(r))


def test_never_raises_on_missing_tables():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_positions", "paper_wallet", "swarm_forecasts"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    assert M.log_loss() is None
    assert isinstance(M.paper_metrics(), dict)
    assert isinstance(M.full_report(), dict)


TESTS = [
    ("log_loss_hand_computed", test_log_loss_hand_computed),
    ("log_loss_none_below_min_n", test_log_loss_none_below_min_n),
    ("log_loss_confident_miss_is_finite", test_log_loss_confident_miss_is_finite),
    ("paper_metrics_hand_computed", test_paper_metrics_hand_computed),
    ("paper_metrics_includes_cashed_out_closed", test_paper_metrics_includes_cashed_out_closed),
    ("paper_metrics_empty_book", test_paper_metrics_empty_book),
    ("profit_factor_none_when_no_losses", test_profit_factor_none_when_no_losses),
    ("gate_report_shape_and_not_faked", test_gate_report_shape_and_not_faked),
    ("full_report_never_raises", test_full_report_never_raises),
    ("never_raises_on_missing_tables", test_never_raises_on_missing_tables),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
