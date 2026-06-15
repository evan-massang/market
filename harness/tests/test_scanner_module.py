"""Unit tests for harness.scanner — NO network.

scanner.scan() is the only network-touching path; it goes through the injectable
wrappers scanner._gamma_within / scanner._gamma_active, which we monkeypatch to
offline stubs. Every other function (group_events / theme_of / exit_risk /
is_stale / rank_candidates) is pure. NOT ONE HTTP request is made.

(The sibling test_scanner.py covers predict_today.find_candidates — different
module; this file covers the new scanner.py.)

Run:  python -m harness.tests.test_scanner_module
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_scanner_mod_")

from harness import scanner as S  # noqa: E402


def _iso_in(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def mk(market_id, question, price, hours, *, volume=200_000.0, liquidity=40_000.0,
       event_slug=None, outcomes=None, prices=None, raw=None, end_date="auto"):
    outs = outcomes or ["Yes", "No"]
    prc = prices if prices is not None else [price, round(1.0 - price, 4)]
    return {
        "market_id": market_id,
        "question": question,
        "description": "",
        "outcomes": outs,
        "outcome_prices": prc,
        "volume": volume,
        "liquidity": liquidity,
        "end_date": _iso_in(hours) if end_date == "auto" else end_date,
        "clob_token_ids": ["t1", "t2"],
        "event_slug": event_slug,
        "raw": raw or {},
    }


# ── scan: window filtering (gamma wrapper stubbed -> NO HTTP) ──────────────────
def _universe():
    return [
        mk("SD-1", "Will the Senate flip?", 0.5, 6),
        mk("SD-2", "Will the incumbent win re-election?", 0.4, 20),
        mk("NT-1", "Will the nominee win the primary?", 0.55, 48),
        mk("WK-1", "Will the candidate win the race?", 0.45, 120),
        mk("FAR-1", "way out", 0.5, 400),
        mk("BAD-1", "no end", 0.5, 6, end_date=None),
    ]


def test_scan_same_day():
    with patched(S, "_gamma_within", lambda hours, limit: _universe()):
        ids = {m["market_id"] for m in S.scan(S.SAME_DAY, limit=50)}
    assert ids == {"SD-1", "SD-2"}, ids


def test_scan_near_term():
    with patched(S, "_gamma_within", lambda hours, limit: _universe()):
        ids = {m["market_id"] for m in S.scan(S.NEAR_TERM, limit=50)}
    assert ids == {"NT-1"}, ids


def test_scan_weekly():
    with patched(S, "_gamma_within", lambda hours, limit: _universe()):
        ids = {m["market_id"] for m in S.scan(S.WEEKLY, limit=50)}
    assert ids == {"WK-1"}, ids


def test_scan_all_excludes_far_and_unparseable():
    with patched(S, "_gamma_within", lambda hours, limit: _universe()):
        ids = {m["market_id"] for m in S.scan("all", limit=50)}
    assert ids == {"SD-1", "SD-2", "NT-1", "WK-1"}, ids


def test_scan_limit_and_annotations():
    with patched(S, "_gamma_within", lambda hours, limit: _universe()):
        res = S.scan("all", limit=2)
        assert len(res) == 2, len(res)
        full = S.scan(S.SAME_DAY, limit=50)
    assert all("_window" in m and "_hours_left" in m for m in full)
    assert all(m["_window"] == "same_day" for m in full)


def test_scan_active_source():
    with patched(S, "_gamma_active", lambda limit: _universe()):
        ids = {m["market_id"] for m in S.scan(S.SAME_DAY, limit=50, source="active")}
    assert ids == {"SD-1", "SD-2"}, ids


def test_scan_unknown_mode_raises():
    try:
        S.scan("yearly")
    except ValueError:
        return
    raise AssertionError("expected ValueError on unknown mode")


# ── group_events ──────────────────────────────────────────────────────────────
def test_group_family_mutually_exclusive():
    legs = [mk(f"F-{i}", f"Will {name} win the 2028 nomination contest?", 0.3, 48,
               event_slug="nom-2028")
            for i, name in enumerate(("Alice", "Bob", "Carol"))]
    evs = {e["key"]: e for e in S.group_events(legs)}
    assert "nom-2028" in evs
    assert evs["nom-2028"]["n_legs"] == 3
    assert evs["nom-2028"]["mutually_exclusive"] is True


def test_group_singleton_not_me():
    evs = S.group_events([mk("S-1", "Totally standalone question?", 0.5, 48)])
    assert len(evs) == 1
    assert evs[0]["n_legs"] == 1 and evs[0]["mutually_exclusive"] is False


def test_group_categorical_single_market_me():
    cat = [mk("C-1", "Who wins?", 0.3, 48, outcomes=["A", "B", "C"], prices=[0.3, 0.3, 0.4])]
    evs = S.group_events(cat)
    assert evs[0]["mutually_exclusive"] is True


def test_group_preserves_order_and_theme():
    ms = [mk("A-1", "Will the Senate flip?", 0.5, 6),
          mk("B-1", "Will Bitcoin close above $100k?", 0.5, 6)]
    evs = S.group_events(ms)
    assert [e["key"] for e in evs] == ["A-1", "B-1"]
    assert evs[0]["theme"] == "elections"


# ── theme_of ──────────────────────────────────────────────────────────────────
def test_theme_of():
    assert S.theme_of("Will the Senate flip?") == "elections"
    assert S.theme_of("totally unrelated") == "other"


# ── exit_risk ─────────────────────────────────────────────────────────────────
def test_exit_risk_depth_proxy():
    deep = mk("D", "q?", 0.5, 12, volume=2_000_000.0, liquidity=500_000.0)
    thin = mk("T", "q?", 0.5, 12, volume=100.0, liquidity=50.0)
    assert 0.0 <= S.exit_risk(deep) <= 1.0
    assert S.exit_risk(deep) < S.exit_risk(thin)
    assert S.exit_risk(thin) > 0.8


def test_exit_risk_spread_signal():
    wide = mk("W", "q?", 0.5, 12, raw={"spread": 0.07})
    tight = mk("W2", "q?", 0.5, 12, raw={"spread": 0.0})
    assert S.exit_risk(wide) > S.exit_risk(tight)


def test_exit_risk_from_bid_ask():
    book = mk("BK", "q?", 0.5, 12, raw={"bestBid": 0.40, "bestAsk": 0.46})
    assert S._spread(book) is not None and abs(S._spread(book) - 0.06) < 1e-9


# ── is_stale ──────────────────────────────────────────────────────────────────
def test_is_stale_cases():
    assert S.is_stale(mk("H", "Will the candidate win?", 0.5, 12))[0] is False
    assert S.is_stale(mk("Z", "q?", 0.5, 12, liquidity=0.0))[0] is True
    assert S.is_stale(mk("E", "q?", 0.5, -3))[0] is True
    assert S.is_stale(mk("B", "q?", 0.5, 6, end_date=None))[0] is True
    assert S.is_stale(mk("P", "q?", 0.0, 12, volume=0.0, prices=[0.0, 1.0]))[0] is True


# ── rank_candidates ───────────────────────────────────────────────────────────
def _rank_set():
    return [
        mk("OPN", "Will the Republicans win control of the Senate?", 0.5, 12,
           volume=500_000.0, liquidity=120_000.0),
        mk("MECH", "Will Bitcoin close above $100,000?", 0.5, 12,
           volume=500_000.0, liquidity=120_000.0),
        mk("STALE", "Will the governor win re-election?", 0.5, 12,
           volume=300.0, liquidity=0.0),
    ]


def test_rank_order_and_observe_only():
    ranked = S.rank_candidates(_rank_set())
    order = [c["market_id"] for c in ranked]
    assert order[0] == "OPN", order
    assert order[-1] == "STALE", order
    stale = next(c for c in ranked if c["market_id"] == "STALE")
    assert stale["_observe_only"] is True
    opn = next(c for c in ranked if c["market_id"] == "OPN")
    mech = next(c for c in ranked if c["market_id"] == "MECH")
    assert opn["_rank_score"] > mech["_rank_score"]


def test_rank_annotations_and_bounds():
    ranked = S.rank_candidates(_rank_set())
    for c in ranked:
        for k in ("_rank_score", "_subscores", "_why", "_theme", "_label",
                  "_exit_risk", "_stale", "_observe_only", "_hours_left"):
            assert k in c, (c["market_id"], k)
        assert 0.0 <= c["_rank_score"] <= 1.0
        assert set(c["_subscores"]) == set(S.W)


def test_rank_inputs_not_mutated():
    cands = _rank_set()
    S.rank_candidates(cands)
    assert all("_rank_score" not in c for c in cands)


def test_rank_theme_pnl_moves_score():
    cands = _rank_set()
    base = next(c for c in S.rank_candidates(cands) if c["market_id"] == "OPN")["_rank_score"]
    up = next(c for c in S.rank_candidates(cands, theme_pnl={"elections": 1.0})
              if c["market_id"] == "OPN")["_rank_score"]
    down = next(c for c in S.rank_candidates(cands, theme_pnl={"elections": 0.0})
                if c["market_id"] == "OPN")["_rank_score"]
    assert up > base > down, (up, base, down)


def test_rank_empty():
    assert S.rank_candidates([]) == []
    assert S.rank_candidates(None) == []


TESTS = [
    ("scan_same_day", test_scan_same_day),
    ("scan_near_term", test_scan_near_term),
    ("scan_weekly", test_scan_weekly),
    ("scan_all_excludes_far_and_unparseable", test_scan_all_excludes_far_and_unparseable),
    ("scan_limit_and_annotations", test_scan_limit_and_annotations),
    ("scan_active_source", test_scan_active_source),
    ("scan_unknown_mode_raises", test_scan_unknown_mode_raises),
    ("group_family_mutually_exclusive", test_group_family_mutually_exclusive),
    ("group_singleton_not_me", test_group_singleton_not_me),
    ("group_categorical_single_market_me", test_group_categorical_single_market_me),
    ("group_preserves_order_and_theme", test_group_preserves_order_and_theme),
    ("theme_of", test_theme_of),
    ("exit_risk_depth_proxy", test_exit_risk_depth_proxy),
    ("exit_risk_spread_signal", test_exit_risk_spread_signal),
    ("exit_risk_from_bid_ask", test_exit_risk_from_bid_ask),
    ("is_stale_cases", test_is_stale_cases),
    ("rank_order_and_observe_only", test_rank_order_and_observe_only),
    ("rank_annotations_and_bounds", test_rank_annotations_and_bounds),
    ("rank_inputs_not_mutated", test_rank_inputs_not_mutated),
    ("rank_theme_pnl_moves_score", test_rank_theme_pnl_moves_score),
    ("rank_empty", test_rank_empty),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
