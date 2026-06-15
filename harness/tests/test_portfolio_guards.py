"""P8 (B2) — no-network unit tests for harness.portfolio_guards.

Temp DB only (make_temp_env). No network, no LLM. Verifies the bet-BETTER-not-MORE
contract: every guard can only BLOCK / TIGHTEN, never loosen, and a clean liquid
opinion market in a healthy book passes ALL of them.

Covered:
  * empty/healthy book        -> drawdown_state in_drawdown False, severity 0,
                                 stricter_tighten() == 1.0, correlation ok,
                                 bad_theme ok  (NO OVER-BLOCK proof)
  * many open same-theme       -> check_correlation blocks 'correlated_exposure'
  * many open same-event       -> check_correlation blocks 'correlated_exposure'
  * heavily-losing theme       -> check_bad_theme blocks 'bad_theme'…
  * mildly-losing theme        -> …while a mild loss does NOT (adaptive's job)
  * tighten lowers the bar     -> a moderate loss blocks only under drawdown
  * drawn-down book            -> stricter_tighten() > 1.0 (and saturates at cap)
  * missing tables / malformed -> never raises; degrades to safe (ok / no-tighten)

Run:  python -m harness.tests.test_portfolio_guards
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_pguards_")

from harness import wallet as paper          # noqa: E402
from harness import portfolio_guards as PG   # noqa: E402


# ── question fixtures (offline-classifiable) ──────────────────────────────────
# scoreboard.theme_of tags these; the election/geo ones also classify OPINION
# offline (use_llm=False), the population adaptive.theme_pnl(opinion_only=True)
# counts.
_ELECTION_Q = "Will candidate {} win the 2028 presidential election?"
_OTHER_Q = "Will widget {} ship before Friday?"   # no theme keyword -> "other"


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Fresh wallet + paper_positions so each test is order-independent."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    paper.init_wallet(1000.0)


def _insert_open(market_id, question, side, stake, event_slug=None):
    """Insert ONE OPEN paper position directly (the module under test is read-only)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions "
        "(market_id, question, side, model_p, market_p, edge, stake, fill_price, "
        " shares, fee, status, event_slug) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (market_id, question, side, 0.6, 0.5, 0.1, stake, 0.51,
         stake / 0.51, 0.0, event_slug),
    )
    conn.commit()
    conn.close()


def _insert_settled(question, side, stake, realized_pnl, market_id="M"):
    """Insert ONE SETTLED paper position directly (for theme_pnl track record)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions "
        "(market_id, question, side, model_p, market_p, edge, stake, fill_price, "
        " shares, fee, status, outcome, payout, realized_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'settled', ?,?,?)",
        (market_id, question, side, 0.6, 0.5, 0.1, stake, 0.51,
         stake / 0.51, 0.0, 1.0 if realized_pnl > 0 else 0.0,
         max(0.0, stake + realized_pnl), realized_pnl),
    )
    conn.commit()
    conn.close()


def _set_wallet(cash, realized_pnl):
    """Force the realized wallet state (no open positions) to simulate a settled
    drawdown. equity == cash here (open_exposure == 0)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=? WHERE id=1",
                 (cash, realized_pnl))
    conn.commit()
    conn.close()


# ── empty / healthy book: NO OVER-BLOCK ───────────────────────────────────────
def test_empty_book_is_clean_and_passes_everything():
    _reset()

    ds = PG.drawdown_state()
    assert ds["in_drawdown"] is False, ds
    assert ds["severity"] == 0.0, ds
    assert ds["drawdown_frac"] == 0.0, ds
    assert PG.stricter_tighten() == 1.0

    assert PG.open_positions() == []

    # a clean, liquid opinion market in a healthy book MUST pass every guard
    clean = {"market_id": "CLEAN", "question": _ELECTION_Q.format("Alice"),
             "event_slug": "us-2028", "liquidity": 40000.0, "volume": 120000.0}
    ok_c, reason_c, _ = PG.check_correlation(clean, "YES")
    assert ok_c is True and reason_c is None
    ok_t, reason_t, _ = PG.check_bad_theme(clean["question"])
    assert ok_t is True and reason_t is None


