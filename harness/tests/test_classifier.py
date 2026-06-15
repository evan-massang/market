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


# ── P4 richer labels: fine_label + reason_code ───────────────────────────────
# fine_label -> expected legacy 3-way label (the backward-compat contract)
_FINE_TO_LABEL = {
    "opinion_forecastable":   "opinion",
    "opinion_unforecastable": "mechanical",
    "mechanical_skip":        "mechanical",
    "data_release_skip":      "mechanical",
    "ambiguous_review":       "unknown",
}
_FINE_LABELS = set(_FINE_TO_LABEL)


def test_fine_label_opinion_forecastable():
    c = tag_market("Will the Republicans win control of the Senate?")
    assert c.fine_label == "opinion_forecastable", c
    assert c.reason_code == "elections_kw", c
    assert c.label == "opinion", c            # forecastable -> legacy 'opinion'


def test_fine_label_opinion_unforecastable():
    # opinion-ish (awards/virality) but NO pollable base rate -> unforecastable.
    c = tag_market("Will the movie win Best Picture at the Oscars?")
    assert c.fine_label == "opinion_unforecastable", c
    assert c.reason_code == "awards_virality", c
    assert c.label == "mechanical", c         # unforecastable -> legacy 'mechanical' (skipped)


def test_fine_label_mechanical_skip():
    c = tag_market("Exact Score: Brazil 2 - 1 Argentina?")
    assert c.fine_label == "mechanical_skip", c
    assert c.reason_code == "scoreline", c
    assert c.label == "mechanical", c


def test_fine_label_data_release_skip():
    c = tag_market("Will the Fed cut interest rates at the March FOMC meeting?")
    assert c.fine_label == "data_release_skip", c
    assert c.reason_code == "central_bank", c
    assert c.label == "mechanical", c         # data_release_skip -> legacy 'mechanical' (skipped)

    # a versioned product ship event is also a data release
    c2 = tag_market("Will GPT-6 be released by December 2026?")
    assert c2.fine_label == "data_release_skip", c2
    assert c2.reason_code in ("release_date", "product_version"), c2
    assert c2.label == "mechanical", c2


def test_fine_label_ambiguous_review():
    # no signal -> ambiguous_review / no_signal
    c = tag_market("Will the thing happen next week?")
    assert c.fine_label == "ambiguous_review", c
    assert c.reason_code == "no_signal", c
    assert c.label == "unknown", c
    # exact opinion/mechanical tie -> ambiguous_review / tie
    t = tag_market("Will the most popular crypto coin go viral?")
    assert t.opinion_score == t.mechanical_score > 0, t
    assert t.fine_label == "ambiguous_review", t
    assert t.reason_code == "tie", t
    assert t.label == "unknown", t


def test_reason_code_present_and_confidence_in_range():
    samples = [
        "Will the Republicans win control of the Senate?",
        "Will the movie win Best Picture at the Oscars?",
        "Exact Score: Brazil 2 - 1 Argentina?",
        "Will the Fed cut interest rates at the March FOMC meeting?",
        "Will Bitcoin close above $100,000?",
        "Will the thing happen next week?",
    ]
    for q in samples:
        c = tag_market(q)
        assert c.fine_label in _FINE_LABELS, (q, c)
        assert isinstance(c.reason_code, str) and c.reason_code, (q, c)
        assert 0.0 <= c.confidence <= 1.0, (q, c)
        # derived-label contract holds for every sample
        assert c.label == _FINE_TO_LABEL[c.fine_label], (q, c)
        # stable: identical re-classification yields identical fine_label/reason_code
        c2 = tag_market(q)
        assert (c2.fine_label, c2.reason_code) == (c.fine_label, c.reason_code), (q, c, c2)


def test_label_backward_compat_mapping():
    # The three explicit contract cases from the spec.
    election = tag_market("Will the Democrats win the presidential election?")
    assert election.fine_label == "opinion_forecastable" and election.label == "opinion", election

    fed = tag_market("Will the Fed cut interest rates at the March FOMC meeting?")
    assert fed.fine_label == "data_release_skip" and fed.label == "mechanical", fed

    scoreline = tag_market("Exact Score: Brazil 2 - 1 Argentina?")
    assert scoreline.fine_label == "mechanical_skip" and scoreline.label == "mechanical", scoreline


TESTS = [
    ("mechanical_clear", test_mechanical_clear),
    ("opinion_election", test_opinion_election),
    ("boilerplate_stripped_to_unknown", test_boilerplate_stripped_to_unknown),
    ("empty_inputs_degenerate", test_empty_inputs_degenerate),
    ("dict_with_str_numerics_does_not_raise", test_dict_with_str_numerics_does_not_raise),
    ("liquidity_floor_str_values", test_liquidity_floor_str_values),
    ("should_forecast_gate", test_should_forecast_gate),
    ("approval_polling_is_opinion", test_approval_polling_is_opinion),
    ("fine_label_opinion_forecastable", test_fine_label_opinion_forecastable),
    ("fine_label_opinion_unforecastable", test_fine_label_opinion_unforecastable),
    ("fine_label_mechanical_skip", test_fine_label_mechanical_skip),
    ("fine_label_data_release_skip", test_fine_label_data_release_skip),
    ("fine_label_ambiguous_review", test_fine_label_ambiguous_review),
    ("reason_code_present_and_confidence_in_range", test_reason_code_present_and_confidence_in_range),
    ("label_backward_compat_mapping", test_label_backward_compat_mapping),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
