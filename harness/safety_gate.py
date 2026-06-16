"""harness/safety_gate.py — the FAIL-CLOSED policy for the money gates (Plan 1).

The four money gates — EV (profitability), risk guards (market-quality +
portfolio), bankroll kill switch, and exposure cap — protect a PAPER bet from
being placed when the safety check cannot prove it is safe. The non-negotiable
rule, applied identically everywhere:

    Safety gate unavailable  = unsafe.
    Safety gate error        = unsafe.
    Safety gate unknown      = unsafe.
    unsafe                   = NO paper bet.

A gate result is the existing ``(allow: bool, reason: str)`` tuple (exposure adds
a trailing detail dict — ``(allow, reason, detail)``). Only an EXPLICIT
``allow is True`` is treated as a pass. Anything else — ``False``, ``None``, a
malformed shape, a missing module, a raised exception, NaN/None inputs — BLOCKS
with a reason that ends in ``_fail_closed`` so the no-bet is specific and visible
in the decisions / journal / obs trail (never hidden as a generic "skip").

This module is pure policy: the canonical reason vocabulary, a ``coerce()`` that
validates an underlying gate's result, and a tiny obs error-logger. It imports
nothing from the rest of the harness at module load (obs is imported lazily), so
it can be imported from any gate module without a cycle. Paper-only.
"""
from __future__ import annotations

import math
from typing import Any

# ── canonical fail-closed reason vocabulary ───────────────────────────────────
# Every reason ends in "_fail_closed" so a fail-closed block is unmistakable in a
# log/journal/decisions row and can never be confused with a normal tightening
# skip (e.g. "neg_ev_after_costs", "drawdown_pause", "high_spread").
EV_UNAVAILABLE = "ev_gate_unavailable_fail_closed"
EV_ERROR = "ev_gate_error_fail_closed"
EV_INVALID = "ev_gate_invalid_fail_closed"

RISK_UNAVAILABLE = "risk_guards_unavailable_fail_closed"
RISK_ERROR = "risk_guards_error_fail_closed"
RISK_INVALID = "risk_guards_invalid_fail_closed"
RISK_INTERNAL_ERROR = "risk_guards_internal_error_fail_closed"
MARKET_QUALITY_ERROR = "market_quality_error_fail_closed"

BANKROLL_UNAVAILABLE = "bankroll_unavailable_fail_closed"
BANKROLL_ERROR = "bankroll_error_fail_closed"
BANKROLL_INVALID = "bankroll_invalid_fail_closed"

EXPOSURE_UNAVAILABLE = "exposure_unavailable_fail_closed"
EXPOSURE_ERROR = "exposure_error_fail_closed"
EXPOSURE_INVALID = "exposure_invalid_fail_closed"

# Every fail-closed reason this module defines (handy for tests / dashboards).
ALL_REASONS = (
    EV_UNAVAILABLE, EV_ERROR, EV_INVALID,
    RISK_UNAVAILABLE, RISK_ERROR, RISK_INVALID, RISK_INTERNAL_ERROR, MARKET_QUALITY_ERROR,
    BANKROLL_UNAVAILABLE, BANKROLL_ERROR, BANKROLL_INVALID,
    EXPOSURE_UNAVAILABLE, EXPOSURE_ERROR, EXPOSURE_INVALID,
)


def is_fail_closed(reason: Any) -> bool:
    """True iff ``reason`` is a fail-closed block reason (ends in '_fail_closed')."""
    return isinstance(reason, str) and reason.endswith("_fail_closed")


def finite(x: Any) -> bool:
    """True iff x is a finite real number (rejects None / NaN / inf / non-numeric)."""
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def coerce(result: Any, *, gate: str, block_reason: str) -> tuple[bool, str]:
    """Validate an underlying gate's result and return an unambiguous ``(allow, reason)``.

    * a clean ``(True, reason[, ...])``  → ``(True, reason or "ok")``           [PASS]
    * a clean ``(False, reason[, ...])`` → ``(False, reason or f"{gate}_blocked")`` [normal block]
    * ANY other shape (not a tuple, len<2, result[0] not a bool, None, etc.)
                                          → ``(False, block_reason)``            [FAIL-CLOSED]

    So only an explicit boolean ``True`` ever passes; a malformed/garbage result
    can never be read as a pass.
    """
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[0], bool):
        ok = result[0]
        reason = result[1]
        if ok:
            return True, (reason if isinstance(reason, str) and reason else "ok")
        return False, (reason if isinstance(reason, str) and reason else f"{gate}_blocked")
    return False, block_reason


def log_error(where: str, exc: BaseException) -> None:
    """Best-effort: record a gate exception to obs as a fail-closed block. Never raises."""
    try:
        from harness import obs
    except Exception:
        return
    if not obs:
        return
    try:
        obs.hooks.on_error(where=where, exc=exc, action="fail-closed-block")
    except Exception:
        pass
