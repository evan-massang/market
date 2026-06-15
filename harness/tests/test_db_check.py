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


TESTS = [
    ("clean_wallet_reconciles_ok", test_clean_wallet_reconciles_ok),
    ("drifted_wallet_warns", test_drifted_wallet_warns),
    ("missing_core_table_fails", test_missing_core_table_fails),
    ("run_is_read_only", test_run_is_read_only),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
