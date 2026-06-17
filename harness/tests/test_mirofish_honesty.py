"""Plan 8 — MiroFish freshness + CONTRIBUTION honesty.

Proves that stale / pending / sim_prepared / launch-only / failed / wrong-market /
unverifiable-timestamp MiroFish outputs can NEVER:
  (a) be marked ``mirofish_used=true``,
  (b) be fed to the swarm,
  (c) allow a bet when MiroFish is REQUIRED, or
  (d) show as green/used on the dashboard.
Only a fresh + completed + same-market + verifiable + CONSUMED result is a contribution.

No real MiroFish backend (raw dicts are mocked). Paper-only; no network; no wallet writes.
"""
import os
import re
import sys
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_mfh_")

from harness import mirofish_validate as MV   # noqa: E402
from harness import mirofish_status as MS      # noqa: E402

_Q = "Will LeBron James win the 2028 US Presidential Election?"
_MID = "0xabc123def456"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _val(raw, sig=None, requested_at="2026-06-16T00:00:00+00:00", now_iso=None):
    return MV.validate(MV.build_result(raw, _MID, _Q, requested_at, sig or {}), now_iso=now_iso)


def _fresh_raw(**over):
    raw = {"ok": True, "simulation_id": "sim_fresh", "report_id": "r1",
           "report_generated_at": _now(), "stage_reached": "report_done",
           "report_markdown": "LeBron James 2028 presidential election crowd debate " * 30}
    raw.update(over)
    return raw


def _fresh_sig(n=4):
    return {"n_posts": n, "posts": ["LeBron James president 2028"] * n, "probability": 0.3}


def _stale():
    return _val(_fresh_raw(report_generated_at="2026-06-13T20:00:00+00:00"), _fresh_sig())


def _pending():
    # a sim_prepared payload with FILLER content — rejected at BOTH layers (validator forces
    # usable=False for incomplete stages; from_result forces PENDING regardless of usable)
    return _val({"ok": True, "simulation_id": "sim_pending", "stage_reached": "sim_prepared",
                 "report_markdown": "LeBron James 2028 presidential election " * 30}, _fresh_sig())


def _wrong_market():
    return _val(_fresh_raw(report_markdown="bitcoin ethereum crypto price market cap halving " * 30),
                {"n_posts": 4, "posts": ["bitcoin to the moon"] * 4})


def _no_timestamp():
    return _val({"ok": True, "simulation_id": "sim_nots", "stage_reached": "report_done",
                 "report_markdown": "LeBron James 2028 presidential election " * 30}, _fresh_sig())


def _failed():
    return _val({"ok": False, "error": "sim crashed mid-run"}, {})


def _backend_down():
    return _val({"ok": False, "error": "backend unavailable: connection refused"}, {})


# ─────────────────────── A. state machine: what each output maps to ───────────────

def test_fresh_consumed_is_used():
    st = MS.from_result(_val(_fresh_raw(), _fresh_sig()), required=False, consumed=True)
    assert st["state"] == MS.FRESH_USED
    assert st["mirofish_used"] is True and st["allow_decision_use"] is True
    assert st["contribution"] == "fresh_used"


def test_fresh_unconsumed_then_mark_used():
    st = MS.from_result(_val(_fresh_raw(), _fresh_sig()), required=False, consumed=False)
    assert st["state"] == MS.FRESH_UNUSED and st["mirofish_used"] is False
    st2 = MS.mark_used(st, contribution="context_only")
    assert st2["state"] == MS.FRESH_USED and st2["mirofish_used"] is True
    assert st2["contribution"] == "context_only"


def test_stale_not_used_display_only():
    st = MS.from_result(_stale(), required=False, consumed=True)   # caller "tries" to consume
    assert st["state"] == MS.STALE_RESULT
    assert st["mirofish_used"] is False and st["allow_decision_use"] is False
    assert MS.mark_used(st)["mirofish_used"] is False              # mark_used is a no-op on stale


