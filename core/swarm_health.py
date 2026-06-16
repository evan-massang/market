"""core/swarm_health.py — swarm degradation policy (Plan 2).

A degraded swarm forecast (too few agents survived, or the run aborted) must NEVER
be mistaken for a healthy high-consensus forecast, and must never be bet on. This
module is the single source of truth for that policy:

  * MIN_SWARM_AGENTS_FOR_BET       — fewer surviving agents than this => NO BET.
  * MIN_SWARM_AGENTS_FOR_CONSENSUS — fewer than this => consensus is meaningless.

``assess(n_requested, n_succeeded)`` turns the surviving-agent counts into explicit
health flags (``degraded`` / ``aborted`` / ``allow_bet`` / ``degradation_reason``)
that the swarm attaches to its result and that predict_today / sameday read before
sizing or opening a position.

THE authoritative betting signal is ``allow_bet``. It is True only when a strict
MAJORITY of the requested agents survived AND at least MIN_SWARM_AGENTS_FOR_BET did
(so a 5-agent swarm bets at 3/4/5 survivors, but a 6-agent swarm does NOT bet at a
3/3 split — a coin-flip survival is not a healthy swarm). Other quality gates
(consensus, EV, risk, bankroll, exposure) may still block a forecast this module
allows; this gate can only ever ADD a block, never approve a bet on its own.

Pure module: no imports from the rest of the project, no I/O. Paper-only.
"""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, str(default)))
        return v if v >= 0 else int(default)
    except (TypeError, ValueError):
        return int(default)


# Defaults per Plan 2. Env-overridable but never below the safe floor at read time.
MIN_SWARM_AGENTS_FOR_BET = _int_env("MIN_SWARM_AGENTS_FOR_BET", 3)
MIN_SWARM_AGENTS_FOR_CONSENSUS = _int_env("MIN_SWARM_AGENTS_FOR_CONSENSUS", 2)

# degradation_reason values (swarm-level)
NO_AGENTS_SUCCEEDED = "no_agents_succeeded"
INSUFFICIENT_SURVIVING_AGENTS = "insufficient_surviving_agents"

# consensus_status values (aggregator-level)
CONSENSUS_OK = "ok"
CONSENSUS_LIMITED = "limited_agents"
CONSENSUS_INSUFFICIENT = "insufficient_agents"

# method marker emitted by the swarm when EVERY agent failed
ALL_AGENTS_FAILED_METHOD = "degraded_all_agents_failed"


def consensus_allowed(n_succeeded) -> bool:
    """True iff there are enough surviving estimates for consensus to mean anything."""
    try:
        return int(n_succeeded) >= MIN_SWARM_AGENTS_FOR_CONSENSUS
    except (TypeError, ValueError):
        return False


def consensus_size_factor(n_succeeded: int) -> float:
    """Sample-size dampener for the consensus score in [0,1].

    0.0 below MIN_SWARM_AGENTS_FOR_CONSENSUS (1 survivor has no agreement signal),
    rising to 1.0 at MIN_SWARM_AGENTS_FOR_BET. This guarantees a single survivor can
    NEVER report consensus 1.0, and that a 2-agent "agreement" cannot read as max
    confidence — while 3+ agents keep the full, unchanged consensus score.
    """
    n = int(n_succeeded or 0)
    if n < MIN_SWARM_AGENTS_FOR_CONSENSUS:
        return 0.0
    span = max(1, MIN_SWARM_AGENTS_FOR_BET - 1)
    return min(1.0, (n - 1) / span)


def assess(n_requested, n_succeeded) -> dict:
    """Swarm health from agent counts. Pure. Returns:

        {degraded, aborted, allow_bet, minor_degradation, degradation_reason,
         n_agents_requested, n_agents_succeeded, n_agents_failed}

    ``allow_bet`` is True ONLY when a strict majority survived AND at least
    MIN_SWARM_AGENTS_FOR_BET survived. ``aborted`` iff zero survived. ``degraded``
    is True for any blocked run OR any run that lost an agent (so a full, healthy
    run is the only thing flagged non-degraded — honest for persistence).
    """
    try:
        nr = max(0, int(n_requested or 0))
    except (TypeError, ValueError):
        nr = 0
    try:
        ns = max(0, int(n_succeeded or 0))
    except (TypeError, ValueError):
        ns = 0
    nf = max(0, nr - ns)

    if ns <= 0:
        allow_bet = False
        reason = NO_AGENTS_SUCCEEDED
    elif ns < MIN_SWARM_AGENTS_FOR_BET or ns * 2 <= nr:
        # below the hard floor, OR not a strict majority of the requested swarm
        # (e.g. 3/6 is a coin-flip survival, not a healthy swarm) -> no bet.
        allow_bet = False
        reason = INSUFFICIENT_SURVIVING_AGENTS
    else:
        allow_bet = True
        reason = None

    return {
        "degraded": bool((not allow_bet) or nf > 0),
        "aborted": bool(ns <= 0),
        "allow_bet": bool(allow_bet),
        "minor_degradation": bool(allow_bet and nf > 0),
        "degradation_reason": reason,
        "n_agents_requested": nr,
        "n_agents_succeeded": ns,
        "n_agents_failed": nf,
    }
