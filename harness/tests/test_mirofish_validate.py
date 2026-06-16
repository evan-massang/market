"""MiroFish honesty — strict result contract + freshness validation + pipeline gate.
No real MiroFish backend (raw dicts are mocked). Paper-only."""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_mfv_")

from harness import mirofish_validate as mfv   # noqa: E402

_Q = "Will LeBron James win the 2028 US Presidential Election?"
_MID = "0xabc123def456"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _result(raw, sig=None, requested_at="2026-06-16T00:00:00+00:00"):
    return mfv.validate(mfv.build_result(raw, _MID, _Q, requested_at, sig or {}))


def _fresh_raw():
    return {"ok": True, "simulation_id": "sim_fresh", "report_id": "r1",
            "report_generated_at": _now(), "stage_reached": "report_done",
            "report_markdown": "LeBron James 2028 presidential election crowd debate " * 30}


def _fresh_sig():
    return {"n_posts": 4, "posts": ["LeBron James president 2028"] * 4, "probability": 0.3}


# 1. fresh passes
def test_fresh_report_passes():
    r = _result(_fresh_raw(), _fresh_sig())
    assert r.usable is True and r.freshness_status == "fresh"
    assert r.question_match_score >= mfv.config()["MATCH_THRESHOLD"]


# 2. empty fails
def test_empty_report_fails():
    r = _result({"ok": True, "simulation_id": "s", "report_generated_at": _now()}, {"n_posts": 0, "posts": []})
    assert r.usable is False and mfv.status_label(r) == "WEAK"


# 3 + 6. old June-13 report for a June-16 request fails (completed before request)
def test_stale_june13_report_fails():
    raw = dict(_fresh_raw(), report_generated_at="2026-06-13T20:08:54+00:00")
    r = _result(raw, _fresh_sig())
    assert r.usable is False and r.freshness_status == "stale"
    assert any("BEFORE this request" in w for w in r.warnings)


# 4. wrong-question report fails
def test_wrong_question_report_fails():
    raw = dict(_fresh_raw(), report_markdown="bitcoin ethereum crypto price market cap " * 30)
    r = _result(raw, {"n_posts": 4, "posts": ["bitcoin to the moon"] * 4})
    assert r.usable is False and r.question_match_score == 0.0


# 5. missing simulation_id fails
def test_missing_sim_id_fails():
    raw = dict(_fresh_raw()); raw["simulation_id"] = ""
    r = _result(raw, _fresh_sig())
    assert r.usable is False and any("simulation_id" in w for w in r.warnings)


# 7. low post count fails when there is no report either
def test_low_posts_no_report_fails():
    raw = {"ok": True, "simulation_id": "s", "report_generated_at": _now(), "report_markdown": ""}
    r = _result(raw, {"n_posts": 1, "posts": ["one"]})
    assert r.usable is False


# 8. probability None fails when required
def test_probability_required_fails():
    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRE_PROBABILITY": "true"}):
        r = mfv.validate(mfv.build_result(_fresh_raw(), _MID, _Q, "2026-06-16T00:00:00+00:00",
                                          {"n_posts": 4, "posts": ["x"] * 4, "probability": None}))
    assert r.usable is False and any("probability" in w for w in r.warnings)


# 12. record + read back the run
def test_record_and_get_runs():
    r = _result(_fresh_raw(), _fresh_sig())
    assert mfv.record_run(r, forecast_id="f1") is True
    runs = mfv.get_runs(_MID)
    assert runs and runs[0]["usable"] == 1 and runs[0]["freshness_status"] == "fresh"


# fresh_project_name is unique per call (no reuse)
def test_fresh_project_name_unique():
    a = mfv.fresh_project_name(_MID, "run1", now_iso="2026-06-16T00:00:00")
    b = mfv.fresh_project_name(_MID, "run2", now_iso="2026-06-16T00:00:01")
    assert a != b and a.startswith("poly_")


from types import SimpleNamespace  # noqa: E402


def _gate(mode, usable, eq=None, use_mf=True):
    import harness.predict_today as PT
    pack = SimpleNamespace(evidence_quality=eq) if eq is not None else None
    m = {"_mf_usable": usable, "_mf_status": "FRESH" if usable else "STALE"}
    with patched(os, "environ", {**os.environ, "MIROFISH_MODE": mode}), \
         patched(PT, "USE_MIROFISH", use_mf):
        return PT._p_mirofish_gate(m, pack)


# 9. required mode blocks the bet when MiroFish is unusable
def test_required_mode_blocks_unusable():
    ok, reason = _gate("required", usable=False)
    assert ok is False and "mirofish_required_unusable" in reason


# 10. degraded mode: blocks on weak evidence, continues on strong evidence
def test_degraded_mode_evidence_gated():
    ok_weak, r_weak = _gate("degraded", usable=False, eq=0.10)
    assert ok_weak is False and "weak_evidence" in r_weak
    ok_strong, _ = _gate("degraded", usable=False, eq=0.60)
    assert ok_strong is True


# 11. off mode never blocks; usable always passes
def test_off_and_usable_pass():
    assert _gate("off", usable=False)[0] is True
    assert _gate("required", usable=True)[0] is True
    assert _gate("required", usable=False, use_mf=False)[0] is True   # mirofish not on


TESTS = [
    ("fresh_report_passes", test_fresh_report_passes),
    ("required_mode_blocks_unusable", test_required_mode_blocks_unusable),
    ("degraded_mode_evidence_gated", test_degraded_mode_evidence_gated),
    ("off_and_usable_pass", test_off_and_usable_pass),
    ("empty_report_fails", test_empty_report_fails),
    ("stale_june13_report_fails", test_stale_june13_report_fails),
    ("wrong_question_report_fails", test_wrong_question_report_fails),
    ("missing_sim_id_fails", test_missing_sim_id_fails),
    ("low_posts_no_report_fails", test_low_posts_no_report_fails),
    ("probability_required_fails", test_probability_required_fails),
    ("record_and_get_runs", test_record_and_get_runs),
    ("fresh_project_name_unique", test_fresh_project_name_unique),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
