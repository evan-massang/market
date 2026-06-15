"""B4 — no-network unit tests for harness.forecaster_weights.

Temp DB only (make_temp_env). No network, no LLM. Verifies the cold-start
invariant and the skill-weighted blend:

  * cold-start (empty, or EITHER forecaster < min_n) -> weights
    {"swarm":1.0,"challenger":0.0} AND blend == swarm_p EXACTLY.
  * with >= min_n for BOTH, a more-accurate (lower-Brier) challenger gets the
    higher weight and the blend moves toward the challenger.
  * challenger_p is None -> blend returns swarm_p exactly (even with live weights).
  * cold weights {"swarm":1.0,"challenger":0.0} -> blend == swarm_p exactly
    even when challenger_p is present.
  * de-dupe on (forecaster, market_id, outcome): a re-record counts ONCE.
  * never raises on garbage input.

Run:  python -m harness.tests.test_forecaster_weights
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_forecaster_weights_")

from harness import forecaster_weights as FW            # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Drop the table so each test starts clean (temp DB is shared in-process)."""
    import os, sqlite3
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(f"DROP TABLE IF EXISTS {FW._TABLE}")
    conn.commit()
    conn.close()


def _record_swarm(n, outcome=1.0, model_p=0.5):
    """n resolved swarm rows: model_p far from outcome -> WORSE (higher) Brier."""
    for i in range(n):
        assert FW.record_forecaster_outcome("swarm", f"M{i}", model_p, outcome) is True


def _record_challenger(n, outcome=1.0, model_p=0.9):
    """n resolved challenger rows: model_p near outcome -> BETTER (lower) Brier."""
    for i in range(n):
        assert FW.record_forecaster_outcome("challenger", f"M{i}", model_p, outcome) is True


# ── tests ─────────────────────────────────────────────────────────────────────
def test_cold_start_empty_is_swarm_only():
    _reset()
    # No resolved history at all -> swarm-only default.
    assert FW.forecaster_weights(min_n=10) == {"swarm": 1.0, "challenger": 0.0}
    # And the blend is NUMERICALLY IDENTICAL to swarm_p, even with a challenger_p.
    for sp in (0.5, 0.6273, 0.01, 0.99):
        w = FW.forecaster_weights(min_n=10)
        assert FW.blend_forecasters(sp, 0.9, w) == sp, sp


def test_cold_start_asymmetric_is_swarm_only():
    _reset()
    # Swarm has a full track record but the challenger is thin (< min_n).
    _record_swarm(12)
    _record_challenger(3)
    # EITHER forecaster < min_n -> still swarm-only (cold-start invariant holds).
    assert FW.forecaster_weights(min_n=10) == {"swarm": 1.0, "challenger": 0.0}
    # forecaster_brier excludes the thin challenger, keeps the swarm.
    b = FW.forecaster_brier(min_n=10)
    assert "swarm" in b and "challenger" not in b, b
    assert b["swarm"]["n"] == 12, b


def test_more_accurate_challenger_gets_higher_weight():
    _reset()
    # Both forecasters have >= min_n. Challenger is far more accurate.
    _record_swarm(12, outcome=1.0, model_p=0.5)        # Brier = 0.25 each
    _record_challenger(12, outcome=1.0, model_p=0.9)   # Brier = 0.01 each

    b = FW.forecaster_brier(min_n=10)
    assert abs(b["swarm"]["mean_brier"] - 0.25) < 1e-9, b
    assert abs(b["challenger"]["mean_brier"] - 0.01) < 1e-9, b

    w = FW.forecaster_weights(min_n=10)
    # Weights are normalized and the lower-Brier challenger dominates.
    assert abs((w["swarm"] + w["challenger"]) - 1.0) < 1e-9, w
    assert w["challenger"] > w["swarm"], w

    # Blend moves toward the more-accurate challenger.
    sp, cp = 0.5, 0.9
    blended = FW.blend_forecasters(sp, cp, w)
    assert sp < blended < cp, blended
    assert abs(blended - cp) < abs(blended - sp), blended


