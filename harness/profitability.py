"""P7 — EV-after-costs (B1). A PURE TIGHTENING gate.

This module answers one question, the SAME way the paper wallet will actually
fill the bet: *after* the wallet's slippage worsens the price you pay and *after*
its per-trade fee, is the expected value of this share still positive?

It NEVER approves a bet on its own. It is meant to be AND-ed with the existing
sizing / event-portfolio gates at the four bet-decision sites (predict_today and
sameday). The only thing it can do is REJECT a bet whose edge is so thin that the
slippage-worsened fill makes it non-positive EV. So wiring it in can only make the
bot bet *less*, never more — consistent with the guiding principle.

Cost-model consistency (non-negotiable):
  fill_price, fee and the $1/share payout are computed with the EXACT same math
  the wallet uses in :func:`harness.wallet.open_position` / ``settle_market``:

    base       = market_p           (YES leg)   |   1 - market_p   (NO leg)
    fill_price = clamp(base + slippage, 0.01, 0.99)
    fee        = fee_frac * stake
    payout     = shares * $1   if the side wins, else 0
    realized   = payout - stake - fee

  slippage and fee_frac default to ``WalletConfig`` (slippage=0.01, fee_frac=0.0)
  so the gate's view of a fill is byte-for-byte the wallet's view of a fill.

EV math (binary $1-payout share):
  You buy ``shares = stake / fill_price``; each pays $1 with probability
  ``win_prob`` (= model_p for YES, = 1 - model_p for NO).
    ev_per_share  = win_prob - fill_price
    ev_per_dollar = ev_per_share / fill_price - fee_frac
                    (each $1 of stake buys 1/fill_price shares; the fee_frac is a
                     flat per-dollar drag, exactly as realized = payout-stake-fee)
    breakeven_p   = the model_p at which ev_per_share == 0
                    = fill_price        for YES   (win_prob == model_p)
                    = 1 - fill_price    for NO     (win_prob == 1 - model_p)
    positive      = ev_per_share > 0   (STRICTLY; a zero-EV fill is NOT positive)

Pure math: no network, no LLM, no DB writes (importing WalletConfig only binds a
path string in wallet.py, it opens nothing).
"""
from __future__ import annotations

import math

from harness.wallet import WalletConfig

# Reason strings for ev_gate. Only the failure reason is contractually fixed.
REJECT_REASON = "neg_ev_after_costs"
PASS_REASON = "positive_ev_after_costs"

_SIDES = ("YES", "NO")


def _finite(x) -> bool:
    """True iff x is a finite real number (rejects None / NaN / inf / non-numeric)."""
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def ev_after_costs(model_p: float, market_p: float, side: str,
                   bankroll: float | None = None, stake: float | None = None,
                   slippage: float | None = None, fee_frac: float | None = None) -> dict:
    """Expected value of one share AFTER the wallet's slippage + fee, for ``side``.

    Parameters mirror the bet-decision call sites. ``bankroll`` and ``stake`` do
    not change per-unit EV (EV scales linearly with stake and the fee is a flat
    per-dollar fraction) — they are accepted only so the gate can be dropped in
    next to ``size_bet`` without reshaping the call.

    Returns a dict::

        {side, fill_price, ev_per_share, ev_per_dollar, breakeven_p, positive}

    ``positive`` is ``False`` for any degenerate / out-of-range input (bad side,
    market_p not strictly in (0,1), model_p outside [0,1], NaN/inf). That is the
    conservative direction: when in doubt, the gate rejects.
    """
    cfg = WalletConfig()
    slip = cfg.slippage if slippage is None else float(slippage)
    fee = cfg.fee_frac if fee_frac is None else float(fee_frac)

    side_u = str(side).upper() if side is not None else ""

    # Validity: a tradable, in-range setup. Short-circuits so float() never sees
    # a non-finite value. market_p must be STRICTLY inside (0,1) (a 0/1 price is a
    # resolved/degenerate market with no tradable share).
    inputs_ok = (
        side_u in _SIDES
        and _finite(model_p) and _finite(market_p)
        and 0.0 <= float(model_p) <= 1.0
        and 0.0 < float(market_p) < 1.0
    )

    mp = float(market_p) if _finite(market_p) else 0.0
    pp = float(model_p) if _finite(model_p) else 0.0

    # Wallet fill: the share you actually buy, at a WORSE price than quoted.
    # Anything other than a clean "NO" is treated as the YES leg for the fill
    # arithmetic; an invalid side is still forced non-positive via inputs_ok.
    base = (1.0 - mp) if side_u == "NO" else mp
    fill_price = min(max(base + slip, 0.01), 0.99)

    if side_u == "NO":
        win_prob = 1.0 - pp
        breakeven_p: float | None = 1.0 - fill_price
    else:
        win_prob = pp
        breakeven_p = fill_price

    ev_per_share = win_prob - fill_price
    # fill_price is clamped to >= 0.01, so this division is always safe.
    ev_per_dollar = (ev_per_share / fill_price) - fee
    # Net of the wallet's per-trade fee for the pass/reject DECISION. The wallet
    # charges fee = fee_frac * stake; each share costs fill_price of that stake, so
    # the fee drag per share is fee_frac * fill_price. Basing `positive` on the
    # NET-of-fee EV keeps the gate byte-consistent with realized = payout-stake-fee
    # even if fee_frac is ever set > 0. At the wallet default fee_frac=0.0 this is
    # identical to ev_per_share > 0 (no behavior change today).
    net_ev_per_share = ev_per_share - fee * fill_price

    positive = bool(inputs_ok and net_ev_per_share > 0.0)

    return {
        "side": side_u if side_u in _SIDES else None,
        "fill_price": fill_price,
        "ev_per_share": ev_per_share,
        "net_ev_per_share": net_ev_per_share,
        "ev_per_dollar": ev_per_dollar,
        "breakeven_p": breakeven_p if side_u in _SIDES else None,
        "positive": positive,
    }


def ev_gate(model_p: float, market_p: float, side: str,
            bankroll: float | None = None, stake: float | None = None,
            slippage: float | None = None, fee_frac: float | None = None) -> tuple[bool, str]:
    """PURE TIGHTENING gate. ``ok`` iff the after-costs EV per share is positive.

    Returns ``(ok, reason)``. On failure ``reason == "neg_ev_after_costs"``. This
    gate can only REJECT a bet the slippage-worsened fill makes non-positive-EV;
    it never approves anything that the upstream edge/sizing gates rejected.
    """
    ev = ev_after_costs(model_p, market_p, side,
                        bankroll=bankroll, stake=stake,
                        slippage=slippage, fee_frac=fee_frac)
    ok = bool(ev["positive"])
    return ok, (PASS_REASON if ok else REJECT_REASON)
