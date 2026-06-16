"""Gate methodology — Gate-1 bet-bias fix + test/demo exclusion (audit Phase 1 + 3).

Gate 1 (forecast quality) must score ALL resolved eligible forecasts, not only the
bet ones; test/demo/benchmark markets must never contaminate the gates.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_gatemeth_")

import core.calibration as CAL          # noqa: E402
from harness import wallet              # noqa: E402
from harness import scoreboard as SB    # noqa: E402
from harness import environment as ENV  # noqa: E402
from harness import loop as LOOP        # noqa: E402


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("swarm_forecasts", "forecasts", "baseline_forecasts", "paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    CAL.init_db(); wallet.init_wallet(1000.0)


def _forecast(market_id, p, market_odds, q="Will candidate X win the 2032 election?"):
    CAL.save_swarm_forecast(q, p, 0.7, market_odds=market_odds, market_id=market_id)


# ── Phase 3: test/demo exclusion ─────────────────────────────────────────────--
def test_environment_classification():
    assert ENV.is_real_market("0xabc1234567") and not ENV.is_real_market("TEST")
    assert ENV.classify("bench-test") == "benchmark"
    assert ENV.is_live("TEST") is False
    assert ENV.is_live("TEST", include_test=True) is True
    assert ENV.is_live("0xdeadbeef99") is True


def test_demo_excluded_from_gate1_by_default():
    _reset()
    _forecast("0xaa00bb00cc", 0.70, 0.50)        # real -> counts
    _forecast("bench-test", 0.70, 0.50)          # benchmark -> excluded
    _forecast("TEST", 0.70, 0.50)                # test -> excluded
    CAL.resolve_forecast("Will candidate X win the 2032 election?", 1.0, market_id="0xaa00bb00cc")
    CAL.resolve_forecast("Will candidate X win the 2032 election?", 1.0, market_id="bench-test")
    CAL.resolve_forecast("Will candidate X win the 2032 election?", 1.0, market_id="TEST")
    s = SB.compute()
    assert s["n"] == 1, s["n"]                    # only the real market
    s_all = SB.compute(include_test=True)
    assert s_all["n"] == 3, s_all["n"]            # opt-in includes test/bench


# ── Phase 1: Gate-1 bet-bias ───────────────────────────────────────────────────
def test_forecast_saved_when_skipped():
    _reset()
    _forecast("0xbeef005678", 0.65, 0.50)        # forecast made, no bet/position
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    n = conn.execute("SELECT COUNT(*) FROM swarm_forecasts WHERE market_id='0xbeef005678'").fetchone()[0]
    conn.close()
    assert n == 1                                 # the forecast record exists even with no trade


def test_unbet_forecast_resolves_for_gate1_not_gate2():
    _reset()
    _forecast("0xdead00beef", 0.70, 0.50)        # forecast, NO position
    cash_before = wallet.get_state()["realized_pnl"]

    # mock Gamma: this market resolved YES, no live network
    def _fetch(mid):
        return {"market_id": mid, "question": "Will candidate X win the 2032 election?"}
    with patched(LOOP.gamma, "fetch_market_by_condition_id", _fetch), \
         patched(LOOP.gamma, "resolution_outcome", lambda m: 1.0):
        scored = LOOP._settle_unbet_forecasts(set())     # nothing already bet
    assert scored == 1, scored
    # the forecast now has an outcome (counts for Gate 1)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    outc = conn.execute("SELECT outcome FROM swarm_forecasts WHERE market_id='0xdead00beef'").fetchone()[0]
    conn.close()
    assert outc == 1.0
    assert SB.compute()["n"] == 1                 # Gate 1 counts the unbet forecast
    # Gate 2 (paper P&L) is unchanged — no trade was placed
    assert wallet.get_state()["realized_pnl"] == cash_before


def test_bet_market_not_reswept():
    _reset()
    _forecast("0x1234abcd99", 0.70, 0.50)
    # if this market is already in the bet set, the unbet sweep must skip it
    def _fetch(mid):
        raise AssertionError("should not fetch an already-bet market")
    with patched(LOOP.gamma, "fetch_market_by_condition_id", _fetch):
        scored = LOOP._settle_unbet_forecasts({"0x1234abcd99"})
    assert scored == 0


def test_market_brier_uses_decision_time_price():
    # Gate 1 must use the market price stored AT FORECAST TIME (market_odds), not a
    # later price. We store market_odds=0.40; outcome=1 -> market_brier=(0.40-1)^2=0.36.
    _reset()
    _forecast("0xcafe001234", 0.80, 0.40)
    CAL.resolve_forecast("Will candidate X win the 2032 election?", 1.0, market_id="0xcafe001234")
    rows = SB._resolved_opinion_rows()
    assert len(rows) == 1
    assert abs(rows[0]["market_brier"] - (0.40 - 1.0) ** 2) < 1e-9, rows[0]


TESTS = [
    ("environment_classification", test_environment_classification),
    ("demo_excluded_from_gate1_by_default", test_demo_excluded_from_gate1_by_default),
    ("forecast_saved_when_skipped", test_forecast_saved_when_skipped),
    ("unbet_forecast_resolves_for_gate1_not_gate2", test_unbet_forecast_resolves_for_gate1_not_gate2),
    ("bet_market_not_reswept", test_bet_market_not_reswept),
    ("market_brier_uses_decision_time_price", test_market_brier_uses_decision_time_price),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
