"""P7 (B3) — no-network unit tests for harness.adaptive (theme P&L + adaptive min_edge).

Temp DB only (make_temp_env). No network, no LLM. Verifies the FLOOR-ONLY-UP
contract:
  * cold start (no settled rows) -> adaptive_min_edge() == DEFAULT_MIN_EDGE EXACTLY,
    for both an explicit theme and the overall book
  * a LOSING theme with n >= MIN_N -> strictly GREATER than the floor, and <= the cap
  * a WINNING theme -> EXACTLY the floor (never below)
  * theme_pnl aggregation correct (n / realized_pnl / n_win / win_rate / roi)
  * a custom floor is honored and still floor-only-up
  * nothing ever raises (best-effort)

Run:  python -m harness.tests.test_adaptive
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_adaptive_")

from harness import wallet as paper          # noqa: E402
from harness import sizing                   # noqa: E402
from harness import adaptive as AD           # noqa: E402

FLOOR = sizing.DEFAULT_MIN_EDGE              # 0.02 today


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Fresh wallet + paper_positions so each test is order-independent."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    paper.init_wallet(1000.0)


def _insert_settled(question, side, stake, realized_pnl, market_id="M"):
    """Insert ONE settled paper position directly (read-only module under test)."""
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


# a question that scoreboard.theme_of tags "elections"
_ELECTION_Q = "Will candidate {} win the 2028 presidential election?"
# a question that tags "geopolitics"
_GEO_Q = "Will there be a ceasefire in the war in {}?"


# ── tests ─────────────────────────────────────────────────────────────────────
def test_cold_start_returns_floor_exactly():
    _reset()
    # no settled positions at all
    assert AD.theme_pnl() == {}
    assert AD.adaptive_min_edge() == FLOOR                  # overall
    assert AD.adaptive_min_edge(theme="elections") == FLOOR  # specific theme
    assert AD.adaptive_min_edge(theme="never-seen") == FLOOR


def test_losing_theme_above_min_n_tightens():
    _reset()
    # MIN_N losing election bets (each loses $5) -> theme is losing after costs
    for i in range(AD.MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -5.0, market_id=f"L{i}")

    me = AD.adaptive_min_edge(theme="elections")
    assert me > FLOOR, me                       # strictly MORE selective
    assert me <= AD.MAX_MIN_EDGE, me            # but never absurd
    assert me >= FLOOR                          # never below the floor
    # overall book is also losing -> overall also tightens
    assert AD.adaptive_min_edge() > FLOOR


def test_winning_theme_stays_at_floor():
    _reset()
    for i in range(AD.MIN_N + 5):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, +4.0, market_id=f"W{i}")
    # winning theme: NEVER loosened below floor, and not raised
    assert AD.adaptive_min_edge(theme="elections") == FLOOR
    assert AD.adaptive_min_edge() == FLOOR


def test_below_min_n_stays_at_floor_even_when_losing():
    _reset()
    # losing, but too few samples to act on -> stay at floor (don't over-react)
    for i in range(AD.MIN_N - 1):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -5.0, market_id=f"S{i}")
    assert AD.adaptive_min_edge(theme="elections") == FLOOR
    assert AD.adaptive_min_edge() == FLOOR


def test_theme_pnl_aggregation_correct():
    _reset()
    # elections: 3 bets, two win (+4 each), one loses (-6) -> realized +2, win_rate 2/3
    _insert_settled(_ELECTION_Q.format(0), "YES", 10.0, +4.0, market_id="E0")
    _insert_settled(_ELECTION_Q.format(1), "YES", 10.0, +4.0, market_id="E1")
    _insert_settled(_ELECTION_Q.format(2), "NO", 10.0, -6.0, market_id="E2")
    # geopolitics: 1 losing bet
    _insert_settled(_GEO_Q.format("Xland"), "YES", 20.0, -8.0, market_id="G0")

    pnl = AD.theme_pnl()
    assert set(pnl) == {"elections", "geopolitics"}, list(pnl)

    el = pnl["elections"]
    assert el["n"] == 3, el
    assert abs(el["realized_pnl"] - 2.0) < 1e-9, el
    assert el["n_win"] == 2, el
    assert abs(el["win_rate"] - (2.0 / 3.0)) < 1e-9, el
    # roi = realized / total_staked = 2 / 30
    assert abs(el["roi"] - (2.0 / 30.0)) < 1e-9, el

    geo = pnl["geopolitics"]
    assert geo["n"] == 1, geo
    assert abs(geo["realized_pnl"] - (-8.0)) < 1e-9, geo
    assert geo["n_win"] == 0, geo
    assert geo["win_rate"] == 0.0, geo
    assert abs(geo["roi"] - (-8.0 / 20.0)) < 1e-9, geo


def test_custom_floor_is_honored_and_floor_only_up():
    _reset()
    # custom floor above the default; losing theme must raise it but never below it
    for i in range(AD.MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, -5.0, market_id=f"C{i}")
    custom = 0.05
    me = AD.adaptive_min_edge(theme="elections", floor=custom)
    assert me >= custom, me
    assert me <= AD.MAX_MIN_EDGE, me
    # a custom floor at/above the cap must STILL never drop below the floor
    high = AD.adaptive_min_edge(theme="elections", floor=0.20)
    assert high >= 0.20, high
    # winning theme at a custom floor returns exactly that floor
    _reset()
    for i in range(AD.MIN_N + 1):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, +4.0, market_id=f"K{i}")
    assert AD.adaptive_min_edge(theme="elections", floor=custom) == custom


def test_break_even_theme_stays_at_floor():
    _reset()
    # exactly break-even (realized 0) is NOT "losing" -> stay at floor
    for i in range(AD.MIN_N):
        _insert_settled(_ELECTION_Q.format(i), "YES", 10.0, 0.0, market_id=f"B{i}")
    assert AD.adaptive_min_edge(theme="elections") == FLOOR


# a question that classifies MECHANICAL (a price threshold) — the favorite-longshot
# price strategy's population, NOT the AI swarm's. theme_of tags it "crypto".
_MECH_Q = "Will Bitcoin close above $100,000 on day {}?"


def test_losing_mechanical_theme_does_not_tighten_ai_knob():
    # The conflation guard (P7 fix): adaptive_min_edge is OPINION-scoped, so a
    # losing MECHANICAL theme (the price strategy / cash-outs) must NOT raise the
    # AI swarm bettor's edge demand. Without the fix these 'other'/'crypto' losses
    # would tighten the AI knob for the wrong reason.
    _reset()
    assert AD._is_opinion(_MECH_Q.format(0)) is False        # sanity: mechanical
    for i in range(AD.MIN_N + 5):
        _insert_settled(_MECH_Q.format(i), "YES", 10.0, -5.0, market_id=f"MECH{i}")
    # theme_pnl() (whole book, for the dashboard) DOES see the losses…
    assert AD.theme_pnl(), "whole-book report should include the mechanical bets"
    # …but the AI's adaptive knob (opinion-scoped) stays at the floor EXACTLY.
    assert AD.adaptive_min_edge() == FLOOR
    assert AD.adaptive_min_edge(theme="crypto") == FLOOR


def test_never_raises_on_missing_table():
    # Drop everything; module must degrade to safe defaults, never raise.
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit()
    conn.close()
    assert AD.theme_pnl() == {}
    assert AD.adaptive_min_edge() == FLOOR
    assert AD.adaptive_min_edge(theme="elections") == FLOOR


TESTS = [
    ("cold_start_returns_floor_exactly", test_cold_start_returns_floor_exactly),
    ("losing_theme_above_min_n_tightens", test_losing_theme_above_min_n_tightens),
    ("winning_theme_stays_at_floor", test_winning_theme_stays_at_floor),
    ("below_min_n_stays_at_floor_even_when_losing", test_below_min_n_stays_at_floor_even_when_losing),
    ("theme_pnl_aggregation_correct", test_theme_pnl_aggregation_correct),
    ("custom_floor_is_honored_and_floor_only_up", test_custom_floor_is_honored_and_floor_only_up),
    ("break_even_theme_stays_at_floor", test_break_even_theme_stays_at_floor),
    ("losing_mechanical_theme_does_not_tighten_ai_knob", test_losing_mechanical_theme_does_not_tighten_ai_knob),
    ("never_raises_on_missing_table", test_never_raises_on_missing_table),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
