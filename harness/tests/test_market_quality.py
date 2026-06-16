"""No-network unit tests for harness.market_quality (P8 build B1).

Pure checks over synthetic normalized market dicts — ZERO HTTP, ZERO LLM, no
prod DB (make_temp_env redirects DATABASE_URL/OBS_LOGS_DIR at import, since
importing market_quality -> scanner -> scoreboard binds a sqlite path).

Run:  python -m harness.tests.test_market_quality
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from harness.tests._util import make_temp_env, patched, run_as_main

make_temp_env("ps_mktqual_")

from harness import market_quality as MQ  # noqa: E402
from harness import classifier, scanner    # noqa: E402
from harness import safety_gate as SG      # noqa: E402


def _iso_in(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def mk(*, price=0.50, hours=6.0, volume=200_000.0, liquidity=40_000.0, raw=None):
    """A NORMALIZED market dict in gamma.normalize_market shape."""
    return {
        "market_id": "M",
        "question": "Will the Republicans win control of the Senate?",
        "description": "",
        "outcomes": ["Yes", "No"],
        "outcome_prices": [price, round(1.0 - price, 4)],
        "volume": volume,
        "liquidity": liquidity,
        "end_date": _iso_in(hours),
        "event_slug": None,
        "raw": raw if raw is not None else {},
    }


# ── 1) NO OVER-BLOCK: a clean liquid near-50 market passes ALL checks ─────────
def test_clean_market_passes_all_at_baseline():
    m = mk()  # vol 200k, liq 40k, price 0.50, no quoted spread, ends in 6h
    v = MQ.evaluate_market_quality(m, tighten=1.0)
    assert v["allow"] is True, v
    assert v["reasons"] == [], v["reasons"]
    # every individual check ok
    assert all(c["ok"] for c in v["checks"]), v["checks"]
    # and the three checks ARE present and named
    assert {c["name"] for c in v["checks"]} == {"stale_price", "liquidity", "spread"}
    # individual functions agree
    assert MQ.check_stale_price(m)[0] is True
    assert MQ.check_liquidity(m)[0] is True
    assert MQ.check_spread(m)[0] is True


# ── 2) STALE: a passed-end-date book blocks 'stale_price' (isolated) ──────────
def test_stale_end_date_blocks():
    m = mk(hours=-5.0)  # end_date already passed; liq/vol still healthy
    ok, reason, detail = MQ.check_stale_price(m)
    assert ok is False and reason == "stale_price", (ok, reason, detail)
    v = MQ.evaluate_market_quality(m)
    assert v["allow"] is False
    assert "stale_price" in v["reasons"], v["reasons"]


def test_stale_zero_liquidity_blocks():
    m = mk(liquidity=0.0)  # nothing to exit into
    ok, reason, _ = MQ.check_stale_price(m)
    assert ok is False and reason == "stale_price"
    v = MQ.evaluate_market_quality(m)
    assert "stale_price" in v["reasons"], v["reasons"]


# ── 3) LOW LIQUIDITY: below the find-time floor blocks 'low_liquidity' ────────
def test_thin_liquidity_blocks():
    # volume BELOW the 5k floor but liquidity high, so exit_risk stays low and
    # staleness does not fire -> isolates the low_liquidity reason.
    m = mk(volume=1_000.0, liquidity=40_000.0)
    ok, reason, detail = MQ.check_liquidity(m)
    assert ok is False and reason == "low_liquidity", (ok, reason, detail)
    v = MQ.evaluate_market_quality(m)
    assert v["allow"] is False
    assert "low_liquidity" in v["reasons"], v["reasons"]


def test_at_floor_passes_liquidity():
    # exactly at the find-time floor still PASSES the liquidity check (no overblock)
    m = mk(volume=classifier.DEFAULT_MIN_VOLUME, liquidity=classifier.DEFAULT_MIN_LIQUIDITY)
    assert MQ.check_liquidity(m)[0] is True


# ── 4) HIGH SPREAD: wide quoted spread AND/OR high exit risk block ────────────
def test_wide_quoted_spread_blocks():
    # a real 10-cent quoted spread (> DEFAULT_MAX_SPREAD 0.05); liq/vol healthy
    m = mk(raw={"spread": 0.10})
    ok, reason, detail = MQ.check_spread(m)
    assert ok is False and reason == "high_spread", (ok, reason, detail)
    v = MQ.evaluate_market_quality(m)
    assert v["allow"] is False
    assert "high_spread" in v["reasons"], v["reasons"]


def test_high_exit_risk_blocks_via_depth_proxy():
    # no quoted spread, but a shallow book (liq just over floor) => exit_risk high.
    m = mk(volume=6_000.0, liquidity=1_500.0)  # passes the liquidity floor
    assert MQ.check_liquidity(m)[0] is True          # not a liquidity-floor block
    ok, reason, detail = MQ.check_spread(m)
    assert ok is False and reason == "high_spread", (ok, reason, detail)
    assert scanner.exit_risk(m) > MQ.DEFAULT_MAX_EXIT_RISK


# ── 5) DRAWDOWN TIGHTENING: tighten=2.0 blocks a borderline tighten=1.0 pass ──
def test_tighten_blocks_borderline_market():
    # borderline depth: exit_risk ~= 0.447 -> under 0.60 (pass at t=1) but over
    # 0.30 (the t=2 cap) -> blocked at t=2. liquidity stays above the scaled floor
    # at t=2 (min_liq 2k <= 25k, min_vol 10k <= 120k), so it blocks ONLY on spread.
    m = mk(volume=120_000.0, liquidity=25_000.0)

    base = MQ.evaluate_market_quality(m, tighten=1.0)
    assert base["allow"] is True, base

    tight = MQ.evaluate_market_quality(m, tighten=2.0)
    assert tight["allow"] is False, tight
    assert "high_spread" in tight["reasons"], tight["reasons"]
    # thresholds really did harden
    assert tight["thresholds"]["max_exit_risk"] < base["thresholds"]["max_exit_risk"]
    assert tight["thresholds"]["min_liquidity"] > base["thresholds"]["min_liquidity"]


def test_tighten_below_one_clamped_to_baseline():
    # a multiplier < 1.0 must NOT loosen below baseline (BETTER not MORE).
    m = mk()
    v = MQ.evaluate_market_quality(m, tighten=0.5)
    assert v["tighten"] == 1.0, v["tighten"]
    assert v["thresholds"]["max_exit_risk"] == MQ.DEFAULT_MAX_EXIT_RISK


# ── 6) DEFENSIVE: never raises on missing / malformed fields ──────────────────
def test_never_raises_on_missing_fields():
    bad_inputs = [
        {},
        {"liquidity": None, "volume": None, "outcome_prices": None,
         "raw": None, "end_date": None, "outcomes": None},
        {"raw": {"spread": "n/a"}, "liquidity": "x", "volume": "y"},
        {"outcome_prices": [], "end_date": "not-a-date"},
    ]
    for m in bad_inputs:
        v = MQ.evaluate_market_quality(m)
        assert isinstance(v, dict) and isinstance(v["allow"], bool), (m, v)
        assert isinstance(v["reasons"], list) and len(v["checks"]) == 3
        # and each standalone check returns a clean 3-tuple, never raising
        for fn in (MQ.check_stale_price, MQ.check_liquidity, MQ.check_spread):
            ok, reason, detail = fn(m)
            assert isinstance(ok, bool) and isinstance(detail, str)


def test_check_return_shape():
    m = mk()
    for fn in (MQ.check_stale_price, MQ.check_liquidity, MQ.check_spread):
        res = fn(m)
        assert isinstance(res, tuple) and len(res) == 3
        ok, reason, detail = res
        assert isinstance(ok, bool)
        assert reason is None  # clean market -> no blocking reason
        assert isinstance(detail, str)


# ── 7) FAIL-CLOSED (Plan 1): a check that ERRORS internally must NOT become "OK" ──
def test_check_error_fails_closed_not_open():
    """A network/parse hiccup inside a quality check must BLOCK, never silently pass.
    Each check is forced to raise; it must return (False, market_quality_error_fail_closed)
    and evaluate_market_quality must then report allow=False with that reason."""
    m = mk()  # a clean market that WOULD pass if the check didn't blow up

    def _boom(*a, **k):
        raise RuntimeError("simulated scanner/classifier outage")

    # stale check error -> block
    with patched(scanner, "is_stale", _boom):
        ok, reason, _ = MQ.check_stale_price(m)
        assert ok is False and reason == SG.MARKET_QUALITY_ERROR, (ok, reason)
        v = MQ.evaluate_market_quality(m)
        assert v["allow"] is False and SG.MARKET_QUALITY_ERROR in v["reasons"], v

    # liquidity check error -> block
    with patched(classifier, "passes_liquidity_floor", _boom):
        ok, reason, _ = MQ.check_liquidity(m)
        assert ok is False and reason == SG.MARKET_QUALITY_ERROR, (ok, reason)

    # spread check error -> block
    with patched(scanner, "_spread", _boom):
        ok, reason, _ = MQ.check_spread(m)
        assert ok is False and reason == SG.MARKET_QUALITY_ERROR, (ok, reason)


TESTS = [
    ("clean_market_passes_all_at_baseline", test_clean_market_passes_all_at_baseline),
    ("stale_end_date_blocks", test_stale_end_date_blocks),
    ("stale_zero_liquidity_blocks", test_stale_zero_liquidity_blocks),
    ("thin_liquidity_blocks", test_thin_liquidity_blocks),
    ("at_floor_passes_liquidity", test_at_floor_passes_liquidity),
    ("wide_quoted_spread_blocks", test_wide_quoted_spread_blocks),
    ("high_exit_risk_blocks_via_depth_proxy", test_high_exit_risk_blocks_via_depth_proxy),
    ("tighten_blocks_borderline_market", test_tighten_blocks_borderline_market),
    ("tighten_below_one_clamped_to_baseline", test_tighten_below_one_clamped_to_baseline),
    ("never_raises_on_missing_fields", test_never_raises_on_missing_fields),
    ("check_return_shape", test_check_return_shape),
    ("check_error_fails_closed_not_open", test_check_error_fails_closed_not_open),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
