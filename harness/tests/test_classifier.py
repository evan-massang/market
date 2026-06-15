"""Unit tests for harness.classifier.tag_market (+ liquidity gate).

PURE: no DB, no network, no LLM (use_llm defaults False; we never pass True).
Covers the rule engine, boilerplate stripping, degenerate inputs, dict/str
coercion, and the liquidity-floor / should_forecast gates.
Run:  python -m harness.tests.test_classifier
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_classifier_")  # uniform isolation (classifier touches no DB/obs)

from harness.classifier import (  # noqa: E402
    tag_market, passes_liquidity_floor, should_forecast, Classification,
)


def test_mechanical_clear():
    c = tag_market("Will Bitcoin close above $100,000?")
    assert c.label == "mechanical", c
    assert c.mechanical_score > 0, c
    assert c.signals, "expected fired signals"


def test_opinion_election():
    c = tag_market("Will the Republicans win control of the Senate?")
    assert c.label == "opinion", c
    assert c.opinion_score >= 4, c


def test_boilerplate_stripped_to_unknown():
    # Question has NO signals; description is ONLY resolution boilerplate.
    # _strip_boilerplate must neutralize 'primary resolution source' so the
    # political 'primary' signal does NOT fire -> unknown.
    m = {"question": "Will the thing occur next week?",
         "description": "This will be the primary resolution source for the market."}
    c = tag_market(m)
    assert c.label == "unknown", c
    assert c.opinion_score == 0 and c.mechanical_score == 0, c


def test_empty_inputs_degenerate():
    for empty in ("", {}):
        c = tag_market(empty)
        assert c.label == "unknown", (empty, c)
        assert c.confidence == 0.0, c
        assert c.ambiguous is True, c
        assert isinstance(c, Classification)


def test_dict_with_str_numerics_does_not_raise():
    c = tag_market({"question": "Will X happen?", "volume": "5", "liquidity": "0"})
    assert c.label in ("opinion", "mechanical", "unknown"), c


def test_liquidity_floor_str_values():
    # string numerics exercise _market_floats coercion
    liquid = {"volume": "250000", "liquidity": "40000"}
    thin = {"volume": "300", "liquidity": "50"}
    assert passes_liquidity_floor(liquid) is True
    assert passes_liquidity_floor(thin) is False


def test_should_forecast_gate():
    liquid_opinion = {"question": "Will the Republicans win the Senate?",
                      "volume": "250000", "liquidity": "40000"}
    thin_opinion = {"question": "Will candidate Z win the primary?",
                    "volume": "300", "liquidity": "50"}
    mech_liquid = {"question": "Will Bitcoin close above $100k?",
                   "volume": "999999", "liquidity": "99999"}
    assert should_forecast(liquid_opinion)[0] is True
    assert should_forecast(thin_opinion)[0] is False        # opinion but too thin
    assert should_forecast(mech_liquid)[0] is False         # liquid but mechanical


def test_approval_polling_is_opinion():
    c = tag_market("Will Trump's approval rating be above 45% in March?")
    assert c.label == "opinion", c   # approval weight-4 beats the numeric threshold


TESTS = [
    ("mechanical_clear", test_mechanical_clear),
    ("opinion_election", test_opinion_election),
    ("boilerplate_stripped_to_unknown", test_boilerplate_stripped_to_unknown),
    ("empty_inputs_degenerate", test_empty_inputs_degenerate),
    ("dict_with_str_numerics_does_not_raise", test_dict_with_str_numerics_does_not_raise),
    ("liquidity_floor_str_values", test_liquidity_floor_str_values),
    ("should_forecast_gate", test_should_forecast_gate),
    ("approval_polling_is_opinion", test_approval_polling_is_opinion),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
