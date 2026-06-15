"""
B2 — EVIDENCE PACK.  One canonical GATHER path for upstream forecast context.

`loop._build_enrichment` historically inlined three optional, independently-guarded
context sources (microstructure signals + GDELT news/sentiment + Wikipedia facts) and
joined them into a single text block fed UPSTREAM to the swarm forecaster. This module
hoists that exact gather into ONE place and, alongside the byte-identical text, exposes
a structured :class:`EvidencePack` (per-source item counts + freshness / relevance /
quality scores + a stable content hash for replay).

CONTRACTS (load-bearing — do NOT change without updating the byte-compat test):
  * ``EvidencePack.text`` reproduces the legacy ``loop._build_enrichment`` output
    BYTE-FOR-BYTE for the same inputs (same header, same per-block headers, same
    ``"\n\n"`` join, ``""`` when no blocks). This string is the swarm input.
  * Each of the three sources is gathered EXACTLY as before — same functions, same
    order (signals -> news -> facts), each wrapped in try/except + obs.hooks.on_error
    so a single source failing skips that block instead of crashing the pass.

Everything here is PURE-ish: the only side effects are the (already-guarded) network
calls inside the source functions and obs error logging. Read-only / paper only.

Public API:
  build_evidence_pack(market: dict, cfg) -> EvidencePack
  EvidencePack(market_id, question, sources, n_sources, total_items,
               evidence_quality, text, content_hash)  + .to_dict()
  Source(name, kind, items, item_count, freshness_score, relevance_score,
         quality, raw_summary)                          + .to_dict()
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from harness import gdelt as gdelt_mod
from harness import signals as signals_mod

# Guarded obs import: a broken/missing obs package can NEVER break a gather. Every obs
# call is gated on ``if obs:`` and is OBSERVATION-ONLY (no return/logic change).
try:
    from harness import obs
except Exception:  # pragma: no cover - defensive
    obs = None


# ── exact legacy strings (byte-compat with loop._build_enrichment) ────────────
# These three constants are load-bearing: the swarm prompt is assembled from them.
_HEADER = "=== HARNESS UPSTREAM CONTEXT (facts + news/sentiment + microstructure) ==="
_SIGNALS_HEADER = "[Market microstructure signals — WhoIsSharp port]"
_GDELT_HEADER = "[News & sentiment — GDELT]"

# ── scoring knobs (transparent + deterministic) ───────────────────────────────
FRESH_WINDOW_DAYS = 14.0          # an item this many days old scores freshness 0.0
ITEMS_SATURATION = 5.0            # item_count at/above which the count term saturates to 1.0
_DEFAULT_SCORE = 0.5              # neutral default when a dimension can't be measured
# per-source quality = ITEMS_W*count + FRESH_W*freshness + REL_W*relevance (clamped 0..1)
ITEMS_W, FRESH_W, REL_W = 0.50, 0.25, 0.25
# evidence_quality = weighted mean of per-source quality over sources WITH real items
KIND_WEIGHTS = {"signals": 0.20, "news": 0.45, "facts": 0.35}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "will", "would", "could", "should", "does", "this", "that", "with", "from",
    "into", "than", "then", "they", "them", "their", "there", "what", "when",
    "which", "have", "been", "were", "the", "and", "for", "are", "any", "all",
}


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens (len >= 4, non-stopword) — the relevance vocabulary."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 4 and t not in _STOP}


def _question_terms(question: str) -> set[str]:
    """Salient terms from the question: content tokens + entity tokens (transparent)."""
    terms = _tokens(question)
    try:
        for ent in gdelt_mod.extract_entities(question or ""):
            for w in _TOKEN_RE.findall(ent.lower()):
                if len(w) >= 3:
                    terms.add(w)
    except Exception:
        pass
    return terms


def _relevance(source_text: str, qterms: set[str]) -> float:
    """Fraction of question terms that appear in the source text (entity/keyword overlap).

    Deterministic and transparent. Neutral 0.5 when the question has no salient terms;
    0.0 when the source carries no usable tokens.
    """
    try:
        if not qterms:
            return _DEFAULT_SCORE
        st = _tokens(source_text)
        if not st:
            return 0.0
        return _clamp01(len(qterms & st) / len(qterms))
    except Exception:
        return _DEFAULT_SCORE


def _parse_seendate(s: str):
    """GDELT seendate ('YYYYMMDDThhmmssZ') -> aware datetime, or None if unparseable."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _gdelt_freshness(articles) -> float:
    """Mean recency over articles with parseable timestamps; 0.5 if none have one."""
    try:
        now = datetime.now(timezone.utc)
        ages = []
        for a in articles or []:
            dt = _parse_seendate(str((a or {}).get("seendate", "")))
            if dt is None:
                continue
            age_days = (now - dt).total_seconds() / 86400.0
            ages.append(_clamp01(1.0 - age_days / FRESH_WINDOW_DAYS))
        if not ages:
            return _DEFAULT_SCORE
        return _clamp01(sum(ages) / len(ages))
    except Exception:
        return _DEFAULT_SCORE


