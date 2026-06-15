"""AUDIT fix — settlement is idempotent and cannot double-credit cash / realized P&L.

Regression for the CRITICAL audit finding: settle_market / close_at_price updated
paper_positions and paper_wallet with no status guard, so two concurrent daemons
(or a re-run) could credit the same position twice, silently inflating the
realized_pnl that Gate 2 reads. The fix guards the UPDATE with `AND status='open'`
and only credits the wallet when rowcount==1.
"""
import os
import sys
import sqlite3
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_settle_idem_")

from harness import wallet  # noqa: E402


def _open(stake=20.0, side="YES", market_id="M", fee_frac=0.0):
    wallet.open_position("M" if market_id is None else market_id, "Q", side, 0.6, 0.5, 0.1,
                         stake, cfg=wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99, fee_frac=fee_frac),
                         end_date=None)


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)


def test_settle_twice_credits_once():
    _reset()
    _open(stake=20.0, side="YES")
    wallet.settle_market("M", 1.0)            # YES wins
    st1 = wallet.get_state()
    wallet.settle_market("M", 1.0)            # re-run / second daemon
    st2 = wallet.get_state()
    assert st1["cash"] == st2["cash"], (st1, st2)
    assert st1["realized_pnl"] == st2["realized_pnl"], (st1, st2)


def test_concurrent_settle_race_credits_once():
    # Simulate the true TOCTOU: two connections both read the row as 'open', then
    # both run settle_market's exact guarded UPDATE. Only one rowcount==1 -> one credit.
    _reset()
    _open(stake=20.0, side="YES")
    a = sqlite3.connect(os.environ["DATABASE_URL"], timeout=30.0); a.row_factory = sqlite3.Row
    b = sqlite3.connect(os.environ["DATABASE_URL"], timeout=30.0); b.row_factory = sqlite3.Row
    ra = a.execute("SELECT * FROM paper_positions WHERE market_id='M' AND status='open'").fetchone()
    rb = b.execute("SELECT * FROM paper_positions WHERE market_id='M' AND status='open'").fetchone()
    assert ra is not None and rb is not None  # both saw it open
    ts = datetime.utcnow().isoformat()
    payout = round(ra["shares"] * 1.0, 6)
    realized = round(payout - ra["stake"] - ra["fee"], 6)
    ca = a.execute("UPDATE paper_positions SET status='settled', outcome=1.0, payout=?, realized_pnl=?, settled_at=? "
                   "WHERE id=? AND status='open'", (payout, realized, ts, ra["id"]))
    n_a = ca.rowcount
    if n_a == 1:
        a.execute("UPDATE paper_wallet SET cash=cash+?, realized_pnl=realized_pnl+? WHERE id=1", (payout, realized))
    a.commit()
    cb = b.execute("UPDATE paper_positions SET status='settled', outcome=1.0, payout=?, realized_pnl=?, settled_at=? "
                   "WHERE id=? AND status='open'", (payout, realized, ts, rb["id"]))
    n_b = cb.rowcount
    if n_b == 1:
        b.execute("UPDATE paper_wallet SET cash=cash+?, realized_pnl=realized_pnl+? WHERE id=1", (payout, realized))
    b.commit()
    a.close(); b.close()
    assert (n_a, n_b) == (1, 0), (n_a, n_b)            # exactly one transition won
    st = wallet.get_state()
    # credited once: realized == payout - stake (single credit), not doubled.
    # get_state rounds to 4 dp, so compare at that precision.
    assert abs(st["realized_pnl"] - realized) < 1e-3, (st, realized)


def test_close_then_settle_cannot_both_credit():
    _reset()
    _open(stake=20.0, side="YES")
    wallet.close_at_price("M", 0.40)          # cash out first
    st1 = wallet.get_state()
    wallet.settle_market("M", 1.0)            # resolution arrives later
    st2 = wallet.get_state()
    # the position was already 'closed'; settle finds no OPEN row -> no second credit
    assert st1["cash"] == st2["cash"], (st1, st2)
    assert st1["realized_pnl"] == st2["realized_pnl"], (st1, st2)


def test_close_twice_credits_once():
    _reset()
    _open(stake=20.0, side="YES")
    wallet.close_at_price("M", 0.40)
    st1 = wallet.get_state()
    wallet.close_at_price("M", 0.40)
    st2 = wallet.get_state()
    assert st1["cash"] == st2["cash"] and st1["realized_pnl"] == st2["realized_pnl"], (st1, st2)


def test_close_realized_includes_fee():
    # close_at_price now subtracts the fee, matching settle_market's convention.
    _reset()
    _open(stake=20.0, side="YES", fee_frac=0.02)   # fee = 0.40
    out = wallet.close_at_price("M", 0.50)
    # realized = sell_value - stake - fee
    r = out[0]
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    pos = conn.execute("SELECT shares, stake, fee FROM paper_positions WHERE market_id='M'").fetchone()
    conn.close()
    expected = round(pos["shares"] * 0.50 - pos["stake"] - pos["fee"], 6)
    assert abs(r["realized_pnl"] - expected) < 1e-6, (r, expected)


def test_open_rejects_when_fee_would_overdraw():
    # affordability covers stake+fee; stake==cash with a fee must be rejected (no negative cash).
    _reset()
    fr = wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 1000.0,
                              cfg=wallet.WalletConfig(max_bet_frac=1.0, max_exposure_frac=1.0, fee_frac=0.05))
    assert fr.opened is False, fr
    assert wallet.get_state()["cash"] >= 0.0


TESTS = [
    ("settle_twice_credits_once", test_settle_twice_credits_once),
    ("concurrent_settle_race_credits_once", test_concurrent_settle_race_credits_once),
    ("close_then_settle_cannot_both_credit", test_close_then_settle_cannot_both_credit),
    ("close_twice_credits_once", test_close_twice_credits_once),
    ("close_realized_includes_fee", test_close_realized_includes_fee),
    ("open_rejects_when_fee_would_overdraw", test_open_rejects_when_fee_would_overdraw),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
