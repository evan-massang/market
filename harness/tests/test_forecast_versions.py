"""B1 — no-network unit tests for harness.forecast_versions (versioned record).

Temp DB only (make_temp_env). No network, no LLM. Verifies:
  * init_db is idempotent (callable twice, no error)
  * record -> get roundtrip preserves scalars
  * JSON fields (challenger model list, probability list, weights map)
    serialize on write and deserialize back into the same Python objects
  * best-effort on bad/un-serializable input -> returns False, does NOT crash
  * version stamps (prompt/method/code) are present on a recorded row, and an
    explicitly-passed stamp wins over the default
  * get_forecast_version on an unknown id -> None
  * multiple records for one forecast_id -> get returns the LATEST

Run:  python -m harness.tests.test_forecast_versions
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_forecast_versions_")

from harness import forecast_versions as FV            # noqa: E402


def _reset():
    """Drop the table so each test starts clean (temp DB shared in-process)."""
    import os
    import sqlite3
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(f"DROP TABLE IF EXISTS {FV._TABLE}")
    conn.commit()
    conn.close()


def _sample(forecast_id="F1", **over):
    kw = dict(
        forecast_id=forecast_id,
        market_id="MKT-1",
        question="Will it resolve YES?",
        swarm_p=0.63,
        challenger_models=["local-llm", "qwen2.5:7b"],
        challenger_ps=[0.60, 0.66],
        blended_p=0.63,
        calibrated_p=0.63,
        weights={"agent_a": 0.5, "agent_b": 0.5},
        calibration_method="none",
        n_calib_history=0,
    )
    kw.update(over)
    return FV.record_forecast_version(**kw)


# ── tests ─────────────────────────────────────────────────────────────────────
def test_init_db_idempotent():
    _reset()
    FV.init_db()
    FV.init_db()  # second call must not raise / must not error
    # table exists and is empty
    assert FV.get_forecast_version("nope") is None


def test_roundtrip_scalars():
    _reset()
    assert _sample("F1") is True
    rec = FV.get_forecast_version("F1")
    assert rec is not None
    assert rec["forecast_id"] == "F1"
    assert rec["market_id"] == "MKT-1"
    assert rec["question"] == "Will it resolve YES?"
    assert abs(rec["swarm_p"] - 0.63) < 1e-12
    assert abs(rec["blended_p"] - 0.63) < 1e-12
    assert abs(rec["calibrated_p"] - 0.63) < 1e-12
    assert rec["calibration_method"] == "none"
    assert rec["n_calib_history"] == 0
    assert rec["created_at"]  # a timestamp string was stamped


def test_json_fields_roundtrip():
    _reset()
    assert _sample("F-JSON") is True
    rec = FV.get_forecast_version("F-JSON")
    assert rec is not None
    # lists come back as lists, dict as dict — full serialize/deserialize
    assert rec["challenger_models"] == ["local-llm", "qwen2.5:7b"]
    assert rec["challenger_ps"] == [0.60, 0.66]
    assert rec["weights"] == {"agent_a": 0.5, "agent_b": 0.5}
    assert isinstance(rec["challenger_models"], list)
    assert isinstance(rec["weights"], dict)


def test_bad_input_returns_false_not_crash():
    _reset()
    # A set is not JSON-serializable -> record must degrade to False, not raise.
    ok = FV.record_forecast_version(
        "F-BAD", "MKT", "q", 0.5,
        challenger_models={"unserializable", "set"},  # bad
        challenger_ps=[0.5],
        blended_p=0.5, calibrated_p=0.5,
        weights={"a": 0.5}, calibration_method="none", n_calib_history=0,
    )
    assert ok is False
    # nothing was written for that id
    assert FV.get_forecast_version("F-BAD") is None

    # An object instance in the weights map is likewise un-serializable.
    ok2 = FV.record_forecast_version(
        "F-BAD2", "MKT", "q", 0.5,
        challenger_models=["m"], challenger_ps=[0.5],
        blended_p=0.5, calibrated_p=0.5,
        weights={"a": object()},  # bad
        calibration_method="none", n_calib_history=0,
    )
    assert ok2 is False
    assert FV.get_forecast_version("F-BAD2") is None


def test_version_stamps_present():
    _reset()
    # Defaults: prompt_version from the module constant; method/code from
    # obs.codeversion (may be None for git_sha off-repo, but the field exists).
    assert _sample("F-VER") is True
    rec = FV.get_forecast_version("F-VER")
    assert rec is not None
    assert rec["prompt_version"] == FV.PROMPT_VERSION == "swarm-v1"
    assert "method_version" in rec
    assert "code_version" in rec

    # Explicit stamps win over the defaults.
    assert _sample(
        "F-VER2",
        prompt_version="swarm-v9",
        method_version="method-abc",
        code_version="code-xyz",
    ) is True
    rec2 = FV.get_forecast_version("F-VER2")
    assert rec2["prompt_version"] == "swarm-v9"
    assert rec2["method_version"] == "method-abc"
    assert rec2["code_version"] == "code-xyz"


def test_unknown_id_returns_none():
    _reset()
    assert FV.get_forecast_version("never-recorded") is None
    assert FV.get_forecast_version(None) is None


def test_latest_wins_for_same_forecast_id():
    _reset()
    assert _sample("F-DUP", swarm_p=0.40, calibrated_p=0.40) is True
    assert _sample("F-DUP", swarm_p=0.70, calibrated_p=0.72) is True
    rec = FV.get_forecast_version("F-DUP")
    # the LATEST (id DESC) row is returned
    assert abs(rec["swarm_p"] - 0.70) < 1e-12
    assert abs(rec["calibrated_p"] - 0.72) < 1e-12


def test_none_fields_are_safe():
    _reset()
    # None challenger ensemble / weights / numeric fields must record cleanly
    # (this is the cold-start shape before any ensemble/weights exist).
    ok = FV.record_forecast_version(
        "F-NONE", "MKT", "q", 0.55,
        challenger_models=None, challenger_ps=None,
        blended_p=None, calibrated_p=0.55,
        weights=None, calibration_method="none", n_calib_history=0,
    )
    assert ok is True
    rec = FV.get_forecast_version("F-NONE")
    assert rec is not None
    assert rec["challenger_models"] is None
    assert rec["challenger_ps"] is None
    assert rec["weights"] is None
    assert rec["blended_p"] is None
    assert abs(rec["calibrated_p"] - 0.55) < 1e-12


TESTS = [
    ("init_db_idempotent", test_init_db_idempotent),
    ("roundtrip_scalars", test_roundtrip_scalars),
    ("json_fields_roundtrip", test_json_fields_roundtrip),
    ("bad_input_returns_false_not_crash", test_bad_input_returns_false_not_crash),
    ("version_stamps_present", test_version_stamps_present),
    ("unknown_id_returns_none", test_unknown_id_returns_none),
    ("latest_wins_for_same_forecast_id", test_latest_wins_for_same_forecast_id),
    ("none_fields_are_safe", test_none_fields_are_safe),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
