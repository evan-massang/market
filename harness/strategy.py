"""
Favorite-longshot bias strategy — a BACKTESTED, slippage-robust +EV edge (no LLM).

Backtest on 394 resolved opinion markets (harness/backtest.py, honest price+slippage
fill): fading OVERPRICED longshots [0.03,0.20) (buy NO) + backing UNDERPRICED
favorites [0.88,0.99) (buy YES) returns ~+4.5% ROI at ~95% win rate, and stays
POSITIVE from 0c to 2c slippage. For contrast, the swarm's "buy cheap YES" behavior
backtests at -98.9% — so this is the opposite, evidence-based, of what the swarm did.

Why it works: prediction-market crowds overbet longshots (so low-priced YES is
overpriced) and slightly underbet near-certain favorites. We exploit that bias with
a pure price rule — fast, scalable, and the only part of the system with a
demonstrated edge after costs.
"""
from __future__ import annotations
from dataclasses import dataclass

# Bands + empirical mispricing (actual_YES - price), measured in the calibration table.
FADE_LO, FADE_HI = 0.03, 0.20    # longshots: overpriced by ~3 cents -> fade (buy NO)
BACK_LO, BACK_HI = 0.88, 0.99    # favorites: underpriced by ~4 cents -> back (buy YES)
FADE_EDGE = 0.03
BACK_EDGE = 0.04

DEFAULT_LAMBDA = 0.50            # half-Kelly (the edge is backtested + slippage-robust)
DEFAULT_CAP = 0.04              # 4% per-bet cap -> ~+9% backtest ROI vs +4.5% at 2%
DEFAULT_MIN_EDGE = 0.015


@dataclass
class StratDecision:
    side: str | None             # "YES" | "NO" | None
    fraction: float              # fraction of bankroll (after lambda + cap)
    est_true_yes: float | None   # our calibration-implied true YES probability
    edge: float                  # estimated edge on the side we take
    reason: str


def decide_bet(price: float, lam: float = DEFAULT_LAMBDA, cap: float = DEFAULT_CAP,
               min_edge: float = DEFAULT_MIN_EDGE) -> StratDecision:
    """price = current market YES price (0..1). Returns the favorite-longshot bet."""
    if not (0.0 < price < 1.0):
        return StratDecision(None, 0.0, None, 0.0, "untradeable price")

    if FADE_LO <= price < FADE_HI:
        true_yes = max(price - FADE_EDGE, 0.005)
        edge = price - true_yes                      # how overpriced YES is = our NO edge
        if edge < min_edge:
            return StratDecision(None, 0.0, true_yes, edge, "fade edge below min")
        c_no = 1.0 - price                           # NO costs this
        true_no = 1.0 - true_yes
        f_star = (true_no - c_no) / (1.0 - c_no)     # Kelly on the NO side
        frac = min(lam * f_star, cap)
        return StratDecision("NO", round(frac, 6), round(true_yes, 4), round(edge, 4),
                             f"fade overpriced longshot: market {price:.0%}, est true {true_yes:.0%} -> NO")

    if BACK_LO <= price < BACK_HI:
        true_yes = min(price + BACK_EDGE, 0.995)
        edge = true_yes - price
        if edge < min_edge:
            return StratDecision(None, 0.0, true_yes, edge, "back edge below min")
        f_star = (true_yes - price) / (1.0 - price)  # Kelly on the YES side
        frac = min(lam * f_star, cap)
        return StratDecision("YES", round(frac, 6), round(true_yes, 4), round(edge, 4),
                             f"back underpriced favorite: market {price:.0%}, est true {true_yes:.0%} -> YES")

    return StratDecision(None, 0.0, None, 0.0, f"price {price:.0%} outside edge bands")
