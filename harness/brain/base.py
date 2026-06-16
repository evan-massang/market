"""harness.brain.base — the external-brain interface + strict JSON-able data models.

A BrainProvider takes a structured EvidencePack and returns a structured ForecastResult
(never free text the core has to parse). Everything here is provider-agnostic: no
local-LLM / Ollama / Manus assumptions leak into these types.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict


def _clamp01(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return min(max(x, 0.0), 1.0)


# ── inputs ──────────────────────────────────────────────────────────────────────
@dataclass
class EvidencePack:
    """Clean, complete, consistent input for the brain — built by the non-LLM pipeline
    (scanner + evidence builder) so the brain never sees random raw text."""
    market_id: str
    question: str
    market_p: float | None = None
    yes_price: float | None = None
    no_price: float | None = None
    liquidity: float | None = None
    volume: float | None = None
    spread: float | None = None
    hours_to_resolution: float | None = None
    event_id: str | None = None
    theme: str | None = None
    classifier_label: str | None = None
    base_rate: float | None = None
    evidence_text: str = ""            # gathered context (news / facts / microstructure)
    evidence_quality: float | None = None
    missing_data: list = field(default_factory=list)
    known_risks: list = field(default_factory=list)
    source_quality: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── outputs (strict, JSON-able) ──────────────────────────────────────────────────
_ACTIONS = ("bet", "observe", "skip")


@dataclass
class ForecastResult:
    probability: float | None                 # YES probability in [0,1] or None
    confidence: float | None = None
    reasons: list = field(default_factory=list)
    risk_flags: list = field(default_factory=list)
    missing_information: list = field(default_factory=list)
    what_would_change: list = field(default_factory=list)
    recommended_action: str = "observe"       # bet | observe | skip
    explanation: str = ""
    provider: str = ""
    ok: bool = True
    error: str | None = None

    def __post_init__(self):
        self.probability = _clamp01(self.probability)
        self.confidence = _clamp01(self.confidence) if self.confidence is not None else None
        if self.recommended_action not in _ACTIONS:
            self.recommended_action = "observe"
        # a result with no usable probability can never be a 'bet'
        if self.probability is None and self.recommended_action == "bet":
            self.recommended_action = "observe"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_provider_json(cls, data: dict, provider: str = "") -> "ForecastResult":
        """Build from an arbitrary provider's JSON, tolerating missing/extra keys and
        a percentage probability. NEVER raises — a malformed reply -> observe-only."""
        if not isinstance(data, dict):
            return cls(probability=None, ok=False, provider=provider,
                       recommended_action="observe", error="non-dict brain output")
        p = data.get("probability", data.get("p"))
        if isinstance(p, str):
            s = p.strip().replace("%", "")
            try:
                p = float(s)
                if p > 1.0:
                    p = p / 100.0
            except ValueError:
                p = None
        return cls(
            probability=p,
            confidence=data.get("confidence"),
            reasons=list(data.get("reasons", []) or []),
            risk_flags=list(data.get("risk_flags", []) or []),
            missing_information=list(data.get("missing_information", data.get("missing", [])) or []),
            what_would_change=list(data.get("what_would_change", []) or []),
            recommended_action=str(data.get("recommended_action", "observe")),
            explanation=str(data.get("explanation", "") or ""),
            provider=provider, ok=True,
        )


@dataclass
class CritiqueResult:
    passed: bool
    severity: str = "low"                      # low | medium | high
    risk_flags: list = field(default_factory=list)
    explanation: str = ""
    recommended_action: str = "observe"
    provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EventInsight:
    event_id: str
    summary: str = ""
    legs: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    provider: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BrainHealth:
    ok: bool
    provider: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── interface ────────────────────────────────────────────────────────────────────
class BrainProvider(ABC):
    """The one boundary between the non-LLM core and the reasoning brain."""
    name = "base"

    @abstractmethod
    def forecast_market(self, pack: EvidencePack) -> ForecastResult:
        ...

    @abstractmethod
    def health_check(self) -> BrainHealth:
        ...

    def critique_forecast(self, pack: EvidencePack, forecast: ForecastResult) -> CritiqueResult:
        """Default NON-LLM critic — cheap, deterministic risk checks every provider gets
        for free (a provider may override with an LLM critic). High severity -> observe/skip."""
        flags, severity = [], "low"
        if forecast is None or forecast.probability is None:
            return CritiqueResult(False, "high", ["no_probability"],
                                  "brain produced no usable probability", "skip", self.name)
        if (pack.evidence_quality is not None) and pack.evidence_quality < 0.25:
            flags.append("low_evidence_quality"); severity = "medium"
        if (pack.liquidity is not None) and pack.liquidity < 1000:
            flags.append("thin_liquidity"); severity = "medium"
        if (pack.market_p is not None) and abs(forecast.probability - pack.market_p) < 0.03:
            flags.append("edge_too_small"); severity = max(severity, "medium", key=_sev_rank)
        if (pack.classifier_label or "") not in ("opinion", "", None):
            flags.append("not_opinion_market"); severity = "high"
        passed = severity != "high"
        action = "observe" if not passed else forecast.recommended_action
        return CritiqueResult(passed, severity, flags,
                              ("; ".join(flags) or "no critic flags"), action, self.name)

    def summarize_event(self, event_pack: dict) -> EventInsight:
        """Default: structural summary (no LLM). Providers may override with reasoning."""
        legs = (event_pack or {}).get("legs", [])
        return EventInsight(event_id=str((event_pack or {}).get("event_id", "")),
                            summary=f"{len(legs)} leg(s) in event", legs=legs,
                            notes=["non-LLM structural summary"], provider=self.name)


def _sev_rank(s):
    return {"low": 0, "medium": 1, "high": 2}.get(s, 0)
