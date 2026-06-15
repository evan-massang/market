"""Unit tests for harness.predict_today._betting_guards — PURE reliability gate.

No I/O, no LLM (this is the function replay_guards.py replays). Returns (ok, reason).
Run:  python -m harness.tests.test_guards
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_guards_")

from harness import predict_today as PT  # noqa: E402
from harness.predict_today import _betting_guards  # noqa: E402


def test_guard_a_mechanical():
    ok, reason = _betting_guards("mechanical", 0.6, 0.6, 0.9, [], 0.5)
    assert ok is False and reason == "mechanical", (ok, reason)


def test_guard_b_divergence():
    ok, reason = _betting_guards("opinion", 0.80, 0.50, 0.9, [], 0.5)  # |0.80-0.50|=0.30>0.15
    assert ok is False and "divergence" in reason, (ok, reason)


def test_guard_b_null_challenger_not_triggered():
    ok, reason = _betting_guards("opinion", 0.80, None, 0.9, [], 0.5)
    assert ok is True and reason == "ok", (ok, reason)


def test_guard_c_low_consensus():
    ok, reason = _betting_guards("opinion", 0.6, 0.6, 0.40, [], 0.5)  # 0.40 < 0.50
    assert ok is False and "consensus" in reason, (ok, reason)


def test_guard_d_one_yes_per_event():
    # side is YES (swarm_p 0.8 > price 0.5) and we already hold a YES leg -> reject.
    legs = [{"side": "YES", "model_p": 0.7}]
    ok, reason = _betting_guards("opinion", 0.8, 0.75, 0.9, legs, 0.5)
    assert ok is False and "YES" in reason, (ok, reason)


def test_guard_d_no_side_unconstrained_by_one_yes():
    # swarm_p < price -> side NO; a held YES leg must NOT block a NO/fade bet.
    legs = [{"side": "YES", "model_p": 0.7}]
    ok, reason = _betting_guards("opinion", 0.40, 0.45, 0.9, legs, 0.50)
    assert ok is True and reason == "ok", (ok, reason)


def test_guard_d_incoherent_group():
    # Reach the incoherence branch: disable ONE_YES_PER_EVENT so multiple YES legs
    # are allowed, then make the YES-prob sum exceed MAX_GROUP_PROB_SUM (1.20).
    legs = [{"side": "YES", "model_p": 0.7}, {"side": "YES", "model_p": 0.6}]
    with patched(PT, "ONE_YES_PER_EVENT", False):
        ok, reason = _betting_guards("opinion", 0.8, 0.75, 0.9, legs, 0.5)  # 0.7+0.6+0.8=2.1
    assert ok is False and "incoherent" in reason, (ok, reason)


def test_happy_path():
    ok, reason = _betting_guards("opinion", 0.60, 0.55, 0.9, [], 0.50)
    assert ok is True and reason == "ok", (ok, reason)


def test_constants_present():
    # Sanity: the tunables the guards key on exist with expected defaults.
    assert PT.MAX_SWARM_CHALLENGER_DIVERGENCE == 0.15
    assert PT.MIN_SWARM_CONSENSUS == 0.50
    assert PT.MAX_GROUP_PROB_SUM == 1.20
    assert PT.SKIP_MECHANICAL is True


TESTS = [
    ("guard_a_mechanical", test_guard_a_mechanical),
    ("guard_b_divergence", test_guard_b_divergence),
    ("guard_b_null_challenger_not_triggered", test_guard_b_null_challenger_not_triggered),
    ("guard_c_low_consensus", test_guard_c_low_consensus),
    ("guard_d_one_yes_per_event", test_guard_d_one_yes_per_event),
    ("guard_d_no_side_unconstrained_by_one_yes", test_guard_d_no_side_unconstrained_by_one_yes),
    ("guard_d_incoherent_group", test_guard_d_incoherent_group),
    ("happy_path", test_happy_path),
    ("constants_present", test_constants_present),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