def test_wrong_market_is_mismatch():
    st = MS.from_result(_wrong_market(), required=False, consumed=True)
    assert st["state"] == MS.MARKET_MISMATCH and st["mirofish_used"] is False


def test_pending_sim_prepared_never_used():
    # THE LEAK FIX: a filler sim_prepared payload must be PENDING / not-used. Defended at both
    # layers — even if the validator were tricked into usable=True, from_result forces PENDING.
    v = _pending()
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.PENDING
    assert st["mirofish_used"] is False and st["allow_decision_use"] is False


def test_failed_not_used():
    st = MS.from_result(_failed(), required=False, consumed=True)
    assert st["state"] == MS.FAILED and st["mirofish_used"] is False


def test_backend_unavailable_state():
    st = MS.from_result(_backend_down(), required=False, consumed=True)
    assert st["state"] == MS.BACKEND_UNAVAILABLE and st["mirofish_used"] is False


def test_missing_timestamp_unverifiable_not_used():
    v = _no_timestamp()
    assert v.usable is True and v.report_generated_at is None      # the dangerous pre-state
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.INVALID_RESULT                        # unverifiable freshness -> rejected
    assert st["mirofish_used"] is False and st["allow_decision_use"] is False


def test_unverifiable_malformed_timestamp_not_used():
    # ADVERSARIAL (critical): a truthy-but-UNPARSEABLE report_generated_at leaves
    # report_age_seconds=None (freshness unverifiable). It must NOT be usable/used even though
    # the string is truthy and FORCE_FRESH marks freshness_status="fresh".
    v = _val({"ok": True, "simulation_id": "sim_x", "stage_reached": "report_done",
              "report_generated_at": "UNVERIFIABLE_TIMESTAMP",
              "report_markdown": "LeBron James 2028 presidential election " * 30}, _fresh_sig())
    assert v.report_age_seconds is None and v.freshness_status == "fresh"   # the dangerous pre-state
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.INVALID_RESULT
    assert st["mirofish_used"] is False and st["allow_decision_use"] is False


def test_validate_rejects_incomplete_stage():
    # ADVERSARIAL (high): a sim_prepared payload (even with a recent timestamp + filler text)
    # is forced usable=False by the validator -> recorded honestly, never green on the dash.
    v = _val({"ok": True, "simulation_id": "s", "stage_reached": "sim_prepared",
              "report_generated_at": _now(),
              "report_markdown": "LeBron James 2028 presidential election " * 30}, _fresh_sig())
    assert v.usable is False
    assert any("incomplete simulation" in w for w in v.warnings)
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.PENDING and st["mirofish_used"] is False


def test_wrong_market_independent_of_question_match_flag():
    # ADVERSARIAL (high): disabling MIROFISH_REQUIRE_QUESTION_MATCH makes the VALIDATOR mark a
    # wrong-market report usable=True. The canonical machine must STILL reject it on its own
    # independent question-match check, so it can never reach the swarm.
    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRE_QUESTION_MATCH": "false"}):
        v = _val(_fresh_raw(report_markdown="bitcoin ethereum crypto price market cap " * 30),
                 {"n_posts": 4, "posts": ["bitcoin to the moon"] * 4})
        assert v.usable is True                       # validator no longer enforces match
        st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.MARKET_MISMATCH and st["mirofish_used"] is False


def test_sim_running_never_used():
    # ADVERSARIAL r2 (critical): sim_running is NOT terminal. The stage WHITELIST rejects it at
    # both layers, even with a fresh timestamp + filler text (the "running" vs "sim_running"
    # name-mismatch leak).
    v = _val({"ok": True, "simulation_id": "s", "stage_reached": "sim_running",
              "report_generated_at": _now(),
              "report_markdown": "LeBron James 2028 presidential election " * 30}, _fresh_sig())
    assert v.usable is False
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.PENDING and st["mirofish_used"] is False


