"""Unit tests for harness.profitability — EV-after-costs (P7 B1). PURE math.

No network, no LLM, no DB writes. make_temp_env redirects DATABASE_URL/OBS_LOGS_DIR
so importing harness.wallet (for WalletConfig) binds only a temp path string and
never touches the live polyswarm.db.

Run:  python -m harness.tests.test_profitability
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_profit_")

from harness.profitability import ev_after_costs, ev_gate, REJECT_REASON  # noqa: E402
from harness.wallet import WalletConfig  # noqa: E402


def _approx(a, b, tol=1e-9):
    return a is not None and abs(a - b) < tol


def _wallet_fill(side: str, market_p: float, slippage: float) -> float:
    """Replicate wallet.open_position's fill formula (wallet.py:186-187)."""
    base = market_p if side == "YES" else (1.0 - market_p)
    return min(max(base + slippage, 0.01), 0.99)


def test_healthy_yes_passes():
    # YES p=0.65, market=0.50, slippage=0.01 -> fill 0.51, ev/share 0.14 > 0.
    ev = ev_after_costs(0.65, 0.50, "YES", slippage=0.01)
    assert _approx(ev["fill_price"], 0.51), ev
    assert _approx(ev["ev_per_share"], 0.14), ev
    assert ev["positive"] is True, ev
    ok, reason = ev_gate(0.65, 0.50, "YES", slippage=0.01)
    assert ok is True and reason != REJECT_REASON, (ok, reason)


def test_thin_edge_eaten_by_slippage_rejected():
    # YES p=0.505, market=0.50 -> fill 0.51, ev/share = -0.005 < 0: REJECT.
    ev = ev_after_costs(0.505, 0.50, "YES", slippage=0.01)
    assert _approx(ev["ev_per_share"], -0.005), ev
    assert ev["positive"] is False, ev
    ok, reason = ev_gate(0.505, 0.50, "YES", slippage=0.01)
    assert ok is False and reason == REJECT_REASON, (ok, reason)


def test_zero_ev_is_rejected_strictly():
    # edge exactly == slippage -> ev/share == 0 -> positive is STRICTLY > 0, so reject.
    ev = ev_after_costs(0.51, 0.50, "YES", slippage=0.01)
    assert _approx(ev["ev_per_share"], 0.0), ev
    assert ev["positive"] is False, ev
    ok, reason = ev_gate(0.51, 0.50, "YES", slippage=0.01)
    assert ok is False and reason == REJECT_REASON, (ok, reason)


def test_no_side_symmetry():
    # A NO bet at model_p=0.35 is the mirror of a YES bet at model_p=0.65 (m=0.50).
    yes = ev_after_costs(0.65, 0.50, "YES", slippage=0.01)
    no = ev_after_costs(0.35, 0.50, "NO", slippage=0.01)
    assert _approx(no["fill_price"], 0.51), no            # base = 1-0.50 = 0.50, +slip
    assert _approx(no["ev_per_share"], yes["ev_per_share"]), (no, yes)
    assert _approx(no["ev_per_share"], 0.14), no
    assert no["positive"] is True and no["side"] == "NO", no
    # And a NO bet with NO real edge is rejected: model_p=0.50, market=0.50.
    flat = ev_after_costs(0.50, 0.50, "NO", slippage=0.01)
    assert flat["positive"] is False, flat


def test_breakeven_equals_fill_for_yes():
    ev = ev_after_costs(0.65, 0.50, "YES", slippage=0.01)
    assert _approx(ev["breakeven_p"], ev["fill_price"]), ev
    assert _approx(ev["breakeven_p"], 0.51), ev
    # At model_p == breakeven_p the EV/share is exactly zero (and thus rejected).
    at_be = ev_after_costs(ev["breakeven_p"], 0.50, "YES", slippage=0.01)
    assert _approx(at_be["ev_per_share"], 0.0) and at_be["positive"] is False, at_be


def test_breakeven_for_no_is_one_minus_fill():
    no = ev_after_costs(0.35, 0.50, "NO", slippage=0.01)
    assert _approx(no["breakeven_p"], 1.0 - no["fill_price"]), no
    assert _approx(no["breakeven_p"], 0.49), no


def test_matches_hand_computed_ev():
    # Hand math: fill=0.51, ev/share=0.14, ev/dollar = 0.14/0.51 - 0 = 0.2745098...
    ev = ev_after_costs(0.65, 0.50, "YES", slippage=0.01, fee_frac=0.0)
    assert _approx(ev["ev_per_share"], 0.14), ev
    assert _approx(ev["ev_per_dollar"], 0.14 / 0.51), ev


def test_fee_frac_flows_into_per_dollar_consistently():
    # fee_frac is a flat per-dollar drag (realized = payout - stake - fee).
    # ev/dollar must drop by exactly fee_frac; ev/share (slippage-only) is unchanged.
    base = ev_after_costs(0.65, 0.50, "YES", slippage=0.01, fee_frac=0.0)
    fee = ev_after_costs(0.65, 0.50, "YES", slippage=0.01, fee_frac=0.05)
    assert _approx(fee["ev_per_share"], base["ev_per_share"]), (fee, base)
    assert _approx(fee["ev_per_dollar"], base["ev_per_dollar"] - 0.05), (fee, base)


