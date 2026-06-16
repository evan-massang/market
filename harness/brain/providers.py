"""harness.brain.providers — concrete BrainProviders.

  SwarmBrainProvider   the existing local PolySwarm + challenger, behind the interface
  MockBrainProvider    deterministic, no network/LLM — for tests
  DisabledBrainProvider observe-only: returns no probability; the bot still scans/scores
  ManusProvider        external HTTP provider placeholder (Manus AI or any endpoint)

None of these crash the core: a failure degrades to an observe-only ForecastResult.
PAPER-ONLY. Manus credentials are NOT managed here — the provider only USES an
endpoint/key if the operator has already set them in the env.
"""
from __future__ import annotations

import os

from .base import BrainProvider, EvidencePack, ForecastResult, BrainHealth


# ── disabled (observe-only) ───────────────────────────────────────────────────--
class DisabledBrainProvider(BrainProvider):
    name = "disabled"

    def __init__(self, reason: str = "brain disabled — observe-only"):
        self._reason = reason

    def forecast_market(self, pack: EvidencePack) -> ForecastResult:
        return ForecastResult(probability=None, confidence=None, recommended_action="observe",
                              ok=False, provider=self.name, explanation=self._reason,
                              risk_flags=["brain_disabled"])

    def health_check(self) -> BrainHealth:
        return BrainHealth(ok=True, provider=self.name, detail=self._reason)


# ── mock (deterministic; for tests) ───────────────────────────────────────────--
class MockBrainProvider(BrainProvider):
    """Deterministic forecast derived from the pack — no LLM, no network. If a fixed
    probability is provided it is returned verbatim; otherwise it nudges the market
    price toward the model's prior by a small, evidence-scaled amount."""
    name = "mock"

    def __init__(self, fixed_probability: float | None = None):
        self._fixed = fixed_probability

    def forecast_market(self, pack: EvidencePack) -> ForecastResult:
        if self._fixed is not None:
            p = self._fixed
        elif pack.market_p is not None:
            q = pack.evidence_quality if pack.evidence_quality is not None else 0.5
            nudge = 0.05 * (q - 0.5) * 2     # +/-0.05 scaled by evidence quality
            p = min(max(pack.market_p + nudge, 0.01), 0.99)
        else:
            p = 0.5
        edge = abs(p - pack.market_p) if pack.market_p is not None else 0.0
        action = "bet" if edge >= 0.05 else "observe"
        return ForecastResult(probability=p, confidence=0.6,
                              reasons=[f"mock: market {pack.market_p}, evidence_q {pack.evidence_quality}"],
                              recommended_action=action, provider=self.name,
                              explanation="deterministic mock forecast")

    def health_check(self) -> BrainHealth:
        return BrainHealth(ok=True, provider=self.name, detail="mock always available")


# ── swarm (the existing local brain, wrapped) ─────────────────────────────────--
class SwarmBrainProvider(BrainProvider):
    """Adapts the existing PolySwarm engine to the BrainProvider interface, so the local
    swarm is just ONE provider. Best-effort: any failure -> observe-only (never crashes)."""
    name = "swarm"

    def __init__(self, size: int = 5, rounds: int = 1):
        self._size, self._rounds = size, rounds

    def forecast_market(self, pack: EvidencePack) -> ForecastResult:
        try:
            from core.swarm import Swarm
            from agents.personas import build_swarm
            os.environ["DEBATE_ROUNDS"] = str(self._rounds)
            swarm = Swarm(agents=build_swarm(self._size) if self._size else None)
            res = swarm.forecast(pack.question, market_odds=pack.market_p,
                                 market_id=pack.market_id, extra_context=pack.evidence_text or "")
            p = res.get("probability")
            cons = res.get("consensus_score")
            edge = abs(p - pack.market_p) if (p is not None and pack.market_p is not None) else 0.0
            action = "bet" if edge >= 0.03 else "observe"
            return ForecastResult(probability=p, confidence=cons,
                                  reasons=[f"swarm {self._size} personas, regime={res.get('regime')}"],
                                  recommended_action=action, provider=self.name,
                                  explanation="local PolySwarm forecast")
        except Exception as e:
            return ForecastResult(probability=None, ok=False, provider=self.name,
                                  recommended_action="observe", error=repr(e),
                                  explanation="swarm failed — observe-only", risk_flags=["brain_error"])

    def health_check(self) -> BrainHealth:
        try:
            from harness.services import ollama_check
            ok, detail = ollama_check()
            return BrainHealth(ok=ok, provider=self.name, detail=detail)
        except Exception as e:
            return BrainHealth(ok=False, provider=self.name, detail=f"unavailable ({type(e).__name__})")


# ── manus (external HTTP; placeholder, provider-agnostic) ─────────────────────--
class ManusProvider(BrainProvider):
    """External Manus-AI (or any HTTP) brain. Provider-agnostic: POSTs the EvidencePack
    JSON to MANUS_API_BASE/forecast and parses a structured ForecastResult. Credentials
    are NOT managed here — if MANUS_API_KEY is set it's sent as a Bearer header, else the
    call is made without auth. If unconfigured/unavailable -> observe-only (never crashes)."""
    name = "manus"

    def __init__(self):
        self._base = (os.getenv("MANUS_API_BASE") or "").rstrip("/")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        key = os.getenv("MANUS_API_KEY")
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def forecast_market(self, pack: EvidencePack) -> ForecastResult:
        if not self._base:
            return ForecastResult(probability=None, ok=False, provider=self.name,
                                  recommended_action="observe", error="MANUS_API_BASE unset",
                                  explanation="Manus not configured — observe-only",
                                  risk_flags=["brain_unconfigured"])
        try:
            import httpx
            r = httpx.post(self._base + "/forecast", json=pack.to_dict(),
                           headers=self._headers(), timeout=float(os.getenv("MANUS_TIMEOUT", "60")))
            if r.status_code >= 400:
                return ForecastResult(probability=None, ok=False, provider=self.name,
                                      recommended_action="observe", error=f"HTTP {r.status_code}",
                                      explanation="Manus error — observe-only")
            return ForecastResult.from_provider_json(r.json(), provider=self.name)
        except Exception as e:
            return ForecastResult(probability=None, ok=False, provider=self.name,
                                  recommended_action="observe", error=repr(e),
                                  explanation="Manus unreachable — observe-only", risk_flags=["brain_error"])

    def health_check(self) -> BrainHealth:
        if not self._base:
            return BrainHealth(ok=False, provider=self.name, detail="MANUS_API_BASE unset (observe-only)")
        try:
            import httpx
            r = httpx.get(self._base + "/health", headers=self._headers(), timeout=5.0)
            return BrainHealth(ok=r.status_code < 400, provider=self.name, detail=f"HTTP {r.status_code}")
        except Exception as e:
            return BrainHealth(ok=False, provider=self.name, detail=f"unreachable ({type(e).__name__})")