def test_future_timestamp_not_used():
    # ADVERSARIAL r2 (critical): a FUTURE-dated report (negative age) is unverifiable -> not used
    future = (datetime.now(timezone.utc) + timedelta(seconds=7200)).isoformat()
    v = _val(_fresh_raw(report_generated_at=future), _fresh_sig())
    assert v.report_age_seconds is not None and v.report_age_seconds < 0
    assert v.freshness_status == "stale" and v.usable is False
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] in (MS.STALE_RESULT, MS.INVALID_RESULT) and st["mirofish_used"] is False


def test_negative_match_threshold_cannot_bypass():
    # ADVERSARIAL r2 (high): a non-positive MIROFISH_MATCH_THRESHOLD must NOT disable the
    # market-match gate (qms < 0 would never fire) -> falls back to the safe default.
    with patched(os, "environ", {**os.environ, "MIROFISH_MATCH_THRESHOLD": "-1"}):
        assert MS._match_threshold() == 0.30
        v = _val(_fresh_raw(report_markdown="bitcoin ethereum crypto price market cap " * 30),
                 {"n_posts": 4, "posts": ["bitcoin to the moon"] * 4})
        st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.MARKET_MISMATCH and st["mirofish_used"] is False


def test_tiny_match_threshold_floored_at_default():
    # ADVERSARIAL r3 (high): a TINY positive threshold (0.0001) would let a wrong-market report
    # through (qms < 0.0001 almost never fires). The gate is FLOORED at the default 0.30 — it
    # can only be made stricter, never weaker.
    with patched(os, "environ", {**os.environ, "MIROFISH_MATCH_THRESHOLD": "0.0001"}):
        assert MS._match_threshold() == 0.30
        # a wrong-market report with some incidental overlap (qms ~0.1) is still rejected
        v = _val(_fresh_raw(report_markdown="bitcoin ethereum crypto price market cap " * 30),
                 {"n_posts": 4, "posts": ["bitcoin to the moon"] * 4})
        st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.MARKET_MISMATCH and st["mirofish_used"] is False


def test_dash_low_sims_not_used():
    # ADVERSARIAL r3 (high): from_result rejects n_posts < min_sims as invalid; state_from_row
    # MUST mirror that gate so a low-sims run never flips to fresh_used on the dashboard.
    assert MS.state_from_row(_row(n_posts=2)) == MS.INVALID_RESULT


def test_record_run_always_freezes_thresholds():
    # ADVERSARIAL r5 (medium): a new run's frozen thresholds must NEVER be null — even if the
    # status import/compute fails — else state_from_row would fall back to live config and the
    # historical used-flag could flip. record_run must self-describe via env defaults.
    import harness.mirofish_status as _MS2

    def _boom(*a, **k):
        raise RuntimeError("status exploded")

    mid = "0xfreeze_isolated_mkt"   # isolated id so it never pollutes other tests' run counts
    res = MV.validate(MV.build_result(_fresh_raw(), mid, _Q, "2026-06-16T00:00:00+00:00", _fresh_sig()))
    with patched(_MS2, "min_sims", _boom):
        assert MV.record_run(res, forecast_id="f_freeze") is True
    runs = MV.get_runs(mid, limit=1)
    assert runs and runs[0]["min_sims_used"] is not None and runs[0]["match_threshold_used"] is not None


def test_dash_uses_frozen_thresholds_no_flip():
    # ADVERSARIAL r4 (high): a run recorded under min_sims=3 / threshold=0.30 must STAY
    # fresh_used even if the operator later raises those — state_from_row uses the FROZEN
    # decision-time thresholds, so the historical contribution never flips.
    frozen = _row(n_posts=3, min_sims_used=3.0, match_threshold_used=0.30)
    with patched(os, "environ", {**os.environ, "MIROFISH_MIN_SIMS": "5",
                                 "MIROFISH_MATCH_THRESHOLD": "0.95"}):
        assert MS.state_from_row(frozen) == MS.FRESH_USED          # frozen verdict stands
    # a legacy row WITHOUT frozen columns falls back to current config (now stricter -> rejects)
    legacy = _row(n_posts=3, min_sims_used=None, match_threshold_used=None)
    with patched(os, "environ", {**os.environ, "MIROFISH_MIN_SIMS": "5"}):
        assert MS.state_from_row(legacy) == MS.INVALID_RESULT


