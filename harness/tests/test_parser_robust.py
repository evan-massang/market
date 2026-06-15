"""AUDIT fix — LLM forecast parser robustness (no crash / no fake 0.99).

Regression for two confirmed audit findings:
  * core/agent.py:235 bare float(data["probability"]) raised on missing-key /
    "60%" / null / prose / non-dict replies, aborting the WHOLE forecast.
  * harness/challenger.py prose fallback turned "60 percent" into 0.99.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_parser_")

from core.agent import _coerce_prob  # noqa: E402


def test_coerce_numeric_and_clamp():
    assert _coerce_prob(0.6) == 0.6
    assert _coerce_prob(8) == 1.0          # out-of-range numeric clamps (prior behavior)
    assert _coerce_prob(-1) == 0.0
    assert _coerce_prob(0) == 0.0


def test_coerce_percentage_and_prose():
    assert abs(_coerce_prob("60%") - 0.60) < 1e-9
    assert abs(_coerce_prob("60 percent") - 0.60) < 1e-9
    assert abs(_coerce_prob("My estimate: 75%") - 0.75) < 1e-9
    assert abs(_coerce_prob("about 0.6") - 0.6) < 1e-9
    assert abs(_coerce_prob("0.30") - 0.30) < 1e-9


def test_coerce_unparseable_returns_default():
    assert _coerce_prob(None) is None
    assert _coerce_prob("high") is None          # no digits
    assert _coerce_prob("") is None
    assert _coerce_prob(float("nan")) is None
    assert _coerce_prob("high", default=0.6) == 0.6


def _agent():
    import core.agent as A
    return A.Agent(agent_id="t", persona="Analyst", description="d",
                   information_focus="f", bias_profile="b")


def test_agent_estimate_does_not_raise_on_malformed():
    # Drive the real Agent.estimate parser path with malformed _call_llm outputs and
    # assert: a usable reply yields a clamped prob; a junk reply raises a CLEAN
    # ValueError (so the swarm skips just that agent) — never a KeyError/TypeError.
    import core.agent as A
    cases_ok = ['{"probability": 0.6}', '{"probability": "60%"}', '{"probability": 8}',
                '0.6', '[0.6]', 'I estimate about 0.7']
    cases_bad = ['{"confidence": 0.7}', '{"probability": null}', 'totally unparseable',
                 '{"probability": "high"}']

    for raw in cases_ok:
        old = A._call_llm
        A._call_llm = (lambda r: (lambda *a, **k: r))(raw)
        try:
            est = _agent().estimate(question="q", context="", debate_round=1)
            assert 0.0 <= est.probability <= 1.0, (raw, est.probability)
        finally:
            A._call_llm = old

    for raw in cases_bad:
        old = A._call_llm
        A._call_llm = (lambda r: (lambda *a, **k: r))(raw)
        try:
            raised = None
            try:
                _agent().estimate(question="q", context="", debate_round=1)
            except ValueError:
                raised = "ValueError"
            except Exception as e:
                raised = type(e).__name__
            assert raised == "ValueError", (raw, raised)   # clean, expected error only
        finally:
            A._call_llm = old


def test_challenger_prose_not_forced_to_099():
    import harness.challenger as C
    # no-JSON prose replies: "60 percent" must become ~0.6, never 0.99
    for raw, expect in [("I estimate around 60 percent", 0.60), ("My estimate: 75%", 0.75),
                        ("probability is 0.6", 0.6), ("roughly 0.30 chance", 0.30)]:
        old = C._local_raw, C._hosted_configured
        C._hosted_configured = lambda: False
        C._local_raw = (lambda r: (lambda system, user, model=None: r))(raw)
        try:
            p = C.single_llm_forecast("q")
            assert p is not None and abs(p - expect) < 0.02, (raw, p, expect)
        finally:
            C._local_raw, C._hosted_configured = old


TESTS = [
    ("coerce_numeric_and_clamp", test_coerce_numeric_and_clamp),
    ("coerce_percentage_and_prose", test_coerce_percentage_and_prose),
    ("coerce_unparseable_returns_default", test_coerce_unparseable_returns_default),
    ("agent_estimate_does_not_raise_on_malformed", test_agent_estimate_does_not_raise_on_malformed),
    ("challenger_prose_not_forced_to_099", test_challenger_prose_not_forced_to_099),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
