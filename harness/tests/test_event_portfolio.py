"""P3 — unit tests for harness.event_portfolio.evaluate_event (PURE, no network/LLM).

This file is the SPEC for the event-level portfolio engine and is written
BEFORE/independently of the engine wiring (TDD-red). It imports
``harness.event_portfolio.evaluate_event`` lazily inside each test, so until the
engine module lands every test FAILs cleanly with a clear ImportError rather
than crashing module import — run_tests auto-discovery + the __main__ block both
still print one PASS/FAIL line per scenario.

The engine API under test (built in parallel to this spec):
    evaluate_event(legs, bankroll, cfg=None) -> EventPortfolio
EventPortfolio fields:
    accept, positions, rejected, portfolio_ev, worst_case_loss, best_case_profit,
    max_exposure, losing_outcome, reject_reason, explanation
leg dict keys:
    leg_id, market_id, model_p, price, liquidity, exit_risk, (bid, ask, has_data)

What "+EV after costs" means here (reused from wallet.open_position so the engine
must agree with the paper fill model): a side is bought at a WORSE-than-mid fill
  base       = price        if side == "YES" else (1 - price)
  fill_price = clamp(base + slippage, 0.01, 0.99)        # default slippage 0.01
  shares     = stake / fill_price ;  payout = shares * $1 if that side wins
=> EV(side) = P(side wins) * stake/fill_price - stake - fee
            > 0  iff  P(side wins) > fill_price  (model prob beats price+slippage).
For a YES leg P(win)=model_p; for a NO leg P(win)=1-model_p.

NOTE: the model probabilities (model_p) used here are taken AS GIVEN — they will
only be CALIBRATED later in P6. These tests exercise portfolio construction logic
on the provided model_p, not its calibration.

Run:  python -m harness.tests.test_event_portfolio
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from harness.tests._util import make_temp_env, run_as_main

# Redirect DB + obs logs to a temp dir BEFORE the engine (which may emit obs hooks)
# is imported, so no live polyswarm.db / logs are touched and no network happens.
make_temp_env("ps_event_portfolio_")

TOL = 1e-6
BANKROLL = 1000.0


# ── lazy engine import ────────────────────────────────────────────────────────
def _evaluate(legs, bankroll, cfg=None):
    """Import + call the engine. Lazy so a missing module FAILs the test (red),
    not the whole module load. When the engine lands this just forwards through."""
    from harness.event_portfolio import evaluate_event
    return evaluate_event(legs, bankroll, cfg=cfg)


# ── field/position accessors (robust to dataclass OR dict returns) ─────────────
_MISSING = object()


def _attr(obj, name, default=_MISSING):
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
    elif hasattr(obj, name):
        return getattr(obj, name)
    if default is _MISSING:
        raise AssertionError(f"result is missing required field {name!r}: {obj!r}")
    return default


def _positions(res):
    return list(_attr(res, "positions", []) or [])


def _side(p) -> str:
    return str(_attr(p, "side", "")).upper()


def _is_yes(p) -> bool:
    s = _side(p)
    return s == "YES" or s.endswith("YES")


def _is_no(p) -> bool:
    s = _side(p)
    return s == "NO" or s.endswith("NO")


def _legid(p):
    for k in ("leg_id", "legId", "id", "market_id"):
        v = _attr(p, k, None)
        if v is not None:
            return v
    return None


def _reason(res) -> str:
    return (_attr(res, "reject_reason", "") or "").lower()


def _mentions(text: str, terms) -> bool:
    t = (text or "").lower()
    return any(term in t for term in terms)


# ── leg builder ────────────────────────────────────────────────────────────────
def _leg(leg_id, model_p, price, *, liquidity=100_000.0, exit_risk=0.02,
         spread=0.004, has_data=True, market_id=None):
    """Build one event-leg dict. Defaults describe a deeply LIQUID, low-exit-risk,
    tight-spread leg (clearly enterable/exitable). Override for the illiquid case."""
    half = spread / 2.0
    return {
        "leg_id": leg_id,
        "market_id": market_id or f"mkt-{leg_id}",
        "model_p": model_p,
        "price": price,
        "liquidity": liquidity,
        "exit_risk": exit_risk,
        "bid": round(max(0.01, price - half), 4),
        "ask": round(min(0.99, price + half), 4),
        "has_data": has_data,
    }


def _make_cfg(**over):
    """Best-effort construction of the engine's config carrying the overrides
    (notably max_event_exposure_frac). Tries the engine's own dataclass/default,
    then falls back to a duck-typed SimpleNamespace the engine can read attrs off."""
    import dataclasses
    import importlib
    try:
        ep = importlib.import_module("harness.event_portfolio")
    except Exception:
        ep = None
    if ep is not None:
        for nm in ("DEFAULT_CFG", "DEFAULT_CONFIG", "DEFAULT"):
            inst = getattr(ep, nm, None)
            if inst is not None and dataclasses.is_dataclass(inst):
                try:
                    return dataclasses.replace(inst, **over)
                except Exception:
                    pass
        for nm in ("EventConfig", "EvalConfig", "PortfolioConfig",
                   "EventPortfolioConfig", "Config"):
            cls = getattr(ep, nm, None)
            if isinstance(cls, type):
                try:
                    return cls(**over)
                except Exception:
                    try:
                        inst = cls()
                        for k, v in over.items():
                            setattr(inst, k, v)
                        return inst
                    except Exception:
                        pass
    base = dict(max_event_exposure_frac=0.30, slippage=0.01, fee_frac=0.0,
                min_liquidity=1_000.0, max_exit_risk=0.50, max_spread=0.05,
                min_edge=0.02)
    base.update(over)
    return SimpleNamespace(**base)


def _exposure_cap(cfg, bankroll):
    return float(_attr(cfg, "max_event_exposure_frac")) * bankroll


# ── (1) one UNDERVALUED YES in a ME event with overpriced rivals ───────────────
def test_one_undervalued_yes():
    # ME event, model_p sums ~1.0 (coherent). Leg A: model 0.70 vs price 0.45 ->
    # YES on A is the single undervalued winner (0.70 > 0.45+slip). Rivals B,C are
    # OVERPRICED (price > model_p), so the only undervalued YES is A.
    legs = [
        _leg("A", model_p=0.70, price=0.45),
        _leg("B", model_p=0.15, price=0.30),
        _leg("C", model_p=0.15, price=0.25),
    ]
    res = _evaluate(legs, BANKROLL, cfg=None)
    assert _attr(res, "accept") is True, ("expected accept", _reason(res))
    yes = [p for p in _positions(res) if _is_yes(p)]
    # exactly ONE YES, and it sits on the undervalued leg A
    assert len(yes) == 1, ("expected exactly one YES position", _positions(res))
    assert _legid(yes[0]) == "A", ("YES must be on the undervalued leg A", yes)
    assert _attr(res, "portfolio_ev") > TOL, _attr(res, "portfolio_ev")


# ── (2) multiple OVERVALUED NOs (fade the losers) ──────────────────────────────
def test_multiple_overvalued_nos():
    # Every ME leg is OVERVALUED (model_p < price), so there is NO undervalued YES;
    # the engine should fade them all with NO bets (NO wins when the leg loses, and
    # P(NO win)=1-model_p clears the NO fill on each).
    legs = [
        _leg("A", model_p=0.30, price=0.40),
        _leg("B", model_p=0.30, price=0.40),
        _leg("C", model_p=0.30, price=0.45),
    ]
    res = _evaluate(legs, BANKROLL, cfg=None)
    assert _attr(res, "accept") is True, ("expected accept", _reason(res))
    nos = [p for p in _positions(res) if _is_no(p)]
    yes = [p for p in _positions(res) if _is_yes(p)]
    assert len(nos) >= 2, ("expected multiple NO positions", _positions(res))
    assert len(yes) == 0, ("no YES bet when nothing is undervalued", yes)
    assert _attr(res, "portfolio_ev") > TOL, _attr(res, "portfolio_ev")
    # worst-case loss is bounded by what is actually staked (within the exposure cap):
    # you can never lose more than the capital deployed, and 2/3 NOs always win.
    wcl = _attr(res, "worst_case_loss")
    max_exp = _attr(res, "max_exposure")
    assert abs(wcl) <= max_exp + TOL, ("worst case exceeds deployed exposure", wcl, max_exp)


# ── (3) BAD MULTIPLE-YES: contradictory model (YES-prob sum ~1.8) -> REJECT ─────
def test_bad_multiple_yes_rejected():
    # Each leg looks like an undervalued YES (model 0.60 > price 0.35), but the
    # model_p sum is 1.80 across MUTUALLY-EXCLUSIVE legs — only one can win, so
    # betting YES on all three is self-contradictory. Engine must REJECT (not just
    # silently pick one): no coherent multi-YES portfolio exists here.
    legs = [
        _leg("A", model_p=0.60, price=0.35),
        _leg("B", model_p=0.60, price=0.35),
        _leg("C", model_p=0.60, price=0.35),
    ]
    res = _evaluate(legs, BANKROLL, cfg=None)
    assert _attr(res, "accept") is not True, ("expected REJECT of contradictory YES set",
                                              _positions(res))
    assert _mentions(_reason(res), ("contradict", "incoher", ">1 yes",
                                    "multiple yes", "more than one yes", "yes")), \
        ("reject_reason must flag the contradictory/>1-YES set", _reason(res))


# ── (4) VALID HEDGE/ARB: NO on every cheap ME leg, guaranteed payoff > cost ─────
def test_valid_arbitrage_accepted():
    # 4 ME legs each priced 0.325 -> YES prices sum to 1.30 (heavy overround). Buying
    # NO on EVERY leg is a true arb: exactly one leg resolves YES, the other three
    # NOs always pay out, so the worst outcome is still a profit even after slippage.
    # Deep liquidity, tiny exit risk, tight spread -> genuinely enterable/exitable.
    legs = [_leg(ch, model_p=0.25, price=0.325) for ch in ("A", "B", "C", "D")]
    res = _evaluate(legs, BANKROLL, cfg=None)
    assert _attr(res, "accept") is True, ("expected accept of the true arb", _reason(res))
    # hedged: the worst-case outcome is not a real loss (>= ~0). Holds under either
    # convention — signed worst-case P&L is the guaranteed (positive) profit, and a
    # max-loss-magnitude convention reports ~0 because no outcome loses money.
    assert _attr(res, "worst_case_loss") >= -TOL, \
        ("a true arb must not have a losing outcome", _attr(res, "worst_case_loss"))


# ── (5) FAKE ARBITRAGE: same shape as (4) but unenterable -> REJECT ────────────
def test_fake_arbitrage_rejected():
    # Identical +EV/arb SHAPE as (4), but the book is a trap: tiny liquidity, high
    # exit risk, and a huge bid/ask spread. The paper engine must NOT chase an arb it
    # could never actually enter and exit — reject on liquidity / exit-risk grounds.
    legs = [
        _leg(ch, model_p=0.25, price=0.325,
             liquidity=2.0, exit_risk=0.98, spread=0.45, has_data=False)
        for ch in ("A", "B", "C", "D")
    ]
    res = _evaluate(legs, BANKROLL, cfg=None)
    assert _attr(res, "accept") is not True, ("must REJECT an unenterable fake arb",
                                              _positions(res))
    assert _mentions(_reason(res), ("liquid", "exit", "spread", "risk")), \
        ("reject_reason must cite liquidity / exit-risk / spread", _reason(res))


# ── (6) EXPOSURE CAP: total cost capped at cfg.max_event_exposure_frac*bankroll ─
def test_exposure_cap_enforced():
    # 5 strongly-overvalued ME legs (model 0.20 vs price 0.35) -> the engine WANTS a
    # large NO book. With a deliberately tight event-exposure cap (3% of a $1000
    # bankroll = $30), the deployed exposure must be scaled UNDER the cap, or the
    # whole event rejected. Either way max_exposure may not breach the cap.
    cfg = _make_cfg(max_event_exposure_frac=0.03)
    cap = _exposure_cap(cfg, BANKROLL)  # 0.03 * 1000 = 30.0
    legs = [_leg(ch, model_p=0.20, price=0.35) for ch in ("A", "B", "C", "D", "E")]
    res = _evaluate(legs, BANKROLL, cfg=cfg)
    max_exp = _attr(res, "max_exposure")
    accepted = _attr(res, "accept") is True
    # scaled down under the cap OR rejected (a reject deploys nothing past the cap).
    assert (max_exp <= cap + TOL) or (not accepted), \
        ("max_exposure must not exceed the event-exposure cap", max_exp, cap, accepted)


def test_forced_me_single_leg_cannot_fabricate_certainty():
    # AUDIT #6: a FORCED mutually-exclusive event with only ONE eligible leg must NOT
    # renormalize p_norm to 1.0 and fabricate a guaranteed win. It falls back to an
    # independent binary, so the lone YES can lose its full stake (worst_case < 0).
    legs = [_leg("win", model_p=0.60, price=0.45)]
    res = _evaluate(legs, BANKROLL, cfg=_make_cfg(mutually_exclusive=True))
    worst = float(_attr(res, "worst_case_loss"))
    assert worst < 0.0, f"lone forced-ME leg fabricated a non-losing worst case: {worst}"
    assert _attr(res, "mutually_exclusive", False) is False   # fell back to independent binary


TESTS = [
    ("one_undervalued_yes", test_one_undervalued_yes),
    ("multiple_overvalued_nos", test_multiple_overvalued_nos),
    ("bad_multiple_yes_rejected", test_bad_multiple_yes_rejected),
    ("valid_arbitrage_accepted", test_valid_arbitrage_accepted),
    ("fake_arbitrage_rejected", test_fake_arbitrage_rejected),
    ("exposure_cap_enforced", test_exposure_cap_enforced),
    ("forced_me_single_leg_cannot_fabricate_certainty", test_forced_me_single_leg_cannot_fabricate_certainty),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