def _quality(item_count: int, freshness: float, relevance: float) -> float:
    """Per-source quality in [0, 1]. Exactly 0.0 when the source produced no real items,
    otherwise a transparent blend of (count, freshness, relevance)."""
    if item_count <= 0:
        return 0.0
    items_score = min(1.0, item_count / ITEMS_SATURATION)
    return _clamp01(ITEMS_W * items_score + FRESH_W * _clamp01(freshness) + REL_W * _clamp01(relevance))


# ── dataclasses ────────────────────────────────────────────────────────────────
@dataclass
class Source:
    """One gathered context source. ``raw_summary`` is the exact text block that this
    source contributes to :attr:`EvidencePack.text` (load-bearing for byte-compat)."""
    name: str
    kind: str                      # "signals" | "news" | "facts"
    items: list = field(default_factory=list)
    item_count: int = 0
    freshness_score: float = _DEFAULT_SCORE
    relevance_score: float = _DEFAULT_SCORE
    quality: float = 0.0
    raw_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "items": self.items,
            "item_count": self.item_count,
            "freshness_score": self.freshness_score,
            "relevance_score": self.relevance_score,
            "quality": self.quality,
            "raw_summary": self.raw_summary,
        }


@dataclass
class EvidencePack:
    market_id: str
    question: str
    sources: list  # list[Source]
    n_sources: int
    total_items: int
    evidence_quality: float
    text: str
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "sources": [s.to_dict() for s in self.sources],
            "n_sources": self.n_sources,
            "total_items": self.total_items,
            "evidence_quality": self.evidence_quality,
            "text": self.text,
            "content_hash": self.content_hash,
        }


