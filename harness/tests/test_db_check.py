"""AUDIT fix — tests for harness.db_check (read-only integrity + reconciliation)."""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_dbcheck_")

from harness import wallet           # noqa: E402
from harness import db_check as DC    # noqa: E402


def _status(res, name):
    for n, s, _ in res["checks"]:
        if n == name:
            return s
    return None


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)


def test_clean_wallet_reconciles_ok():
    _reset()
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0,
                         cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99))
    wallet.settle_market("M", 1.0)
    res = DC.run()
    assert res["fail"] == 0, res["checks"]
    assert _status(res, "reconcile_cash") == "OK", res["checks"]
    assert _status(res, "reconcile_realized") == "OK", res["checks"]
    assert _status(res, "integrity") == "OK"


def test_drifted_wallet_warns():
    _reset()
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0,
                         cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99))
    wallet.settle_market("M", 1.0)
    # corrupt the wallet running total (simulate external surgery / the live drift)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("UPDATE paper_wallet SET realized_pnl = realized_pnl + 5.0 WHERE id=1")
    conn.commit(); conn.close()
    res = DC.run()
    assert _status(res, "reconcile_realized") == "WARN", res["checks"]


def test_missing_core_table_fails():
    _reset()
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE paper_positions")
    conn.commit(); conn.close()
    res = DC.run()
    assert res["fail"] >= 1, res["checks"]
    assert _status(res, "tables") == "FAIL"


def test_run_is_read_only():
    # db_check must never mutate the DB
    _reset()
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0,
                         cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99))
    before = wallet.get_state()
    DC.run()
    after = wallet.get_state()
    assert before == after, (before, after)


def _drift_wallet():
    _reset()
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0,
                         cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99))
    wallet.settle_market("M", 1.0)
    # corrupt the wallet running totals (simulate the live drift)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("UPDATE paper_wallet SET cash=cash+30, realized_pnl=realized_pnl+5 WHERE id=1")
    conn.commit(); conn.close()


def test_reconciliation_report_detects_drift():
    _drift_wallet()
    rep = DC.ledger_reconciliation_report()
    assert rep["ok"] and abs(rep["cash_delta"] - 30.0) < 1e-6, rep
    assert abs(rep["realized_delta"] - 5.0) < 1e-6, rep


def test_repair_dry_run_changes_nothing():
    _drift_wallet()
    before = wallet.get_state()
    r = DC.repair(dry_run=True)
    assert r["applied"] is False and r["needs_repair"] is True
    assert wallet.get_state() == before          # untouched


def test_repair_applies_and_reconciles():
    _drift_wallet()
    r = DC.repair(dry_run=False)
    assert r["applied"] is True, r
    rep = DC.ledger_reconciliation_report()
    assert abs(rep["cash_delta"]) < 0.01 and abs(rep["realized_delta"]) < 0.01, rep


def test_repair_noop_when_consistent():
    _reset()
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0,
                         cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99))
    wallet.settle_market("M", 1.0)
    r = DC.repair(dry_run=False)
    assert r["applied"] is False and r["needs_repair"] is False


TESTS = [
    ("clean_wallet_reconciles_ok", test_clean_wallet_reconciles_ok),
    ("reconciliation_report_detects_drift", test_reconciliation_report_detects_drift),
    ("repair_dry_run_changes_nothing", test_repair_dry_run_changes_nothing),
    ("repair_applies_and_reconciles", test_repair_applies_and_reconciles),
    ("repair_noop_when_consistent", test_repair_noop_when_consistent),
    ("drifted_wallet_warns", test_drifted_wallet_warns),
    ("missing_core_table_fails", test_missing_core_table_fails),
    ("run_is_read_only", test_run_is_read_only),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
