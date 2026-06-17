"""Plan 11 — opportunity ranking (PAPER-ONLY profit intelligence).

Ranks candidate markets BEFORE the expensive swarm forecast, using ONLY information available
at that point (liquidity, spread, volume, freshness, time-to-resolution, market type, exit risk,
stale/observe-only/event-coherence status, MiroFish *availability* config). It produces an
explainable score + bucket + reasons so the bot can prioritise where to spend a forecast.

HARD GUARANTEES (proven by tests + static scans in test_profit_intelligence.py):
  * This module NEVER opens a position, never calls wallet/safe_bet, never sizes a bet.
  * It NEVER reads settlement/outcome/CLV/later-price fields — ranking is pre-forecast only.
    The only signals it touches are the PRE_FORECAST_FIELDS whitelist below.
  * Ranking does NOT override any safety gate: a deterministically-blocked market is ranked
    `blocked` (so it is de-prioritised / not forecasted), never promoted.
  * Missing data lowers confidence (→ lower score + an "unknown" reason), never raises it.
"""
from __future__ import annotations

PAPER_ONLY_PROFIT_INTELLIGENCE = True

# The ONLY candidate keys this module is allowed to read. Deliberately excludes every
# settlement/outcome/post-trade field (outcome, payout, realized_pnl, settled_at, won, clv,
# final_price, close_price, resolution, result) so pre-forecast ranking cannot leak future data.
PRE_FORECAST_FIELDS = (
    "market_id", "question", "price", "_price", "outcome_prices", "liquidity", "volume",
    "end_date", "event_slug", "_label", "_hl", "_hours_left", "_theme", "_exit_risk",
    "_rank_score", "_subscores", "_stale", "_stale_reason", "_observe_only", "_fine_label",
    "_conf", "_event_coherent", "_mf_available",
)

# Fields that must NEVER influence a pre-forecast rank (asserted by a static source scan).
FUTURE_FIELDS = (
    "outcome", "payout", "realized_pnl", "settled_at", "won", "clv",
    "final_price", "close_price", "resolution", "result", "settled",
)

_HIGH = 0.66
_MEDIUM = 0.40