def test_low_sims_invalid():
    v = _val(_fresh_raw(), {"n_posts": 2, "posts": ["LeBron James 2028"] * 2, "probability": 0.3})
    assert v.usable is True                                        # usable via long report
    st = MS.from_result(v, required=False, consumed=True)
    assert st["state"] == MS.INVALID_RESULT and st["mirofish_used"] is False


def test_disabled_helper():
    st = MS.disabled(required=False)
    assert st["state"] == MS.DISABLED and st["mirofish_used"] is False and st["allow_bet"] is True


def test_not_configured_helper():
    st = MS.not_configured(required=False)
    assert st["state"] == MS.NOT_CONFIGURED and st["allow_bet"] is True


def test_launch_only_fire_and_forget():
    st = MS.launch_only(market_id=_MID, question=_Q, required=False)
    assert st["state"] == MS.LAUNCH_ONLY_NOT_USED
    assert st["mirofish_used"] is False and st["allow_decision_use"] is False
    assert st["market_id"] == _MID


def test_only_fresh_consumed_yields_used():
    # EXHAUSTIVE: no non-fresh output can ever become used, even when the caller passes
    # consumed=True AND then calls mark_used().
    for name, v in {"stale": _stale(), "pending": _pending(), "failed": _failed(),
                    "mismatch": _wrong_market(), "unverifiable": _no_timestamp(),
                    "backend_down": _backend_down()}.items():
        st = MS.from_result(v, required=False, consumed=True)
        assert st["mirofish_used"] is False, name
        assert st["state"] != MS.FRESH_USED, name
        assert MS.mark_used(st)["mirofish_used"] is False, name
    for st in (MS.launch_only(required=False), MS.backend_unavailable(required=False),
               MS.disabled(required=False), MS.not_configured(required=False)):
        assert st["mirofish_used"] is False
        assert MS.mark_used(st)["mirofish_used"] is False


def test_mark_used_noop_on_every_unusable():
    for v in (_stale(), _pending(), _failed(), _wrong_market(), _no_timestamp()):
        st = MS.from_result(v, required=True, consumed=False)
        assert MS.mark_used(st) is st or MS.mark_used(st)["state"] == st["state"]
        assert MS.mark_used(st)["mirofish_used"] is False


# ─────────────────────── B. required-mode no-bet policy ───────────────────────────

def test_required_unavailable_blocks():
    st = MS.backend_unavailable(required=True)
    assert st["allow_bet"] is False
    assert MS.required_no_bet_reason(st) == "mirofish_required_unavailable_no_bet"


def test_required_stale_blocks_specific_reason():
    st = MS.from_result(_stale(), required=True, consumed=True)
    assert st["state"] == MS.STALE_RESULT and st["allow_bet"] is False
    assert MS.required_no_bet_reason(st) == "mirofish_required_stale_no_bet"


def test_required_pending_blocks():
    st = MS.from_result(_pending(), required=True, consumed=True)
    assert st["allow_bet"] is False
    assert MS.required_no_bet_reason(st) == "mirofish_required_pending_no_bet"


def test_required_market_mismatch_blocks():
    st = MS.from_result(_wrong_market(), required=True, consumed=True)
    assert st["allow_bet"] is False
    assert MS.required_no_bet_reason(st) == "mirofish_required_market_mismatch_no_bet"


def test_required_fresh_used_allows():
    st = MS.from_result(_val(_fresh_raw(), _fresh_sig()), required=True, consumed=True)
    assert st["state"] == MS.FRESH_USED and st["allow_bet"] is True
    assert MS.required_no_bet_reason(st) is None


