"""Tests for the external-brain interface (harness.brain) — no real LLM / network.
Uses the mock + disabled providers and the strict JSON parser."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_brain_")

from harness import brain                              # noqa: E402
from harness.brain import (EvidencePack, ForecastResult,  # noqa: E402
                           get_provider, available_providers)
from harness.brain import providers as P               # noqa: E402


def _pack(market_p=0.50, eq=0.6, label="opinion", liq=20000):
    return EvidencePack(market_id="0xabc1234567", question="Will X win the 2032 election?",
                        market_p=market_p, evidence_quality=eq, classifier_label=label,
                        liquidity=liq, theme="elections")


def test_factory_returns_providers():
    assert isinstance(get_provider("mock"), P.MockBrainProvider)
    assert isinstance(get_provider("disabled"), P.DisabledBrainProvider)
    assert isinstance(get_provider("manus"), P.ManusProvider)
    assert isinstance(get_provider("swarm"), P.SwarmBrainProvider)
    # unknown -> disabled (observe-only), never crash
    assert isinstance(get_provider("nope"), P.DisabledBrainProvider)
    assert set(available_providers()) >= {"swarm", "mock", "disabled", "manus"}


def test_mock_forecast_and_edge_action():
    m = P.MockBrainProvider()
    r = m.forecast_market(_pack(market_p=0.50, eq=1.0))
    assert r.probability is not None and 0.0 <= r.probability <= 1.0
    assert r.recommended_action in ("bet", "observe", "skip")
    # a fixed probability with a big edge -> 'bet'
    r2 = P.MockBrainProvider(fixed_probability=0.80).forecast_market(_pack(market_p=0.50))
    assert r2.probability == 0.80 and r2.recommended_action == "bet"
    # tiny edge -> observe
    r3 = P.MockBrainProvider(fixed_probability=0.505).forecast_market(_pack(market_p=0.50))
    assert r3.recommended_action == "observe"


def test_disabled_is_observe_only():
    d = P.DisabledBrainProvider()
    r = d.forecast_market(_pack())
    assert r.probability is None and r.ok is False and r.recommended_action == "observe"
    assert d.health_check().ok is True   # disabled is a valid (intentional) state


def test_forecast_result_strict_parsing():
    # well-formed
    r = ForecastResult.from_provider_json({"probability": 0.62, "confidence": 0.7,
                                           "recommended_action": "bet"}, "x")
    assert r.probability == 0.62 and r.recommended_action == "bet"
    # percentage string
    assert ForecastResult.from_provider_json({"probability": "62%"}, "x").probability == 0.62
    # out of range clamps; non-dict -> observe-only, ok False
    assert ForecastResult.from_provider_json({"probability": 8}, "x").probability == 1.0
    bad = ForecastResult.from_provider_json("garbage", "x")
    assert bad.probability is None and bad.ok is False and bad.recommended_action == "observe"
    # no probability can never be a 'bet'
    nob = ForecastResult(probability=None, recommended_action="bet")
    assert nob.recommended_action == "observe"


def test_non_llm_critic():
    prov = P.MockBrainProvider()
    # no probability -> high severity, skip
    c = prov.critique_forecast(_pack(), ForecastResult(probability=None))
    assert c.passed is False and c.severity == "high" and c.recommended_action == "skip"
    # mechanical market -> high severity
    c2 = prov.critique_forecast(_pack(label="mechanical"), ForecastResult(probability=0.7))
    assert c2.passed is False and "not_opinion_market" in c2.risk_flags
    # thin liquidity + low evidence -> flagged, medium
    c3 = prov.critique_forecast(_pack(eq=0.1, liq=500), ForecastResult(probability=0.7))
    assert "low_evidence_quality" in c3.risk_flags and "thin_liquidity" in c3.risk_flags


def test_manus_unconfigured_is_observe_only():
    # no MANUS_API_BASE -> observe-only, no network call, never crash
    os.environ.pop("MANUS_API_BASE", None)
    r = P.ManusProvider().forecast_market(_pack())
    assert r.probability is None and r.ok is False and "unconfigured" in (r.error or "").lower() + " ".join(r.risk_flags)
    assert P.ManusProvider().health_check().ok is False


def test_results_are_json_serializable():
    r = P.MockBrainProvider().forecast_market(_pack())
    assert json.loads(json.dumps(r.to_dict()))["provider"] == "mock"
    h = P.MockBrainProvider().health_check()
    assert json.loads(json.dumps(h.to_dict()))["ok"] is True
    assert json.loads(json.dumps(_pack().to_dict()))["market_id"] == "0xabc1234567"


def test_observe_only_mode_without_llm():
    # The acceptance bar: with the brain disabled, the system can still produce a
    # (no-bet) decision without any LLM — it never depends on a working brain.
    os.environ["BRAIN_PROVIDER"] = "disabled"
    try:
        r = get_provider().forecast_market(_pack())
        assert r.recommended_action == "observe" and r.probability is None
    finally:
        os.environ.pop("BRAIN_PROVIDER", None)


TESTS = [
    ("factory_returns_providers", test_factory_returns_providers),
    ("mock_forecast_and_edge_action", test_mock_forecast_and_edge_action),
    ("disabled_is_observe_only", test_disabled_is_observe_only),
    ("forecast_result_strict_parsing", test_forecast_result_strict_parsing),
    ("non_llm_critic", test_non_llm_critic),
    ("manus_unconfigured_is_observe_only", test_manus_unconfigured_is_observe_only),
    ("results_are_json_serializable", test_results_are_json_serializable),
    ("observe_only_mode_without_llm", test_observe_only_mode_without_llm),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
