"""Unit tests for harness.predict_today.find_candidates — NO network.

find_candidates calls gamma.fetch_markets_ending_within (network) and
wallet.get_open_positions (DB). Both are monkeypatched to offline stubs, so the
WHOLE filter/sort chain is exercised without a single HTTP request or DB row.
Run:  python -m harness.tests.test_scanner
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_scanner_")

from harness import predict_today as PT  # noqa: E402
from harness import gamma  # noqa: E402
from harness.loop import _days_until  # noqa: E402


def _iso_in(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def mk(market_id, question, price, hours, volume=200000.0, liquidity=40000.0,
       event_slug=None):
    """Build a NORMALIZED market dict in gamma.normalize_market shape."""
    return {
        "market_id": market_id,
        "question": question,
        "description": "",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [price, round(1.0 - price, 4)],
        "volume": volume,
        "liquidity": liquidity,
        "end_date": _iso_in(hours),
        "clob_token_ids": ["t1", "t2"],
        "event_slug": event_slug,
        "raw": {},
    }


def _scan(markets, held=None, *, max_n=3, max_hours=24.0, include_mechanical=False):
    held = held or []
    with patched(PT.gamma, "fetch_markets_ending_within", lambda *a, **k: list(markets)), \
         patched(PT.wallet, "get_open_positions", lambda: list(held)):
        return PT.find_candidates(max_hours=max_hours, max_n=max_n,
                                  include_mechanical=include_mechanical)


def test_opinion_in_window_included():
    res = _scan([mk("OPN-1", "Will the Republicans win control of the Senate?", 0.50, 6)])
    ids = [m["market_id"] for m in res]
    assert ids == ["OPN-1"], ids
    assert res[0]["_label"] == "opinion" and res[0]["_price"] == 0.50


def test_held_excluded():
    res = _scan([mk("HELD-1", "Will the incumbent win re-election?", 0.5, 6)],
                held=[{"market_id": "HELD-1"}])
    assert res == [], res


def test_out_of_window_excluded():
    res = _scan([mk("FAR-1", "Will the governor win re-election?", 0.5, 50)], max_hours=24.0)
    assert res == [], res


def test_untradeable_price_excluded():
    res = _scan([mk("PRICE-1", "Will the senator win the primary?", 0.99, 6)])
    assert res == [], res


def test_illiquid_excluded():
    res = _scan([mk("THIN-1", "Will the candidate win the election?", 0.5, 6,
                    volume=100.0, liquidity=10.0)])
    assert res == [], res


def test_mechanical_dropped_when_not_included():
    markets = [mk("OPN-1", "Will the Republicans win control of the Senate?", 0.5, 6),
               mk("MECH-1", "Will Bitcoin close above $100,000?", 0.5, 6)]
    res = _scan(markets, include_mechanical=False)
    ids = [m["market_id"] for m in res]
    assert "MECH-1" not in ids and "OPN-1" in ids, ids


def test_mechanical_kept_when_no_forecastable():
    # include_mechanical False, but the ONLY candidate is mechanical -> the
    # forecastable filter is empty, so the original list (mechanical) is kept.
    res = _scan([mk("MECH-1", "Will Bitcoin close above $100,000?", 0.5, 6)],
                include_mechanical=False)
    assert [m["market_id"] for m in res] == ["MECH-1"], res


def test_opinion_sorts_before_unknown():
    markets = [mk("UNK-1", "Will the situation be resolved as described?", 0.5, 6,
                  liquidity=99999.0),
               mk("OPN-1", "Will the Democrats win the Senate majority?", 0.5, 6,
                  liquidity=1000.0)]
    res = _scan(markets)
    assert res[0]["market_id"] == "OPN-1", [m["market_id"] for m in res]
    assert res[0]["_label"] == "opinion"


def test_max_n_capped():
    markets = [mk(f"OPN-{i}", "Will the nominee win the primary election?", 0.5, 6)
               for i in range(5)]
    res = _scan(markets, max_n=2)
    assert len(res) == 2, len(res)


def test_pure_helpers():
    m = mk("X", "Q?", 0.42, 6)
    hl = PT._hours_left(m)
    assert hl is not None and 5.0 < hl < 7.0, hl
    assert abs(gamma.yes_price(m) - 0.42) < 1e-9
    assert _days_until(m["end_date"]) is not None
    assert _days_until(None) is None


TESTS = [
    ("opinion_in_window_included", test_opinion_in_window_included),
    ("held_excluded", test_held_excluded),
    ("out_of_window_excluded", test_out_of_window_excluded),
    ("untradeable_price_excluded", test_untradeable_price_excluded),
    ("illiquid_excluded", test_illiquid_excluded),
    ("mechanical_dropped_when_not_included", test_mechanical_dropped_when_not_included),
    ("mechanical_kept_when_no_forecastable", test_mechanical_kept_when_no_forecastable),
    ("opinion_sorts_before_unknown", test_opinion_sorts_before_unknown),
    ("max_n_capped", test_max_n_capped),
    ("pure_helpers", test_pure_helpers),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