def _f(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _yes_price(c: dict):
    p = _f(c.get("_price"))
    if p is None:
        p = _f(c.get("price"))
    if p is None:
        op = c.get("outcome_prices")
        if isinstance(op, (list, tuple)) and op:
            p = _f(op[0])
    return p


def _spread_proxy(c: dict):
    """Tightness proxy from the two outcome prices (their sum's deviation from 1.0 = the vig).
    None when we cannot tell. NOT a future field — it is the *current* book."""
    op = c.get("outcome_prices")
    if isinstance(op, (list, tuple)) and len(op) >= 2:
        a, b = _f(op[0]), _f(op[1])
        if a is not None and b is not None:
            return abs((a + b) - 1.0)
    return None


def _norm(v, lo, hi):
    if v is None:
        return None
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _hours_left(c: dict):
    return _f(c.get("_hl")) if c.get("_hl") is not None else _f(c.get("_hours_left"))


def _deterministic_block(c: dict) -> str | None:
    """A reason string if this candidate is a DETERMINISTIC pre-forecast block (will not be
    bet regardless of the forecast), else None. These are de-prioritised, never promoted."""
    if not c.get("market_id"):
        return "no_market_id"
    if c.get("_stale"):
        return f"stale:{c.get('_stale_reason') or 'no_recent_trade'}"
    if c.get("_observe_only"):
        return f"observe_only:{c.get('_fine_label') or c.get('_theme') or 'label_backtest'}"
    if c.get("_event_coherent") is False:
        return "event_incoherent"
    price = _yes_price(c)
    if price is not None and not (0.02 < price < 0.98):
        return f"untradeable_price:{price:.3f}"
    return None


def rank_one(c: dict, *, held_market_ids=None, held_themes=None) -> dict:
    """Rank a single candidate. Pure; reads only PRE_FORECAST_FIELDS. Returns the canonical
    ranking dict. A deterministic block → bucket 'blocked' (NOT a high score)."""
    held_market_ids = held_market_ids or set()
    held_themes = held_themes or set()
    reasons: list[str] = []

    blocked = _deterministic_block(c)
    if blocked:
        return {
            "market_id": c.get("market_id"), "question": c.get("question"),
            "rank_score": 0.0, "rank_bucket": "blocked",
            "rank_reasons": [f"blocked: {blocked}"], "pre_forecast_only": True,
            "paper_only": True,
        }

    # already held / correlated same-theme exposure → de-prioritise (never block; it's a hint)
    mid = c.get("market_id")
    theme = c.get("_theme") or c.get("_fine_label")
    if mid in held_market_ids:
        reasons.append("already held → skip duplicate")
    elif theme and theme in held_themes:
        reasons.append(f"correlated exposure in theme '{theme}' → de-prioritise")

    parts: list[float] = []

    liq = _f(c.get("liquidity"))
    s = _norm(liq, 200.0, 8000.0)
    if s is None:
        reasons.append("liquidity unknown → lower confidence")
    else:
        parts.append(s)
        reasons.append(f"{'deep' if s > 0.6 else 'thin' if s < 0.3 else 'moderate'} liquidity"
                       + (f" (${liq:,.0f})" if liq is not None else ""))

    vol = _f(c.get("volume"))
    s = _norm(vol, 500.0, 50000.0)
    if s is None:
        reasons.append("volume unknown → lower confidence")
    else:
        parts.append(s)
        reasons.append(f"{'high' if s > 0.6 else 'low' if s < 0.3 else 'moderate'} volume")

    spread = _spread_proxy(c)
    if spread is None:
        reasons.append("spread unknown → lower confidence")
    else:
        s = 1.0 - max(0.0, min(1.0, spread / 0.10))    # 0 vig → 1.0, >=10c vig → 0.0
        parts.append(s)
        reasons.append(f"{'tight' if s > 0.6 else 'wide' if s < 0.3 else 'moderate'} spread")

    hl = _hours_left(c)
    if hl is None:
        reasons.append("time-to-resolution unknown → lower confidence")
    else:
        # too soon (<2h, no time for the edge to realise) or too far (>14d) is less attractive
        if hl < 2.0:
            s = 0.25
        elif hl <= 72.0:
            s = 1.0
        elif hl <= 336.0:
            s = 0.6
        else:
            s = 0.35
        parts.append(s)
        reasons.append(f"resolves in {hl:.1f}h")

    label = (c.get("_label") or "unknown")
    type_score = {"opinion": 1.0, "unknown": 0.5}.get(label, 0.2)
    parts.append(type_score)
    reasons.append(f"{label} market" + (" (forecastable)" if label == "opinion"
                   else " (mechanical — low priority)" if type_score < 0.3 else ""))

    exit_risk = _f(c.get("_exit_risk"))
    if exit_risk is not None:
        parts.append(1.0 - max(0.0, min(1.0, exit_risk)))
        if exit_risk > 0.6:
            reasons.append("high exit risk → harder to close")

    # MiroFish *availability* (config status only — NOT a run result, never future data)
    if c.get("_mf_available") is False:
        reasons.append("MiroFish backend unavailable (availability only — not a contribution)")

    if not parts:
        score = 0.0
        reasons.append("no rankable signals → unknown")
    else:
        score = sum(parts) / len(parts)
        # confidence penalty: the fewer signals we had, the more we shrink toward 0 (never up).
        # Aggressive enough that a single available signal (e.g. market type alone) can NEVER
        # reach the 'high' bucket — missing data must not look like a strong opportunity.
        coverage = len(parts) / 6.0
        score = round(score * (0.3 + 0.7 * min(1.0, coverage)), 4)

    bucket = "high" if score >= _HIGH else "medium" if score >= _MEDIUM else "low"
    return {
        "market_id": mid, "question": c.get("question"),
        "rank_score": score, "rank_bucket": bucket,
        "rank_reasons": reasons, "pre_forecast_only": True, "paper_only": True,
    }


def rank_candidates(candidates: list[dict], *, now=None, held_market_ids=None,
                    held_themes=None) -> list[dict]:
    """Rank candidates by pre-forecast attractiveness (highest first). Blocked candidates are
    ranked `blocked` and sorted last. Read-only, never trades. `now` is accepted for interface
    stability (ranking uses the candidate's own `_hl`/`end_date`, not wall-clock outcomes)."""
    out = []
    for c in (candidates or []):
        if not isinstance(c, dict):
            continue
        try:
            out.append(rank_one(c, held_market_ids=held_market_ids, held_themes=held_themes))
        except Exception:
            # a single malformed candidate is ranked unknown/low, never dropped silently as "good"
            out.append({"market_id": c.get("market_id") if isinstance(c, dict) else None,
                        "question": None, "rank_score": 0.0, "rank_bucket": "low",
                        "rank_reasons": ["ranking_error → unknown"], "pre_forecast_only": True,
                        "paper_only": True})
    # blocked last; then by score desc; stable
    order = {"blocked": 0}
    out.sort(key=lambda r: (1 if r["rank_bucket"] != "blocked" else 0, r["rank_score"]),
             reverse=True)
    return out
