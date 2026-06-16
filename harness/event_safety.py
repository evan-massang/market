"""harness/event_safety.py — Plan 6 event-portfolio EXECUTION safety policy.

The event-portfolio engine (`harness.event_portfolio.evaluate_event`) is PURE: it
only RECOMMENDS a coherent basket. The live consumers (predict_today / sameday)
open exactly ONE leg per cycle — the current market's slot (`my_pos`) — never the
whole basket, never atomically, and they read sibling legs at STALE open-prices
over an INCOMPLETE held-sibling legset. So a multi-leg / "arbitrage" basket can
NEVER be held complete: opening a single NO leg of a 4-leg hedge is NOT risk-free.

This module enforces the conservative policy:
  * a multi-leg / arbitrage basket is NOT executable (no atomic multi-leg executor
    exists) → DISABLED by default; the engine's recommendation is observe-only.
  * only a genuine SINGLE-LEG edge opportunity may open — and only through the
    per-leg Plan 1-5 gate stack (EV/risk/bankroll/exposure), which validates it is
    INDEPENDENTLY +EV (not a hedge component).
  * a new YES is blocked when the book already holds a YES in the same event
    (one-YES coherence); a duplicate open on the same market is blocked.

Strict labels replace the loose "risk-free/arbitrage" claim. ``risk_free`` is never
emitted as an executable claim. Pure module (only `os`); no I/O, no cycle.
"""
from __future__ import annotations

import os

# ── strict labels (Plan 6 §3) ─────────────────────────────────────────────────
LABEL_SINGLE_LEG = "event_single_leg_opportunity"
LABEL_CANDIDATE_UNVERIFIED = "event_basket_candidate_unverified"
LABEL_VERIFIED_NOT_EXECUTABLE = "event_basket_verified_but_not_executable"
LABEL_EXECUTABLE = "event_basket_executable"
LABEL_BLOCKED = "event_basket_blocked"

# ── no-bet reasons (Plan 6 §3/§5/§6) ──────────────────────────────────────────
INCOMPLETE_LEGSET = "event_incomplete_legset_no_bet"
STALE_LEGSET = "event_stale_legset_no_bet"
UNKNOWN_RELATIONSHIP = "event_unknown_relationship_no_bet"
FAKE_ARBITRAGE_BLOCKED = "event_fake_arbitrage_blocked_no_bet"
BASKET_NOT_EXECUTABLE = "event_basket_not_executable_no_bet"
EXECUTION_DISABLED = "event_basket_execution_disabled_no_bet"
ALREADY_HOLD_YES = "event_already_hold_yes_no_bet"
MULTIPLE_YES_BLOCKED = "event_multiple_yes_blocked_no_bet"
INCOHERENT_POSITION = "event_incoherent_position_no_bet"
CORRELATED_EXPOSURE = "event_correlated_exposure_no_bet"