def test_healthy_book_with_a_few_diverse_positions_still_passes():
    _reset()
    # a small, diversified book — different themes, different events
    _insert_open("A", _ELECTION_Q.format("Bob"), "YES", 10.0, event_slug="evt-a")
    _insert_open("B", _OTHER_Q.format("X"), "NO", 8.0, event_slug="evt-b")

    snap = PG.open_positions()
    assert len(snap) == 2
    assert {p["theme"] for p in snap} == {"elections", "other"}

    clean = {"market_id": "C", "question": _ELECTION_Q.format("Carol"),
             "event_slug": "evt-c"}
    ok, reason, detail = PG.check_correlation(clean, "YES")
    assert ok is True and reason is None, detail
    assert detail["n_same_theme"] == 1   # one elections position open, cap is 4
    assert detail["n_same_event"] == 0


# ── correlation / concentration blocks ────────────────────────────────────────
def test_many_same_theme_blocks_correlation():
    _reset()
    # fill the book with DEFAULT_MAX_SAME_THEME elections positions (distinct events
    # so the EVENT cap can't be what trips it)
    for i in range(PG.DEFAULT_MAX_SAME_THEME):
        _insert_open(f"E{i}", _ELECTION_Q.format(i), "YES", 5.0, event_slug=f"evt-{i}")

    cand = {"market_id": "NEW", "question": _ELECTION_Q.format("new"),
            "event_slug": "evt-fresh"}
    ok, reason, detail = PG.check_correlation(cand, "YES")
    assert ok is False, detail
    assert reason == "correlated_exposure", detail
    assert detail["trigger"] == "theme", detail
    assert detail["n_same_theme"] >= PG.DEFAULT_MAX_SAME_THEME, detail


def test_many_same_event_blocks_correlation():
    _reset()
    # DEFAULT_MAX_SAME_EVENT positions on ONE event, themed 'other' so the theme cap
    # is exempt and ONLY the event cap can trip
    for i in range(PG.DEFAULT_MAX_SAME_EVENT):
        _insert_open(f"V{i}", _OTHER_Q.format(i), "NO", 5.0, event_slug="big-event")

    cand = {"market_id": "NEW", "question": _OTHER_Q.format("z"),
            "event_slug": "big-event"}
    ok, reason, detail = PG.check_correlation(cand, "NO")
    assert ok is False, detail
    assert reason == "correlated_exposure", detail
    assert detail["trigger"] == "event", detail
    assert detail["n_same_event"] >= PG.DEFAULT_MAX_SAME_EVENT, detail


def test_other_theme_is_exempt_from_theme_cap():
    _reset()
    # many 'other' positions with DISTINCT events must NOT trip the theme cap
    for i in range(PG.DEFAULT_MAX_SAME_THEME + 3):
        _insert_open(f"O{i}", _OTHER_Q.format(i), "YES", 3.0, event_slug=f"e-{i}")
    cand = {"market_id": "NEW", "question": _OTHER_Q.format("k"), "event_slug": "e-new"}
    ok, reason, _ = PG.check_correlation(cand, "YES")
    assert ok is True and reason is None


def test_correlation_accepts_explicit_positions_list():
    # the open_positions= override is honored (no DB read needed)
    positions = [{"theme": "elections", "event_slug": f"e{i}"}
                 for i in range(PG.DEFAULT_MAX_SAME_THEME)]
    cand = {"question": _ELECTION_Q.format("x"), "event_slug": "e-new"}
    ok, reason, _ = PG.check_correlation(cand, "YES", open_positions=positions)
    assert ok is False and reason == "correlated_exposure"


