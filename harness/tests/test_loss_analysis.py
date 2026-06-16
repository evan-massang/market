"""AUDIT fix — dedicated losing-trade CAUSE analyzer (#20) + sqlite:/// db-path (#17)."""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_loss_")

from harness import wallet            # noqa: E402
from harness import loss_analysis as LA  # noqa: E402


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)


def _ins(market_id, side, model_p, market_p, stake, pnl, outcome, status,
         opened_at="2026-06-10T00:00:00", end_date="2026-06-10T12:00:00"):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, outcome, realized_pnl, opened_at, end_date, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (market_id, f"Q-{market_id}", side, model_p, market_p, abs(model_p - market_p), stake,
         0.51, stake / 0.51, 0.0, status, outcome, pnl, opened_at, end_date, "2026-06-10T12:00:00"),
    )
    conn.commit(); conn.close()


def test_classify_confident_wrong_is_bad_forecast():
    c = LA.classify_loss({"side": "YES", "model_p": 0.9, "market_p": 0.5, "stake": 10,
                          "outcome": 0.0, "status": "settled", "question": "Q"})
    assert c["primary_cause"] == "bad_forecast", c


def test_classify_low_conf_loss_is_expected_variance():
    c = LA.classify_loss({"side": "YES", "model_p": 0.52, "market_p": 0.50, "stake": 10,
                          "outcome": 0.0, "status": "settled", "question": "Q"})
    # thin edge OR expected variance — both honest (not "bad_forecast")
    assert c["primary_cause"] in ("expected_variance", "thin_edge_selection"), c


def test_classify_cashed_out():
    c = LA.classify_loss({"side": "YES", "model_p": 0.6, "market_p": 0.55, "stake": 10,
                          "outcome": None, "status": "closed", "question": "Q"})
    assert c["primary_cause"] == "cashed_out_early", c


def test_analyze_and_summary_over_db():
    _reset()
    _ins("A", "YES", 0.9, 0.5, 10, -10.0, 0.0, "settled")     # bad_forecast
    _ins("B", "YES", 0.6, 0.55, 10, -8.0, None, "closed")     # cashed_out_early
    rows = LA.analyze_losses()
    assert len(rows) == 2, rows
    summ = LA.cause_summary()
    assert summ.get("bad_forecast", 0) >= 1 and summ.get("cashed_out_early", 0) >= 1, summ
    assert isinstance(LA.recommendations(), list) and LA.recommendations()


def test_recommendations_never_claim_profit():
    _reset()
    for r in LA.recommendations():
        assert "guaranteed" not in r.lower() and "will profit" not in r.lower(), r


def test_sqlite_bare_url_db_path():
    # audit #17: the OLD modules must strip a bare sqlite:/// prefix like the new ones.
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite:///./_probe.db"
    try:
        import importlib
        import harness.wallet as W
        import core.calibration as C
        importlib.reload(W); importlib.reload(C)
        assert W.DB_PATH == "_probe.db", W.DB_PATH
        assert C.DB_PATH == "_probe.db", C.DB_PATH
    finally:
        os.environ["DATABASE_URL"] = old
        import importlib
        import harness.wallet as W
        import core.calibration as C
        importlib.reload(W); importlib.reload(C)


TESTS = [
    ("classify_confident_wrong_is_bad_forecast", test_classify_confident_wrong_is_bad_forecast),
    ("classify_low_conf_loss_is_expected_variance", test_classify_low_conf_loss_is_expected_variance),
    ("classify_cashed_out", test_classify_cashed_out),
    ("analyze_and_summary_over_db", test_analyze_and_summary_over_db),
    ("recommendations_never_claim_profit", test_recommendations_never_claim_profit),
    ("sqlite_bare_url_db_path", test_sqlite_bare_url_db_path),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
