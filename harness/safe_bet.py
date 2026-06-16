"""harness/safe_bet.py — the ONE safety-gated paper-position opener (Plan 3).

Every non-test path that wants to open a paper position must go through
``open_position_if_safe``. It runs the SAME safety stack predict_today / sameday
run inline (Plan 1 + Plan 2), in the same order, reusing the exact same gate
wrappers — so a "shortcut" path (strategy_bet, legacy loop, manual place_bet) can
no longer bypass EV / risk / bankroll / exposure / swarm-health.

    swarm-health (AI sources)  →  EV  →  risk  →  bankroll  →  exposure  →  open once

If ANY gate blocks: no `wallet.open_position` is called, a no-bet decision is
recorded (print + obs.on_trade_skip + journal.record_decision), and a structured
``{"opened": False, "reason": ...}`` is returned with the exact gate reason.
If all pass: `wallet.open_position` is called EXACTLY once and the bet is recorded.

This module also owns the disabled-by-default switches for the legacy shortcut
paths (``ENABLE_STRATEGY_BET`` / ``ENABLE_LEGACY_LOOP_BETTING``, both default
false) and the canonical Plan-3 no-bet reason vocabulary. Paper-only.
"""
from __future__ import annotations

import os

try:
    from harness import obs
except Exception:   # pragma: no cover - obs optional
    obs = None

# ── Plan-3 no-bet reason vocabulary ───────────────────────────────────────────
STRATEGY_DISABLED = "strategy_bet_disabled_by_default"
STRATEGY_MISSING_EV_PROB = "strategy_bet_missing_ev_probability_no_bet"
STRATEGY_GATE_BLOCKED = "strategy_bet_gate_blocked_no_bet"
LEGACY_LOOP_DISABLED = "legacy_loop_betting_disabled_by_default"
LEGACY_LOOP_FALLBACK = "legacy_loop_fallback_probability_no_bet"
SHORTCUT_BLOCKED = "shortcut_path_blocked_no_bet"

# AI-forecast sources: these MUST carry swarm-health metadata (Plan 2) or they block.
_AI_SOURCES = ("predict_today", "sameday", "place_bet", "loop", "ai")


