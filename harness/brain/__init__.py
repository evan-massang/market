"""harness.brain — provider-agnostic EXTERNAL-BRAIN boundary.

The reasoning LLM/agent is ONE replaceable component. Core logic (scanner, evidence,
guards, EV, event portfolio, sizing, scoring) never imports a specific LLM — it talks
to a `BrainProvider`. Swap providers via the BRAIN_PROVIDER env var:

    swarm     (default) — the existing local PolySwarm + challenger, wrapped
    mock                — deterministic, for tests (no network/LLM)
    disabled            — observe-only: no forecasts, the bot still scans/scores/observes
    manus               — external Manus-AI (or any HTTP) provider (placeholder; no keys here)

    from harness.brain import get_provider
    brain = get_provider()                    # honors BRAIN_PROVIDER
    result = brain.forecast_market(evidence_pack)   # -> ForecastResult (strict JSON-able)

If the brain is unavailable, the system degrades to observe-only — it never crashes.
PAPER-ONLY; no real-money path.
"""
from __future__ import annotations

import os

from .base import (BrainProvider, EvidencePack, ForecastResult, CritiqueResult,
                   EventInsight, BrainHealth)

__all__ = ["BrainProvider", "EvidencePack", "ForecastResult", "CritiqueResult",
           "EventInsight", "BrainHealth", "get_provider", "available_providers",
           "build_brain_pack", "status"]


def build_brain_pack(market, cfg=None):
    from .pack import build_brain_pack as _b
    return _b(market, cfg)


def status() -> dict:
    """The configured provider + its health (for `dashboard /api/brain/status`)."""
    name = os.getenv("BRAIN_PROVIDER", "swarm")
    try:
        h = get_provider().health_check()
        return {"provider": name, "available": available_providers(),
                "health": h.to_dict()}
    except Exception as e:
        return {"provider": name, "available": available_providers(),
                "health": {"ok": False, "detail": f"error: {e}"}}


def available_providers() -> list[str]:
    return ["swarm", "mock", "disabled", "manus"]


def get_provider(name: str | None = None) -> BrainProvider:
    """Return the configured BrainProvider. Defaults to BRAIN_PROVIDER env, then 'swarm'.
    An unknown name falls back to DisabledBrainProvider (observe-only) rather than crash."""
    name = (name or os.getenv("BRAIN_PROVIDER", "swarm")).strip().lower()
    from . import providers
    if name == "mock":
        return providers.MockBrainProvider()
    if name == "disabled":
        return providers.DisabledBrainProvider()
    if name == "manus":
        return providers.ManusProvider()
    if name == "swarm":
        return providers.SwarmBrainProvider()
    return providers.DisabledBrainProvider(reason=f"unknown BRAIN_PROVIDER '{name}' — observe-only")
