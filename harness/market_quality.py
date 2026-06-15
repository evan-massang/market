"""
P8 — MARKET-QUALITY GUARDS (build B1).

Pure, read-only checks over a SINGLE normalized market dict. Each guard answers
one question — is this book stale? thin? wide/illiquid-to-exit? — and returns a
transparent, auditable ``(ok, reason, detail)`` triple so the composed P8 verdict
can explain itself per-guard.

DESIGN — REUSE, DON'T REIMPLEMENT
=================================
This module adds NO new market math. It composes the existing P2 scanner +
classifier signals (already battle-tested and used at find-time):

  * check_stale_price  -> scanner.is_stale            (reason 'stale_price')
  * check_liquidity    -> classifier.passes_liquidity_floor
                          with classifier.DEFAULT_MIN_VOLUME / _MIN_LIQUIDITY
                          defaults == the find-time keep_market floor
                          (reason 'low_liquidity')
  * check_spread       -> scanner._spread (when a real quoted spread / bid-ask
                          exists) AND scanner.exit_risk (the depth-proxy fallback
                          for the common Gamma case with no quoted book)
                          (reason 'high_spread')

"BETTER, NOT MORE" — these guards can only make the bot MORE selective. They
NEVER loosen an existing guard and NEVER raise bet frequency.

NO OVER-BLOCK (critical invariant)
==================================
A normal, clean, liquid opinion market in a healthy book MUST pass ALL three
checks at the baseline ``tighten=1.0``:

  * check_liquidity defaults to the SAME floor the candidate already cleared in
    classifier.keep_market at find-time, so anything that was found still passes.
  * DEFAULT_MAX_SPREAD / DEFAULT_MAX_EXIT_RISK are set so a typical Gamma market
    with NO quoted spread and decent liquidity (exit_risk ~= 0.33) sails through;
    only a genuinely thin / wide / hard-to-exit book trips 'high_spread'.

DRAWDOWN TIGHTENING
===================
``evaluate_market_quality(market, tighten=t)`` with t >= 1.0 scales every
threshold HARDER (lower allowed spread + exit_risk, higher required liquidity +
volume). t == 1.0 is the baseline; the drawdown layer raises t only when the
book is losing. t < 1.0 is clamped up to 1.0 so the guard can never be loosened
below baseline.

DEFENSIVE
=========
Every check is wrapped: it never raises on a missing / malformed field. The
underlying scanner/classifier helpers already coerce defensively; the extra
wrapper guarantees that even an unexpected internal error degrades to FAIL-OPEN
(ok=True, recorded in `detail`) rather than over-blocking a clean market — P8 is
ADDITIVE on top of the existing P4/P5/P7 guards, so a P8 hiccup must never veto a
bet the rest of the stack would have allowed.

No network, no LLM, no DB writes. Self-tests:
    python -m harness.tests.test_market_quality
"""
from __future__ import annotations

from harness import scanner, classifier

# ── tunable thresholds (exposed for the drawdown layer + experiments) ─────────
# Spread is in PRICE units (0..1). 0.05 = a 5-cent quoted spread is the most a
# clean book should show; scanner.SPREAD_CAP (0.08) is where spread alone maxes
# out exit risk, so 0.05 stays comfortably below the "untradeable" cliff.
DEFAULT_MAX_SPREAD = 0.05

# exit_risk is 0..1 (higher = harder to unwind a paper position). The depth-proxy
# (no quoted book — the common Gamma case) runs ~0.30 at $40k+ liquidity but rises
# fast: ~0.55 at $20k, ~0.71 at $10k, ~0.83 at $5k, ~0.95 at $1k. The find-time
# keep_market floor is only $1k liquidity, so a tight ceiling would block the bulk
# of real opinion candidates and STARVE the bot of resolved data it needs for the
# gates. 0.85 blocks only genuinely un-exitable dust (< ~$5k liquidity) — where
# free paper fills would otherwise produce dishonest results — while letting normal
# thin-ish markets ($5k+) through. The drawdown layer divides this by `tighten`, so
# a losing book demands a deeper book automatically.
DEFAULT_MAX_EXIT_RISK = 0.85


def check_stale_price(market) -> tuple[bool, str | None, str]:
    """Block stale / suspicious books. REUSES scanner.is_stale.

    Returns ``(ok, reason, detail)``. reason is 'stale_price' when blocked, else
    None. detail carries scanner.is_stale's human-readable explanation.
    """
    try:
        stale, why = scanner.is_stale(market if isinstance(market, dict) else {})
    except Exception as exc:  # never raise on a malformed field — fail open
        return True, None, f"stale_check_error: {exc!r} (fail-open, not blocking)"
    if stale:
        return False, "stale_price", str(why)
    return True, None, str(why)  # why == "ok"


