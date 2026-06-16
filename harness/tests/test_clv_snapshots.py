"""Timed CLV snapshots (Phase 7/12) — entry vs 15m/1h/6h. No network (mocked prices)."""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_clvsnap_")

from harness import wallet, clv  # noqa: E402


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions", "clv_snapshots"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)


def _open(market_id, side, fill_price, opened_at, q="Will X win the 2032 election?"):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (market_id, q, side, 0.6, fill_price, 0.1, 10.0, fill_price, 10.0 / fill_price, 0.0, opened_at))
    conn.commit(); conn.close()


def test_due_buckets_recorded_by_age():
    _reset()
    base = datetime(2026, 6, 16, 0, 0, 0)
    _open("0xaa11bb22cc", "YES", 0.40, base.isoformat())
    # 30 minutes later -> only 15m is due
    n = clv.snapshot_open_positions({"0xaa11bb22cc": 0.50}, now=base + timedelta(minutes=30))
    assert n == 1, n
    summ = clv.clv_snapshot_summary()
    assert "15m" in summ and "1h" not in summ, summ
    # 7 hours later -> 1h and 6h become due (15m already taken)
    n2 = clv.snapshot_open_positions({"0xaa11bb22cc": 0.55}, now=base + timedelta(hours=7))
    assert n2 == 2, n2
    summ2 = clv.clv_snapshot_summary()
    assert set(summ2) == {"15m", "1h", "6h"}, summ2


def test_clv_sign_and_dedup():
    _reset()
    base = datetime(2026, 6, 16, 0, 0, 0)
    _open("0xdead00beef", "YES", 0.40, base.isoformat())
    # YES bought at 0.40, price rose to 0.55 -> positive CLV (good entry)
    clv.snapshot_open_positions({"0xdead00beef": 0.55}, now=base + timedelta(minutes=20))
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT clv FROM clv_snapshots WHERE bucket='15m'").fetchone()
    conn.close()
    assert abs(row["clv"] - 0.15) < 1e-9, row["clv"]
    # re-running does not duplicate the 15m bucket
    n = clv.snapshot_open_positions({"0xdead00beef": 0.60}, now=base + timedelta(minutes=25))
    assert n == 0


def test_no_position_no_snapshot():
    _reset()
    assert clv.snapshot_open_positions({"0xabc": 0.5}) == 0       # no open positions
    assert clv.clv_snapshot_summary() == {}


def test_missing_price_skipped():
    _reset()
    base = datetime(2026, 6, 16, 0, 0, 0)
    _open("0xcafe001234", "NO", 0.30, base.isoformat())
    # price_map has no entry for this market -> skipped, no crash
    assert clv.snapshot_open_positions({"0xother": 0.5}, now=base + timedelta(hours=1)) == 0


TESTS = [
    ("due_buckets_recorded_by_age", test_due_buckets_recorded_by_age),
    ("clv_sign_and_dedup", test_clv_sign_and_dedup),
    ("no_position_no_snapshot", test_no_position_no_snapshot),
    ("missing_price_skipped", test_missing_price_skipped),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
