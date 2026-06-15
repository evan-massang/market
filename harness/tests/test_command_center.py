"""P11 — no-network tests for harness.command_center (read-only dashboard data)."""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_cc_")

from harness import wallet            # noqa: E402
from harness import journal           # noqa: E402
from harness import command_center as CC  # noqa: E402


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions", "decisions", "equity_snapshots"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    wallet.init_wallet(1000.0)
    journal.init_journal()


def _settled_loss(market_id, model_p=0.7, side="YES", pnl=-10.0, outcome=0.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, outcome, realized_pnl, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'settled', ?, ?, ?)",
        (market_id, f"Q-{market_id}", side, model_p, 0.5, 0.2, 10.0, 0.51, 19.6, 0.0,
         outcome, pnl, "2026-06-12T00:00:00"),
    )
    conn.commit()
    conn.close()


def test_skipped_markets_surface_reasons():
    _reset()
    journal.record_decision("M1", "Q1", 0.55, 0.50, 0.05, None, 0.0, None,
                            "", "no edge", "no_bet", "Guard skip: neg_ev_after_costs.")
    journal.record_decision("M2", "Q2", 0.62, 0.50, 0.12, "YES", 10.0, 0.51,
                            "", "LONG", "bet", "placed a bet")  # not a skip
    sk = CC.skipped_markets()
    ids = {s["market_id"] for s in sk}
    assert "M1" in ids and "M2" not in ids, sk
    m1 = next(s for s in sk if s["market_id"] == "M1")
    assert "neg_ev_after_costs" in m1["reason"], m1


def test_skip_reason_counts():
    _reset()
    for i in range(3):
        journal.record_decision(f"S{i}", "q", 0.5, 0.5, 0.0, None, 0.0, None,
                                "", "x", "no_bet", "Guard skip: high_spread")
    counts = CC.skip_reason_counts()
    assert counts.get("high_spread") == 3, counts


def test_losing_trades_diagnosis():
    _reset()
    _settled_loss("L1", model_p=0.8, side="YES", pnl=-10.0, outcome=0.0)
    lt = CC.losing_trades()
    assert len(lt) == 1 and lt[0]["market_id"] == "L1", lt
    assert "80%" in lt[0]["diagnosis"] and "wrong" in lt[0]["diagnosis"], lt[0]


def test_next_best_actions_nonempty_and_honest():
    _reset()
    acts = CC.next_best_actions()
    assert isinstance(acts, list) and len(acts) >= 1
    # cold book -> the gate-progress action is present, no profit claim
    assert any("GATE 1" in a or "resolved opinion" in a for a in acts), acts
    assert not any("profit" in a.lower() and "guarantee" in a.lower() for a in acts)


def test_command_center_shape():
    _reset()
    cc = CC.command_center()
    for k in ("skipped_markets", "skip_reason_counts", "losing_trades",
              "theme_label_performance", "next_best_actions", "replay_handles"):
        assert k in cc, (k, list(cc))


def test_replay_handles_have_explain_path():
    _reset()
    _settled_loss("R1")
    rh = CC.replay_handles()
    assert rh and rh[0]["explain_path"].endswith("R1"), rh


def test_never_raises_on_missing_tables():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_positions", "decisions", "paper_wallet"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    assert CC.skipped_markets() == []
    assert CC.losing_trades() == []
    assert isinstance(CC.command_center(), dict)


TESTS = [
    ("skipped_markets_surface_reasons", test_skipped_markets_surface_reasons),
    ("skip_reason_counts", test_skip_reason_counts),
    ("losing_trades_diagnosis", test_losing_trades_diagnosis),
    ("next_best_actions_nonempty_and_honest", test_next_best_actions_nonempty_and_honest),
    ("command_center_shape", test_command_center_shape),
    ("replay_handles_have_explain_path", test_replay_handles_have_explain_path),
    ("never_raises_on_missing_tables", test_never_raises_on_missing_tables),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