def test_fee_aware_positive_flag_flips_marginal_bet():
    # The pass/reject DECISION must net the per-trade fee (P7 fix). A bet that is
    # marginally +EV on slippage alone becomes -EV once a non-zero fee_frac is
    # charged, so `positive` flips to False. At the wallet default fee_frac=0.0 the
    # flag is unchanged (no behavior change today).
    no_fee = ev_after_costs(0.52, 0.50, "YES", slippage=0.01, fee_frac=0.0)
    fee = ev_after_costs(0.52, 0.50, "YES", slippage=0.01, fee_frac=0.10)
    assert no_fee["positive"] is True, no_fee          # +EV with no fee
    assert fee["positive"] is False, fee               # fee eats the thin edge
    # net_ev_per_share = ev_per_share - fee*fill_price; reject-only invariant holds.
    assert fee["net_ev_per_share"] < no_fee["net_ev_per_share"]
    # And a healthy edge survives even a fee:
    healthy = ev_after_costs(0.65, 0.50, "YES", slippage=0.01, fee_frac=0.10)
    assert healthy["positive"] is True, healthy


def test_fill_price_matches_wallet_formula():
    # The gate's fill MUST equal the wallet's fill for every leg/price (cost-model
    # consistency). Sweep a range including clamp edges.
    slip = WalletConfig().slippage
    for side in ("YES", "NO"):
        for m in (0.02, 0.10, 0.30, 0.50, 0.70, 0.90, 0.985):
            ev = ev_after_costs(0.5, m, side)   # slippage defaults to WalletConfig
            assert _approx(ev["fill_price"], _wallet_fill(side, m, slip)), (side, m, ev)


def test_uses_wallet_defaults_when_none():
    # slippage/fee_frac None -> WalletConfig defaults (slippage=0.01, fee_frac=0.0).
    cfg = WalletConfig()
    ev = ev_after_costs(0.65, 0.50, "YES")
    assert _approx(ev["fill_price"], 0.50 + cfg.slippage), ev
    assert _approx(ev["ev_per_dollar"], (0.65 - ev["fill_price"]) / ev["fill_price"] - cfg.fee_frac), ev


def test_degenerate_prices_guarded():
    # No crash; every degenerate / out-of-range input is REJECTED (positive False).
    cases = [
        (0.50, 0.0, "YES"),     # market_p == 0 (resolved)
        (0.50, 1.0, "YES"),     # market_p == 1 (resolved)
        (0.50, 0.0, "NO"),
        (0.50, 1.0, "NO"),
        (1.5, 0.50, "YES"),     # model_p out of [0,1]
        (-0.2, 0.50, "NO"),
        (0.65, 0.50, "MAYBE"),  # bad side
        (0.65, 0.50, None),     # missing side
        (float("nan"), 0.50, "YES"),
        (0.65, float("inf"), "YES"),
    ]
    for mp, mk, sd in cases:
        ev = ev_after_costs(mp, mk, sd)
        assert ev["positive"] is False, (mp, mk, sd, ev)
        ok, reason = ev_gate(mp, mk, sd)
        assert ok is False and reason == REJECT_REASON, (mp, mk, sd, ok, reason)
    # Bad side reports side=None / breakeven_p=None and still does not raise.
    bad = ev_after_costs(0.65, 0.50, "MAYBE")
    assert bad["side"] is None and bad["breakeven_p"] is None, bad


def test_gate_is_pure_tightening():
    # The gate's ok is EXACTLY ev_after_costs(...).positive — it adds no new YES,
    # it can only reject. Confirm both poles map correctly.
    ok_pos, _ = ev_gate(0.65, 0.50, "YES")
    assert ok_pos is ev_after_costs(0.65, 0.50, "YES")["positive"] is True
    ok_neg, reason = ev_gate(0.505, 0.50, "YES")
    assert ok_neg is ev_after_costs(0.505, 0.50, "YES")["positive"] is False
    assert reason == REJECT_REASON


TESTS = [
    ("healthy_yes_passes", test_healthy_yes_passes),
    ("thin_edge_eaten_by_slippage_rejected", test_thin_edge_eaten_by_slippage_rejected),
    ("zero_ev_is_rejected_strictly", test_zero_ev_is_rejected_strictly),
    ("no_side_symmetry", test_no_side_symmetry),
    ("breakeven_equals_fill_for_yes", test_breakeven_equals_fill_for_yes),
    ("breakeven_for_no_is_one_minus_fill", test_breakeven_for_no_is_one_minus_fill),
    ("matches_hand_computed_ev", test_matches_hand_computed_ev),
    ("fee_frac_flows_into_per_dollar_consistently", test_fee_frac_flows_into_per_dollar_consistently),
    ("fee_aware_positive_flag_flips_marginal_bet", test_fee_aware_positive_flag_flips_marginal_bet),
    ("fill_price_matches_wallet_formula", test_fill_price_matches_wallet_formula),
    ("uses_wallet_defaults_when_none", test_uses_wallet_defaults_when_none),
    ("degenerate_prices_guarded", test_degenerate_prices_guarded),
    ("gate_is_pure_tightening", test_gate_is_pure_tightening),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
