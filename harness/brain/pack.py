"""harness.brain.pack — build a structured brain EvidencePack from the NON-LLM pipeline.

Reuses the existing evidence builder (harness.evidence_pack), scanner microstructure,
and classifier so the brain always gets clean, complete, consistent input — never raw
text. Provider-agnostic; best-effort (a missing source never crashes the build)."""
from __future__ import annotations

from .base import EvidencePack


def build_brain_pack(market: dict, cfg=None) -> EvidencePack:
    """Assemble a brain EvidencePack for one market dict (Gamma-shaped)."""
    mid = str(market.get("market_id") or market.get("id") or "")
    q = market.get("question") or ""

    market_p = theme = label = None
    liquidity = volume = spread = hours = None
    evidence_text = ""
    evidence_quality = None
    missing, risks = [], []

    try:
        from harness import scanner, classifier, scoreboard
        market_p = (scanner.gamma.yes_price(market) if hasattr(scanner, "gamma") else None)
        if market_p is None:
            market_p = scanner._f(market.get("price"), None) or scanner._f(market.get("yes_price"), None)
        liquidity = scanner._f(market.get("liquidity"), 0.0)
        volume = scanner._f(market.get("volume"), 0.0)
        spread = scanner._spread(market)
        hours = scanner._hours_left(market)
        theme = scoreboard.theme_of(q)
        cls = classifier.tag_market(market)
        label = cls.label
    except Exception as e:
        risks.append(f"signal_extract_error:{type(e).__name__}")

    try:
        from harness.evidence_pack import build_evidence_pack
        pack = build_evidence_pack(market, cfg)
        evidence_text = pack.text
        evidence_quality = pack.evidence_quality
        if pack.total_items == 0:
            missing.append("no_external_evidence")
    except Exception as e:
        risks.append(f"evidence_error:{type(e).__name__}")

    yes_p = market_p
    no_p = (1.0 - market_p) if market_p is not None else None
    return EvidencePack(
        market_id=mid, question=q, market_p=market_p, yes_price=yes_p, no_price=no_p,
        liquidity=liquidity, volume=volume, spread=spread, hours_to_resolution=hours,
        event_id=market.get("event_slug"), theme=theme, classifier_label=label,
        evidence_text=evidence_text, evidence_quality=evidence_quality,
        missing_data=missing, known_risks=risks,
    )
