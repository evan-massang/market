"""P2 scanner v2 — no-network behaviour tests for harness.scanner AND its wiring into
harness.predict_today.find_candidates.

scanner.scan() is the only network-touching path; it routes through the injectable
wrappers scanner._gamma_within / scanner._gamma_active (and, for the find_candidates
wiring, gamma.fetch_markets_ending_within), which we monkeypatch to offline stubs.
Every other scanner function (group_events / theme_of / exit_risk / is_stale /
rank_candidates) is pure. NOT ONE HTTP request is made; NO live DB is touched
(make_temp_env redirects DATABASE_URL + OBS_LOGS_DIR at a throwaway temp dir).

This file is the explicit P2 acceptance suite: window filtering, event grouping,
exit-risk monotonicity, staleness, theme mapping, transparent ranking, and the new
window-selector wiring in predict_today. Auto-discovered by run_tests.py (which globs
harness/tests/test_*.py), so creating the file registers it.

Run:  python -m harness.tests.test_scanner_v2
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_scanner_v2_")

from harness import scanner as S          # noqa: E402
from harness import gamma                 # noqa: E402
from harness import predict_today as PT   # noqa: E402


def _iso_in(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def mk(market_id, question, price, hours, *, volume=200_000.0, liquidity=40_000.0,
       event_slug=None, outcomes=None, prices=None, raw=None, end_date="auto"):
    """Build a NORMALIZED market dict in gamma.normalize_market shape (no network)."""
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


# ── 1) WINDOW FILTERING ─────────────────────────────────────────────────────────
def test_window_30h_is_near_term_not_same_day():
    """A 30h market belongs to NEAR_TERM (24–72h), NOT SAME_DAY (0.5–24h)."""
    uni = [mk("M30", "Will the nominee win the primary?", 0.5, 30)]
    with patched(S, "_gamma_within", lambda hours, limit: list(uni)):
        assert {m["market_id"] for m in S.scan(S.NEAR_TERM, limit=50)} == {"M30"}
        assert S.scan(S.SAME_DAY, limit=50) == []
        assert S.scan(S.WEEKLY, limit=50) == []


def test_window_5d_is_weekly():
    """A 5-day (120h) market belongs to WEEKLY (72–168h) and to neither shorter band."""
    uni = [mk("M5D", "Will the candidate win the race?", 0.5, 120)]
    with patched(S, "_gamma_within", lambda hours, limit: list(uni)):
        assert {m["market_id"] for m in S.scan(S.WEEKLY, limit=50)} == {"M5D"}
        assert S.scan(S.NEAR_TERM, limit=50) == []
        assert S.scan(S.SAME_DAY, limit=50) == []


def test_window_each_market_lands_in_exactly_one_band():
    """Across a mixed universe, each in-range market lands in exactly one of the 3 bands."""
    uni = [mk("SD", "Will the Senate flip?", 0.5, 6),       # same_day
           mk("NT", "Will the nominee win?", 0.5, 30),       # near_term
           mk("WK", "Will the candidate win?", 0.5, 120),    # weekly
           mk("FAR", "way out", 0.5, 400)]                   # beyond all windows
    with patched(S, "_gamma_within", lambda hours, limit: list(uni)):
        sd = {m["market_id"] for m in S.scan(S.SAME_DAY, limit=50)}
        nt = {m["market_id"] for m in S.scan(S.NEAR_TERM, limit=50)}
        wk = {m["market_id"] for m in S.scan(S.WEEKLY, limit=50)}
    assert sd == {"SD"} and nt == {"NT"} and wk == {"WK"}
    assert "FAR" not in (sd | nt | wk)
    assert sd.isdisjoint(nt) and nt.isdisjoint(wk) and sd.isdisjoint(wk)


# ── 2) EVENT GROUPING ───────────────────────────────────────────────────────────
def test_event_grouping_three_sibling_legs_one_event():
    """3 sibling legs sharing an event_slug collapse into ONE Event with n_legs=3."""
    legs = [mk(f"L-{i}", f"Will {name} win the 2028 nomination contest?", 0.3, 48,
               event_slug="nom-2028")
            for i, name in enumerate(("Alice", "Bob", "Carol"))]
    evs = S.group_events(legs)
    assert len(evs) == 1, [e["key"] for e in evs]
    ev = evs[0]
    assert ev["key"] == "nom-2028"
    assert ev["n_legs"] == 3
    assert [l["market_id"] for l in ev["legs"]] == ["L-0", "L-1", "L-2"]
    assert ev["mutually_exclusive"] is True   # one winner among the legs


# ── 3) EXIT-RISK MONOTONIC IN LIQUIDITY ─────────────────────────────────────────
def test_exit_risk_monotonic_decreasing_in_liquidity():
    """With volume held fixed, exit_risk is strictly DECREASING as liquidity rises
    (deeper book = easier to unwind a paper position)."""
    liqs = [50.0, 5_000.0, 50_000.0, 500_000.0]
    risks = [S.exit_risk(mk(f"L{int(l)}", "q?", 0.5, 12, volume=100_000.0, liquidity=l))
             for l in liqs]
    assert all(0.0 <= r <= 1.0 for r in risks), risks
    assert all(risks[i] > risks[i + 1] for i in range(len(risks) - 1)), risks


# ── 4) STALENESS ────────────────────────────────────────────────────────────────
def test_is_stale_zero_liquidity():
    stale, reason = S.is_stale(mk("Z", "q?", 0.5, 12, liquidity=0.0))
    assert stale is True
    assert "liquidity" in reason.lower(), reason


def test_is_stale_degenerate_price_no_freshness():
    """Price pinned at 0/1 with no volume / trade signal => never-traded / settled => stale."""
    stale, _ = S.is_stale(mk("P", "q?", 0.0, 12, volume=0.0, prices=[0.0, 1.0]))
    assert stale is True


def test_is_stale_healthy_market_is_not_stale():
    assert S.is_stale(mk("H", "Will the candidate win?", 0.5, 12))[0] is False


# ── 5) THEME MAPPING ────────────────────────────────────────────────────────────
def test_theme_of_maps_samples():
    assert S.theme_of("Will the Senate flip in the 2026 election?") == "elections"
    assert S.theme_of("Will Trump's approval rating exceed 50%?") == "approval"
    assert S.theme_of("Will Russia and Ukraine sign a ceasefire?") == "geopolitics"
    assert S.theme_of("Will the movie win Best Picture at the Oscars?") == "culture"
    assert S.theme_of("Totally unrelated neutral question?") == "other"


# ── 6) TRANSPARENT RANK ─────────────────────────────────────────────────────────
def test_rank_liquid_high_disagreement_opinion_over_illiquid_mechanical():
    """A liquid, high-disagreement OPINION market ranks ABOVE an illiquid MECHANICAL one."""
    cands = [
        mk("OPN", "Will the Democrats win the Senate majority?", 0.80, 12,   # opinion, deep, extreme price
           volume=800_000.0, liquidity=200_000.0),
        mk("MECH", "Will Bitcoin close above $100,000?", 0.50, 12,           # mechanical, illiquid, no edge
           volume=2_000.0, liquidity=500.0),
    ]
    ranked = S.rank_candidates(cands)
    assert ranked[0]["market_id"] == "OPN", [c["market_id"] for c in ranked]
    opn = next(c for c in ranked if c["market_id"] == "OPN")
    mech = next(c for c in ranked if c["market_id"] == "MECH")
    assert opn["_label"] == "opinion" and mech["_label"] == "mechanical"
    assert opn["_rank_score"] > mech["_rank_score"], (opn["_rank_score"], mech["_rank_score"])
    # transparency: a 'why ranked' string + per-component sub-scores travel with the candidate
    assert isinstance(opn["_why"], str) and opn["_why"]
    assert set(opn["_subscores"]) == set(S.W)
    # OPN's disagreement proxy (extreme price 0.80) clearly beats MECH's (coin-flip 0.50)
    assert opn["_subscores"]["disagreement"] > mech["_subscores"]["disagreement"]


# ── 7) WIRING: predict_today.find_candidates window selector (no network) ────────
def _wire(markets, *, window, held=None, max_n=5, max_hours=24.0, include_mechanical=False):
    """Run find_candidates fully offline: gamma fetch + wallet positions are stubbed.
    scanner.scan() -> scanner._gamma_within() -> gamma.fetch_markets_ending_within(),
    so patching the gamma fetch reaches the scanner path too."""
    held = held or []
    with patched(gamma, "fetch_markets_ending_within", lambda *a, **k: list(markets)), \
         patched(PT.wallet, "get_open_positions", lambda: list(held)):
        return PT.find_candidates(max_hours=max_hours, max_n=max_n,
                                  include_mechanical=include_mechanical, window=window)


def test_wiring_same_day_default_excludes_30h_market():
    res = _wire([mk("M30", "Will the nominee win the primary?", 0.5, 30)], window="same_day")
    assert res == [], [m["market_id"] for m in res]


def test_wiring_near_term_includes_30h_and_annotates():
    res = _wire([mk("M30", "Will the nominee win the primary?", 0.5, 30)], window="near_term")
    assert [m["market_id"] for m in res] == ["M30"], [m["market_id"] for m in res]
    c = res[0]
    # legacy keys predict_one consumes are present and correct …
    assert c["_label"] == "opinion"
    assert c["_hl"] is not None and 29.0 < c["_hl"] < 31.0, c["_hl"]
    assert abs(c["_price"] - 0.5) < 1e-9
    # … plus scanner's transparent rank annotations rode along.
    assert "_rank_score" in c and "_why" in c and "_exit_risk" in c


def test_wiring_same_day_preserves_opinion_first_and_drops_stale():
    """Same-day contract preserved: opinion sorts first even when illiquid vs an unknown;
    a zero-liquidity (stale/illiquid) row is dropped entirely."""
    markets = [
        mk("UNK", "Will the situation be resolved as described?", 0.5, 6, liquidity=150_000.0),
        mk("OPN", "Will the Democrats win the Senate majority?", 0.5, 6, liquidity=2_000.0),
        mk("DEAD", "Will the governor win re-election?", 0.5, 6, volume=300.0, liquidity=0.0),
    ]
    res = _wire(markets, window="same_day")
    ids = [m["market_id"] for m in res]
    assert "DEAD" not in ids, ids                 # zero-liquidity stale row dropped
    assert ids[0] == "OPN", ids                   # opinion strictly first (despite lower liquidity)
    assert res[0]["_label"] == "opinion"


def test_wiring_held_excluded_via_scanner_path():
    res = _wire([mk("H1", "Will the incumbent win re-election?", 0.5, 6)],
                window="same_day", held=[{"market_id": "H1"}])
    assert res == [], [m["market_id"] for m in res]


def test_wiring_unknown_window_falls_back_to_same_day():
    # 30h market: a bogus window resolves to same_day, which excludes it.
    assert _wire([mk("M30", "Will the nominee win the primary?", 0.5, 30)], window="bogus") == []
    # explicit None also resolves to same_day (env unset in the test harness).
    assert _wire([mk("M30", "Will the nominee win the primary?", 0.5, 30)], window=None) == []


TESTS = [
    ("window_30h_is_near_term_not_same_day", test_window_30h_is_near_term_not_same_day),
    ("window_5d_is_weekly", test_window_5d_is_weekly),
    ("window_each_market_lands_in_exactly_one_band", test_window_each_market_lands_in_exactly_one_band),
    ("event_grouping_three_sibling_legs_one_event", test_event_grouping_three_sibling_legs_one_event),
    ("exit_risk_monotonic_decreasing_in_liquidity", test_exit_risk_monotonic_decreasing_in_liquidity),
    ("is_stale_zero_liquidity", test_is_stale_zero_liquidity),
    ("is_stale_degenerate_price_no_freshness", test_is_stale_degenerate_price_no_freshness),
    ("is_stale_healthy_market_is_not_stale", test_is_stale_healthy_market_is_not_stale),
    ("theme_of_maps_samples", test_theme_of_maps_samples),
    ("rank_liquid_high_disagreement_opinion_over_illiquid_mechanical",
     test_rank_liquid_high_disagreement_opinion_over_illiquid_mechanical),
    ("wiring_same_day_default_excludes_30h_market", test_wiring_same_day_default_excludes_30h_market),
    ("wiring_near_term_includes_30h_and_annotates", test_wiring_near_term_includes_30h_and_annotates),
    ("wiring_same_day_preserves_opinion_first_and_drops_stale",
     test_wiring_same_day_preserves_opinion_first_and_drops_stale),
    ("wiring_held_excluded_via_scanner_path", test_wiring_held_excluded_via_scanner_path),
    ("wiring_unknown_window_falls_back_to_same_day", test_wiring_unknown_window_falls_back_to_same_day),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
