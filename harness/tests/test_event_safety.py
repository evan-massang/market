"""Plan 6 — EVENT PORTFOLIO SAFETY. A multi-leg / "arbitrage" basket can never be
held complete (we open one leg at a time over an incomplete, stale legset), so its
EXECUTION is disabled: opening a single NO leg of a hedge is NOT risk-free. Only a
genuine single-leg edge opportunity executes, coherently (one YES per event).

NO network, NO LLM. Temp DB only. Run: python -m harness.tests.test_event_safety
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_event_safety_")
os.environ["LLM_PROVIDER"] = "ollama"

from harness import event_safety as ES        # noqa: E402
from harness import event_portfolio as EP      # noqa: E402
from harness import predict_today as PT        # noqa: E402
from harness import wallet as W                # noqa: E402
from harness import db_check as DC             # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BANKROLL = 1000.0


def _leg(leg_id, model_p, price, **kw):
    d = {"leg_id": leg_id, "market_id": leg_id, "model_p": model_p, "price": price,
         "liquidity": 100_000.0, "exit_risk": 0.02, "has_data": True}
    d.update(kw)
    return d


def _reset_wallet():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(BANKROLL)


def _seed_open(market_id, side, event_slug, stake=20.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, "
        "stake, fill_price, shares, fee, status, event_slug) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (market_id, "Q", side, 0.6, 0.5, 0.1, stake, 0.51, stake / 0.51, 0.0, event_slug))
    conn.commit(); conn.close()


# ════════════════════════════════════════════════════════════════════════════
# (1-6) validate_event_legset — completeness / staleness / relationship
# ════════════════════════════════════════════════════════════════════════════
def test_complete_legset_validates():
    legs = [_leg("A", 0.25, 0.32), _leg("B", 0.25, 0.32)]
    r = ES.validate_event_legset(legs, mutually_exclusive=True)
    assert r["ok"] is True and r["can_evaluate_basket"] is True, r
    assert r["n_active_markets"] == 2 and r["exhaustive"] is False   # never provably exhaustive


def test_missing_required_leg_blocks():
    legs = [_leg("A", 0.25, 0.32), _leg("B", None, 0.32)]   # B missing model_p
    r = ES.validate_event_legset(legs, mutually_exclusive=True)
    assert r["ok"] is False and r["reason"] == ES.INCOMPLETE_LEGSET, r
    assert "B" in r["missing_required_legs"]


def test_stale_leg_blocks():
    # 2 active legs + 1 stale leg (no price) -> the stale leg blocks the whole basket
    legs = [_leg("A", 0.25, 0.32), _leg("B", 0.25, 0.32), _leg("C", 0.25, None)]
    r = ES.validate_event_legset(legs, mutually_exclusive=True)
    assert r["ok"] is False and r["reason"] == ES.STALE_LEGSET, r
    assert "C" in r["stale_legs"]


def test_closed_or_degenerate_leg_reduces_active():
    legs = [_leg("A", 0.25, 0.32), _leg("B", 0.25, 1.0)]   # B price 1.0 = resolved/degenerate
    r = ES.validate_event_legset(legs, mutually_exclusive=True)
    assert r["ok"] is False and r["n_active_markets"] == 1, r   # fewer than 2 active -> not a basket


def test_unknown_relationship_blocks():
    legs = [_leg("A", 0.25, 0.32), _leg("B", 0.25, 0.32)]
    r = ES.validate_event_legset(legs, mutually_exclusive=None)   # relationship unknown
    assert r["ok"] is False and r["reason"] == ES.UNKNOWN_RELATIONSHIP, r
    assert r["unknown_relationships"] is True


def test_legset_never_exhaustive_so_never_risk_free():
    # even a complete-looking, all-fresh legset is NOT exhaustive (built from held siblings),
    # so it can never be labeled risk-free.
    legs = [_leg(c, 0.25, 0.32) for c in ("A", "B", "C", "D")]
    r = ES.validate_event_legset(legs, mutually_exclusive=True)
    assert r["exhaustive"] is False


# ════════════════════════════════════════════════════════════════════════════
# (11-14) coherence — one YES per event, duplicate, unrelated
# ════════════════════════════════════════════════════════════════════════════
def test_already_hold_yes_blocks_new_yes():
    pos = [{"market_id": "X", "event_slug": "evt1", "side": "YES"}]
    r = ES.check_event_position_coherence("evt1", "YES", "Y", pos)
    assert r["ok"] is False and r["reason"] == ES.ALREADY_HOLD_YES, r


def test_duplicate_market_open_blocks():
    pos = [{"market_id": "Y", "event_slug": "evt1", "side": "NO"}]
    r = ES.check_event_position_coherence("evt1", "YES", "Y", pos)   # Y already open
    assert r["ok"] is False and r["reason"] == ES.INCOHERENT_POSITION, r


def test_new_no_allowed_alongside_held_yes():
    # a NO add is coherent (fade) even when a YES is held in the event
    pos = [{"market_id": "X", "event_slug": "evt1", "side": "YES"}]
    r = ES.check_event_position_coherence("evt1", "NO", "Y", pos)
    assert r["ok"] is True, r


def test_unrelated_event_does_not_block():
    pos = [{"market_id": "X", "event_slug": "OTHER", "side": "YES"}]
    r = ES.check_event_position_coherence("evt1", "YES", "Y", pos)
    assert r["ok"] is True, r


# ════════════════════════════════════════════════════════════════════════════
# (7-10, 23-24) classify_event_execution — arb disabled, single-leg allowed
# ════════════════════════════════════════════════════════════════════════════
def test_arbitrage_basket_not_executable():
    ep = EP.EventPortfolio(accept=True, is_arbitrage=True,
                           positions=[{"leg_id": "A", "side": "NO"}, {"leg_id": "B", "side": "NO"}])
    label, executable, reason = ES.classify_event_execution(ep, {"leg_id": "A", "side": "NO"})
    assert executable is False and reason == ES.EXECUTION_DISABLED
    assert label == ES.LABEL_VERIFIED_NOT_EXECUTABLE


def test_single_leg_edge_is_executable():
    ep = EP.EventPortfolio(accept=True, is_arbitrage=False, positions=[{"leg_id": "A", "side": "YES"}])
    label, executable, reason = ES.classify_event_execution(ep, {"leg_id": "A", "side": "YES"})
    assert executable is True and label == ES.LABEL_SINGLE_LEG and reason is None


def test_rejected_basket_not_executable():
    ep = EP.EventPortfolio(accept=False, reject_reason="incoherent")
    label, executable, reason = ES.classify_event_execution(ep, None)
    assert executable is False and label == ES.LABEL_BLOCKED


def test_classify_never_returns_executable_basket_label():
    # the engine never gets to emit an *executable basket* — a basket is at most a
    # not-executable recommendation; only single legs execute.
    ep = EP.EventPortfolio(accept=True, is_arbitrage=True, positions=[{"leg_id": "A", "side": "NO"}])
    label, executable, _ = ES.classify_event_execution(ep, {"leg_id": "A", "side": "NO"})
    assert label != ES.LABEL_EXECUTABLE and executable is False
    assert ES.multi_leg_execution_enabled() is False   # disabled by default


# ════════════════════════════════════════════════════════════════════════════
# engine still RECOMMENDS the arb (pure) — Plan 6 only disables EXECUTION
# ════════════════════════════════════════════════════════════════════════════
def test_engine_still_recommends_arb_but_softened_language():
    legs = [_leg(c, 0.25, 0.325) for c in ("A", "B", "C", "D")]
    ep = EP.evaluate_event(legs, BANKROLL, cfg=EP.Config(mutually_exclusive=True))
    assert ep.accept is True and ep.is_arbitrage is True          # engine recommendation unchanged
    assert ep.worst_case_loss >= -1e-6                            # genuine hedge math intact
    # but the prose no longer CLAIMS executed risk-free
    assert "⇒ risk-free" not in ep.explanation
    assert "RECOMMENDATION ONLY" in ep.explanation


# ════════════════════════════════════════════════════════════════════════════
# (25-29) run_event_portfolio integration — arb blocked, single-leg allowed, coherence
# ════════════════════════════════════════════════════════════════════════════
def test_run_event_portfolio_blocks_arbitrage_execution():
    _reset_wallet()
    legs = [_leg(c, 0.25, 0.325) for c in ("A", "B", "C", "D")]
    ep, my_pos = PT.run_event_portfolio("A", legs, "evtARB", BANKROLL)
    assert ep.is_arbitrage is True
    assert my_pos is None, "an arbitrage basket leg must NOT be opened (partial/fake-risk-free)"
    assert PT.event_leg_reject_reason(ep, "A") == ES.EXECUTION_DISABLED


def test_run_event_portfolio_allows_single_leg_edge():
    _reset_wallet()
    legs = [_leg("A", 0.70, 0.45), _leg("B", 0.15, 0.30), _leg("C", 0.15, 0.25)]
    ep, my_pos = PT.run_event_portfolio("A", legs, "evtEDGE", BANKROLL)
    assert ep.accept is True and ep.is_arbitrage is False
    assert my_pos is not None and my_pos["side"] == "YES" and my_pos["leg_id"] == "A"


def test_run_event_portfolio_blocks_incoherent_second_yes():
    _reset_wallet()
    _seed_open("HELD", "YES", "evtEDGE")          # already hold a YES in this event
    legs = [_leg("A", 0.70, 0.45), _leg("B", 0.15, 0.30), _leg("C", 0.15, 0.25)]
    ep, my_pos = PT.run_event_portfolio("A", legs, "evtEDGE", BANKROLL)
    assert my_pos is None, "must not open a second YES in the same event"
    assert PT.event_leg_reject_reason(ep, "A") == ES.ALREADY_HOLD_YES


# ════════════════════════════════════════════════════════════════════════════
# (30) db_check reports multiple open YES in the same event
# ════════════════════════════════════════════════════════════════════════════
def test_db_check_flags_multiple_open_yes():
    _reset_wallet()
    _seed_open("M1", "YES", "evtX")
    _seed_open("M2", "YES", "evtX")               # 2 open YES in one event
    res = DC.run()
    row = next((s for n, s, _ in res["checks"] if n == "event_multiple_open_yes"), None)
    assert row == "WARN", res["checks"]


# ════════════════════════════════════════════════════════════════════════════
# (31-32) static enforcement
# ════════════════════════════════════════════════════════════════════════════
def test_event_portfolio_engine_never_opens_positions():
    # the engine is PURE — it must never CALL wallet.open_position (docstring mentions of
    # the name are fine; an actual `.open_position(` call is not).
    import re
    src = open(os.path.join(_REPO, "harness", "event_portfolio.py"), encoding="utf-8").read()
    assert re.search(r"\.open_position\s*\(", src) is None, "engine must not call open_position"


def test_consumer_gates_arbitrage_execution_in_source():
    # run_event_portfolio must consult the Plan-6 execution classifier before returning a leg.
    src = open(os.path.join(_REPO, "harness", "predict_today.py"), encoding="utf-8").read()
    assert "_esafe.classify_event_execution(" in src
    assert "_esafe.check_event_position_coherence(" in src


def test_arbitrage_blocked_even_if_env_flag_set():
    # adversarial caveat #1: flipping ENABLE_EVENT_BASKET_EXECUTION must NOT let a single
    # arb leg open (no atomic executor exists) — arb execution is blocked UNCONDITIONALLY.
    ep = EP.EventPortfolio(accept=True, is_arbitrage=True, positions=[{"leg_id": "A", "side": "NO"}])
    old = os.environ.get("ENABLE_EVENT_BASKET_EXECUTION")
    os.environ["ENABLE_EVENT_BASKET_EXECUTION"] = "true"
    try:
        label, executable, reason = ES.classify_event_execution(ep, {"leg_id": "A", "side": "NO"})
    finally:
        if old is None:
            os.environ.pop("ENABLE_EVENT_BASKET_EXECUTION", None)
        else:
            os.environ["ENABLE_EVENT_BASKET_EXECUTION"] = old
    assert executable is False and reason == ES.EXECUTION_DISABLED, (label, executable, reason)


def test_coherence_failure_fails_closed():
    # adversarial caveat #2: if the coherence read raises, the leg must FAIL CLOSED (no open),
    # consistent with the Plan-1 money gates (not silently skip the one-YES check).
    _reset_wallet()
    legs = [_leg("A", 0.70, 0.45), _leg("B", 0.15, 0.30), _leg("C", 0.15, 0.25)]

    def _boom():
        raise RuntimeError("simulated wallet read failure")

    with patched(W, "get_open_positions", _boom):
        ep, my_pos = PT.run_event_portfolio("A", legs, "evtEDGE", BANKROLL)
    assert my_pos is None, "a coherence-check failure must fail CLOSED (no open)"
    assert PT.event_leg_reject_reason(ep, "A") == ES.INCOHERENT_POSITION


def test_no_executed_risk_free_claim_remains():
    # no source emits an executed "risk-free" claim; the engine's softened prose says NOT risk-free.
    ep_src = open(os.path.join(_REPO, "harness", "event_portfolio.py"), encoding="utf-8").read()
    assert "⇒ risk-free" not in ep_src       # the old executed-claim wording is gone
    assert "NOT risk-free" in ep_src          # honest language present


TESTS = [
    ("complete_legset_validates", test_complete_legset_validates),
    ("missing_required_leg_blocks", test_missing_required_leg_blocks),
    ("stale_leg_blocks", test_stale_leg_blocks),
    ("closed_or_degenerate_leg_reduces_active", test_closed_or_degenerate_leg_reduces_active),
    ("unknown_relationship_blocks", test_unknown_relationship_blocks),
    ("legset_never_exhaustive_so_never_risk_free", test_legset_never_exhaustive_so_never_risk_free),
    ("already_hold_yes_blocks_new_yes", test_already_hold_yes_blocks_new_yes),
    ("duplicate_market_open_blocks", test_duplicate_market_open_blocks),
    ("new_no_allowed_alongside_held_yes", test_new_no_allowed_alongside_held_yes),
    ("unrelated_event_does_not_block", test_unrelated_event_does_not_block),
    ("arbitrage_basket_not_executable", test_arbitrage_basket_not_executable),
    ("single_leg_edge_is_executable", test_single_leg_edge_is_executable),
    ("rejected_basket_not_executable", test_rejected_basket_not_executable),
    ("classify_never_returns_executable_basket_label", test_classify_never_returns_executable_basket_label),
    ("engine_still_recommends_arb_but_softened_language", test_engine_still_recommends_arb_but_softened_language),
    ("run_event_portfolio_blocks_arbitrage_execution", test_run_event_portfolio_blocks_arbitrage_execution),
    ("run_event_portfolio_allows_single_leg_edge", test_run_event_portfolio_allows_single_leg_edge),
    ("run_event_portfolio_blocks_incoherent_second_yes", test_run_event_portfolio_blocks_incoherent_second_yes),
    ("db_check_flags_multiple_open_yes", test_db_check_flags_multiple_open_yes),
    ("event_portfolio_engine_never_opens_positions", test_event_portfolio_engine_never_opens_positions),
    ("consumer_gates_arbitrage_execution_in_source", test_consumer_gates_arbitrage_execution_in_source),
    ("no_executed_risk_free_claim_remains", test_no_executed_risk_free_claim_remains),
    ("arbitrage_blocked_even_if_env_flag_set", test_arbitrage_blocked_even_if_env_flag_set),
    ("coherence_failure_fails_closed", test_coherence_failure_fails_closed),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
