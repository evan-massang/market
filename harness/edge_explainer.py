"""Plan 11 — edge explanation (PAPER-ONLY profit intelligence).

Turns a decision (a decision_features snapshot, a journal decision row, or a constructed dict)
into an honest, human-readable explanation of WHY the bot bet or — just as valuably — did NOT.

HONESTY RULES (enforced by tests):
  * Never claims "guaranteed", "free money", "safe profit", "risk-free", "sure thing" — see
    BANNED_PHRASES; a test asserts none appear in any output.
  * A no-bet is explained as a USEFUL outcome (a gate doing its job), never a failure.
  * Shows AFTER-COST EV, not just the raw edge.
  * Surfaces when disagreement / low evidence / low liquidity / a safety gate caused a reject.
"""
from __future__ import annotations

PAPER_ONLY_PROFIT_INTELLIGENCE = True

# language that overclaims certainty — must never appear in profit-intelligence output
BANNED_PHRASES = ("guaranteed", "free money", "safe profit", "risk-free", "riskless",
                  "sure thing", "can't lose", "cannot lose", "no risk", "easy money")


def _f(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _get(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def _is_bet(action) -> bool:
    return str(action or "").lower().startswith("bet")


def explain_edge(decision: dict) -> dict:
    """Explain a single decision. Read-only, never trades. Returns the canonical explanation."""
    d = decision or {}
    action = _get(d, "action", "status") or "unknown"
    is_bet = _is_bet(action)
    reason = _get(d, "reason", "why", "ev_reason") or ""
    blocked_by = _get(d, "blocked_by_gate") or (reason if (not is_bet and reason) else None)

    fp = _f(_get(d, "forecast_probability", "model_p", "final_p"))
    mp = _f(_get(d, "price", "market_p"))
    edge_raw = _f(_get(d, "edge_raw", "edge"))
    if edge_raw is None and fp is not None and mp is not None:
        edge_raw = fp - mp
    edge_ac = _f(_get(d, "edge_after_costs"))
    consensus = _f(_get(d, "consensus", "confidence"))
    divergence = _f(_get(d, "divergence"))
    evq = _f(_get(d, "evidence_quality"))
    liq = _f(_get(d, "liquidity"))
    spread = _f(_get(d, "spread"))
    mf_state = _get(d, "mirofish_state")
    mf_used = _get(d, "mirofish_used")
    acc = _get(d, "accounting_status")
    g2 = _get(d, "gate2_status")

    positive, negative, uncertainty = [], [], []

    # --- edge / EV (always prefer AFTER-COST) -------------------------------------------------
    if edge_raw is not None:
        (positive if edge_raw > 0 else negative).append(
            f"raw edge {edge_raw:+.1%} (forecast vs market price)")
    if edge_ac is not None:
        (positive if edge_ac > 0 else negative).append(
            f"after-cost EV {edge_ac:+.1%} (slippage + fees applied)")
    else:
        uncertainty.append("after-cost EV not computed for this record — raw edge shown only")

    # --- agreement / disagreement -------------------------------------------------------------
    if consensus is not None:
        (positive if consensus >= 0.5 else negative).append(
            f"swarm consensus {consensus:.2f}" + ("" if consensus >= 0.5 else " (below 0.50 → low agreement)"))
    if divergence is not None and divergence > 0.15:
        negative.append(f"swarm/challenger divergence {divergence:.2f} (> 0.15 → models disagree)")
    elif divergence is not None:
        positive.append(f"swarm/challenger agree (divergence {divergence:.2f})")

    # --- evidence -----------------------------------------------------------------------------
    if evq is not None:
        if evq >= 0.4:
            positive.append(f"evidence quality {evq:.2f}")
        elif evq > 0:
            negative.append(f"low evidence quality {evq:.2f}")
        else:
            negative.append("no usable evidence gathered")

    # --- microstructure -----------------------------------------------------------------------
    if liq is not None:
        (positive if liq >= 2000 else negative).append(
            f"liquidity ${liq:,.0f}" + ("" if liq >= 2000 else " (thin)"))
    if spread is not None and spread > 0.04:
        negative.append(f"wide spread (~{spread:.0%} vig)")

    # --- MiroFish (Plan 8 honesty — used ≠ alive) ---------------------------------------------
    if mf_used is not None:
        if mf_used:
            positive.append("MiroFish crowd simulation fed the forecast (fresh, matched run)")
        else:
            uncertainty.append("MiroFish not used for this decision (not a contribution)")
    elif mf_state:
        uncertainty.append(f"MiroFish state: {mf_state}")

    # --- uncertainty from missing data --------------------------------------------------------
    for label, val in (("forecast probability", fp), ("market price", mp), ("consensus", consensus),
                       ("evidence quality", evq), ("liquidity", liq)):
        if val is None:
            uncertainty.append(f"{label} unavailable in this record")
    if acc and acc not in ("ok",):
        uncertainty.append(f"accounting status: {acc} (performance unverified)")
    if g2 and g2 not in ("pass",):
        uncertainty.append(f"Gate 2: {g2} (not cleared for real money)")

    # --- safety gates a BET implies it cleared ------------------------------------------------
    safety_gates_passed = []
    if is_bet:
        safety_gates_passed = ["swarm_health", "model_agreement", "consensus", "evidence",
                               "after_cost_ev", "risk_guards", "exposure_cap", "wallet_atomic"]

    # --- narrative ----------------------------------------------------------------------------
    why_no_bet = None
    why_bet_if_bet = None
    if is_bet:
        bits = []
        if edge_ac is not None:
            bits.append(f"positive after-cost EV ({edge_ac:+.1%})")
        elif edge_raw is not None:
            bits.append(f"positive edge ({edge_raw:+.1%})")
        if consensus is not None and consensus >= 0.5:
            bits.append(f"swarm agreed ({consensus:.2f})")
        why_bet_if_bet = ("Paper bet placed because " + ", ".join(bits)
                          + ". This is a probabilistic paper position, not a certainty.") if bits \
            else "Paper bet placed; see recorded factors."
        summary = (f"PAPER BET {(_get(d,'side') or '').upper()} — "
                   + (f"after-cost EV {edge_ac:+.1%}" if edge_ac is not None
                      else f"edge {edge_raw:+.1%}" if edge_raw is not None else "see factors")
                   + ". Uncertain by nature; sized by risk rules.")
    else:
        # a no-bet is a SIGNAL, not a failure
        cause = reason or blocked_by or "below the edge/EV threshold"
        why_no_bet = (f"No paper bet — {cause}. This is the safety/EV logic working as intended "
                      f"(a skipped −EV or low-confidence market protects the paper bankroll), "
                      f"not a missed win.")
        summary = f"NO BET (useful signal) — {cause}."

    return {
        "summary": summary,
        "positive_factors": positive,
        "negative_factors": negative,
        "uncertainty_factors": uncertainty,
        "blocked_by": blocked_by,
        "why_no_bet": why_no_bet,
        "why_bet_if_bet": why_bet_if_bet,
        "safety_gates_passed": safety_gates_passed,
        "paper_only": True,
    }