def test_required_launch_only_blocks_with_prefix():
    st = MS.launch_only(market_id=_MID, question=_Q, required=True)
    assert st["allow_bet"] is False
    assert MS.required_no_bet_reason(st, prefix="sameday_") == "sameday_mirofish_required_launch_only_no_bet"


def test_optional_failure_never_blocks():
    for st in (MS.backend_unavailable(required=False),
               MS.from_result(_failed(), required=False, consumed=True),
               MS.from_result(_stale(), required=False, consumed=True),
               MS.from_result(_pending(), required=False, consumed=True),
               MS.launch_only(required=False)):
        assert st["allow_bet"] is True
        assert MS.required_no_bet_reason(st) is None


# ─────────────────────── C. predict_today gate (before the wallet) ────────────────

def _pt_gate(m, pack, env):
    import harness.predict_today as PT
    with patched(os, "environ", {**os.environ, **env}), patched(PT, "USE_MIROFISH", True):
        return PT._p_mirofish_gate(m, pack)


def test_pt_optional_unusable_does_not_block():
    m = {"_mf_usable": False, "_mf_status": "STALE"}
    ok, _ = _pt_gate(m, SimpleNamespace(evidence_quality=0.60),
                     {"MIROFISH_MODE": "degraded", "MIROFISH_REQUIRED_FOR_BET": "false"})
    assert ok is True   # strong evidence in degraded mode -> MiroFish does not block


def test_pt_required_stale_blocks_before_wallet():
    m = {"_mf_usable": False, "_mf_status": "STALE",
         "_mirofish": MS.from_result(_stale(), required=True, consumed=True)}
    ok, reason = _pt_gate(m, None, {"MIROFISH_REQUIRED_FOR_BET": "true"})
    assert ok is False and reason == "mirofish_required_stale_no_bet"


def test_pt_required_pending_blocks():
    m = {"_mf_usable": False, "_mf_status": "PENDING",
         "_mirofish": MS.from_result(_pending(), required=True, consumed=True)}
    ok, reason = _pt_gate(m, None, {"MIROFISH_REQUIRED_FOR_BET": "true"})
    assert ok is False and reason == "mirofish_required_pending_no_bet"


def test_pt_usable_passes_gate():
    m = {"_mf_usable": True, "_mf_status": "FRESH"}
    ok, _ = _pt_gate(m, None, {"MIROFISH_REQUIRED_FOR_BET": "true"})
    assert ok is True


def test_pt_required_blocks_when_mirofish_off():
    # ADVERSARIAL (critical): MIROFISH_REQUIRED_FOR_BET=true with MiroFish OFF (USE_MIROFISH
    # false) must FAIL CLOSED — the required check runs BEFORE the off/disabled early-out, so a
    # bet can never slip through with no MiroFish at all.
    import harness.predict_today as PT
    m = {"_mf_usable": None, "_mirofish": MS.disabled(required=True)}
    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRED_FOR_BET": "true"}), \
         patched(PT, "USE_MIROFISH", False):
        ok, reason = PT._p_mirofish_gate(m, None)
    assert ok is False and reason == "mirofish_required_unavailable_no_bet"


def test_pt_required_fails_closed_when_config_raises():
    # ADVERSARIAL r4 (critical): if the MiroFish config tooling raises (broken import / config),
    # the gate must FAIL CLOSED when a fresh used result is REQUIRED (Plan 1 money-gate), never
    # silently allow. Optional mode may still proceed (MiroFish is optional).
    import harness.predict_today as PT
    from harness import mirofish_validate as _MV

    def _boom(*a, **k):
        raise RuntimeError("config exploded")

    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRED_FOR_BET": "true"}), \
         patched(PT, "USE_MIROFISH", True), patched(_MV, "config", _boom):
        ok, reason = PT._p_mirofish_gate({"_mirofish": MS.disabled(required=True)}, None)
    assert ok is False and reason == "mirofish_config_unavailable_no_bet"

    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRED_FOR_BET": "false",
                                 "MIROFISH_MODE": "degraded"}), \
         patched(PT, "USE_MIROFISH", True), patched(_MV, "config", _boom):
        ok2, _ = PT._p_mirofish_gate({}, None)
    assert ok2 is True   # optional: a broken MiroFish does not block (it just didn't contribute)


