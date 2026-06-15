"""P3 — event-portfolio engine (PURE: no I/O, no network, fully deterministic).

Given the LEGS of one event (the output of scanner.group_events → Event.legs,
plus a per-leg model probability and live price), decide the *coherent set* of
paper positions to hold across the whole event — instead of sizing each leg in
isolation. The classic case is a MUTUALLY-EXCLUSIVE event ("who wins?"): exactly
one leg resolves YES, so the legs are not independent and the portfolio's real
risk is the worst single outcome, not the sum of per-leg risks.

What this module does (and only this — it is NOT wired into the pipeline):
  • net_cost(side, price, liquidity): per-share effective cost that REUSES
    wallet.py's slippage+fee model (see wallet.open_position): a YES share costs
    clamp(price + slippage, .01, .99); a NO share costs clamp((1-price)+slippage).
    A YES share pays $1 if the leg wins; a NO share pays $1 if it does NOT.
  • evaluate_event(legs, bankroll, cfg) → EventPortfolio: build a candidate set
    of positions, enumerate every event outcome, compute EV / worst-case /
    best-case / max-exposure, and ACCEPT or REJECT with a full explanation.

Construction rules (per the plan):
  • NO on every leg the model thinks is OVERVALUED (model_p < price − margin).
  • at most ONE YES, on the single most-undervalued leg (largest model_p − price
    > margin) — mirrors predict_today's Guard D "one YES (winner) per event".
  • detect a true ARBITRAGE / hedge: buying NO on every leg of an ME event pays
    out on all-but-the-winner, so if that guaranteed payoff exceeds total cost
    it is risk-free money — take it, sized to the exposure cap.
  • size each leg with fractional-Kelly via sizing.size_bet (the ONE sizer),
    then scale the whole basket down so total capital-at-risk ≤ the event cap.

EV / payoff math:
  • MUTUALLY-EXCLUSIVE: outcomes = {leg i wins}. P_model_norm(i) = model_p_i /
    Σ model_p (normalized so the ME probabilities sum to 1). For each outcome,
    portfolio payoff = Σ(positions that pay under that outcome) − total cost.
    EV = Σ_o P_norm(o)·payoff(o); worst_case_loss = min_o payoff(o); etc.
  • non-ME / single market: each leg is an independent 2-outcome binary
    (YES wins w.p. model_p, NO wins w.p. 1−model_p); EV is the sum of the
    per-leg EVs and the joint worst case is "every position loses" (= −exposure).

NOTE on calibration: model_p is the *raw* model probability passed in. It is NOT
yet calibrated (that is P6); sizing uses it as-is via size_bet, exactly like the
live harness, and the ME normalization above is the only transform applied — and
only for the EV/outcome math, never to fabricate an edge. If raw model_p over a
ME event sums far above 1, normalization deflates each leg's true win-prob, which
can flip a "selected" YES leg to negative EV — caught by the free-−EV-leg reject.

Run the self-test:  python -m harness.event_portfolio   (or python event_portfolio.py)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ── reuse wallet.py's cost params and sizing.py's Kelly sizer ─────────────────
# (import-guarded so the module also runs standalone for the __main__ self-test)
try:
    from harness.wallet import WalletConfig as _WalletConfig  # type: ignore
except Exception:  # pragma: no cover - standalone fallback
    try:
        from wallet import WalletConfig as _WalletConfig  # type: ignore
    except Exception:
        _WalletConfig = None  # type: ignore

try:
    from harness.sizing import (  # type: ignore
        size_bet as _size_bet,
        DEFAULT_LAMBDA as _DEF_LAM,
        DEFAULT_CAP as _DEF_CAP,
        DEFAULT_MIN_EDGE as _DEF_MIN_EDGE,
    )
except Exception:  # pragma: no cover - standalone fallback
    try:
        from sizing import (  # type: ignore
            size_bet as _size_bet,
            DEFAULT_LAMBDA as _DEF_LAM,
            DEFAULT_CAP as _DEF_CAP,
            DEFAULT_MIN_EDGE as _DEF_MIN_EDGE,
        )
    except Exception:
        _size_bet = None  # type: ignore
        _DEF_LAM, _DEF_CAP, _DEF_MIN_EDGE = 0.25, 0.02, 0.02

_W = _WalletConfig() if _WalletConfig is not None else None
_WALLET_SLIPPAGE = _W.slippage if _W is not None else 0.01
_WALLET_FEE_FRAC = _W.fee_frac if _W is not None else 0.0

_EPS = 1e-9


# ── minimal standalone fallback for size_bet (only used if the import fails) ──
@dataclass
class _Sz:
    side: Optional[str]
    edge: float
    f_star: float
    fraction: float
    stake: float


def _fallback_size_bet(p, c, bankroll, lam=_DEF_LAM, cap=_DEF_CAP, min_edge=_DEF_MIN_EDGE):
    """Mirror of sizing.size_bet for standalone runs (kept byte-faithful in spirit)."""
    if not (0.0 < c < 1.0) or not (0.0 <= p <= 1.0) or bankroll <= 0:
        return _Sz(None, p - c, 0.0, 0.0, 0.0)
    if p > c + _EPS:
        side, f_star = "YES", (p - c) / (1.0 - c)
    elif p < c - _EPS:
        side, f_star = "NO", (c - p) / c
    else:
        return _Sz(None, p - c, 0.0, 0.0, 0.0)
    if abs(p - c) < min_edge:
        return _Sz(None, p - c, f_star, 0.0, 0.0)
    fraction = min(lam * f_star, cap)
    return _Sz(side, p - c, f_star, fraction, round(fraction * bankroll, 6))


_SIZER = _size_bet if _size_bet is not None else _fallback_size_bet


# ── configuration ─────────────────────────────────────────────────────────────
@dataclass
class Config:
    """Tunable knobs for the event-portfolio engine. Defaults per the P3 plan."""
    margin: float = 0.03                 # |model_p − price| must beat this to act on a leg
    max_worst_case_frac: float = 0.05    # reject if worst-case loss > this·bankroll
    max_event_exposure_frac: float = 0.10  # cap total capital-at-risk per event
    max_exit_risk: float = 0.7           # reject if a material leg's exit_risk exceeds this
    min_leg_ev: float = 0.0              # a leg below this EV is "negative-EV"
    # tri-state: True/False forces the mode; None auto-detects (≥2 competing legs ⇒
    # mutually-exclusive, a single market ⇒ independent 2-outcome binary). The live
    # pipeline should pass scanner Event.mutually_exclusive explicitly.
    mutually_exclusive: Optional[bool] = None
    # Guard D(b) (mirrors predict_today.MAX_GROUP_PROB_SUM): a ME event whose model_p
    # sums past this is self-contradictory (only one leg can win) — reject it.
    max_group_prob_sum: float = 1.20

    # cost model — mirrors harness.wallet.WalletConfig (single source of truth)
    slippage: float = _WALLET_SLIPPAGE   # absolute price worsening on the share you buy
    fee_frac: float = _WALLET_FEE_FRAC   # per-trade fee as a fraction of stake

    # sizing — mirrors harness.sizing defaults (the ONE sizer)
    kelly_lambda: float = _DEF_LAM       # fractional-Kelly multiplier (quarter-Kelly)
    kelly_cap: float = _DEF_CAP          # per-leg hard cap (fraction of bankroll)
    min_edge: float = _DEF_MIN_EDGE      # sizing's own thin-edge floor

    # liquidity-aware slippage — OFF by default ⇒ identical to wallet's flat slippage.
    # When on, a thin book (liquidity < liquidity_ref) widens the slippage on net_cost.
    liquidity_slippage: bool = False
    liquidity_ref: float = 1000.0

    # a position whose capital-at-risk exceeds this fraction of bankroll is "material"
    # (only material legs are gated on exit_risk)
    material_stake_frac: float = 1e-6

    def to_dict(self) -> dict:
        return asdict(self)


# ── per-share cost (reuses wallet.py's slippage + fee) ────────────────────────
def net_cost(side: str, price: float, liquidity: Optional[float] = None,
             cfg: Optional[Config] = None) -> float:
    """Effective per-share cost of one share, REUSING wallet.open_position's model.

    YES share → base = price; NO share → base = 1 − price. fill = clamp(base +
    slippage, 0.01, 0.99) (the exact clamp wallet uses), then a per-share fee of
    fee_frac (wallet charges fee_frac·stake; per share that is fee_frac·fill, so
    total per-share cost = fill·(1 + fee_frac)). A YES share pays $1 if the leg
    wins; a NO share pays $1 if the leg does NOT win.

    `liquidity` only affects the price when cfg.liquidity_slippage is enabled
    (default OFF, so this is byte-faithful to wallet's flat slippage); otherwise
    liquidity is captured by the leg's exit_risk, which gates acceptance instead.
    """
    cfg = cfg or Config()
    if side not in ("YES", "NO"):
        raise ValueError(f"bad side {side!r}")
    base = price if side == "YES" else (1.0 - price)
    slip = cfg.slippage
    if cfg.liquidity_slippage and liquidity is not None and 0.0 < liquidity < cfg.liquidity_ref:
        slip = cfg.slippage * (cfg.liquidity_ref / liquidity)
    fill = min(max(base + slip, 0.01), 0.99)
    return fill * (1.0 + cfg.fee_frac)


# ── internal leg representation ───────────────────────────────────────────────
@dataclass
class _Leg:
    leg_id: Any
    market_id: Any
    model_p: Optional[float]
    price: Optional[float]
    liquidity: Optional[float]
    exit_risk: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    has_data: bool      # INTRINSIC: a tradeable price in (0,1) and a valid model_p
    model_data: bool    # EXPLICIT has_data flag: was model_p built from real data?


def _coerce(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _to_leg(d: dict, idx: int) -> _Leg:
    leg_id = d.get("leg_id", d.get("market_id", idx))
    price = _coerce(d.get("price"))
    model_p = _coerce(d.get("model_p"))
    liquidity = _coerce(d.get("liquidity"))
    exit_risk = _coerce(d.get("exit_risk"))
    # has_data: a real, tradeable price strictly in (0,1) and a valid model_p in [0,1].
    # Degenerate prices (≤0 or ≥1) or a missing model_p ⇒ untradeable, drop the leg.
    has_data = (price is not None and 0.0 < price < 1.0
                and model_p is not None and 0.0 <= model_p <= 1.0)
    # The EXPLICIT has_data flag means "model_p built from real data". It does NOT
    # make a tradeable leg untradeable — a price-driven NO/arb still works without a
    # trustworthy model_p — but a model-EDGE (YES) leg that rests on no-data model_p
    # is illusory and is rejected downstream.
    explicit = d.get("has_data")
    model_data = True if explicit is None else bool(explicit)
    return _Leg(leg_id, d.get("market_id"), model_p, price, liquidity, exit_risk,
                _coerce(d.get("bid")), _coerce(d.get("ask")), has_data, model_data)


def _nodata_reason(l: _Leg) -> str:
    if l.price is None:
        return "missing price (no data)"
    if not (0.0 < l.price < 1.0):
        return f"degenerate price {l.price} (≤0 or ≥1 — untradeable)"
    if l.model_p is None:
        return "missing model_p (no data)"
    if not (0.0 <= l.model_p <= 1.0):
        return f"model_p {l.model_p} out of [0,1]"
    return "flagged has_data=False (model built from no data)"


# ── output dataclass ──────────────────────────────────────────────────────────
@dataclass
class EventPortfolio:
    accept: bool
    positions: list = field(default_factory=list)   # [{leg_id, side, stake, shares, leg_ev, reason, ...}]
    rejected: list = field(default_factory=list)     # [{leg_id, reason}]
    portfolio_ev: float = 0.0
    worst_case_loss: float = 0.0                     # min over outcomes of portfolio P&L (≤0 = loss)
    best_case_profit: float = 0.0                    # max over outcomes of portfolio P&L
    max_exposure: float = 0.0                        # total capital at risk (sum of stakes)
    losing_outcome: Optional[dict] = None            # which outcome produces worst_case_loss
    reject_reason: Optional[str] = None
    explanation: str = ""
    is_arbitrage: bool = False
    mutually_exclusive: bool = False
    n_outcomes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── outcome / payoff helpers ──────────────────────────────────────────────────
def _me_payoffs(positions: list, eligible: list, total_p: float) -> list:
    """For a ME event, one row per outcome (leg `winner` resolves YES). payoff =
    gross payout under that outcome − total cost spent (net portfolio P&L)."""
    total_cost = sum(p["cost"] for p in positions)
    rows = []
    for l in eligible:
        p_norm = (l.model_p / total_p) if total_p > _EPS else 0.0
        gross = 0.0
        for pos in positions:
            same = pos["leg"].leg_id == l.leg_id
            if pos["side"] == "YES":
                if same:
                    gross += pos["shares"]
            else:  # NO pays unless this leg is the winner
                if not same:
                    gross += pos["shares"]
        rows.append({"winner": l.leg_id, "prob": p_norm, "gross": gross,
                     "payoff": gross - total_cost})
    return rows


def _worst_without(target, positions, eligible, total_p, is_me) -> float:
    """Worst-case portfolio P&L with `target` removed — used to test whether a
    negative-EV leg is paying for itself by tightening the downside (a hedge)."""
    remaining = [p for p in positions if p is not target]
    if not remaining:
        return 0.0
    if is_me:
        return min(r["payoff"] for r in _me_payoffs(remaining, eligible, total_p))
    return -sum(p["cost"] for p in remaining)


def _win_prob(pos, is_me, p_norm_map) -> float:
    """P(this position pays $1): normalized P(leg wins) for ME, raw model_p else."""
    leg = pos["leg"]
    p_yes = p_norm_map[leg.leg_id] if is_me else leg.model_p
    return p_yes if pos["side"] == "YES" else (1.0 - p_yes)


def _size_position(leg: _Leg, bankroll: float, cfg: Config, edge: float):
    """Size one leg via the shared Kelly sizer. Returns a position dict or None
    if the sizer declines (no edge / below its own min_edge / depleted bankroll)."""
    s = _SIZER(leg.model_p, leg.price, bankroll,
               lam=cfg.kelly_lambda, cap=cfg.kelly_cap, min_edge=cfg.min_edge)
    if s.side is None or s.stake <= 0:
        return None
    per_share = net_cost(s.side, leg.price, leg.liquidity, cfg)
    shares = (s.stake / per_share) if per_share > 0 else 0.0
    return {"leg": leg, "side": s.side, "shares": shares, "per_share_cost": per_share,
            "cost": s.stake, "edge": edge, "reason": _select_reason(s.side, leg, edge)}


def _select_reason(side: str, leg: _Leg, edge: float) -> str:
    if side == "YES":
        return (f"undervalued winner: model_p {leg.model_p:.3f} > price {leg.price:.3f} "
                f"(edge {edge:+.3f} beats margin) — the one YES (winner) leg")
    return (f"overvalued: model_p {leg.model_p:.3f} < price {leg.price:.3f} "
            f"(edge {edge:+.3f}) — fade it with NO")


# ── main entry point ──────────────────────────────────────────────────────────
def evaluate_event(legs, bankroll, cfg: Optional[Config] = None) -> EventPortfolio:
    """Evaluate the legs of one event and return an EventPortfolio decision.

    legs: iterable of dicts {leg_id, market_id, model_p, price, liquidity,
          exit_risk, bid=None, ask=None}. The event is mutually-exclusive iff
          cfg.mutually_exclusive (exactly one leg resolves YES).
    bankroll: current paper bankroll (dollars). cfg: Config (defaults if None).
    """
    cfg = cfg or Config()
    raw = [_to_leg(d, i) for i, d in enumerate(legs or [])]

    rejected: list = []
    eligible: list = []
    for l in raw:
        (eligible if l.has_data else rejected).append(
            l if l.has_data else {"leg_id": l.leg_id, "reason": _nodata_reason(l)})

    # Resolve the mode. Explicit cfg.mutually_exclusive wins; otherwise auto-detect:
    # ≥2 competing legs ⇒ one winner (ME); a single market ⇒ independent binary.
    if cfg.mutually_exclusive is None:
        is_me = len(eligible) >= 2
    else:
        is_me = bool(cfg.mutually_exclusive)

    # guard: no usable legs / depleted bankroll  →  no portfolio
    if not eligible:
        return _decision(cfg, is_me, bankroll, [], [], rejected, False,
                         "no leg has usable data (degenerate or missing price/model_p)")
    if bankroll is None or bankroll <= 0:
        rejected = rejected + [{"leg_id": l.leg_id, "reason": "bankroll depleted"} for l in eligible]
        return _decision(cfg, is_me, bankroll, [], [], rejected, False, "bankroll depleted")

    total_p = sum(l.model_p for l in eligible)
    if is_me and total_p <= _EPS:
        rejected = rejected + [{"leg_id": l.leg_id, "reason": "model_p sums to 0 over ME legs (no data)"} for l in eligible]
        return _decision(cfg, is_me, bankroll, eligible, [], rejected, False,
                         "ME model_p sums to zero (degenerate / no data) — any positive EV would be illusory")

    # Guard D(b) — model incoherence. In a mutually-exclusive event exactly one leg
    # wins, so the model's YES-probs must sum to ≈1; a sum past max_group_prob_sum
    # means the model is self-contradictory (e.g. three legs each "60% to win").
    # Reject the whole event rather than cherry-pick a leg from an incoherent model.
    if is_me and total_p > cfg.max_group_prob_sum + _EPS:
        rejected = rejected + [{"leg_id": l.leg_id,
                                "reason": f"incoherent ME event (YES-prob sum {total_p:.2f})"} for l in eligible]
        return _decision(cfg, is_me, bankroll, eligible, [], rejected, False,
                         f"incoherent / contradictory mutually-exclusive event: model_p sums to {total_p:.2f} "
                         f"> {cfg.max_group_prob_sum:.2f} (only one leg can win, so betting these YES probs is "
                         f"self-contradictory)")
    p_norm_map = {l.leg_id: (l.model_p / total_p if is_me else l.model_p) for l in eligible}

    # ── construction ──────────────────────────────────────────────────────────
    positions: list = []
    is_arb = False

    # (1) true arbitrage / hedge: NO on every leg of a ME event. Exactly one leg
    #     wins ⇒ N−1 of the NO shares pay $1 no matter which. If that guaranteed
    #     payoff exceeds total cost, it is risk-free — size it to the exposure cap.
    if is_me and len(eligible) >= 2:
        no_costs = [net_cost("NO", l.price, l.liquidity, cfg) for l in eligible]
        sum_cost = sum(no_costs)
        guaranteed = float(len(eligible) - 1)
        if sum_cost > 0 and (guaranteed - sum_cost) > _EPS:
            is_arb = True
            cap_dollars = cfg.max_event_exposure_frac * bankroll
            shares = cap_dollars / sum_cost           # equal shares on every leg
            for l, c in zip(eligible, no_costs):
                positions.append({
                    "leg": l, "side": "NO", "shares": shares, "per_share_cost": c,
                    "cost": shares * c, "edge": l.model_p - l.price,
                    "reason": "arbitrage hedge: NO on every ME leg — guaranteed payoff (N−1 shares) exceeds total cost",
                })

    # (2) normal edge-based construction (skipped when an arbitrage basket was built)
    if not positions:
        yes_candidates = []
        for l in eligible:
            edge = l.model_p - l.price
            if edge < -cfg.margin:                    # overvalued ⇒ NO (unlimited)
                pos = _size_position(l, bankroll, cfg, edge)
                if pos is not None:
                    positions.append(pos)
                else:
                    rejected.append({"leg_id": l.leg_id,
                                     "reason": f"overvalued (edge {edge:+.3f}) but sizer declined (below min_edge)"})
            elif edge > cfg.margin:                   # undervalued ⇒ YES candidate
                yes_candidates.append((edge, l))
            else:
                rejected.append({"leg_id": l.leg_id,
                                 "reason": f"edge {edge:+.3f} within ±margin {cfg.margin:.3f} — no bet"})

        # at most ONE YES — the single most-undervalued leg (one-YES-per-event)
        if yes_candidates:
            yes_candidates.sort(key=lambda t: (t[0], str(t[1].leg_id)), reverse=True)
            best_edge, best_leg = yes_candidates[0]
            yp = _size_position(best_leg, bankroll, cfg, best_edge)
            if yp is not None:
                positions.append(yp)
            else:
                rejected.append({"leg_id": best_leg.leg_id,
                                 "reason": "top YES leg but sizer declined (below min_edge)"})
            for e, l in yes_candidates[1:]:
                rejected.append({"leg_id": l.leg_id,
                                 "reason": f"undervalued (edge {e:+.3f}) but not the top YES leg — one YES (winner) per event"})

    # nothing actionable
    if not positions:
        return _decision(cfg, is_me, bankroll, eligible, [], rejected, False,
                         "no actionable edge (no leg beats the margin; no arbitrage present)")

    # (3) scale the whole basket down to the per-event exposure cap (arb is already
    #     sized exactly to the cap, so only the edge-based path can need scaling)
    total_cost = sum(p["cost"] for p in positions)
    cap_dollars = cfg.max_event_exposure_frac * bankroll
    if not is_arb and total_cost > cap_dollars + _EPS and total_cost > 0:
        factor = cap_dollars / total_cost
        for p in positions:
            p["cost"] *= factor
            p["shares"] *= factor

    # ── metrics: per-leg EV, then portfolio EV / worst / best / losing outcome ──
    for p in positions:
        q = _win_prob(p, is_me, p_norm_map)
        p["leg_ev"] = q * p["shares"] - p["cost"]

    max_exposure = sum(p["cost"] for p in positions)
    if is_me:
        rows = _me_payoffs(positions, eligible, total_p)
        portfolio_ev = sum(r["prob"] * r["payoff"] for r in rows)
        worst_row = min(rows, key=lambda r: r["payoff"])
        best_row = max(rows, key=lambda r: r["payoff"])
        worst_case_loss = worst_row["payoff"]
        best_case_profit = best_row["payoff"]
        losing_outcome = {"winner_leg_id": worst_row["winner"], "prob": round(worst_row["prob"], 6),
                          "payoff": round(worst_row["payoff"], 6)}
        n_outcomes = len(rows)
    else:
        portfolio_ev = sum(p["leg_ev"] for p in positions)
        worst_case_loss = -max_exposure                         # every independent position loses
        best_case_profit = sum(p["shares"] for p in positions) - max_exposure
        losing_outcome = {"winner_leg_id": None,
                          "description": "every position resolves against us (independent legs all lose)",
                          "payoff": round(worst_case_loss, 6)}
        n_outcomes = 2

    # ── rejection rules ─────────────────────────────────────────────────────────
    reasons: list = []

    # contradictory: YES on more than one mutually-exclusive leg
    n_yes = sum(1 for p in positions if p["side"] == "YES")
    if is_me and n_yes > 1:
        reasons.append(f"contradictory: YES on {n_yes} mutually-exclusive legs (only one can win)")

    # weak-liquidity gate: any MATERIAL-stake leg with exit_risk above the cap
    mat = cfg.material_stake_frac * bankroll
    for p in positions:
        er = p["leg"].exit_risk
        if p["cost"] > mat and er is not None and er > cfg.max_exit_risk:
            reasons.append(f"leg {p['leg'].leg_id} exit_risk {er:.2f} > max {cfg.max_exit_risk:.2f} "
                           f"(book too thin to exit a ${p['cost']:.2f} stake)")

    # worst-case loss exceeds the risk limit
    wc_cap = cfg.max_worst_case_frac * bankroll
    if worst_case_loss < -wc_cap - _EPS:
        reasons.append(f"worst-case loss ${-worst_case_loss:.2f} exceeds limit ${wc_cap:.2f} "
                       f"({cfg.max_worst_case_frac:.0%} of bankroll)")

    # max exposure exceeds the per-event cap
    if max_exposure > cap_dollars + 1e-6:
        reasons.append(f"max exposure ${max_exposure:.2f} exceeds cap ${cap_dollars:.2f} "
                       f"({cfg.max_event_exposure_frac:.0%} of bankroll)")

    # no free negative-EV legs: a −EV leg is only allowed if it tightens worst-case
    for p in positions:
        if p["leg_ev"] < cfg.min_leg_ev - 1e-12:
            wc_without = _worst_without(p, positions, eligible, total_p, is_me)
            improves = worst_case_loss > wc_without + 1e-12
            if not improves:
                reasons.append(f"free negative-EV leg {p['leg'].leg_id} (leg_ev ${p['leg_ev']:+.2f} "
                               f"< {cfg.min_leg_ev}) that does not improve worst-case")

    # no illusory EV: a model-EDGE (YES) position must not rest on a leg whose
    # model_p was built from no data (explicit has_data=False). Price-driven NO /
    # arbitrage legs are exempt — they do not depend on the model probability.
    for p in positions:
        if p["side"] == "YES" and not p["leg"].model_data:
            reasons.append(f"YES leg {p['leg'].leg_id} rests on model_p built from no data "
                           f"(has_data=False) — its edge would be illusory")

    accept = not reasons
    reject_reason = "; ".join(reasons) if reasons else None

    return _build(cfg, is_me, bankroll, eligible, positions, rejected, accept, reject_reason,
                  portfolio_ev, worst_case_loss, best_case_profit, max_exposure,
                  losing_outcome, is_arb, n_outcomes)


# ── result + explanation builders ────────────────────────────────────────────
def _public_positions(positions: list) -> list:
    out = []
    for p in positions:
        out.append({
            "leg_id": p["leg"].leg_id,
            "market_id": p["leg"].market_id,
            "side": p["side"],
            "stake": round(p["cost"], 6),
            "shares": round(p["shares"], 6),
            "per_share_cost": round(p["per_share_cost"], 6),
            "edge": round(p["edge"], 6),
            "leg_ev": round(p.get("leg_ev", 0.0), 6),
            "reason": p["reason"],
        })
    return out


def _explain(cfg, is_me, bankroll, eligible, positions, rejected, portfolio_ev,
             worst_case_loss, best_case_profit, max_exposure, losing_outcome,
             is_arb, accept, reject_reason) -> str:
    kind = "MUTUALLY-EXCLUSIVE" if is_me else "independent / single-market"
    br = bankroll if (bankroll is not None) else 0.0
    L = [f"Event portfolio ({kind}; {len(eligible)} legs with data; bankroll ${br:,.2f})."]
    if is_arb:
        L.append("ARBITRAGE: NO on every ME leg pays out on N−1 shares whichever leg wins; "
                 "guaranteed payoff exceeds total cost ⇒ risk-free.")
    if positions:
        L.append("Selected positions:")
        for p in positions:
            L.append(f"  • {p['side']} leg {p['leg'].leg_id}: stake ${p['cost']:.2f} → "
                     f"{p['shares']:.2f} shares @ ${p['per_share_cost']:.3f}; "
                     f"leg_ev ${p.get('leg_ev', 0.0):+.2f}; {p['reason']}")
    else:
        L.append("Selected positions: none.")
    if rejected:
        L.append("Skipped legs:")
        for r in rejected:
            L.append(f"  • leg {r['leg_id']}: {r['reason']}")
    L.append(f"Portfolio EV = ${portfolio_ev:+.2f} "
             f"(over the {'normalized ME outcomes' if is_me else 'per-leg binary outcomes'}).")
    L.append(f"Max exposure (capital at risk) = ${max_exposure:.2f} "
             f"(cap ${cfg.max_event_exposure_frac * br:.2f} = {cfg.max_event_exposure_frac:.0%} of bankroll).")
    if losing_outcome and losing_outcome.get("winner_leg_id") is not None:
        L.append(f"Worst case: leg {losing_outcome['winner_leg_id']} wins "
                 f"(p={losing_outcome.get('prob', 0.0):.2f}) → portfolio P&L ${worst_case_loss:+.2f}.")
    elif losing_outcome:
        L.append(f"Worst case: {losing_outcome.get('description', 'all positions lose')} "
                 f"→ portfolio P&L ${worst_case_loss:+.2f}.")
    else:
        L.append(f"Worst case: ${worst_case_loss:+.2f}.")
    L.append(f"Best case profit = ${best_case_profit:+.2f}.")
    wc_cap = cfg.max_worst_case_frac * br
    rel = "within" if worst_case_loss >= -wc_cap - _EPS else "EXCEEDS"
    L.append(f"Worst-case limit ${wc_cap:.2f} ({cfg.max_worst_case_frac:.0%}); worst case {rel} the limit"
             + (" — acceptable." if rel == "within" else " — REJECT."))
    L.append("DECISION: ACCEPT." if accept else f"DECISION: REJECT — {reject_reason}.")
    return "\n".join(L)


def _build(cfg, is_me, bankroll, eligible, positions, rejected, accept, reject_reason,
           portfolio_ev, worst_case_loss, best_case_profit, max_exposure,
           losing_outcome, is_arb, n_outcomes) -> EventPortfolio:
    explanation = _explain(cfg, is_me, bankroll, eligible, positions, rejected,
                           portfolio_ev, worst_case_loss, best_case_profit, max_exposure,
                           losing_outcome, is_arb, accept, reject_reason)
    return EventPortfolio(
        accept=accept,
        positions=_public_positions(positions),
        rejected=rejected,
        portfolio_ev=round(portfolio_ev, 6),
        worst_case_loss=round(worst_case_loss, 6),
        best_case_profit=round(best_case_profit, 6),
        max_exposure=round(max_exposure, 6),
        losing_outcome=losing_outcome,
        reject_reason=reject_reason,
        explanation=explanation,
        is_arbitrage=is_arb,
        mutually_exclusive=is_me,
        n_outcomes=n_outcomes,
    )


def _decision(cfg, is_me, bankroll, eligible, positions, rejected, accept, reject_reason) -> EventPortfolio:
    """Shorthand for the no-position / early-out branches (EV = worst = 0)."""
    losing = None
    return _build(cfg, is_me, bankroll, eligible, positions, rejected, accept, reject_reason,
                  0.0, 0.0, 0.0, 0.0, losing, False, (len(eligible) if is_me else 0))


# ── synthetic self-test ───────────────────────────────────────────────────────
def _selftest() -> int:
    failures = 0

    def check(name, cond, detail=""):
        nonlocal failures
        status = "PASS" if cond else "FAIL"
        if not cond:
            failures += 1
        print(f"  [{status}] {name}" + (f"  — {detail}" if detail and not cond else ""))

    BR = 1000.0
    print("=" * 74)
    print(" event_portfolio.py — synthetic self-test")
    print("=" * 74)

    # net_cost reuses wallet's clamp(base + slippage) with default fee_frac=0
    print("\n[net_cost — reuses wallet slippage/fee]")
    c_yes = net_cost("YES", 0.45)
    c_no = net_cost("NO", 0.45)
    check("YES share cost = price+slip", abs(c_yes - 0.46) < 1e-9, c_yes)
    check("NO share cost = (1-price)+slip", abs(c_no - 0.56) < 1e-9, c_no)
    check("YES near 1 clamps to 0.99", abs(net_cost("YES", 0.995) - 0.99) < 1e-9)
    check("0.01 floor binds with zero slippage",
          abs(net_cost("NO", 0.999, cfg=Config(slippage=0.0)) - 0.01) < 1e-9)

    # A — single undervalued market → YES, accept
    print("\n[A] single undervalued market → YES")
    a = evaluate_event([{"leg_id": "m1", "market_id": "m1", "model_p": 0.60,
                         "price": 0.45, "liquidity": 5000, "exit_risk": 0.2}], BR)
    check("accept", a.accept, a.reject_reason)
    check("one YES position", len(a.positions) == 1 and a.positions[0]["side"] == "YES")
    check("EV > 0", a.portfolio_ev > 0, a.portfolio_ev)
    check("worst-case = -stake", abs(a.worst_case_loss + a.positions[0]["stake"]) < 1e-6,
          a.worst_case_loss)

    # B — single overvalued market → NO, accept
    print("\n[B] single overvalued market → NO")
    b = evaluate_event([{"leg_id": "m2", "market_id": "m2", "model_p": 0.30,
                         "price": 0.50, "liquidity": 5000, "exit_risk": 0.2}], BR)
    check("accept", b.accept, b.reject_reason)
    check("one NO position", len(b.positions) == 1 and b.positions[0]["side"] == "NO")
    check("EV > 0", b.portfolio_ev > 0, b.portfolio_ev)

    # C — ME 3-leg: one YES (winner) + NO on the two overvalued losers
    print("\n[C] ME 3-leg → one YES + two NO")
    legs_c = [
        {"leg_id": "L0", "market_id": "L0", "model_p": 0.55, "price": 0.40, "liquidity": 9000, "exit_risk": 0.2},
        {"leg_id": "L1", "market_id": "L1", "model_p": 0.25, "price": 0.35, "liquidity": 9000, "exit_risk": 0.2},
        {"leg_id": "L2", "market_id": "L2", "model_p": 0.20, "price": 0.25, "liquidity": 9000, "exit_risk": 0.2},
    ]
    c = evaluate_event(legs_c, BR, Config(mutually_exclusive=True))
    sides = sorted((p["leg_id"], p["side"]) for p in c.positions)
    check("accept", c.accept, c.reject_reason)
    check("exactly one YES", sum(1 for p in c.positions if p["side"] == "YES") == 1, sides)
    check("two NO fades", sum(1 for p in c.positions if p["side"] == "NO") == 2, sides)
    check("EV > 0", c.portfolio_ev > 0, c.portfolio_ev)
    check("losing outcome = a leg winning", c.losing_outcome["winner_leg_id"] in {"L0", "L1", "L2"})
    check("n_outcomes == 3", c.n_outcomes == 3)
    print(c.explanation)

    # D — ME arbitrage: prices sum to 1.25 → NO basket is risk-free
    print("\n[D] ME arbitrage (overround) → risk-free NO basket")
    legs_d = [
        {"leg_id": "A", "market_id": "A", "model_p": 0.40, "price": 0.50, "liquidity": 9000, "exit_risk": 0.2},
        {"leg_id": "B", "market_id": "B", "model_p": 0.35, "price": 0.45, "liquidity": 9000, "exit_risk": 0.2},
        {"leg_id": "C", "market_id": "C", "model_p": 0.25, "price": 0.30, "liquidity": 9000, "exit_risk": 0.2},
    ]
    d = evaluate_event(legs_d, BR, Config(mutually_exclusive=True))
    check("flagged arbitrage", d.is_arbitrage, d.reject_reason)
    check("accept", d.accept, d.reject_reason)
    check("worst case is a PROFIT (risk-free)", d.worst_case_loss > 0, d.worst_case_loss)
    check("all legs are NO", all(p["side"] == "NO" for p in d.positions))
    check("exposure at the cap", abs(d.max_exposure - 0.10 * BR) < 1e-3, d.max_exposure)
    print(d.explanation)

    # E — degenerate price leg is dropped (no data)
    print("\n[E] degenerate prices → rejected / no data")
    e = evaluate_event([{"leg_id": "z", "market_id": "z", "model_p": 0.6,
                         "price": 1.0, "liquidity": 5000, "exit_risk": 0.1}], BR)
    check("reject (no usable data)", (not e.accept) and "usable data" in (e.reject_reason or ""),
          e.reject_reason)
    check("leg recorded in rejected[]", any(r["leg_id"] == "z" for r in e.rejected))

    # F — material stake on an illiquid (high exit-risk) leg → reject
    print("\n[F] high exit_risk material leg → reject")
    f = evaluate_event([{"leg_id": "q", "market_id": "q", "model_p": 0.60,
                         "price": 0.45, "liquidity": 50, "exit_risk": 0.9}], BR)
    check("reject", not f.accept, f.reject_reason)
    check("reason cites exit_risk", "exit_risk" in (f.reject_reason or ""), f.reject_reason)

    # G — no edge beyond margin → no bet
    print("\n[G] no edge → no actionable bet")
    g = evaluate_event([{"leg_id": "n", "market_id": "n", "model_p": 0.51,
                         "price": 0.50, "liquidity": 5000, "exit_risk": 0.1}], BR)
    check("reject (no actionable edge)", (not g.accept) and "no actionable edge" in (g.reject_reason or ""),
          g.reject_reason)
    check("no positions", len(g.positions) == 0)

    # H — worst-case loss too large (big single-leg stake via a loose cap) → reject
    print("\n[H] worst-case loss exceeds limit → reject")
    h = evaluate_event([{"leg_id": "big", "market_id": "big", "model_p": 0.70,
                         "price": 0.40, "liquidity": 9000, "exit_risk": 0.1}],
                       BR, Config(kelly_cap=0.10))   # 10% stake → 10% worst-case > 5% limit
    check("reject", not h.accept, h.reject_reason)
    check("reason cites worst-case", "worst-case loss" in (h.reject_reason or ""), h.reject_reason)
    check("EV was actually positive", h.portfolio_ev > 0, h.portfolio_ev)

    # I — ME, model_p sum = 1.19 (coherent enough to pass Guard D(b)), prices ~fair
    #     so the NO basket is NOT an arbitrage. Normalizing the winner's 0.60 by 1.19
    #     gives 0.504 < its 0.51 effective cost ⇒ the lone YES is fractionally −EV and
    #     hedges nothing ⇒ rejected as a free negative-EV leg.
    print("\n[I] ME normalization → free −EV leg rejected")
    legs_i = [
        {"leg_id": "w", "market_id": "w", "model_p": 0.60, "price": 0.50, "liquidity": 9000, "exit_risk": 0.1},
        {"leg_id": "x", "market_id": "x", "model_p": 0.59, "price": 0.52, "liquidity": 9000, "exit_risk": 0.1},
    ]
    i_res = evaluate_event(legs_i, BR, Config(mutually_exclusive=True))
    check("reject", not i_res.accept, i_res.reject_reason)
    check("reason cites negative-EV leg", "negative-EV" in (i_res.reject_reason or ""), i_res.reject_reason)
    check("only one YES was built (loser skipped)",
          sum(1 for p in i_res.positions if p["side"] == "YES") == 1, i_res.positions)

    print("\n" + "=" * 74)
    print(f" SELF-TEST: {'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