def _env_true(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def strategy_bet_enabled() -> bool:
    """ENABLE_STRATEGY_BET — default FALSE. The favorite-longshot strategy bets on a
    price pattern, not a real forecast, so it is OFF unless explicitly opted in."""
    return _env_true("ENABLE_STRATEGY_BET", "false")


def legacy_loop_betting_enabled() -> bool:
    """ENABLE_LEGACY_LOOP_BETTING — default FALSE. loop.run_once's paper-bet opening is
    legacy + ungated; settlement/scoring stay on, only the bet opening is gated off."""
    return _env_true("ENABLE_LEGACY_LOOP_BETTING", "false")


def _forecast_id():
    if not obs:
        return None
    try:
        return obs.current().get("forecast_id")
    except Exception:
        return None


def record_no_bet(source: str, market: dict, reason: str, *, probability=None,
                  price=None, context: dict | None = None) -> dict:
    """Make a blocked attempt VISIBLE everywhere: print + obs.on_trade_skip +
    journal.record_decision(action='no_bet'). Returns a structured no-bet result."""
    market = market or {}
    mid, q = market.get("market_id"), market.get("question")
    print(f"[safe_bet:{source}] NO BET — {reason}")
    if obs:
        try:
            obs.hooks.on_trade_skip(
                forecast_id=_forecast_id(), reason=f"no_bet:{reason}",
                inputs={"source": source, "market_id": mid, "p": probability,
                        "price": price, "layer": "safe_bet", **(context or {})})
        except Exception:
            pass
    try:
        from harness import journal
        journal.record_decision(mid, q, probability, price, None, None, 0.0, None,
                                "", source, "no_bet", f"safe_bet[{source}] gate: {reason}.")
    except Exception:
        pass
    return {"opened": False, "source": source, "reason": reason, "market_id": mid}


def open_position_if_safe(*, source: str, market: dict, side: str, probability: float,
                          price: float, stake: float, confidence: float | None = None,
                          forecast_meta: dict | None = None, evidence_meta: dict | None = None,
                          reason_context: dict | None = None, wallet_config=None) -> dict:
    """Open a paper position ONLY if the full safety stack passes. Returns
    ``{"opened": bool, "reason": str, ...}``. Never opens more than one position;
    never opens when any gate blocks. ``source`` labels the caller (e.g.
    'strategy_bet', 'loop', 'place_bet')."""
    from harness import wallet
    # Reuse the EXACT Plan 1 + Plan 2 gate wrappers so every path is byte-consistent
    # with predict_today / sameday. (Lazy import keeps safe_bet import-light + cycle-free.)
    from harness.predict_today import (_p7_ev_gate, _p8_risk_guards, _p9_can_trade,
                                       _p9_exposure_ok, _p_swarm_health)
    market = market or {}
    mid, q = market.get("market_id"), market.get("question")
    ctx = dict(reason_context or {})

    # 1. SWARM HEALTH — required for AI-forecast sources. An AI source with no health
    #    metadata is unsafe (can't prove the swarm was healthy) -> block.
    if forecast_meta is not None or source in _AI_SOURCES:
        sh_ok, sh_reason = _p_swarm_health(forecast_meta if isinstance(forecast_meta, dict) else {})
        if not sh_ok:
            return record_no_bet(source, market, sh_reason, probability=probability, price=price, context=ctx)

    # 2. EV gate (after-costs, with spread/liquidity/exit-risk penalties from the market)
    ev_ok, ev_reason = _p7_ev_gate(probability, price, side, m=market, confidence=confidence)
    if not ev_ok:
        return record_no_bet(source, market, ev_reason, probability=probability, price=price, context=ctx)

    # 3. RISK guard (market-quality + correlation + bad-theme)
    rg_ok, rg_reason = _p8_risk_guards(market, side, q)
    if not rg_ok:
        return record_no_bet(source, market, rg_reason, probability=probability, price=price, context=ctx)

    # 4. BANKROLL kill switch
    ct_ok, ct_reason = _p9_can_trade()
    if not ct_ok:
        return record_no_bet(source, market, ct_reason, probability=probability, price=price, context=ctx)

    # 5. EXPOSURE cap (per theme / event)
    ex_ok, ex_reason = _p9_exposure_ok(q, market.get("event_slug"), stake)
    if not ex_ok:
        return record_no_bet(source, market, ex_reason, probability=probability, price=price, context=ctx)

    # ── all gates passed → open EXACTLY once ──
    try:
        edge = round(float(probability) - float(price), 4)
    except (TypeError, ValueError):
        return record_no_bet(source, market, SHORTCUT_BLOCKED, probability=probability, price=price, context=ctx)
    kwargs = {"end_date": market.get("end_date"), "event_slug": market.get("event_slug")}
    if wallet_config is not None:
        kwargs["cfg"] = wallet_config
    fr = wallet.open_position(mid, q, side, probability, price, edge, stake, **kwargs)
    if not getattr(fr, "opened", False):
        # the wallet's own guardrail (cash / per-bet / exposure cap) rejected it
        return record_no_bet(source, market, f"wallet_rejected: {getattr(fr, 'reason', '?')}",
                             probability=probability, price=price, context=ctx)
    try:
        from harness import journal
        journal.record_decision(mid, q, probability, price, edge, fr.side, fr.stake, fr.fill_price,
                                "", source, "bet",
                                f"safe_bet[{source}]: all gates passed → {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f}.")
    except Exception:
        pass
    print(f"[safe_bet:{source}] OPENED {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f}")
    return {"opened": True, "source": source, "side": fr.side, "stake": fr.stake,
            "fill_price": fr.fill_price, "shares": getattr(fr, "shares", None),
            "edge": edge, "market_id": mid, "reason": "all_gates_passed"}