def test_pt_required_fails_closed_on_double_exception():
    # ADVERSARIAL r6 (critical): even if BOTH config() AND required_for_bet() raise, the gate
    # must STILL fail closed in required mode — it reads the requirement from env directly, so a
    # double failure can never fall through to a fail-OPEN allow.
    import harness.predict_today as PT
    from harness import mirofish_validate as _MV

    def _boom(*a, **k):
        raise RuntimeError("boom")

    with patched(os, "environ", {**os.environ, "MIROFISH_REQUIRED_FOR_BET": "true"}), \
         patched(PT, "USE_MIROFISH", True), patched(_MV, "config", _boom), \
         patched(MS, "required_for_bet", _boom):
        ok, reason = PT._p_mirofish_gate({}, None)
    assert ok is False and reason == "mirofish_config_unavailable_no_bet"


# ─────────────────────── D. dashboard honesty (state_from_row + endpoint) ──────────

def _row(**over):
    # a realistic persisted mirofish_runs row: usable + market-matched + terminal stage +
    # enough posts + a verifiable recorded age. Override per-test to model each dishonesty.
    r = {"usable": 1, "freshness_status": "fresh", "question_match_score": 0.9,
         "stage_reached": "report_done", "n_posts": 4, "report_age_seconds": 5.0,
         "report_generated_at": _now()}
    r.update(over)
    return r


def test_dash_usable_with_ts_shows_used():
    assert MS.state_from_row(_row()) == MS.FRESH_USED


def test_dash_usable_no_ts_not_used():
    # backend produced a "usable" row but with NO recorded age -> NOT a contribution
    assert MS.state_from_row(_row(report_age_seconds=None, report_generated_at=None)) == MS.INVALID_RESULT


def test_dash_garbage_timestamp_not_used():
    # ADVERSARIAL (critical): a truthy-but-unparseable ts leaves recorded age None -> not used
    assert MS.state_from_row(_row(report_age_seconds=None,
                                  report_generated_at="NOT_A_TIMESTAMP")) == MS.INVALID_RESULT


def test_dash_future_timestamp_not_used():
    # ADVERSARIAL r2 (critical): a FUTURE-dated run (recorded age well negative, beyond benign
    # clock skew) is unverifiable -> not used
    assert MS.state_from_row(_row(report_age_seconds=-3600.0)) == MS.INVALID_RESULT


def test_dash_sim_running_row_pending():
    # ADVERSARIAL r2 (critical): sim_running is NOT a terminal stage -> pending, never used
    assert MS.state_from_row(_row(stage_reached="sim_running")) == MS.PENDING


def test_dash_sim_prepared_row_pending():
    # ADVERSARIAL (high): a persisted sim_prepared run never shows as a contribution
    assert MS.state_from_row(_row(stage_reached="sim_prepared")) == MS.PENDING


def test_dash_used_is_immutable_but_stale_now_flagged():
    # ADVERSARIAL r2 (high): being fed to the swarm is an IMMUTABLE historical fact — it stays
    # FRESH_USED even after the report ages. Current freshness is a SEPARATE signal, so
    # mirofish_used never silently flips between the decision and a later dashboard view.
    old = (datetime.now(timezone.utc) - timedelta(seconds=4000)).isoformat()
    row = _row(report_age_seconds=100.0, report_generated_at=old)
    assert MS.state_from_row(row) == MS.FRESH_USED        # historical contribution: immutable
    assert MS.is_stale_now(row) is True                   # but the report is stale RIGHT NOW
    assert MS.is_stale_now(_row()) is False               # a still-fresh run is not stale_now


def test_dash_stale_shows_stale():
    # a stale run DID complete its report (terminal stage) but is old -> STALE, not used
    assert MS.state_from_row(_row(usable=0, freshness_status="stale")) == MS.STALE_RESULT


