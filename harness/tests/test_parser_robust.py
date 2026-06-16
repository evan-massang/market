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


# NOTE (Plan 7): _coerce_prob is the LEGACY lenient helper. It is NO LONGER on any
# tradeable path (Agent.estimate + challenger now use the strict
# core.probability_parser). These three tests document its retained legacy behavior;
# the strict-contract tests below cover the tradeable path.
def test_coerce_numeric_and_clamp():
    assert _coerce_prob(0.6) == 0.6
    assert _coerce_prob(8) == 1.0          # legacy clamp (helper only; not tradeable)
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


def test_agent_estimate_strict_parsing():
    # Plan 7: Agent.estimate uses the STRICT parser. A valid reply yields a tradable
    # prob in [0.01,0.99] with parse_ok; an out-of-range / bare-number / no-wording /
    # malformed reply raises a CLEAN ValueError (so the swarm counts it as a parse
    # failure) — never a fabricated/clamped probability, never KeyError/TypeError.
    import core.agent as A
    cases_ok = ['{"probability": 0.6}', '{"probability": "60%"}', '{"probability_percent": 60}',
                '{"probability": 60, "unit": "percent"}', '{"p_yes": 0.6}', 'probability is 0.6']
    cases_bad = ['{"probability": 8}',          # out of range -> rejected, NOT clamped
                 '{"probability": 2026}',        # year
                 '0.6', '[0.6]',                 # bare number, no probability wording
                 'I estimate about 0.7',         # prose, no "probability" wording
                 '{"confidence": 0.7}', '{"probability": null}',
                 'totally unparseable', '{"probability": "high"}']

    for raw in cases_ok:
        old = A._call_llm
        A._call_llm = (lambda r: (lambda *a, **k: r))(raw)
        try:
            est = _agent().estimate(question="q", context="", debate_round=1)
            assert 0.01 <= est.probability <= 0.99, (raw, est.probability)
            assert est.parse_ok is True, (raw, est.parse_reason)
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
            assert raised == "ValueError", (raw, raised)   # clean parse-failure only
        finally:
            A._call_llm = old


def test_challenger_strict_prose():
    import harness.challenger as C
    # Plan 7: only a number attached to probability wording is accepted; prose WITHOUT
    # probability wording ("60 percent", "75%", "0.30 chance", dates) returns None
    # (a skipped vote), never a fabricated/clamped value.
    accept = [("probability is 0.6", 0.6), ('{"probability": 0.75}', 0.75)]
    reject = ["I estimate around 60 percent", "My estimate: 75%", "roughly 0.30 chance",
              "June 2026", "the market resolves in 2026"]
    for raw, expect in accept:
        old = C._local_raw, C._hosted_configured
        C._hosted_configured = lambda: False
        C._local_raw = (lambda r: (lambda system, user, model=None: r))(raw)
        try:
            p = C.single_llm_forecast("q")
            assert p is not None and abs(p - expect) < 0.02, (raw, p, expect)
        finally:
            C._local_raw, C._hosted_configured = old
    for raw in reject:
        old = C._local_raw, C._hosted_configured
        C._hosted_configured = lambda: False
        C._local_raw = (lambda r: (lambda system, user, model=None: r))(raw)
        try:
            assert C.single_llm_forecast("q") is None, (raw, "should reject")
        finally:
            C._local_raw, C._hosted_configured = old


TESTS = [
    ("coerce_numeric_and_clamp", test_coerce_numeric_and_clamp),
    ("coerce_percentage_and_prose", test_coerce_percentage_and_prose),
    ("coerce_unparseable_returns_default", test_coerce_unparseable_returns_default),
    ("agent_estimate_strict_parsing", test_agent_estimate_strict_parsing),
    ("challenger_strict_prose", test_challenger_strict_prose),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