# ── bad-theme HARD block ──────────────────────────────────────────────────────
def test_heavily_losing_theme_blocks_bad_theme():
    _reset()
    # BAD_THEME_MIN_N settled election losses of -$5 each -> realized -$100 (>> bar)
    for i in range(PG.BAD_THEME_MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -5.0, market_id=f"L{i}")

    ok, reason, detail = PG.check_bad_theme(_ELECTION_Q.format("new"))
    assert ok is False, detail
    assert reason == "bad_theme", detail
    assert detail["n"] >= PG.BAD_THEME_MIN_N, detail
    assert detail["realized_pnl"] <= -PG.BAD_THEME_LOSS, detail


def test_mildly_losing_theme_does_not_block_bad_theme():
    _reset()
    # enough samples, but only a SMALL loss (-$0.50 each -> -$10) -> NOT a hard block
    # (that's adaptive_min_edge's job to merely raise the edge bar)
    for i in range(PG.BAD_THEME_MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -0.5, market_id=f"S{i}")

    ok, reason, detail = PG.check_bad_theme(_ELECTION_Q.format("new"))
    assert ok is True, detail
    assert reason is None, detail
    assert detail["n"] >= PG.BAD_THEME_MIN_N, detail
    # a different, history-free theme is cold-start ok even with elections losing
    ok2, reason2, _ = PG.check_bad_theme(_OTHER_Q.format("q"))
    assert ok2 is True and reason2 is None


def test_below_min_n_never_hard_blocks_even_on_big_loss():
    _reset()
    # huge per-bet loss but too few settled bets -> below evidence floor -> ok
    for i in range(PG.BAD_THEME_MIN_N - 1):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -9.0, market_id=f"F{i}")
    ok, reason, _ = PG.check_bad_theme(_ELECTION_Q.format("new"))
    assert ok is True and reason is None


def test_tighten_lowers_the_loss_bar_so_drawdown_bites_sooner():
    _reset()
    # a MODERATE loss: -$1.50 each over MIN_N bets -> realized -$30.
    #   tighten=1.0 -> bar = BAD_THEME_LOSS (50) -> -30 > -50 -> NOT blocked
    #   tighten=2.0 -> bar = 25              -> -30 <= -25 -> BLOCKED
    for i in range(PG.BAD_THEME_MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -1.5, market_id=f"M{i}")

    ok_lo, reason_lo, _ = PG.check_bad_theme(_ELECTION_Q.format("new"), tighten=1.0)
    assert ok_lo is True and reason_lo is None

    ok_hi, reason_hi, detail = PG.check_bad_theme(_ELECTION_Q.format("new"), tighten=2.0)
    assert ok_hi is False, detail
    assert reason_hi == "bad_theme", detail
    # tighten can only LOWER the bar (never below... it's a divisor >= 1) -> stricter
    assert detail["loss_bar"] < PG.BAD_THEME_LOSS, detail


# ── drawdown -> stricter_tighten ──────────────────────────────────────────────
def test_drawn_down_book_tightens():
    _reset()
    # equity 900 vs starting 1000 -> drawdown_frac 0.10 -> severity 0.5 -> tighten 1.5
    _set_wallet(cash=900.0, realized_pnl=-100.0)

    ds = PG.drawdown_state()
    assert ds["in_drawdown"] is True, ds
    assert ds["drawdown_frac"] > 0.0, ds
    assert 0.0 < ds["severity"] <= 1.0, ds

    t = PG.stricter_tighten()
    assert t > 1.0, t
    assert t <= PG.MAX_TIGHTEN, t


def test_deep_drawdown_saturates_at_cap():
    _reset()
    # equity 700 vs 1000 -> drawdown_frac 0.30 >= DRAWDOWN_REF (0.20) -> severity 1.0
    _set_wallet(cash=700.0, realized_pnl=-300.0)
    ds = PG.drawdown_state()
    assert ds["severity"] == 1.0, ds
    assert PG.stricter_tighten() == PG.MAX_TIGHTEN


