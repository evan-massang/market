"""
P3 — position sizing: fractional Kelly on a Polymarket binary share.

The ONE sizing engine for the whole harness (we deliberately do NOT use
WhoIsSharp's Kelly). Stake is a fraction of the CURRENT paper bankroll, so it
compounds up as the wallet grows and brakes down as it shrinks.

Mechanics (per the plan):
  A YES share costs c (market price, 0..1) and pays $1 if YES resolves.
    p = model probability of YES.
    p > c -> buy YES, full-Kelly  f* = (p - c) / (1 - c)
    p < c -> buy NO,  full-Kelly  f* = (c - p) / c
  Stake = min(lambda * f*, cap) * bankroll      (lambda = 0.25 quarter-Kelly)

Caveat baked into the defaults: Kelly assumes the edge is REAL — the very thing
the gates are still testing. Quarter-Kelly + a hard cap + the paper wallet are
the margin against a wrong edge estimate. Full Kelly on unproven forecasts
torches a bankroll; do not raise lambda until a gate has actually passed.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# ── conservative defaults ────────────────────────────────────────────────────
DEFAULT_LAMBDA = 0.25      # quarter-Kelly
DEFAULT_CAP = 0.02         # hard cap: at most 2% of bankroll on any single bet
DEFAULT_MIN_EDGE = 0.02    # ignore edges thinner than 2 cents (noise / costs)
_EPS = 1e-9


@dataclass
class Sizing:
    side: str | None        # "YES" | "NO" | None (no bet)
    edge: float             # signed model-vs-market edge (p - c)
    abs_edge: float         # |p - c|
    f_star: float           # full-Kelly fraction (0 if no bet)
    fraction: float         # fraction of bankroll actually staked (after lambda + cap)
    stake: float            # dollar stake off the CURRENT bankroll
    capped: bool            # was the hard cap binding?
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def kelly_fraction(p: float, c: float) -> tuple[str | None, float]:
    """Return (side, full-Kelly fraction f*) for model prob p vs market price c.

    Guards the degenerate market prices c<=0 and c>=1 (no tradable edge there).
    """
    if not (0.0 < c < 1.0) or not (0.0 <= p <= 1.0):
        return None, 0.0
    if p > c + _EPS:
        return "YES", (p - c) / (1.0 - c)
    if p < c - _EPS:
        return "NO", (c - p) / c
    return None, 0.0


def size_bet(p: float, c: float, bankroll: float,
             lam: float = DEFAULT_LAMBDA, cap: float = DEFAULT_CAP,
             min_edge: float = DEFAULT_MIN_EDGE) -> Sizing:
    """Size a simulated bet. Returns a Sizing; stake==0 / side==None means no bet."""
    edge = p - c
    abs_edge = abs(edge)
    side, f_star = kelly_fraction(p, c)

    if side is None:
        return Sizing(None, edge, abs_edge, 0.0, 0.0, 0.0, False, "no edge (p == c or degenerate price)")
    if abs_edge < min_edge:
        return Sizing(None, edge, abs_edge, f_star, 0.0, 0.0, False,
                      f"edge {abs_edge:.3f} below min_edge {min_edge:.3f}")
    if bankroll <= 0:
        return Sizing(None, edge, abs_edge, f_star, 0.0, 0.0, False, "bankroll depleted")

    scaled = lam * f_star
    fraction = min(scaled, cap)
    capped = scaled > cap
    stake = round(fraction * bankroll, 6)
    reason = f"buy {side}: f*={f_star:.3f}, {lam:g}x Kelly={scaled:.4f}" + (f" -> capped at {cap:g}" if capped else "")
    return Sizing(side, round(edge, 6), round(abs_edge, 6), round(f_star, 6),
                  round(fraction, 6), stake, capped, reason)
