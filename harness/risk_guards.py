"""harness/risk_guards.py — P8 unified adaptive bet-guard evaluator.

Composes the P8 market-quality guards (stale / low-liquidity / high-spread, in
``harness.market_quality``) with the portfolio guards (correlation / bad-theme,
in ``harness.portfolio_guards``) under a SINGLE drawdown-derived ``tighten``
multiplier — so every threshold gets STRICTER when the book is losing and is at
its baseline (no over-block) when the book is healthy.

It also SURFACES, informational-only, the already-enforced P4 observe-only status
so one audit record shows the full guard picture; it does NOT re-enforce P4 / P5
no_data / P7 EV (those stay at their own call sites — re-enforcing would
double-skip and muddy the reason codes).

FAIL-OPEN: any internal error -> ``allow=True`` (== pre-P8 behavior), logged via
obs.on_error. Like every guard in this program it can only ever SKIP a bet, never
approve one.
"""
from __future__ import annotations

from harness import market_quality
from harness import portfolio_guards
from harness import scoreboard

try:
    from harness import obs
except Exception:  # pragma: no cover - obs optional
    obs = None
try:
    from harness import label_perf
except Exception:  # pragma: no cover
    label_perf = None


def _observe_only_status(question) -> dict:
    """P4 observe-only is ALREADY enforced in predict_one; surfaced here only so the
    audit trail is complete. ok=True means 'not flagged' — it never blocks here."""
    try:
        if label_perf is None:
            return {"name": "observe_only", "ok": True, "detail": "label_perf unavailable (informational)"}
        theme = scoreboard.theme_of(question or "")
        bad = bool(label_perf.should_observe_only(theme))
        return {"name": "observe_only", "ok": (not bad),
                "detail": f"theme '{theme}' observe_only={bad} (informational; enforced upstream)"}
    except Exception:
        return {"name": "observe_only", "ok": True, "detail": "observe-only status unavailable"}


def evaluate(market, side, question, positions=None, cfg=None) -> dict:
    """Evaluate the unified P8 risk verdict for one candidate bet.

    Returns ``{allow, blocking_reason, tighten, checks:[{name, ok, detail}]}``.
    ``allow`` iff every ENFORCED P8 check passes (the informational observe_only
    row never blocks). ``blocking_reason`` is the first failing enforced check's
    reason code (one of: stale_price, low_liquidity, high_spread,
    correlated_exposure, bad_theme).
    """
    try:
        tighten = portfolio_guards.stricter_tighten()
        if positions is None:
            positions = portfolio_guards.open_positions()

        mq = market_quality.evaluate_market_quality(market, tighten=tighten)
        checks: list[dict] = list(mq.get("checks", []))

        ok_c, r_c, d_c = portfolio_guards.check_correlation(market, side, positions)
        checks.append({"name": "correlated_exposure", "ok": ok_c,
                       "reason": (r_c if not ok_c else None), "detail": d_c})

        bt = portfolio_guards.check_bad_theme(question, tighten=tighten)
        ok_t, r_t, d_t = bt if isinstance(bt, tuple) else (True, None, str(bt))
        checks.append({"name": "bad_theme", "ok": ok_t,
                       "reason": (r_t if not ok_t else None), "detail": d_t})

        # informational only (P4 observe-only enforced upstream)
        checks.append(_observe_only_status(question))

        # blocking = the ENFORCED P8 checks only (exclude the informational row). Use each
        # failing check's reason CODE (market_quality emits short names + a `reason` code;
        # the appended checks set `reason` too) so the skip reason is the canonical code.
        enforced = [c for c in checks if c.get("name") != "observe_only"]
        failed = [c for c in enforced if not c.get("ok")]
        blocking_reason = (failed[0].get("reason") or failed[0].get("name")) if failed else None
        return {
            "allow": len(failed) == 0,
            "blocking_reason": blocking_reason,
            "tighten": tighten,
            "checks": checks,
        }
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="risk_guards.evaluate", exc=e, action="fail-open-allow")
            except Exception:
                pass
        # FAIL-OPEN to pre-P8 behavior: never crash or wrongly block the bettor.
        return {"allow": True, "blocking_reason": None, "tighten": 1.0, "checks": []}