def check_liquidity(market, min_volume: float | None = None,
                    min_liquidity: float | None = None) -> tuple[bool, str | None, str]:
    """Block thin books. REUSES classifier.passes_liquidity_floor.

    Defaults to classifier.DEFAULT_MIN_VOLUME / DEFAULT_MIN_LIQUIDITY — the SAME
    floor the candidate cleared at find-time, so a found market still passes here
    at the default (NO over-block). Returns ``(ok, reason, detail)``; reason is
    'low_liquidity' when blocked.
    """
    mv = classifier.DEFAULT_MIN_VOLUME if min_volume is None else float(min_volume)
    ml = classifier.DEFAULT_MIN_LIQUIDITY if min_liquidity is None else float(min_liquidity)
    try:
        ok = classifier.passes_liquidity_floor(market, mv, ml)
        vol, liq = classifier._market_floats(market)
        detail = (f"volume=${vol:,.0f} (min ${mv:,.0f}); "
                  f"liquidity=${liq:,.0f} (min ${ml:,.0f})")
    except Exception as exc:  # fail open rather than over-block
        return True, None, f"liquidity_check_error: {exc!r} (fail-open, not blocking)"
    if not ok:
        return False, "low_liquidity", detail
    return True, None, detail


def check_spread(market, max_spread: float = DEFAULT_MAX_SPREAD,
                 max_exit_risk: float = DEFAULT_MAX_EXIT_RISK) -> tuple[bool, str | None, str]:
    """Block wide-quoted / hard-to-exit books. REUSES scanner._spread + exit_risk.

    Two signals, blocked if EITHER trips:
      * a REAL quoted spread (raw 'spread' or bestAsk-bestBid) > max_spread, when
        the book actually quotes one; and
      * exit_risk (the depth-proxy fallback that covers the common Gamma case with
        no quoted book) > max_exit_risk.

    A normal Gamma market with NO quoted spread and decent liquidity has spread=None
    (so the spread test is skipped) and a low exit_risk, so it passes. Returns
    ``(ok, reason, detail)``; reason is 'high_spread' when blocked.
    """
    m = market if isinstance(market, dict) else {}
    try:
        spread = scanner._spread(m)        # price units, or None if no quoted book
        er = scanner.exit_risk(m)          # 0..1 depth/spread proxy
    except Exception as exc:  # fail open rather than over-block
        return True, None, f"spread_check_error: {exc!r} (fail-open, not blocking)"

    blocked = False
    parts: list[str] = []
    if spread is not None:
        parts.append(f"spread={spread:.4f} (max {max_spread:.4f})")
        if spread > max_spread:
            blocked = True
    else:
        parts.append("spread=n/a (no quoted book)")
    parts.append(f"exit_risk={er:.4f} (max {max_exit_risk:.4f})")
    if er > max_exit_risk:
        blocked = True

    detail = "; ".join(parts)
    if blocked:
        return False, "high_spread", detail
    return True, None, detail


def evaluate_market_quality(market, tighten: float = 1.0) -> dict:
    """Compose the three B1 guards into one auditable verdict.

    Args:
      market:  a normalized market dict.
      tighten: drawdown multiplier (>= 1.0). 1.0 = baseline. Larger = HARDER
               thresholds: allowed spread + exit_risk are DIVIDED by tighten,
               required volume + liquidity are MULTIPLIED by tighten. Values
               below 1.0 are clamped up to 1.0 so the guard never loosens.

    Returns dict:
      {
        "allow":   bool,                 # True iff EVERY check ok
        "reasons": [reason_code, ...],   # blocking reason codes (empty if allow)
        "checks":  [{name, ok, detail, reason}, ...],
        "tighten": float,                # the (clamped) multiplier actually used
        "thresholds": {max_spread, max_exit_risk, min_volume, min_liquidity},
      }
    """
    try:
        t = float(tighten)
    except (TypeError, ValueError):
        t = 1.0
    if not (t >= 1.0):  # also catches NaN; never loosen below baseline
        t = 1.0

    max_spread = DEFAULT_MAX_SPREAD / t
    max_exit_risk = DEFAULT_MAX_EXIT_RISK / t
    min_volume = classifier.DEFAULT_MIN_VOLUME * t
    min_liquidity = classifier.DEFAULT_MIN_LIQUIDITY * t

    graded = [
        ("stale_price", check_stale_price(market)),
        ("liquidity", check_liquidity(market, min_volume, min_liquidity)),
        ("spread", check_spread(market, max_spread, max_exit_risk)),
    ]

    checks: list[dict] = []
    reasons: list[str] = []
    allow = True
    for name, (ok, reason, detail) in graded:
        checks.append({"name": name, "ok": ok, "detail": detail, "reason": reason})
        if not ok:
            allow = False
            reasons.append(reason or name)

    return {
        "allow": allow,
        "reasons": reasons,
        "checks": checks,
        "tighten": t,
        "thresholds": {
            "max_spread": max_spread,
            "max_exit_risk": max_exit_risk,
            "min_volume": min_volume,
            "min_liquidity": min_liquidity,
        },
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import json
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    clean = {
        "market_id": "DEMO", "question": "Will X win?",
        "outcomes": ["Yes", "No"], "outcome_prices": [0.50, 0.50],
        "volume": 200_000.0, "liquidity": 40_000.0, "end_date": end, "raw": {},
    }
    print(json.dumps(evaluate_market_quality(clean), indent=2))