def test_none_challenger_returns_swarm_exactly():
    _reset()
    # Even with fully active weights, a None challenger_p -> swarm_p exactly.
    _record_swarm(12, outcome=1.0, model_p=0.5)
    _record_challenger(12, outcome=1.0, model_p=0.9)
    w = FW.forecaster_weights(min_n=10)
    assert w["challenger"] > 0.0, w        # weights ARE live here
    for sp in (0.42, 0.6273, 0.5):
        assert FW.blend_forecasters(sp, None, w) == sp, sp


def test_zero_challenger_weight_returns_swarm_exactly():
    _reset()
    # The cold-start weights themselves, with a present challenger_p, still give
    # back swarm_p with no float drift.
    cold = {"swarm": 1.0, "challenger": 0.0}
    for sp in (0.5, 0.6273, 0.01, 0.99, 0.123456789):
        assert FW.blend_forecasters(sp, 0.9, cold) == sp, sp
        assert FW.blend_forecasters(sp, 0.9, cold) is sp, sp  # same object: no arithmetic


def test_dedupe_on_record_and_read():
    _reset()
    # First record succeeds; an exact re-record (same forecaster+market+outcome)
    # is skipped, and read-side de-dupe keeps ONE row regardless.
    assert FW.record_forecaster_outcome("swarm", "D1", 0.7, 1.0) is True
    assert FW.record_forecaster_outcome("swarm", "D1", 0.7, 1.0) is False
    b = FW.forecaster_brier(min_n=1)
    assert b["swarm"]["n"] == 1, b
    assert abs(b["swarm"]["mean_brier"] - (0.7 - 1.0) ** 2) < 1e-9, b

    # A re-record with a DIFFERENT model_p (same forecaster+market+outcome) is
    # still de-duped to the LATEST single row on read.
    assert FW.record_forecaster_outcome("swarm", "D1", 0.4, 1.0) is False  # dup key
    assert FW.forecaster_brier(min_n=1)["swarm"]["n"] == 1


def test_same_market_different_forecaster_coexist():
    _reset()
    # Same market_id under each forecaster are independent rows (keyed by both).
    assert FW.record_forecaster_outcome("swarm", "X1", 0.6, 1.0) is True
    assert FW.record_forecaster_outcome("challenger", "X1", 0.8, 1.0) is True
    b = FW.forecaster_brier(min_n=1)
    assert b["swarm"]["n"] == 1 and b["challenger"]["n"] == 1, b


def test_never_raises_on_garbage():
    _reset()
    # Malformed inputs must degrade to a bool (never raise). A non-numeric model_p
    # coerces to NULL; the row may still insert, so we only assert "returns a bool".
    assert FW.record_forecaster_outcome("swarm", None, "not-a-number", None) in (True, False)
    assert FW.record_forecaster_outcome(None, None, None, None) in (True, False)
    # Garbage challenger_p with live-looking weights -> falls back to swarm_p.
    assert FW.blend_forecasters(0.5, "xyz", {"swarm": 0.5, "challenger": 0.5}) == 0.5
    # Garbage / missing weights -> swarm_p.
    assert FW.blend_forecasters(0.5, 0.9, None) == 0.5
    assert FW.blend_forecasters(0.5, 0.9, {}) == 0.5
    # forecaster_weights never raises -> always a dict with both keys.
    w = FW.forecaster_weights(min_n=10)
    assert set(w) == {"swarm", "challenger"}, w


TESTS = [
    ("cold_start_empty_is_swarm_only", test_cold_start_empty_is_swarm_only),
    ("cold_start_asymmetric_is_swarm_only", test_cold_start_asymmetric_is_swarm_only),
    ("more_accurate_challenger_gets_higher_weight", test_more_accurate_challenger_gets_higher_weight),
    ("none_challenger_returns_swarm_exactly", test_none_challenger_returns_swarm_exactly),
    ("zero_challenger_weight_returns_swarm_exactly", test_zero_challenger_weight_returns_swarm_exactly),
    ("dedupe_on_record_and_read", test_dedupe_on_record_and_read),
    ("same_market_different_forecaster_coexist", test_same_market_different_forecaster_coexist),
    ("never_raises_on_garbage", test_never_raises_on_garbage),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
