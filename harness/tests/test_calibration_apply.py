"""B2 — no-network unit tests for harness.calibration_apply (GATED calibration).

Temp DB only (make_temp_env). No network, no LLM, no real-money path. Verifies:
  * COLD START / empty history          -> exact passthrough (calibrated==raw, applied False)
  * n < min_n (even above the curve's own floor) -> passthrough (our stricter gate)
  * n >= min_n with systematic OVERCONFIDENCE    -> calibrated pulls TOWARD truth,
                                                    applied True, ece present, method set
  * gate boundary (29 -> passthrough, 30 -> applied)
  * build_history reads ONLY resolved rows from swarm_forecasts (DATABASE_URL-aware)
  * never raises: bad raw_p, broken DB -> safe passthrough
  * calibration_report is report-only (returns ece/bins/n; would_apply gating)

Run:  python -m harness.tests.test_calibration_apply
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, patched, run_as_main

make_temp_env("ps_calib_apply_")

from harness import calibration_apply as CA          # noqa: E402
import core.calibration as CAL                        # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Recreate an empty swarm_forecasts table in the temp DB."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS swarm_forecasts")
    conn.commit()
    conn.close()
    CAL.init_db()


def _insert(forecast, outcome, market_id=None):
    """Insert ONE swarm_forecasts row (outcome=None => pending/unresolved)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO swarm_forecasts "
        "(question, market_id, final_probability, consensus_score, outcome, market_odds) "
        "VALUES (?,?,?,?,?,?)",
        ("q", market_id, forecast, 0.5, outcome, 0.5),
    )
    conn.commit()
    conn.close()


def _overconfident_history():
    """40 synthetic resolved rows that are SYSTEMATICALLY OVERCONFIDENT:
       forecast 0.9 only resolves YES 60% of the time (truth 0.6),
       forecast 0.1 resolves YES 40% of the time (truth 0.4)."""
    h = []
    h += [{"forecast": 0.9, "outcome": 1.0}] * 12
    h += [{"forecast": 0.9, "outcome": 0.0}] * 8      # 0.9 -> actual 0.6
    h += [{"forecast": 0.1, "outcome": 1.0}] * 8
    h += [{"forecast": 0.1, "outcome": 0.0}] * 12     # 0.1 -> actual 0.4
    return h                                          # n = 40


# ── tests ─────────────────────────────────────────────────────────────────────
def test_empty_history_is_exact_passthrough():
    # Cold start: no history -> calibrated_p is the SAME value as raw_p, untouched.
    for raw in (0.0, 0.05, 0.5, 0.63, 0.99, 1.0):
        r = CA.apply_calibration(raw, history=[], min_n=30)
        assert r["applied"] is False, r
        assert r["method"] == "none", r
        assert r["calibrated_p"] == raw, r          # NUMERIC IDENTITY
        assert r["raw_p"] == raw, r
        assert r["ece"] is None, r
        assert r["n_history"] == 0, r


def test_below_min_n_passthrough_even_above_curve_floor():
    # 12 resolved rows: ABOVE the curve's own internal floor (10) but BELOW our
    # stricter gate (min_n=30) -> must still be an exact passthrough.
    hist = _overconfident_history()[:12]
    r = CA.apply_calibration(0.9, history=hist, min_n=30)
    assert r["applied"] is False, r
    assert r["method"] == "none", r
    assert r["calibrated_p"] == 0.9, r              # unchanged
    assert r["n_history"] == 12, r


def test_overconfidence_is_calibrated_toward_truth():
    hist = _overconfident_history()
    assert len(hist) == 40

    # High, overconfident forecast 0.9 should be pulled DOWN toward ~0.6.
    hi = CA.apply_calibration(0.9, history=hist, min_n=30)
    assert hi["applied"] is True, hi
    assert hi["method"] in ("isotonic", "platt"), hi
    assert hi["ece"] is not None, hi               # ece present
    assert hi["ece"] > 0, hi
    assert hi["calibrated_p"] < 0.9, hi            # pulled toward truth
    assert abs(hi["calibrated_p"] - 0.6) < 0.1, hi
    assert hi["n_history"] == 40, hi

    # Low, overconfident forecast 0.1 should be pulled UP toward ~0.4.
    lo = CA.apply_calibration(0.1, history=hist, min_n=30)
    assert lo["applied"] is True, lo
    assert lo["calibrated_p"] > 0.1, lo            # pulled toward truth
    assert abs(lo["calibrated_p"] - 0.4) < 0.1, lo


