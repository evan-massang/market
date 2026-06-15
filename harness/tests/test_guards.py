"""Unit tests for harness.predict_today._betting_guards — PURE reliability gate.

No I/O, no LLM (this is the function replay_guards.py replays). Returns (ok, reason).
Run:  python -m harness.tests.test_guards
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_guards_")

from harness import predict_today as PT  # noqa: E402
from harness.predict_today import _betting_guards, _evidence_guard, _conviction  # noqa: E402


def _fake_pack(total_items, n_sources, evidence_quality):
    """Minimal stand-in for evidence_pack.EvidencePack — _evidence_guard only reads these
    three attributes (pure function, no I/O / no network)."""
    return SimpleNamespace(total_items=total_items, n_sources=n_sources,
                           evidence_quality=evidence_quality)


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
    assert PT.MIN_EVIDENCE_QUALITY == 0.25


# ── B-series WIRE: data-sufficiency gate ("no data, no bet") ──────────────────────────
def test_evidence_no_data_zero_items():
    # GDELT block can be present (n_sources=1) yet carry 0 articles -> still no real data.
    ok, reason = _evidence_guard(_fake_pack(total_items=0, n_sources=1, evidence_quality=0.0))
    assert ok is False and reason == "no_data", (ok, reason)


def test_evidence_no_data_zero_sources():
    ok, reason = _evidence_guard(_fake_pack(total_items=0, n_sources=0, evidence_quality=0.0))
    assert ok is False and reason == "no_data", (ok, reason)


def test_evidence_no_data_none_pack():
    # A gather failure (pack=None) must be treated as no_data — never a green light.
    ok, reason = _evidence_guard(None)
    assert ok is False and reason == "no_data", (ok, reason)


def test_evidence_low_evidence_observe_only():
    # 0 < quality < MIN_EVIDENCE_QUALITY -> observe-only skip (forecast logged, no bet).
    ok, reason = _evidence_guard(_fake_pack(total_items=2, n_sources=1, evidence_quality=0.10))
    assert ok is False and reason == "low_evidence:0.10", (ok, reason)


def test_evidence_adequate_not_skipped():
    ok, reason = _evidence_guard(_fake_pack(total_items=5, n_sources=2, evidence_quality=0.55))
    assert ok is True and reason == "ok", (ok, reason)


def test_evidence_boundary_quality_allowed():
    # Exactly MIN_EVIDENCE_QUALITY is NOT "low" (strict <) — the bet may proceed.
    ok, reason = _evidence_guard(_fake_pack(total_items=3, n_sources=1,
                                            evidence_quality=PT.MIN_EVIDENCE_QUALITY))
    assert ok is True and reason == "ok", (ok, reason)


# ── B-series WIRE: evidence_quality refines conviction (monotonic, bounded, no inflation) ──
def test_conviction_quality_monotonic():
    args = (0.60, 0.55, 0.9, 0.10, True)   # swarm, challenger, consensus, edge, had_data
    c_weak = _conviction(*args, evidence_quality=0.30)
    c_strong = _conviction(*args, evidence_quality=0.90)
    assert c_strong > c_weak, (c_weak, c_strong)


def test_conviction_backward_compatible_default():
    # Omitting evidence_quality preserves the legacy had_data behavior exactly...
    args = (0.60, 0.55, 0.9, 0.10)
    legacy = _conviction(*args, True)
    # ...and full quality (1.0) reproduces the had_data=True data term (1.0).
    assert _conviction(*args, True, evidence_quality=1.0) == legacy, legacy


def test_conviction_weak_evidence_does_not_inflate():
    # Thin evidence (just above the gate) must NOT score higher than the legacy had_data
    # baseline — weak evidence can never inflate a bet.
    args = (0.60, 0.55, 0.9, 0.10, True)
    assert _conviction(*args, evidence_quality=0.25) <= _conviction(*args), "weak evidence inflated"


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
    ("evidence_no_data_zero_items", test_evidence_no_data_zero_items),
    ("evidence_no_data_zero_sources", test_evidence_no_data_zero_sources),
    ("evidence_no_data_none_pack", test_evidence_no_data_none_pack),
    ("evidence_low_evidence_observe_only", test_evidence_low_evidence_observe_only),
    ("evidence_adequate_not_skipped", test_evidence_adequate_not_skipped),
    ("evidence_boundary_quality_allowed", test_evidence_boundary_quality_allowed),
    ("conviction_quality_monotonic", test_conviction_quality_monotonic),
    ("conviction_backward_compatible_default", test_conviction_backward_compatible_default),
    ("conviction_weak_evidence_does_not_inflate", test_conviction_weak_evidence_does_not_inflate),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