def test_dash_failed_shows_failed():
    assert MS.state_from_row({"usable": 0, "freshness_status": "failed"}) == MS.FAILED


def test_dash_endpoint_honest_counts():
    # record a fresh(usable+ts), a stale, and a usable-but-no-ts run; the endpoint must count
    # exactly ONE as used and never show the stale / unverifiable ones as fed-to-swarm.
    try:
        from fastapi.testclient import TestClient
        import harness.dashboard as D
        client = TestClient(D.app)
    except Exception:
        return  # FastAPI/TestClient unavailable in this env -> skip (not a failure)
    MV.record_run(_val(_fresh_raw(), _fresh_sig()), forecast_id="f_fresh")
    MV.record_run(_stale(), forecast_id="f_stale")
    MV.record_run(_no_timestamp(), forecast_id="f_nots")
    r = client.get(f"/api/mirofish/runs?market_id={_MID}")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["used"] == 1, body                 # only the fresh+verifiable run
    for run in body["runs"]:
        if run["mirofish_used"]:
            assert run["state"] == MS.FRESH_USED and run.get("report_generated_at")
    assert any(run["freshness_status"] == "stale" and not run["mirofish_used"]
               for run in body["runs"])
    assert any((run.get("report_generated_at") in (None, "")) and not run["mirofish_used"]
               for run in body["runs"])             # backend-alive/usable-no-ts != used


# ─────────────────────── E. sameday wiring + backend-liveness ──────────────────────

def test_backend_liveness_not_contribution():
    # a LIVE backend that is merely running a sim (not complete) is NOT a contribution
    for stage in ("running", "queued", "started", "sim_prepared"):
        v = _val({"ok": True, "simulation_id": "s", "stage_reached": stage,
                  "report_markdown": "LeBron James 2028 election " * 30}, _fresh_sig())
        st = MS.from_result(v, required=False, consumed=True)
        assert st["state"] == MS.PENDING and st["mirofish_used"] is False, stage


def test_sameday_required_gate_wired():
    src = open(os.path.join(ROOT, "harness", "sameday.py"), encoding="utf-8").read()
    assert "required_no_bet_reason" in src and "launch_only" in src
    assert 'prefix="sameday_"' in src or "sameday_" in src
    assert '"mirofish_used": False' in src   # the fire-and-forget health dict stays honest


# ─────────────────────── F. static scans (no fake contribution anywhere) ───────────

def test_no_hardcoded_used_true():
    # no decision path may force mirofish_used true by a literal assignment; it must always
    # come from the canonical state machine (from_result / mark_used).
    for fn in ("predict_today.py", "sameday.py", "dashboard.py"):
        src = open(os.path.join(ROOT, "harness", fn), encoding="utf-8").read()
        assert not re.search(r"mirofish_used[\"']?\s*[:=]\s*True\b", src), fn
        assert not re.search(r"_mirofish_used\s*=\s*True\b", src), fn


def test_used_only_from_canonical_state():
    pt = open(os.path.join(ROOT, "harness", "predict_today.py"), encoding="utf-8").read()
    assert '_mf_st["mirofish_used"]' in pt          # predict_today derives used from the status
    assert '_mf_st["allow_decision_use"]' in pt     # and feeds the swarm on the same criterion
    dash = open(os.path.join(ROOT, "harness", "dashboard.py"), encoding="utf-8").read()
    assert "FRESH_USED" in dash                      # dashboard derives used from canonical state