# ── builder ────────────────────────────────────────────────────────────────────
def build_evidence_pack(market: dict, cfg) -> EvidencePack:
    """Gather the three upstream context sources (same functions, same order, same
    guards as the legacy ``loop._build_enrichment``) and return a structured pack.

    ``cfg`` is a ``loop.LoopConfig`` (or anything with ``use_signals`` / ``use_gdelt`` /
    optional ``use_wiki`` attributes). Errors in any single source are logged via
    ``obs.hooks.on_error`` and that source is skipped — never crashes the pass.
    """
    market = market or {}
    mid = market.get("market_id")
    question = market.get("question", "") or ""
    qterms = _question_terms(question)
    sources: list[Source] = []

    # 1) SIGNALS — WhoIsSharp microstructure (pure; no network). Block appended only when
    #    at least one signal FIRED, exactly as the legacy gather did.
    if getattr(cfg, "use_signals", True):
        try:
            sig = signals_mod.compute_signals(market)
            fired = sig.get("fired", [])
            if fired:
                raw_metrics = sig.get("raw_metrics", {})
                block = (_SIGNALS_HEADER + "\n"
                         f"fired: {', '.join(fired)} | metrics: {raw_metrics}")
                # microstructure is market-intrinsic: no text to keyword-match and no
                # timestamps -> neutral relevance/freshness defaults; quality is driven
                # by how many signals fired.
                fresh = _DEFAULT_SCORE
                rel = _DEFAULT_SCORE
                sources.append(Source(
                    name="whoissharp_signals", kind="signals",
                    items=list(fired), item_count=len(fired),
                    freshness_score=fresh, relevance_score=rel,
                    quality=_quality(len(fired), fresh, rel), raw_summary=block,
                ))
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="loop._build_enrichment.signals", exc=e, action="skip",
                                   context={"market_id": mid})

    # 2) NEWS — GDELT news/sentiment. The block is appended UNCONDITIONALLY (even with 0
    #    articles, format_context_for_llm renders a "no headlines" note) — legacy behavior.
    if getattr(cfg, "use_gdelt", True):
        try:
            q = gdelt_mod.build_query(question)
            ctx = gdelt_mod.gdelt_context(q, timespan="14d", max_records=30)
            block = _GDELT_HEADER + "\n" + gdelt_mod.format_context_for_llm(ctx)
            articles = (ctx.get("articles") if isinstance(ctx, dict) else None) or []
            fresh = _gdelt_freshness(articles)
            rel = _relevance(" ".join(str((a or {}).get("title", "")) for a in articles), qterms)
            sources.append(Source(
                name="gdelt", kind="news",
                items=list(articles), item_count=len(articles),
                freshness_score=fresh, relevance_score=rel,
                quality=_quality(len(articles), fresh, rel), raw_summary=block,
            ))
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="loop._build_enrichment.gdelt", exc=e, action="skip",
                                   context={"market_id": mid})

    # 3) FACTS — Wikipedia entity grounding. wiki_context() already returns its own
    #    "[Background facts — Wikipedia]" header (or "" when nothing usable). Block
    #    appended only when non-empty, exactly as the legacy gather did.
    if getattr(cfg, "use_wiki", True):
        try:
            from harness import wiki as wiki_mod
            ents = gdelt_mod.extract_entities(question)
            wblock = wiki_mod.wiki_context(ents, n=2)
            if wblock:
                # lines after the header are one per grounded entity ("- term: summary").
                lines = [ln for ln in wblock.split("\n")[1:] if ln.strip()]
                fresh = _DEFAULT_SCORE   # Wikipedia summaries carry no per-item timestamp
                rel = _relevance(" ".join(lines), qterms)
                sources.append(Source(
                    name="wikipedia", kind="facts",
                    items=lines, item_count=len(lines),
                    freshness_score=fresh, relevance_score=rel,
                    quality=_quality(len(lines), fresh, rel), raw_summary=wblock,
                ))
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="loop._build_enrichment.wiki", exc=e, action="skip",
                                   context={"market_id": mid})

    # ── text: byte-identical to the legacy join (sources carry their own block) ──
    text = (_HEADER + "\n" + "\n\n".join(s.raw_summary for s in sources)) if sources else ""

    # ── aggregate quality over sources that produced REAL items (0.0 if none) ──
    present = [s for s in sources if s.item_count > 0]
    if present:
        wsum = sum(KIND_WEIGHTS.get(s.kind, 0.30) for s in present)
        evidence_quality = _clamp01(
            sum(s.quality * KIND_WEIGHTS.get(s.kind, 0.30) for s in present) / wsum
        ) if wsum > 0 else 0.0
    else:
        evidence_quality = 0.0

    # ── content hash: stable across rebuilds, changes iff the gathered ITEMS change ──
    canonical = json.dumps(
        [{"name": s.name, "kind": s.kind, "item_count": s.item_count, "items": s.items}
         for s in sources],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return EvidencePack(
        market_id=mid,
        question=question,
        sources=sources,
        n_sources=len(sources),
        total_items=sum(s.item_count for s in sources),
        evidence_quality=round(evidence_quality, 6),
        text=text,
        content_hash=content_hash,
    )
