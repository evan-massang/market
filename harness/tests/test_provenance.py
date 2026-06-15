"""P12 — no-network tests for harness.provenance (versions, config-change, diffs)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_prov_")

from harness import provenance as P     # noqa: E402
from harness import forecast_versions as FV  # noqa: E402
from harness import sizing              # noqa: E402


def test_versions_present():
    v = P.versions()
    for k in ("classifier", "guards", "sizing", "prompt", "strategy"):
        assert k in v and isinstance(v[k], str), (k, v)
    # code version stamps are added defensively (may be None in a non-git dir)
    assert "code_version" in v


def test_config_snapshot_has_hash_and_knobs():
    s = P.config_snapshot()
    assert "_hash" in s and isinstance(s["_hash"], str) and len(s["_hash"]) == 16
    assert "sizing.DEFAULT_MIN_EDGE" in s, list(s)
    assert "market_quality.DEFAULT_MAX_EXIT_RISK" in s, list(s)
    assert "wallet.slippage" in s


def test_config_change_recorded_only_on_change():
    # first snapshot -> changed True (no prior row)
    r1 = P.record_config_snapshot(recorded_at="t0")
    assert r1["changed"] is True, r1
    # identical config -> changed False, no new row
    r2 = P.record_config_snapshot(recorded_at="t1")
    assert r2["changed"] is False, r2
    # change a knob -> changed True with a precise diff
    with patched(sizing, "DEFAULT_MIN_EDGE", 0.999):
        r3 = P.record_config_snapshot(recorded_at="t2")
    assert r3["changed"] is True, r3
    assert "sizing.DEFAULT_MIN_EDGE" in r3["diff"], r3["diff"]
    assert r3["diff"]["sizing.DEFAULT_MIN_EDGE"]["new"] == 0.999
    # history reflects the two recorded states (+ the change)
    hist = P.config_history()
    assert len(hist) >= 2


def test_provenance_for_assembles_decision_record():
    FV.record_forecast_version(
        "FID-1", "MKT-1", "Q-1", swarm_p=0.62,
        challenger_models=["qwen2.5:3b"], challenger_ps=[0.60],
        blended_p=0.62, calibrated_p=0.62, weights={"swarm": 1.0, "challenger": 0.0},
        calibration_method="none", n_calib_history=0,
        prompt_version="swarm-v1", method_version="abc", code_version="def",
    )
    prov = P.provenance_for("FID-1")
    assert prov["forecast_id"] == "FID-1"
    assert prov["forecast_version"] is not None and prov["forecast_version"]["swarm_p"] == 0.62
    assert "versions" in prov and "config_hash" in prov
    # experiment lazily resolves to a baseline (best-effort; may be a dict or None)
    assert "experiment" in prov


def test_decision_diff():
    FV.record_forecast_version("A", "M", "Q", swarm_p=0.60, challenger_models=["x"],
                               challenger_ps=[0.6], blended_p=0.60, calibrated_p=0.60,
                               weights={}, calibration_method="none", n_calib_history=0,
                               prompt_version="swarm-v1", method_version="m", code_version="c")
    FV.record_forecast_version("B", "M", "Q", swarm_p=0.80, challenger_models=["x"],
                               challenger_ps=[0.6], blended_p=0.80, calibrated_p=0.80,
                               weights={}, calibration_method="none", n_calib_history=0,
                               prompt_version="swarm-v1", method_version="m", code_version="c")
    d = P.decision_diff("A", "B")
    assert "swarm_p" in d and d["swarm_p"]["a"] == 0.60 and d["swarm_p"]["b"] == 0.80, d


def test_never_raises_on_missing_db():
    # provenance is best-effort; calling against a fresh env never raises
    assert isinstance(P.versions(), dict)
    assert isinstance(P.provenance_for("nope"), dict)
    assert isinstance(P.decision_diff("x", "y"), dict)


TESTS = [
    ("versions_present", test_versions_present),
    ("config_snapshot_has_hash_and_knobs", test_config_snapshot_has_hash_and_knobs),
    ("config_change_recorded_only_on_change", test_config_change_recorded_only_on_change),
    ("provenance_for_assembles_decision_record", test_provenance_for_assembles_decision_record),
    ("decision_diff", test_decision_diff),
    ("never_raises_on_missing_db", test_never_raises_on_missing_db),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
