"""Unit tests for harness.sizing — fractional Kelly. PURE math, no network/LLM.

obs.hooks.on_sizing is guarded and observation-only (never alters the return);
OBS_LOGS_DIR is redirected so any emit lands in a temp dir, never live logs.
Run:  python -m harness.tests.test_sizing
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_sizing_")

from harness.sizing import size_bet, kelly_fraction  # noqa: E402


def _approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) < tol


def test_kelly_direction_and_value():
    side, f = kelly_fraction(0.60, 0.40)
    assert side == "YES" and _approx(f, 0.2 / 0.6), (side, f)
    side, f = kelly_fraction(0.40, 0.60)
    assert side == "NO" and _approx(f, 0.2 / 0.6), (side, f)
    side, f = kelly_fraction(0.50, 0.50)
    assert side is None and f == 0.0


def test_degenerate_prices():
    assert kelly_fraction(0.9, 1.0)[0] is None   # c=1
    assert kelly_fraction(0.1, 0.0)[0] is None   # c=0
    assert kelly_fraction(1.5, 0.5)[0] is None   # p out of range


def test_cap_binds_on_big_edge():
    s = size_bet(0.60, 0.40, bankroll=1000)
    assert s.side == "YES"
    assert s.capped is True and _approx(s.fraction, 0.02), s
    assert _approx(s.stake, 20.0), s
    assert "capped" in s.reason


def test_lambda_path_without_cap():
    s = size_bet(0.53, 0.50, bankroll=1000)   # f*=0.06, quarter=0.015 < cap
    assert s.capped is False
    assert _approx(s.f_star, 0.06) and _approx(s.fraction, 0.015), s
    assert _approx(s.stake, 15.0), s


def test_min_edge_cutoff():
    s = size_bet(0.51, 0.50, bankroll=1000)   # edge 0.01 < min_edge 0.02
    assert s.side is None and s.stake == 0.0, s.reason
    s = size_bet(0.525, 0.50, bankroll=1000)  # edge 0.025 > min_edge
    assert s.side == "YES" and s.stake > 0


def test_bankroll_scaling_and_depletion():
    s1 = size_bet(0.60, 0.40, bankroll=1000)
    s2 = size_bet(0.60, 0.40, bankroll=2000)
    assert _approx(s2.stake, 2 * s1.stake), (s1.stake, s2.stake)
    assert size_bet(0.60, 0.40, bankroll=0).side is None
    assert size_bet(0.60, 0.40, bankroll=-5).side is None


def test_no_side_sizing():
    s = size_bet(0.20, 0.50, bankroll=1000)   # NO; f*=0.6, quarter 0.15 -> cap 0.02
    assert s.side == "NO" and _approx(s.stake, 20.0), s


def test_conviction_overrides():
    # predict_today passes conviction-scaled lam/cap. Half-Kelly at a 10% cap:
    # p=0.60,c=0.40 -> f*=0.3333; 0.5*0.3333=0.1667 capped at 0.10 -> stake 100.
    s = size_bet(0.60, 0.40, bankroll=1000, lam=0.5, cap=0.10)
    assert s.side == "YES"
    assert s.capped is True and _approx(s.fraction, 0.10), s
    assert _approx(s.stake, 100.0), s
    # A thinner edge under the same generous cap stays UNcapped and small.
    s2 = size_bet(0.53, 0.50, bankroll=1000, lam=0.5, cap=0.10)  # f*=0.06 -> 0.03
    assert s2.capped is False and _approx(s2.fraction, 0.03), s2
    assert _approx(s2.stake, 30.0), s2


TESTS = [
    ("kelly_direction_and_value", test_kelly_direction_and_value),
    ("degenerate_prices", test_degenerate_prices),
    ("cap_binds_on_big_edge", test_cap_binds_on_big_edge),
    ("lambda_path_without_cap", test_lambda_path_without_cap),
    ("min_edge_cutoff", test_min_edge_cutoff),
    ("bankroll_scaling_and_depletion", test_bankroll_scaling_and_depletion),
    ("no_side_sizing", test_no_side_sizing),
    ("conviction_overrides", test_conviction_overrides),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