def test_realized_profit_is_not_a_drawdown():
    _reset()
    # winning book: more cash than start, positive realized -> healthy
    _set_wallet(cash=1200.0, realized_pnl=200.0)
    ds = PG.drawdown_state()
    assert ds["in_drawdown"] is False, ds
    assert ds["severity"] == 0.0, ds
    assert PG.stricter_tighten() == 1.0


def test_open_positions_do_not_flap_the_verdict():
    _reset()
    # opening a bet moves cash into open_exposure; marked-at-cost equity is
    # unchanged, realized_pnl still 0 -> NOT a drawdown (no flapping)
    paper.open_position("OP", _ELECTION_Q.format("x"), "YES",
                        model_p=0.60, market_p=0.50, edge=0.10, stake=15.0)
    ds = PG.drawdown_state()
    assert ds["in_drawdown"] is False, ds
    assert ds["severity"] == 0.0, ds
    assert PG.stricter_tighten() == 1.0


# ── best-effort: never raises ─────────────────────────────────────────────────
def test_never_raises_on_missing_tables():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit()
    conn.close()

    # all degrade to SAFE defaults (do-not-block / do-not-tighten), no exception
    ds = PG.drawdown_state()
    assert ds["in_drawdown"] is False and ds["severity"] == 0.0, ds
    assert PG.stricter_tighten() == 1.0
    assert PG.open_positions() == []
    ok_c, reason_c, _ = PG.check_correlation({"question": "anything"}, "YES")
    assert ok_c is True and reason_c is None
    ok_t, reason_t, _ = PG.check_bad_theme("anything")
    assert ok_t is True and reason_t is None


def test_guards_tolerate_garbage_inputs():
    _reset()
    # None / empty / weird shapes must not raise
    assert PG.check_correlation(None, None)[0] is True
    assert PG.check_correlation({}, "YES")[0] is True
    assert PG.check_correlation("not-a-dict", "YES")[0] is True
    assert PG.check_bad_theme(None)[0] is True
    assert PG.check_bad_theme("")[0] is True


TESTS = [
    ("empty_book_is_clean_and_passes_everything", test_empty_book_is_clean_and_passes_everything),
    ("healthy_book_with_a_few_diverse_positions_still_passes", test_healthy_book_with_a_few_diverse_positions_still_passes),
    ("many_same_theme_blocks_correlation", test_many_same_theme_blocks_correlation),
    ("many_same_event_blocks_correlation", test_many_same_event_blocks_correlation),
    ("other_theme_is_exempt_from_theme_cap", test_other_theme_is_exempt_from_theme_cap),
    ("correlation_accepts_explicit_positions_list", test_correlation_accepts_explicit_positions_list),
    ("heavily_losing_theme_blocks_bad_theme", test_heavily_losing_theme_blocks_bad_theme),
    ("mildly_losing_theme_does_not_block_bad_theme", test_mildly_losing_theme_does_not_block_bad_theme),
    ("below_min_n_never_hard_blocks_even_on_big_loss", test_below_min_n_never_hard_blocks_even_on_big_loss),
    ("tighten_lowers_the_loss_bar_so_drawdown_bites_sooner", test_tighten_lowers_the_loss_bar_so_drawdown_bites_sooner),
    ("drawn_down_book_tightens", test_drawn_down_book_tightens),
    ("deep_drawdown_saturates_at_cap", test_deep_drawdown_saturates_at_cap),
    ("realized_profit_is_not_a_drawdown", test_realized_profit_is_not_a_drawdown),
    ("open_positions_do_not_flap_the_verdict", test_open_positions_do_not_flap_the_verdict),
    ("never_raises_on_missing_tables", test_never_raises_on_missing_tables),
    ("guards_tolerate_garbage_inputs", test_guards_tolerate_garbage_inputs),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