def multi_leg_execution_enabled() -> bool:
    """Atomic multi-leg basket execution is OFF by default. There is NO atomic
    batch executor (the consumers open one leg at a time), so this stays False and
    every multi-leg / arbitrage basket is observe-only until one is built+verified."""
    return os.getenv("ENABLE_EVENT_BASKET_EXECUTION", "false").strip().lower() in ("1", "true", "yes", "on")


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def validate_event_legset(legs, *, mutually_exclusive=None, event_id=None) -> dict:
    """Plan 6 §4 — assess a legset's completeness / staleness / relationship.

    ``legs``: list of dicts {leg_id, market_id, model_p, price, has_data, ...}.
    Conservative: an unknown relationship, a stale/missing leg price, a missing
    model_p, or fewer than 2 active legs means the basket CANNOT be evaluated.
    NOTE: ``exhaustive`` is always False here — the live legset is built from the
    current market + currently-HELD siblings, never the full event, so the set can
    never be proven exhaustive (hence no basket may be labeled risk-free)."""
    legs = list(legs or [])
    n = len(legs)
    active, stale, missing = [], [], []
    for l in legs:
        p = _f(l.get("price"))
        if p is None:
            stale.append(l.get("leg_id"))
        elif 0.0 < p < 1.0:
            active.append(l)
        if l.get("model_p") is None:
            missing.append(l.get("leg_id"))
    relationship_known = mutually_exclusive is not None
    if len(active) < 2:
        ok, reason = False, "fewer than 2 active legs — not a basket"
    elif not relationship_known:
        ok, reason = False, UNKNOWN_RELATIONSHIP
    elif stale:
        ok, reason = False, STALE_LEGSET
    elif missing:
        ok, reason = False, INCOMPLETE_LEGSET
    else:
        ok, reason = True, "ok"
    return {
        "ok": ok, "reason": reason, "event_id": event_id,
        "n_markets": n, "n_active_markets": len(active),
        "missing_required_legs": missing, "stale_legs": stale,
        "unknown_relationships": (not relationship_known),
        "mutually_exclusive": bool(mutually_exclusive),
        "exhaustive": False,                # held-sibling legset is never provably exhaustive
        "can_evaluate_basket": ok,
        "details": {"active": len(active), "stale": len(stale), "missing_model_p": len(missing)},
    }


def check_event_position_coherence(event_id, side, market_id, open_positions) -> dict:
    """Plan 6 §5 — block incoherent adds against the CURRENT open book.

    Returns ``{ok, reason, detail}``. Blocks: a duplicate open on this market; a new
    YES when a YES is already held elsewhere in the same event (one-YES coherence).
    """
    side = str(side or "").upper()
    positions = open_positions or []
    if any(p.get("market_id") == market_id for p in positions):
        return {"ok": False, "reason": INCOHERENT_POSITION,
                "detail": f"market {market_id} already has an open position (duplicate)"}
    in_event = [p for p in positions
                if event_id and p.get("event_slug") == event_id and p.get("market_id") != market_id]
    held_yes = [p for p in in_event if str(p.get("side") or "").upper() == "YES"]
    if side == "YES" and held_yes:
        return {"ok": False, "reason": ALREADY_HOLD_YES,
                "detail": f"already hold {len(held_yes)} YES leg(s) in event {event_id!r}"}
    return {"ok": True, "reason": "ok",
            "detail": f"{len(in_event)} sibling position(s) in event {event_id!r}"}


def classify_event_execution(ep, my_pos) -> tuple[str, bool, str | None]:
    """Decide whether THIS leg may EXECUTE. Returns ``(label, executable, reason)``.

    * engine rejected            → (blocked, False, None)  [caller uses ep.reject_reason]
    * arbitrage / hedge basket   → (verified-but-not-executable, False, EXECUTION_DISABLED)
        a hedge is only risk-free if ALL legs are held; we open one leg at a time over
        an incomplete, stale legset with no atomic executor → executing one leg is
        partial + fake-risk-free, so it is DISABLED.
    * this leg not selected      → (blocked, False, None)
    * single-leg edge            → (single-leg, True, None)
        opening one independently-+EV leg is fine — the per-leg EV/risk/bankroll/
        exposure gate stack validates it standalone (it is NOT a basket claim).
    """
    if not getattr(ep, "accept", False):
        return LABEL_BLOCKED, False, None
    if bool(getattr(ep, "is_arbitrage", False)):
        # A hedge/arb is risk-free ONLY if ALL legs are held atomically. There is NO
        # atomic multi-leg executor — the consumers open ONE leg at a time — so a single
        # arb leg is ALWAYS partial / fake-risk-free. Block UNCONDITIONALLY: even with the
        # env opt-in below, the single-leg consumer path can never execute an arb safely
        # (the flag is reserved for a FUTURE atomic batch opener that does not exist yet).
        return LABEL_VERIFIED_NOT_EXECUTABLE, False, EXECUTION_DISABLED
    if my_pos is None:
        return LABEL_BLOCKED, False, None
    return LABEL_SINGLE_LEG, True, None