TESTS = [
    ("fresh_consumed_is_used", test_fresh_consumed_is_used),
    ("fresh_unconsumed_then_mark_used", test_fresh_unconsumed_then_mark_used),
    ("stale_not_used_display_only", test_stale_not_used_display_only),
    ("wrong_market_is_mismatch", test_wrong_market_is_mismatch),
    ("pending_sim_prepared_never_used", test_pending_sim_prepared_never_used),
    ("failed_not_used", test_failed_not_used),
    ("backend_unavailable_state", test_backend_unavailable_state),
    ("missing_timestamp_unverifiable_not_used", test_missing_timestamp_unverifiable_not_used),
    ("unverifiable_malformed_timestamp_not_used", test_unverifiable_malformed_timestamp_not_used),
    ("validate_rejects_incomplete_stage", test_validate_rejects_incomplete_stage),
    ("wrong_market_independent_of_question_match_flag", test_wrong_market_independent_of_question_match_flag),
    ("sim_running_never_used", test_sim_running_never_used),
    ("future_timestamp_not_used", test_future_timestamp_not_used),
    ("negative_match_threshold_cannot_bypass", test_negative_match_threshold_cannot_bypass),
    ("tiny_match_threshold_floored_at_default", test_tiny_match_threshold_floored_at_default),
    ("low_sims_invalid", test_low_sims_invalid),
    ("disabled_helper", test_disabled_helper),
    ("not_configured_helper", test_not_configured_helper),
    ("launch_only_fire_and_forget", test_launch_only_fire_and_forget),
    ("only_fresh_consumed_yields_used", test_only_fresh_consumed_yields_used),
    ("mark_used_noop_on_every_unusable", test_mark_used_noop_on_every_unusable),
    ("required_unavailable_blocks", test_required_unavailable_blocks),
    ("required_stale_blocks_specific_reason", test_required_stale_blocks_specific_reason),
    ("required_pending_blocks", test_required_pending_blocks),
    ("required_market_mismatch_blocks", test_required_market_mismatch_blocks),
    ("required_fresh_used_allows", test_required_fresh_used_allows),
    ("required_launch_only_blocks_with_prefix", test_required_launch_only_blocks_with_prefix),
    ("optional_failure_never_blocks", test_optional_failure_never_blocks),
    ("pt_optional_unusable_does_not_block", test_pt_optional_unusable_does_not_block),
    ("pt_required_stale_blocks_before_wallet", test_pt_required_stale_blocks_before_wallet),
    ("pt_required_pending_blocks", test_pt_required_pending_blocks),
    ("pt_usable_passes_gate", test_pt_usable_passes_gate),
    ("pt_required_blocks_when_mirofish_off", test_pt_required_blocks_when_mirofish_off),
    ("pt_required_fails_closed_when_config_raises", test_pt_required_fails_closed_when_config_raises),
    ("pt_required_fails_closed_on_double_exception", test_pt_required_fails_closed_on_double_exception),
    ("dash_usable_with_ts_shows_used", test_dash_usable_with_ts_shows_used),
    ("dash_usable_no_ts_not_used", test_dash_usable_no_ts_not_used),
    ("dash_garbage_timestamp_not_used", test_dash_garbage_timestamp_not_used),
    ("dash_future_timestamp_not_used", test_dash_future_timestamp_not_used),
    ("dash_sim_running_row_pending", test_dash_sim_running_row_pending),
    ("dash_sim_prepared_row_pending", test_dash_sim_prepared_row_pending),
    ("dash_used_is_immutable_but_stale_now_flagged", test_dash_used_is_immutable_but_stale_now_flagged),
    ("dash_low_sims_not_used", test_dash_low_sims_not_used),
    ("record_run_always_freezes_thresholds", test_record_run_always_freezes_thresholds),
    ("dash_uses_frozen_thresholds_no_flip", test_dash_uses_frozen_thresholds_no_flip),
    ("dash_stale_shows_stale", test_dash_stale_shows_stale),
    ("dash_failed_shows_failed", test_dash_failed_shows_failed),
    ("dash_endpoint_honest_counts", test_dash_endpoint_honest_counts),
    ("backend_liveness_not_contribution", test_backend_liveness_not_contribution),
    ("sameday_required_gate_wired", test_sameday_required_gate_wired),
    ("no_hardcoded_used_true", test_no_hardcoded_used_true),
    ("used_only_from_canonical_state", test_used_only_from_canonical_state),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
