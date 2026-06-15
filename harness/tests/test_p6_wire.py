"""P6 WIRE — cold-start invariant for the wired decision-probability chain.

The acceptance bar for wiring P6 (skill-weighted forecaster blend + gated
calibration + versioned record) into predict_today / sameday / loop is:

    With NO resolved history — the situation TODAY (0 resolved opinion markets) —
    the decision probability used for sizing/conviction/betting MUST be
    NUMERICALLY IDENTICAL to the pre-P6 raw swarm probability.

The EXACT decision-prob formula (predict_today._decision_probability, mirrored in
sameday._ai_scout) is:

    w        = forecaster_weights.forecaster_weights()
    blended  = forecaster_weights.blend_forecasters(swarm_p, challenger_p, w)
    final_p  = calibration_apply.apply_calibration(blended)["calibrated_p"]

These tests prove (1) final_p == raw swarm p to full float precision at cold
start, (2) the blend returns the swarm value as the SAME object (zero drift),
(3) calibration is a passthrough below its data floor, (4) the divergence guard
keeps keying on the RAW swarm p vs the RAW challenger bp (so a blend/calibration
step can never mask model disagreement), (5) the machinery DOES activate once
real per-forecaster history accrues (not a permanent no-op), and (6) the loop
settlement hook attributes a resolved market's Brier to each forecaster.

NO network, NO LLM, temp DB only. Run:  python -m harness.tests.test_p6_wire
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_p6wire_")

from harness import calibration_apply, forecast_versions  # noqa: E402
from harness import forecaster_weights as fw  # noqa: E402
from harness import predict_today as PT  # noqa: E402
from harness import loop as LP  # noqa: E402
from harness.predict_today import _betting_guards  # noqa: E402


# Several p / challenger-bp pairs incl. a None challenger (ensemble all-failed).
P_BP_PAIRS = [(0.5, 0.9), (0.6273, 0.1), (0.01, 0.99), (0.99, 0.01), (0.42, 0.42), (0.7, None)]


# ── (1) THE invariant: cold-start final_p == raw swarm p to full float precision ──────
def test_cold_start_decision_p_identical_to_swarm():
    make_temp_env("ps_p6wire_cs_")  # fresh empty DB -> 0 resolved history
    for p, bp in P_BP_PAIRS:
        final_p, blended, w, cal = PT._decision_probability(p, bp)
        assert blended == p, (p, bp, blended)
        assert final_p == p, (p, bp, final_p)            # EXACT, no rounding/clamp/drift
        assert w == {"swarm": 1.0, "challenger": 0.0}, w
        assert cal["applied"] is False, cal
        assert cal["method"] == "none", cal
        assert cal["n_history"] == 0, cal


# ── (2) blend returns the swarm value as the SAME object (zero arithmetic drift) ──────
def test_cold_start_blend_returns_swarm_object():
    make_temp_env("ps_p6wire_obj_")
    w = fw.forecaster_weights()
    assert w == {"swarm": 1.0, "challenger": 0.0}, w
    sp = 0.6273
    assert fw.blend_forecasters(sp, 0.9, w) is sp        # object identity
    assert fw.blend_forecasters(sp, None, w) is sp        # challenger None -> swarm exactly


# ── (3) gated calibration is an exact passthrough below its data floor ────────────────
def test_cold_start_calibration_passthrough():
    make_temp_env("ps_p6wire_cal_")
    for raw in (0.5, 0.6273, 0.01, 0.99):
        cal = calibration_apply.apply_calibration(raw)
        assert cal["calibrated_p"] == raw, cal
        assert cal["applied"] is False and cal["method"] == "none", cal
        assert cal["n_history"] == 0, cal


# ── (4) the divergence guard keys on RAW swarm p, never the blended/calibrated value ──
def test_divergence_guard_keys_on_raw_swarm_not_blended():
    """The decision probability may be moved by the blend, but the reliability guard
    must ALWAYS see the RAW swarm p vs RAW challenger bp. Use a (hypothetical) non-cold
    50/50 weight so the blend is pulled toward the challenger, then confirm the guard —
    fed raw p — STILL rejects the genuine swarm/challenger disagreement."""
    make_temp_env("ps_p6wire_div_")
    p, bp = 0.80, 0.50                       # |0.80-0.50| = 0.30 > 0.15 divergence cap
    w = {"swarm": 0.3, "challenger": 0.7}    # pull the blend close to bp (well inside the cap)
    blended = fw.blend_forecasters(p, bp, w)  # = 0.59 -> |0.59-0.50| = 0.09 < 0.15
    assert abs(blended - bp) < abs(p - bp)   # blend really did move toward the challenger
    assert abs(blended - bp) < 0.15          # the blended value would itself PASS the guard
    # The guard is fed RAW p (as the wiring does), NOT the blend -> it still fires.
    ok, reason = _betting_guards("opinion", p, bp, 0.9, [], 0.5)
    assert ok is False and "divergence" in reason, (ok, reason)
    # Proof the distinction matters: had the guard been (wrongly) fed the blended midpoint
    # it would have PASSED — masking the disagreement. The wiring avoids exactly this.
    ok2, _ = _betting_guards("opinion", blended, bp, 0.9, [], 0.5)
    assert ok2 is True, ok2


# ── (5) the machinery is NOT a permanent no-op: it activates once BOTH qualify ────────
def test_machinery_activates_once_both_forecasters_qualify():
    make_temp_env("ps_p6wire_act_")
    for i in range(12):                              # >= min_n (10) for BOTH forecasters
        fw.record_forecaster_outcome("swarm", f"s{i}", 0.60, 1.0)        # brier 0.16
        fw.record_forecaster_outcome("challenger", f"c{i}", 0.95, 1.0)   # brier ~0.0025 (better)
    w = fw.forecaster_weights()
    assert w != {"swarm": 1.0, "challenger": 0.0}, w
    assert w["challenger"] > w["swarm"], w           # lower-Brier forecaster gets MORE weight
    blended = fw.blend_forecasters(0.60, 0.90, w)
    assert abs(blended - 0.60) > 1e-9, blended       # no longer swarm-only


def test_asymmetric_history_stays_swarm_only():
    """Only the swarm has a track record (challenger thin) -> STILL swarm-only."""
    make_temp_env("ps_p6wire_asym_")
    for i in range(15):
        fw.record_forecaster_outcome("swarm", f"s{i}", 0.60, 1.0)
    fw.record_forecaster_outcome("challenger", "c0", 0.70, 1.0)   # 1 < min_n
    w = fw.forecaster_weights()
    assert w == {"swarm": 1.0, "challenger": 0.0}, w
    assert fw.blend_forecasters(0.55, 0.90, w) == 0.55


# ── (6) loop settlement hook attributes the resolved Brier to each forecaster ─────────
def test_settlement_records_per_forecaster_brier():
    make_temp_env("ps_p6wire_settle_")
    # Stash the per-forecaster probs the way predict_one does (raw swarm + ensemble probs).
    forecast_versions.record_forecast_version(
        forecast_id="f1", market_id="MKT1", question="Q?", swarm_p=0.62,
        challenger_models=["a", "b"], challenger_ps=[0.70, 0.80],
        blended_p=0.62, calibrated_p=0.62, weights={"swarm": 1.0, "challenger": 0.0},
        calibration_method="none", n_calib_history=0)
    LP._record_forecaster_outcomes("MKT1", 1.0)
    b = fw.forecaster_brier(min_n=1)
    assert abs(b["swarm"]["mean_brier"] - (0.62 - 1.0) ** 2) < 1e-9, b
    # challenger uses the ENSEMBLE MEAN (0.75) -> (0.75 - 1.0)^2.
    assert abs(b["challenger"]["mean_brier"] - (0.75 - 1.0) ** 2) < 1e-9, b


def test_settlement_never_raises_when_no_version_row():
    make_temp_env("ps_p6wire_settle2_")
    # No version / swarm_forecasts / baseline rows -> graceful no-op, never raises.
    LP._record_forecaster_outcomes("UNKNOWN_MKT", 0.0)
    assert fw.forecaster_brier(min_n=1) == {}


# ── forecast_versions by-market read helper (settlement lookup) round-trips ───────────
def test_version_by_market_roundtrip():
    make_temp_env("ps_p6wire_ver_")
    assert forecast_versions.get_forecast_version_by_market("NOPE") is None
    forecast_versions.record_forecast_version(
        forecast_id="fX", market_id="MKTX", question="Q", swarm_p=0.33,
        challenger_models=["m"], challenger_ps=[0.4], blended_p=0.33, calibrated_p=0.33,
        weights={"swarm": 1.0, "challenger": 0.0}, calibration_method="none", n_calib_history=0)
    row = forecast_versions.get_forecast_version_by_market("MKTX")
    assert row is not None, "expected a row"
    assert row["swarm_p"] == 0.33, row
    assert row["challenger_ps"] == [0.4], row
    assert row["weights"] == {"swarm": 1.0, "challenger": 0.0}, row


TESTS = [
    ("cold_start_decision_p_identical_to_swarm", test_cold_start_decision_p_identical_to_swarm),
    ("cold_start_blend_returns_swarm_object", test_cold_start_blend_returns_swarm_object),
    ("cold_start_calibration_passthrough", test_cold_start_calibration_passthrough),
    ("divergence_guard_keys_on_raw_swarm_not_blended", test_divergence_guard_keys_on_raw_swarm_not_blended),
    ("machinery_activates_once_both_forecasters_qualify", test_machinery_activates_once_both_forecasters_qualify),
    ("asymmetric_history_stays_swarm_only", test_asymmetric_history_stays_swarm_only),
    ("settlement_records_per_forecaster_brier", test_settlement_records_per_forecaster_brier),
    ("settlement_never_raises_when_no_version_row", test_settlement_never_raises_when_no_version_row),
    ("version_by_market_roundtrip", test_version_by_market_roundtrip),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
