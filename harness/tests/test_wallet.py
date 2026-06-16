"""Unit tests for harness.wallet — simulated paper wallet. No network/LLM.

DATABASE_URL is redirected to a throwaway temp DB BEFORE importing wallet
(wallet binds DB_PATH at import). Each test resets the wallet so it is
order-independent under both run_as_main and pytest.
Run:  python -m harness.tests.test_wallet
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_wallet_")

from harness import wallet as W            # noqa: E402
from harness.wallet import WalletConfig    # noqa: E402
from harness.sizing import size_bet        # noqa: E402


def _approx(a, b, tol=1e-4):
    return a is not None and abs(a - b) < tol


def _reset(starting=1000.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit()
    conn.close()
    W.init_wallet(starting)


def test_init():
    _reset()
    s = W.get_state()
    assert s["cash"] == 1000 and s["equity"] == 1000 and s["realized_pnl"] == 0, s
    assert s["n_open"] == 0


def test_open_yes_fill_and_accounting():
    _reset()
    sz = size_bet(0.60, 0.40, bankroll=W.bankroll_for_sizing())   # stake 20 (capped)
    assert sz.side == "YES" and _approx(sz.stake, 20.0), sz
    fr = W.open_position("MKT-1", "Will X win?", "YES", 0.60, 0.40, sz.edge, sz.stake)
    assert fr.opened, fr.reason
    assert _approx(fr.fill_price, 0.41), fr.fill_price           # mid 0.40 + slippage 0.01
    assert _approx(fr.shares, 20 / 0.41), fr.shares
    assert _approx(W.get_state()["cash"], 980.0), W.get_state()
    assert _approx(W.get_open_exposure(), 20.0)


def test_settle_win():
    _reset()
    W.open_position("MKT-1", "Will X win?", "YES", 0.60, 0.40, 0.2, 20.0)
    res = W.settle_market("MKT-1", outcome=1.0)
    assert _approx(res[0]["payout"], 20 / 0.41), res[0]
    assert _approx(res[0]["realized_pnl"], 20 / 0.41 - 20), res[0]
    st = W.get_state()
    assert _approx(st["cash"], 980 + 20 / 0.41), st
    assert _approx(st["realized_pnl"], 20 / 0.41 - 20)
    assert st["n_open"] == 0


def test_settle_loss_costs_exactly_stake():
    _reset()
    cash_before = W.get_state()["cash"]
    W.open_position("MKT-2", "Will Y win?", "YES", 0.60, 0.40, 0.2, 20.0)
    W.settle_market("MKT-2", outcome=0.0)
    st = W.get_state()
    assert _approx(st["cash"], cash_before - 20.0), st
    assert _approx(st["realized_pnl"], -20.0), st


def test_no_side_round_trip():
    _reset()
    fr = W.open_position("MKT-3", "NO market", "NO", 0.50, 0.70, -0.20, 20.0)
    assert fr.opened and _approx(fr.fill_price, 0.31), fr   # (1-0.70)+0.01
    res = W.settle_market("MKT-3", outcome=0.0)             # NO wins
    assert res[0]["won"] is True
    assert _approx(res[0]["payout"], 20 / 0.31), res[0]


def test_guardrails():
    # Plan 4: canonical wallet_* rejection reasons.
    _reset()
    big = W.open_position("MKT-X", "too big", "YES", 0.6, 0.4, 0.2, stake=50.0)  # > per-bet cap (20)
    assert (not big.opened) and big.reason == "wallet_per_bet_cap_exceeded", big.reason
    tight = WalletConfig(max_exposure_frac=0.001)
    exp = W.open_position("MKT-Y", "exposure", "YES", 0.6, 0.4, 0.2,
                          stake=W.bankroll_for_sizing() * 0.02, cfg=tight)
    assert not exp.opened and exp.reason == "wallet_exposure_cap_exceeded", exp.reason
    bad = W.open_position("MKT-Z", "bad side", "MAYBE", 0.6, 0.4, 0.2, stake=5.0)
    assert (not bad.opened) and bad.reason == "wallet_invalid_side", bad.reason
    nonpos = W.open_position("MKT-0", "zero", "YES", 0.6, 0.4, 0.2, stake=0.0)
    assert (not nonpos.opened) and nonpos.reason == "wallet_invalid_stake", nonpos.reason
    over = W.open_position("MKT-C", "over cash", "YES", 0.6, 0.4, 0.2, stake=5000.0)
    assert (not over.opened) and over.reason == "wallet_insufficient_cash", over.reason


def test_cap_exact_stake_fills():
    # Relative tolerance (*(1+1e-6)) means a stake sized EXACTLY at the per-bet
    # cap (max_bet_frac * cash) must NOT be rejected by float rounding.
    _reset()
    cash = W.bankroll_for_sizing()
    exact = WalletConfig().max_bet_frac * cash   # 0.02 * 1000 == 20.0
    fr = W.open_position("MKT-CAP", "cap exact", "YES", 0.6, 0.4, 0.2, stake=exact)
    assert fr.opened, fr.reason


def test_bankroll_for_sizing_tracks_cash():
    _reset()
    assert _approx(W.bankroll_for_sizing(), 1000.0)
    W.open_position("MKT-1", "Will X win?", "YES", 0.60, 0.40, 0.2, 20.0)
    assert _approx(W.bankroll_for_sizing(), 980.0)


TESTS = [
    ("init", test_init),
    ("open_yes_fill_and_accounting", test_open_yes_fill_and_accounting),
    ("settle_win", test_settle_win),
    ("settle_loss_costs_exactly_stake", test_settle_loss_costs_exactly_stake),
    ("no_side_round_trip", test_no_side_round_trip),
    ("guardrails", test_guardrails),
    ("cap_exact_stake_fills", test_cap_exact_stake_fills),
    ("bankroll_for_sizing_tracks_cash", test_bankroll_for_sizing_tracks_cash),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
