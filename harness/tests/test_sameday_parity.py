"""Plan 5 — SAME-DAY PARITY with predict_today. Same-day must build evidence BEFORE
forecasting, pass it to swarm + challenger, run the full EV penalties (m+confidence),
apply a stale/quality filter, journal EVERY no-bet, and be honest that MiroFish is
launch-only.

NO network, NO real LLM. place_sameday is driven offline via patched
gamma/classifier/scanner/build_pack/_ai_scout/gates. Temp DB only.
Run: python -m harness.tests.test_sameday_parity
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_sameday_parity_")
os.environ["LLM_PROVIDER"] = "ollama"

from harness import sameday as SD          # noqa: E402
from harness import predict_today as PT    # noqa: E402
from harness import wallet as W            # noqa: E402
from harness import journal                # noqa: E402
import harness.loop as LP                  # noqa: E402

_MKT = {"market_id": "0xsd01", "question": "Will the incumbent win the 2032 election?",
        "outcomes": ["Yes", "No"], "outcome_prices": [0.50, 0.50], "volume": 200_000.0,
        "liquidity": 40_000.0, "end_date": "2099-01-01T00:00:00Z", "event_slug": None, "raw": {}}

_HEALTHY_HEALTH = {"allow_bet": True, "aborted": False, "degraded": False, "n_agents_succeeded": 5,
                   "n_agents_requested": 5, "method": "swarm", "consensus": 0.8,
                   "consensus_status": "ok", "mirofish_used": False, "evidence_used": True}
_HEALTHY_SCOUT = (0.55, 0.54, 0.8, 0.55, _HEALTHY_HEALTH)   # p, bp, cons, final_p, health


def _reset():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions", "decisions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(1000.0)
    journal.init_journal()


def _decisions(reason_substr=None):
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM decisions WHERE status='no_bet'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    if reason_substr is None:
        return rows
    return [r for r in rows if reason_substr in (r["why"] or "")]


def _fake_pack(kind="ok"):
    if kind == "no_data":
        return NS(text="", evidence_quality=0.0, content_hash="h", n_sources=0, total_items=0, sources=[], to_dict=lambda: {})
    if kind == "low":
        return NS(text="thin", evidence_quality=0.05, content_hash="h", n_sources=1, total_items=1, sources=[], to_dict=lambda: {})
    return NS(text="EVID-PACK-XYZ", evidence_quality=0.8, content_hash="h", n_sources=3, total_items=8, sources=[], to_dict=lambda: {})


def _drive(*, scout="healthy", is_stale=(False, "ok"), tag="opinion", build_pack="ok",
           ev=(True, "positive_ev_after_costs"), risk=(True, "ok"), bankroll=(True, "ok"),
           exposure=(True, "ok"), swarm_health=(True, "ok"), wallet_open="real",
           market=None, scout_raises=False):
    """Drive place_sameday over ONE market with controllable gates. Returns (decisions, cap)."""
    _reset()
    cap = {}
    mkt = market or dict(_MKT)

    def _scout_fn(m, price, evidence_text=""):
        cap["scout_evidence"] = evidence_text
        cap["scout_called"] = True
        if scout_raises:
            raise AssertionError("_ai_scout called when it must not (market should be skipped earlier)")
        if scout == "healthy":
            return _HEALTHY_SCOUT
        if scout == "none":
            return (None, None, None, None, None)
        return scout

    def _build_pack_fn(m, cfg):
        if build_pack == "raise":
            raise RuntimeError("simulated evidence build failure")
        return _fake_pack(build_pack)

    def _is_stale_fn(m):
        if is_stale == "raise":
            raise RuntimeError("simulated staleness eval failure")
        return is_stale

    def _ev_fn(model_p, market_p, side, m=None, confidence=None):
        cap["ev_m"] = m
        cap["ev_conf"] = confidence
        return ev

    def _wallet_open(*a, **k):
        cap["wallet_open_called"] = True
        if wallet_open == "reject":
            return W.FillResult(False, "wallet_insufficient_cash")
        return W.FillResult(True, "filled", 1, a[2], 0.51, round(a[6] / 0.51, 4), a[6], 0.0)

    stack = [
        patched(SD.gamma, "fetch_markets_ending_within", lambda h, limit=150: [dict(mkt)]),
        patched(SD, "_hours_left", lambda ed: 6.0),
        patched(SD.gamma, "yes_price", lambda m: 0.50),
        patched(SD.classifier, "tag_market", lambda m, use_llm=False: NS(label=tag, signals=None)),
        patched(SD.scanner, "is_stale", _is_stale_fn),
        patched(LP, "build_pack", _build_pack_fn),
        patched(SD, "_ai_scout", _scout_fn),
        patched(PT, "_observe_only_for", lambda q: (None, False)),
        patched(PT, "_p7_experiment_tag", lambda: ""),
        patched(PT, "build_event_legs", lambda m, fp, pr, legs: (False, [], None)),
        patched(PT, "_conviction", lambda *a, **k: 0.5),
        patched(PT, "_conviction_sizing", lambda c: (0.5, 0.5)),
        patched(PT, "_p7_adaptive_min_edge", lambda q, me: me),
        patched(SD.sizing, "size_bet", lambda *a, **k: NS(side="YES", stake=20.0, edge=0.05, reason="edge 5%")),
        patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": swarm_health),
        patched(PT, "_p8_risk_guards", lambda m, side, q: risk),
        patched(PT, "_p9_can_trade", lambda: bankroll),
        patched(PT, "_p9_exposure_ok", lambda q, ev, stake: exposure),
        patched(SD.wallet, "open_position", _wallet_open),
        patched(SD.subprocess, "Popen", lambda *a, **k: None),
    ]
    if ev != "real":
        stack.append(patched(PT, "_p7_ev_gate", _ev_fn))
    with contextlib.ExitStack() as es:
        for s in stack:
            es.enter_context(s)
        SD.place_sameday(max_new=6, use_ai=True)
    return _decisions(), cap


# ════════════════════════════════════════════════════════════════════════════
# (1-3) evidence built BEFORE forecast, passed to swarm + challenger
# ════════════════════════════════════════════════════════════════════════════
def test_ai_scout_passes_evidence_to_swarm_and_challenger():
    _reset()
    cap = {}

    class _FakeSwarm:
        def __init__(self, agents=None):
            pass

        def forecast(self, question, market_odds=None, market_id=None, extra_context=""):
            cap["swarm_ctx"] = extra_context
            return {"probability": 0.6, "consensus_score": 0.8, "allow_bet": True, "aborted": False,
                    "degraded": False, "method": "swarm", "n_agents_requested": 5, "n_agents_succeeded": 5,
                    "n_agents_failed": 0, "consensus_status": "ok", "degradation_reason": None}

    def _fake_ensemble(q, price, ctx="", models=None):
        cap["chal_ctx"] = ctx
        return {"mean": 0.58, "models": ["m"], "probs": [0.58], "n": 1}

    import core.swarm as CS
    with patched(CS, "Swarm", _FakeSwarm), \
         patched(SD.challenger, "ensemble_forecast", _fake_ensemble), \
         patched(SD.challenger, "save_baseline", lambda *a, **k: None), \
         patched(SD, "_record_forecast_version", lambda *a, **k: None), \
         patched(SD.subprocess, "Popen", lambda *a, **k: None), \
         patched(SD.forecaster_weights_mod, "forecaster_weights", lambda: {"swarm": 1.0, "challenger": 0.0}), \
         patched(SD.forecaster_weights_mod, "blend_forecasters", lambda sp, bp, w: sp), \
         patched(SD.calibration_apply, "apply_calibration", lambda b: {"calibrated_p": b, "method": "none", "n_history": 0}):
        sp, bp, cons, final_p, health = SD._ai_scout(_MKT, 0.50, evidence_text="EVIDENCE-XYZ-123")
    assert cap["swarm_ctx"] == "EVIDENCE-XYZ-123", cap          # (2) swarm saw evidence
    assert cap["chal_ctx"] == "EVIDENCE-XYZ-123", cap           # (3) challenger saw evidence
    assert health["evidence_used"] is True and health["mirofish_used"] is False


def test_evidence_is_built_before_scout():
    # (1) the pack text reaches _ai_scout (i.e. it was built before the forecast)
    _, cap = _drive()
    assert cap.get("scout_evidence") == "EVID-PACK-XYZ", cap


# ════════════════════════════════════════════════════════════════════════════
# (4-5) evidence build failure / low quality block the bet (journaled)
# ════════════════════════════════════════════════════════════════════════════
def test_evidence_build_error_blocks_and_does_not_forecast():
    dec, cap = _drive(build_pack="raise", scout_raises=True)
    assert _has(dec, "sameday_evidence_build_error_no_bet")
    assert not cap.get("scout_called"), "must NOT forecast blind on a build error"


def test_no_evidence_blocks():
    dec, _ = _drive(build_pack="no_data")
    assert _has(dec, "sameday_no_evidence_no_bet")


def test_low_evidence_quality_blocks():
    dec, _ = _drive(build_pack="low")
    assert _has(dec, "sameday_low_evidence_quality_no_bet")


# ════════════════════════════════════════════════════════════════════════════
# (6-9) EV parity — m + confidence passed; penalties can flip EV negative
# ════════════════════════════════════════════════════════════════════════════
def test_ev_gate_receives_market_and_confidence():
    _, cap = _drive()                       # default _ev_fn captures the args
    assert isinstance(cap.get("ev_m"), dict) and cap["ev_m"].get("market_id") == "0xsd01", cap
    assert cap.get("ev_conf") == 0.8, cap    # the swarm consensus is passed as confidence


def test_ev_blocks_after_spread_penalty_with_real_gate():
    # a +0.05 raw edge passes the sizer but a 12c spread penalty flips after-cost EV negative.
    # WITHOUT m (the old same-day bug) this would have passed; WITH m it is correctly rejected.
    mkt = dict(_MKT, raw={"spread": 0.12})
    dec, cap = _drive(market=mkt, ev="real", scout=(0.55, 0.54, 0.8, 0.55, _HEALTHY_HEALTH))
    assert _has(dec, "neg_ev_after_costs"), [r["why"] for r in dec]
    assert W.get_open_positions() == [], "a negative-after-cost EV bet must not open"


# ════════════════════════════════════════════════════════════════════════════
# (10-12) stale / market-quality parity (journaled, before forecast)
# ════════════════════════════════════════════════════════════════════════════
def test_stale_market_skipped_before_forecast():
    dec, cap = _drive(is_stale=(True, "stale_price"), scout_raises=True)
    assert _has(dec, "sameday_stale_market_no_bet")
    assert not cap.get("scout_called"), "stale market must be skipped BEFORE the forecast"


def test_unknown_market_quality_blocks():
    dec, cap = _drive(is_stale="raise", scout_raises=True)
    assert _has(dec, "sameday_market_quality_unknown_no_bet")
    assert not cap.get("scout_called")


# ════════════════════════════════════════════════════════════════════════════
# (13-21) every no-bet branch is JOURNALED (not only printed/obs)
# ════════════════════════════════════════════════════════════════════════════
def test_divergence_skip_journaled():
    dec, _ = _drive(scout=(0.80, 0.20, 0.8, 0.80, _HEALTHY_HEALTH))   # |0.80-0.20| huge
    assert _has(dec, "sameday_divergence_no_bet")


def test_consensus_skip_journaled():
    h = dict(_HEALTHY_HEALTH)
    dec, _ = _drive(scout=(0.55, 0.54, 0.10, 0.55, h))               # consensus 0.10 < min
    assert _has(dec, "sameday_consensus_no_bet")


def test_swarm_health_skip_journaled():
    dec, _ = _drive(swarm_health=(False, "sameday_swarm_aborted_no_bet"))
    assert _has(dec, "sameday_swarm_aborted_no_bet")


def test_ev_skip_journaled():
    dec, _ = _drive(ev=(False, "neg_ev_after_costs"))
    assert _has(dec, "neg_ev_after_costs")


def test_risk_skip_journaled():
    dec, _ = _drive(risk=(False, "risk_guards_error_fail_closed"))
    assert _has(dec, "risk_guards_error_fail_closed")


def test_bankroll_skip_journaled():
    dec, _ = _drive(bankroll=(False, "bankroll_error_fail_closed"))
    assert _has(dec, "bankroll_error_fail_closed")


def test_exposure_skip_journaled():
    dec, _ = _drive(exposure=(False, "exposure_error_fail_closed"))
    assert _has(dec, "exposure_error_fail_closed")


def test_wallet_rejection_journaled():
    dec, _ = _drive(wallet_open="reject")
    assert _has(dec, "sameday_wallet_rejected_no_bet")


def test_mechanical_skip_not_scouted():
    # mechanical/classifier skip: no forecast, recorded via obs.on_classify (parity with
    # predict_today's loop), NOT journaled to decisions (avoid flooding each cycle).
    classify_calls = []
    _reset()
    with patched(SD.gamma, "fetch_markets_ending_within", lambda h, limit=150: [dict(_MKT)]), \
         patched(SD, "_hours_left", lambda ed: 6.0), \
         patched(SD.gamma, "yes_price", lambda m: 0.50), \
         patched(SD.classifier, "tag_market", lambda m, use_llm=False: NS(label="sports", signals=None)), \
         patched(SD.obs.hooks, "on_classify", lambda *a, **k: classify_calls.append(a)), \
         patched(SD, "_ai_scout", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scouted a mechanical market!"))):
        SD.place_sameday(max_new=6, use_ai=True)
    assert classify_calls, "mechanical skip must be recorded via obs.on_classify"
    assert _decisions() == [], "mechanical skip should not flood the decisions journal"


# ════════════════════════════════════════════════════════════════════════════
# (22-23) MiroFish honesty — launch-only, never claimed used
# ════════════════════════════════════════════════════════════════════════════
def test_mirofish_marked_not_used():
    _, cap = _drive()
    # the scout's health dict (built in _ai_scout) marks mirofish_used False; the source
    # also emits 'mirofish_launched_not_used'. Here we assert the honest flag on the result.
    sd_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                               "harness", "sameday.py"), encoding="utf-8").read()
    assert "mirofish_launched_not_used" in sd_src
    assert '"mirofish_used": False' in sd_src
    # and a healthy decision never claims MiroFish contributed
    assert "mirofish_used" not in "".join(r["why"] or "" for r in _decisions())


# ════════════════════════════════════════════════════════════════════════════
# (24-25) healthy path still reaches the (atomic) wallet open after ALL gates pass
# ════════════════════════════════════════════════════════════════════════════
def test_healthy_path_reaches_wallet_open():
    dec, cap = _drive()                      # everything allows
    assert cap.get("wallet_open_called") is True, "healthy same-day path must reach wallet.open_position"


def test_healthy_path_real_wallet_opens_one_position():
    # let the REAL atomic wallet.open_position run (Plan 4) after the full gate stack passes
    _reset()
    cap = {}
    with contextlib.ExitStack() as es:
        for s in [
            patched(SD.gamma, "fetch_markets_ending_within", lambda h, limit=150: [dict(_MKT)]),
            patched(SD, "_hours_left", lambda ed: 6.0),
            patched(SD.gamma, "yes_price", lambda m: 0.50),
            patched(SD.classifier, "tag_market", lambda m, use_llm=False: NS(label="opinion", signals=None)),
            patched(SD.scanner, "is_stale", lambda m: (False, "ok")),
            patched(LP, "build_pack", lambda m, cfg: _fake_pack("ok")),
            patched(SD, "_ai_scout", lambda m, price, evidence_text="": _HEALTHY_SCOUT),
            patched(PT, "_observe_only_for", lambda q: (None, False)),
            patched(PT, "_p7_experiment_tag", lambda: ""),
            patched(PT, "build_event_legs", lambda m, fp, pr, legs: (False, [], None)),
            patched(PT, "_conviction", lambda *a, **k: 0.5),
            patched(PT, "_conviction_sizing", lambda c: (0.5, 0.5)),
            patched(PT, "_p7_adaptive_min_edge", lambda q, me: me),
            patched(SD.sizing, "size_bet", lambda *a, **k: NS(side="YES", stake=20.0, edge=0.05, reason="edge 5%")),
            patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": (True, "ok")),
            patched(PT, "_p7_ev_gate", lambda *a, **k: (True, "positive_ev_after_costs")),
            patched(PT, "_p8_risk_guards", lambda m, side, q: (True, "ok")),
            patched(PT, "_p9_can_trade", lambda: (True, "ok")),
            patched(PT, "_p9_exposure_ok", lambda q, ev, stake: (True, "ok")),
            patched(SD.subprocess, "Popen", lambda *a, **k: None),
        ]:
            es.enter_context(s)
        SD.place_sameday(max_new=6, use_ai=True)
    assert len(W.get_open_positions()) == 1, "healthy same-day path should open exactly one position"


def _has(decisions, substr):
    return any(substr in (r["why"] or "") for r in decisions)


TESTS = [
    ("ai_scout_passes_evidence_to_swarm_and_challenger", test_ai_scout_passes_evidence_to_swarm_and_challenger),
    ("evidence_is_built_before_scout", test_evidence_is_built_before_scout),
    ("evidence_build_error_blocks_and_does_not_forecast", test_evidence_build_error_blocks_and_does_not_forecast),
    ("no_evidence_blocks", test_no_evidence_blocks),
    ("low_evidence_quality_blocks", test_low_evidence_quality_blocks),
    ("ev_gate_receives_market_and_confidence", test_ev_gate_receives_market_and_confidence),
    ("ev_blocks_after_spread_penalty_with_real_gate", test_ev_blocks_after_spread_penalty_with_real_gate),
    ("stale_market_skipped_before_forecast", test_stale_market_skipped_before_forecast),
    ("unknown_market_quality_blocks", test_unknown_market_quality_blocks),
    ("divergence_skip_journaled", test_divergence_skip_journaled),
    ("consensus_skip_journaled", test_consensus_skip_journaled),
    ("swarm_health_skip_journaled", test_swarm_health_skip_journaled),
    ("ev_skip_journaled", test_ev_skip_journaled),
    ("risk_skip_journaled", test_risk_skip_journaled),
    ("bankroll_skip_journaled", test_bankroll_skip_journaled),
    ("exposure_skip_journaled", test_exposure_skip_journaled),
    ("wallet_rejection_journaled", test_wallet_rejection_journaled),
    ("mechanical_skip_not_scouted", test_mechanical_skip_not_scouted),
    ("mirofish_marked_not_used", test_mirofish_marked_not_used),
    ("healthy_path_reaches_wallet_open", test_healthy_path_reaches_wallet_open),
    ("healthy_path_real_wallet_opens_one_position", test_healthy_path_real_wallet_opens_one_position),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
