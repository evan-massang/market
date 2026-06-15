"""P8 wire — no-network tests for the unified risk_guards.evaluate composition
and the predict_today fail-open wrapper.

Covers:
  * NO OVER-BLOCK: a clean, liquid opinion market in a HEALTHY book -> allow True
  * each guard fires with the right blocking_reason (stale / low_liquidity /
    high_spread / correlated_exposure)
  * STRICTER WHEN LOSING: a drawn-down book raises tighten>1.0 and blocks a
    borderline market that was allowed when healthy
  * FAIL-OPEN: an internal error -> allow True (never crashes / wrongly blocks)
  * predict_today._p8_risk_guards returns (allow, reason) and fails open
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_p8wire_")

from harness import wallet                       # noqa: E402
from harness import risk_guards as RG            # noqa: E402
from harness import market_quality as MQ         # noqa: E402
from harness import portfolio_guards as PG       # noqa: E402
import harness.predict_today as PT               # noqa: E402

# a question that classifies opinion + theme 'elections'
_ELECTION_Q = "Will candidate {} win the 2032 presidential election?"


def _reset_wallet(starting=1000.0, cash=None, realized=0.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    wallet.init_wallet(starting)
    if cash is not None or realized:
        conn = sqlite3.connect(os.environ["DATABASE_URL"])
        conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=? WHERE id=1",
                     (starting if cash is None else cash, realized))
        conn.commit()
        conn.close()


def _open_position(question, market_id, side="YES", stake=10.0, event_slug=None):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions "
        "(market_id, question, side, model_p, market_p, edge, stake, fill_price, shares, fee, status, event_slug) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (market_id, question, side, 0.6, 0.5, 0.1, stake, 0.51, stake / 0.51, 0.0, event_slug),
    )
    conn.commit()
    conn.close()


def mk(*, question="Will the incumbent win the 2032 governor race?", price=0.50,
       volume=200_000.0, liquidity=40_000.0, raw=None, event_slug=None,
       end_date="2032-01-01T00:00:00Z"):
    return {
        "market_id": "MQ1",
        "question": question,
        "price": price,
        "outcome_prices": [price, round(1 - price, 4)],
        "volume": volume,
        "liquidity": liquidity,
        "end_date": end_date,
        "event_slug": event_slug,
        "raw": raw or {},
    }


# ── 1) NO OVER-BLOCK ──────────────────────────────────────────────────────────
def test_clean_market_healthy_book_allowed():
    _reset_wallet()
    v = RG.evaluate(mk(), "YES", "Will the incumbent win the 2032 governor race?")
    assert v["allow"] is True, v
    assert v["blocking_reason"] is None, v
    assert v["tighten"] == 1.0, v                       # healthy book == baseline
    names = {c["name"] for c in v["checks"]}
    # market_quality emits short check names; the reason CODES are low_liquidity/high_spread
    assert {"stale_price", "liquidity", "spread"} <= names, names
    assert {"correlated_exposure", "bad_theme"} <= names, names


# ── 2) EACH GUARD FIRES ───────────────────────────────────────────────────────
def test_stale_blocks():
    _reset_wallet()
    v = RG.evaluate(mk(liquidity=0.0), "YES", "q")
    assert v["allow"] is False and v["blocking_reason"] == "stale_price", v


def test_low_liquidity_blocks():
    _reset_wallet()
    v = RG.evaluate(mk(volume=100.0, liquidity=40_000.0), "YES", "q")  # vol below 5k floor
    assert v["allow"] is False and v["blocking_reason"] == "low_liquidity", v


def test_high_spread_blocks():
    _reset_wallet()
    v = RG.evaluate(mk(raw={"spread": 0.12}), "YES", "q")  # 12c quoted spread
    assert v["allow"] is False and v["blocking_reason"] == "high_spread", v


def test_correlated_exposure_blocks():
    _reset_wallet()
    # fill the book with >= DEFAULT_MAX_SAME_THEME open election positions
    for i in range(PG.DEFAULT_MAX_SAME_THEME):
        _open_position(_ELECTION_Q.format(i), f"E{i}")
    cand = mk(question=_ELECTION_Q.format("X"))
    v = RG.evaluate(cand, "YES", cand["question"])
    assert v["allow"] is False and v["blocking_reason"] == "correlated_exposure", v


# ── 3) STRICTER WHEN LOSING ───────────────────────────────────────────────────
def test_drawdown_tightens_and_blocks_borderline():
    # borderline depth: exit_risk ~0.447 -> passes at tighten 1.0, blocks at 2.0
    borderline = mk(volume=120_000.0, liquidity=25_000.0)
    # healthy book -> allowed
    _reset_wallet()
    assert PG.stricter_tighten() == 1.0
    assert RG.evaluate(borderline, "YES", borderline["question"])["allow"] is True
    # drawn-down book (equity 700 < starting 1000, realized -300) -> tighten 2.0
    _reset_wallet(starting=1000.0, cash=700.0, realized=-300.0)
    assert PG.stricter_tighten() > 1.0
    v = RG.evaluate(borderline, "YES", borderline["question"])
    assert v["allow"] is False and v["blocking_reason"] == "high_spread", v


# ── 4) FAIL-OPEN ──────────────────────────────────────────────────────────────
def test_fail_open_on_internal_error():
    _reset_wallet()
    def _boom(*a, **k):
        raise RuntimeError("boom")
    with patched(MQ, "evaluate_market_quality", _boom):
        v = RG.evaluate(mk(), "YES", "q")
    assert v["allow"] is True and v["blocking_reason"] is None, v


def test_predict_today_wrapper_allows_clean_and_fails_open():
    _reset_wallet()
    ok, reason = PT._p8_risk_guards(mk(), "YES", "Will the incumbent win the 2032 governor race?")
    assert ok is True, (ok, reason)
    # fail-open: a broken evaluate still returns allow
    def _boom(*a, **k):
        raise RuntimeError("boom")
    with patched(RG, "evaluate", _boom):
        ok2, reason2 = PT._p8_risk_guards(mk(), "YES", "q")
    assert ok2 is True, (ok2, reason2)


TESTS = [
    ("clean_market_healthy_book_allowed", test_clean_market_healthy_book_allowed),
    ("stale_blocks", test_stale_blocks),
    ("low_liquidity_blocks", test_low_liquidity_blocks),
    ("high_spread_blocks", test_high_spread_blocks),
    ("correlated_exposure_blocks", test_correlated_exposure_blocks),
    ("drawdown_tightens_and_blocks_borderline", test_drawdown_tightens_and_blocks_borderline),
    ("fail_open_on_internal_error", test_fail_open_on_internal_error),
    ("predict_today_wrapper_allows_clean_and_fails_open", test_predict_today_wrapper_allows_clean_and_fails_open),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