def test_gate_boundary():
    hist = _overconfident_history()
    # 29 < 30 -> passthrough; 30 == min_n -> applied.
    below = CA.apply_calibration(0.9, history=hist[:29], min_n=30)
    assert below["applied"] is False, below
    assert below["calibrated_p"] == 0.9, below

    at = CA.apply_calibration(0.9, history=hist[:30], min_n=30)
    assert at["applied"] is True, at
    assert at["calibrated_p"] != 0.9, at


def test_build_history_reads_only_resolved_rows():
    _reset()
    # 3 resolved + 2 pending (outcome NULL)
    _insert(0.8, 1.0, market_id="A")
    _insert(0.3, 0.0, market_id="B")
    _insert(0.6, 1.0, market_id="C")
    _insert(0.7, None, market_id="D")               # pending
    _insert(0.4, None, market_id="E")               # pending

    hist = CA.build_history()
    assert len(hist) == 3, hist                     # pending rows excluded
    for h in hist:
        assert h["outcome"] in (0.0, 1.0), h
        assert isinstance(h["forecast"], float), h


def test_apply_loads_history_from_db_when_none():
    _reset()
    # 0 resolved rows -> apply_calibration(history=None) loads [] -> passthrough.
    r = CA.apply_calibration(0.42, min_n=30)
    assert r["applied"] is False, r
    assert r["calibrated_p"] == 0.42, r
    assert r["n_history"] == 0, r


def test_never_raises_on_bad_input():
    # Non-numeric raw_p must NOT raise; degrades to a passthrough dict.
    r = CA.apply_calibration("not-a-number", history=[], min_n=30)
    assert isinstance(r, dict), r
    assert r["applied"] is False, r

    r2 = CA.apply_calibration(None, history=[], min_n=30)
    assert isinstance(r2, dict), r2
    assert r2["applied"] is False, r2


def test_never_raises_on_broken_db():
    def _boom():
        raise RuntimeError("db blew up")

    # build_history degrades to [] when the DB path resolution raises.
    with patched(CA, "_db_path", _boom):
        assert CA.build_history() == []
        # apply_calibration(history=None) -> build_history() -> [] -> passthrough.
        r = CA.apply_calibration(0.55, min_n=30)
        assert r["applied"] is False, r
        assert r["calibrated_p"] == 0.55, r
        # report-only also degrades safely.
        rep = CA.calibration_report(min_n=30)
        assert rep["n_history"] == 0, rep
        assert rep["would_apply"] is False, rep


def test_report_is_report_only():
    _reset()
    for h in _overconfident_history():
        _insert(h["forecast"], h["outcome"])

    rep = CA.calibration_report(min_n=30)
    assert rep["n_history"] == 40, rep
    assert rep["ece"] is not None and rep["ece"] > 0, rep
    assert isinstance(rep["reliability_bins"], list) and rep["reliability_bins"], rep
    assert rep["would_apply"] is True, rep          # n>=min_n and method != none

    # With a high min_n the report still computes diagnostics but would_apply False.
    rep2 = CA.calibration_report(min_n=1000)
    assert rep2["n_history"] == 40, rep2
    assert rep2["would_apply"] is False, rep2


TESTS = [
    ("empty_history_is_exact_passthrough", test_empty_history_is_exact_passthrough),
    ("below_min_n_passthrough_even_above_curve_floor", test_below_min_n_passthrough_even_above_curve_floor),
    ("overconfidence_is_calibrated_toward_truth", test_overconfidence_is_calibrated_toward_truth),
    ("gate_boundary", test_gate_boundary),
    ("build_history_reads_only_resolved_rows", test_build_history_reads_only_resolved_rows),
    ("apply_loads_history_from_db_when_none", test_apply_loads_history_from_db_when_none),
    ("never_raises_on_bad_input", test_never_raises_on_bad_input),
    ("never_raises_on_broken_db", test_never_raises_on_broken_db),
    ("report_is_report_only", test_report_is_report_only),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
