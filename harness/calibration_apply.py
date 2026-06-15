"""harness/calibration_apply.py — B2: GATED calibration wiring (passthrough cold).

A thin, additive wrapper around the EXISTING ``core.calibration_curve.calibrate_probability``
(PAVA isotonic + Platt + ECE). This module does NOT modify that curve — it only

  1. loads the resolved swarm-forecast history (DATABASE_URL-aware),
  2. GATES whether the curve's calibrated probability is actually applied, and
  3. exposes a report-only view for the dashboard.

COLD-START INVARIANT (non-negotiable)
-------------------------------------
With no (or thin) resolved history — the situation TODAY (0 resolved opinion
markets) — :func:`apply_calibration` is an EXACT PASSTHROUGH: it returns
``calibrated_p == raw_p`` (the same float object value, no rounding, no clamp),
``method == "none"`` and ``applied is False``. The curve's calibrated value is
returned ONLY once ``n_history >= min_n`` (default 30). This guarantees the
sizing/decision probability is NUMERICALLY IDENTICAL to the pre-P6 raw swarm
probability until enough resolved data has actually accrued.

Note the gate here (``min_n``, default 30) is STRICTER than the curve's own
internal floor (10 entries; 15 for isotonic). Between those thresholds we still
passthrough — calibration activates only at our own, higher bar.

Safety
------
Every DB / curve call is wrapped in try/except + ``obs.hooks.on_error``. Nothing
here raises into the bettor or settlement: on ANY error the functions degrade to
a safe passthrough / empty report.
"""
from __future__ import annotations

import os
import sqlite3

from core.calibration_curve import calibrate_probability

try:
    from harness import obs
except Exception:  # pragma: no cover - obs must never make import fail
    obs = None


# ── error reporting (best-effort, never raises) ───────────────────────────────
def _report_error(where: str, exc: Exception, context=None) -> None:
    if obs is None:
        return
    try:
        obs.hooks.on_error(where=where, exc=exc, action="passthrough", context=context)
    except Exception:
        pass


# ── db path (DATABASE_URL-aware, copies the harness/label_perf.py pattern) ─────
def _db_path() -> str:
    """Resolve the harness DB path.

    Honors DATABASE_URL with the exact normalization core.calibration /
    label_perf use (so a test pointing DATABASE_URL at a temp file hits the same
    file the rest of the harness wrote). Otherwise defers to
    obs.config.resolve_db_path() — the canonical polyswarm.db.
    """
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


# ── history ───────────────────────────────────────────────────────────────────
def build_history() -> list[dict]:
    """Resolved swarm forecasts as ``[{"forecast": final_probability, "outcome": outcome}]``.

    Reads RESOLVED rows (``outcome IS NOT NULL``) from the ``swarm_forecasts``
    table — the same table core.calibration writes. Best-effort: a missing table
    / missing DB / malformed row degrades to ``[]`` (the cold-start case, which
    makes :func:`apply_calibration` a passthrough). Never raises.
    """
    try:
        conn = sqlite3.connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT final_probability, outcome FROM swarm_forecasts "
                "WHERE outcome IS NOT NULL AND final_probability IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _report_error("calibration_apply.build_history", e)
        return []

    out: list[dict] = []
    for f, o in rows:
        try:
            out.append({"forecast": float(f), "outcome": float(o)})
        except Exception:
            # one bad row never sinks the whole history
            continue
    return out


# ── gated application ───────────────────────────────────────────────────────--
def apply_calibration(raw_p, history: list[dict] | None = None, min_n: int = 30) -> dict:
    """GATED calibration of ``raw_p``.

    Args:
        raw_p:   the raw (pre-calibration) probability — the swarm aggregate output.
        history: ``[{"forecast", "outcome"}]``. If None, loaded via build_history().
        min_n:   minimum resolved-history count before calibration is applied.

    Returns dict:
        ``{calibrated_p, raw_p, method, ece, n_history, applied}``.

    PASSTHROUGH (the cold-start guarantee): when ``n_history < min_n`` (or on any
    error, or when the curve declines to calibrate) the result is
    ``calibrated_p == raw_p`` exactly, ``method == "none"``, ``applied is False``.
    Only when ``n_history >= min_n`` AND the curve produces a real method does it
    return the curve's ``calibrated_probability`` with ``applied is True``.
    """
    try:
        raw_p = float(raw_p)
    except Exception as e:
        _report_error("calibration_apply.apply_calibration", e, context={"raw_p": repr(raw_p)})
        # Cannot even coerce raw_p — there is nothing safe to size on; surface 0.5
        # is wrong, so we re-raise into the caller? No: contract is "never raises".
        # Fall back to the passthrough of whatever we can represent.
        raw_p = 0.5

    # The exact-passthrough result. calibrated_p IS raw_p (no rounding / no clamp)
    # so the decision probability is numerically identical to the pre-P6 value.
    passthrough = {
        "calibrated_p": raw_p,
        "raw_p": raw_p,
        "method": "none",
        "ece": None,
        "n_history": 0,
        "applied": False,
    }

    try:
        if history is None:
            history = build_history()
        n = len(history)
        passthrough["n_history"] = n

        # GATE: below our own (stricter) threshold -> exact passthrough.
        if n < min_n:
            return passthrough

        curve = calibrate_probability(raw_p, history=history)
        method = curve.get("calibration_method", "none")
        ece = curve.get("expected_calibration_error")
        n_hist = curve.get("n_historical", n)

        # Curve itself declined to calibrate (its internal floor) -> passthrough,
        # but report what metadata we have.
        if method == "none":
            passthrough["ece"] = ece
            passthrough["n_history"] = n_hist
            return passthrough

        return {
            "calibrated_p": float(curve.get("calibrated_probability", raw_p)),
            "raw_p": raw_p,
            "method": method,
            "ece": ece,
            "n_history": n_hist,
            "applied": True,
        }
    except Exception as e:
        _report_error("calibration_apply.apply_calibration", e)
        return passthrough


# ── report-only (dashboard) ───────────────────────────────────────────────────
def calibration_report(min_n: int = 30) -> dict:
    """Report-only calibration view for the dashboard — NEVER applied to a decision.

    Returns ``{n_history, ece, reliability_bins, method, overconfidence_score,
    underconfidence_score, min_n, would_apply}``. ``would_apply`` reflects whether
    :func:`apply_calibration` would currently apply the curve (``n_history >= min_n``
    AND the curve produces a non-"none" method). Best-effort: empty/error -> a
    zeroed report. Never raises.
    """
    empty = {
        "n_history": 0,
        "ece": None,
        "reliability_bins": [],
        "method": "none",
        "overconfidence_score": None,
        "underconfidence_score": None,
        "min_n": min_n,
        "would_apply": False,
    }
    try:
        history = build_history()
        n = len(history)
        # raw_p here is a probe only — used to ask the curve for its diagnostics,
        # NOT to drive any decision.
        curve = calibrate_probability(0.5, history=history)
        method = curve.get("calibration_method", "none")
        return {
            "n_history": curve.get("n_historical", n),
            "ece": curve.get("expected_calibration_error"),
            "reliability_bins": curve.get("reliability_bins", []),
            "method": method,
            "overconfidence_score": curve.get("overconfidence_score"),
            "underconfidence_score": curve.get("underconfidence_score"),
            "min_n": min_n,
            "would_apply": (n >= min_n) and (method != "none"),
        }
    except Exception as e:
        _report_error("calibration_apply.calibration_report", e)
        return empty
