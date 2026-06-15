"""Unit tests for harness.evidence_pack (B2 — EVIDENCE PACK).

NO network / NO LLM. The three context sources are monkeypatched to offline stubs via
the dependency-free ``patched`` helper, and a temp DATABASE_URL/OBS_LOGS_DIR is set
BEFORE importing anything that binds a DB path. We assert:

  1. BYTE-COMPAT — pack.text == an independent replica of the legacy _build_enrichment
     join, for representative inputs (all three blocks; partial; and the empty case).
  2. evidence_quality == 0.0 when no source produced real items.
  3. quality rises with more / fresher / more-relevant items.
  4. content_hash is stable across repeat builds and changes when the items change.
  5. to_dict() is JSON-serializable.

Run:  python -m harness.tests.test_evidence_pack
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_evpack_")

from harness import evidence_pack as EP   # noqa: E402
from harness import gdelt                 # noqa: E402
from harness import signals               # noqa: E402
from harness import wiki                  # noqa: E402
from harness import loop                  # noqa: E402


# ── a config with the three switches the gather reads ─────────────────────────
class _Cfg:
    def __init__(self, use_signals=True, use_gdelt=True, use_wiki=True):
        self.use_signals = use_signals
        self.use_gdelt = use_gdelt
        self.use_wiki = use_wiki


# ── independent replica of the ORIGINAL loop._build_enrichment (the gold join) ──
# This MUST mirror the pre-B2 inline logic exactly; it is the byte-compat oracle.
def _legacy_enrichment(market: dict, cfg) -> str:
    blocks: list[str] = []
    if getattr(cfg, "use_signals", True):
        sig = signals.compute_signals(market)
        fired = sig.get("fired", [])
        if fired:
            blocks.append("[Market microstructure signals — WhoIsSharp port]\n"
                          f"fired: {', '.join(fired)} | metrics: {sig.get('raw_metrics', {})}")
    if getattr(cfg, "use_gdelt", True):
        q = gdelt.build_query(market.get("question", ""))
        ctx = gdelt.gdelt_context(q, timespan="14d", max_records=30)
        blocks.append("[News & sentiment — GDELT]\n" + gdelt.format_context_for_llm(ctx))
    if getattr(cfg, "use_wiki", True):
        ents = gdelt.extract_entities(market.get("question", ""))
        wblock = wiki.wiki_context(ents, n=2)
        if wblock:
            blocks.append(wblock)
    if not blocks:
        return ""
    return ("=== HARNESS UPSTREAM CONTEXT (facts + news/sentiment + microstructure) ===\n"
            + "\n\n".join(blocks))


# ── offline source stubs ──────────────────────────────────────────────────────
def _fake_signals(fired, metrics=None):
    metrics = metrics if metrics is not None else {"yes_price": 0.42, "vol_liq_ratio": 3.1}
    return lambda market, **kw: {"fired": list(fired), "raw_metrics": metrics}


def _article(title, seendate, domain="example.com"):
    return {"title": title, "url": "http://x", "domain": domain,
            "seendate": seendate, "language": "English", "sourcecountry": "US"}


def _fake_ctx(articles, *, latest_tone=0.3, trend="rising", query="Iran peace"):
    """A gdelt_context()-shaped dict the REAL format_context_for_llm can render."""
    return lambda q, **kw: {
        "query": query, "articles": list(articles),
        "tone_timeline": [], "volume_timeline": [],
        "latest_tone": latest_tone, "attention_trend": trend,
    }


def _fresh_date(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")


def _fake_wiki(block):
    return lambda entities, **kw: block


_Q = "Will Iran and the United States sign a peace deal in 2026?"
_MARKET = {"market_id": "CID-EP-1", "question": _Q,
           "outcome_prices": [0.42, 0.58], "volume": 50000, "liquidity": 8000}


# ── (1) BYTE-COMPAT ─────────────────────────────────────────────────────────--
def test_text_byte_compat_full():
    cfg = _Cfg()
    arts = [_article("Iran United States peace deal nears", _fresh_date(1)),
            _article("Tehran Washington talks resume", _fresh_date(2))]
    wblock = ("[Background facts — Wikipedia]\n"
              "- Iran: Iran is a country in Western Asia.\n"
              "- United States: The United States is a country in North America.")
    with patched(signals, "compute_signals", _fake_signals(["insider_alert", "near_fifty"])), \
         patched(gdelt, "gdelt_context", _fake_ctx(arts)), \
         patched(wiki, "wiki_context", _fake_wiki(wblock)):
        pack = EP.build_evidence_pack(_MARKET, cfg)
        gold = _legacy_enrichment(_MARKET, cfg)
        thru_loop = loop._build_enrichment(_MARKET, cfg)
    assert pack.text == gold, f"byte-compat (full) failed:\n--pack--\n{pack.text!r}\n--gold--\n{gold!r}"
    assert thru_loop == gold, "loop._build_enrichment must delegate byte-identically"
    assert pack.n_sources == 3 and pack.text.startswith("=== HARNESS UPSTREAM CONTEXT")


def test_text_byte_compat_partial_no_signals_no_wiki():
    # signals fired empty -> no signals block; wiki "" -> no wiki block; GDELT block stays.
    cfg = _Cfg()
    arts = [_article("Some unrelated headline", _fresh_date(3))]
    with patched(signals, "compute_signals", _fake_signals([])), \
         patched(gdelt, "gdelt_context", _fake_ctx(arts)), \
         patched(wiki, "wiki_context", _fake_wiki("")):
        pack = EP.build_evidence_pack(_MARKET, cfg)
        gold = _legacy_enrichment(_MARKET, cfg)
    assert pack.text == gold, f"byte-compat (partial) failed:\n{pack.text!r}\n{gold!r}"
    assert pack.n_sources == 1 and pack.sources[0].kind == "news"


def test_text_byte_compat_gdelt_zero_articles_block_still_present():
    # GDELT block is ALWAYS emitted (legacy behaviour), even with no articles.
    cfg = _Cfg(use_signals=False, use_wiki=False)
    with patched(gdelt, "gdelt_context", _fake_ctx([])):
        pack = EP.build_evidence_pack(_MARKET, cfg)
        gold = _legacy_enrichment(_MARKET, cfg)
    assert pack.text == gold
    assert pack.n_sources == 1 and pack.sources[0].item_count == 0
    assert pack.text != ""  # the "no headlines" block is non-empty text...
    assert pack.evidence_quality == 0.0  # ...but contributes no real evidence


def test_text_byte_compat_empty():
    # Nothing fires, GDELT disabled, no wiki -> "" (exactly the legacy empty case).
    cfg = _Cfg(use_signals=True, use_gdelt=False, use_wiki=True)
    with patched(signals, "compute_signals", _fake_signals([])), \
         patched(wiki, "wiki_context", _fake_wiki("")):
        pack = EP.build_evidence_pack(_MARKET, cfg)
        gold = _legacy_enrichment(_MARKET, cfg)
    assert pack.text == "" and gold == "", (pack.text, gold)
    assert pack.n_sources == 0 and pack.total_items == 0 and pack.evidence_quality == 0.0


# ── (2) evidence_quality == 0.0 when all sources empty ────────────────────────
def test_evidence_quality_zero_when_empty():
    cfg = _Cfg()
    with patched(signals, "compute_signals", _fake_signals([])), \
         patched(gdelt, "gdelt_context", _fake_ctx([])), \
         patched(wiki, "wiki_context", _fake_wiki("")):
        pack = EP.build_evidence_pack(_MARKET, cfg)
    # GDELT block present but 0 articles; no signals; no wiki -> no real items at all.
    assert pack.total_items == 0
    assert pack.evidence_quality == 0.0


# ── (3) quality rises with more / fresher / more-relevant items ───────────────
def test_quality_rises_with_more_fresher_relevant():
    cfg = _Cfg(use_signals=False, use_wiki=False)

    low_arts = [_article("Local bakery wins award", _fresh_date(60))]  # 1, stale, off-topic
    high_arts = [_article("Iran United States peace deal signed", _fresh_date(0)),
                 _article("Tehran Washington peace agreement", _fresh_date(1)),
                 _article("Iran peace deal talks advance", _fresh_date(1)),
                 _article("United States Iran sign accord", _fresh_date(2))]  # 4, fresh, on-topic

    with patched(gdelt, "gdelt_context", _fake_ctx(low_arts)):
        low = EP.build_evidence_pack(_MARKET, cfg)
    with patched(gdelt, "gdelt_context", _fake_ctx(high_arts)):
        high = EP.build_evidence_pack(_MARKET, cfg)

    ls, hs = low.sources[0], high.sources[0]
    assert hs.item_count > ls.item_count
    assert hs.freshness_score > ls.freshness_score, (ls.freshness_score, hs.freshness_score)
    assert hs.relevance_score > ls.relevance_score, (ls.relevance_score, hs.relevance_score)
    assert hs.quality > ls.quality, (ls.quality, hs.quality)
    assert high.evidence_quality > low.evidence_quality, (low.evidence_quality, high.evidence_quality)
    # scores stay in [0, 1]
    for s in (ls, hs):
        for v in (s.freshness_score, s.relevance_score, s.quality):
            assert 0.0 <= v <= 1.0


# ── (4) content_hash stable across rebuilds; changes when items change ────────
def test_content_hash_stable_and_sensitive():
    cfg = _Cfg(use_signals=False, use_wiki=False)
    arts = [_article("Iran United States peace deal", _fresh_date(1))]
    with patched(gdelt, "gdelt_context", _fake_ctx(arts)):
        a = EP.build_evidence_pack(_MARKET, cfg)
        b = EP.build_evidence_pack(_MARKET, cfg)
    assert a.content_hash == b.content_hash, "hash must be stable across identical rebuilds"
    assert len(a.content_hash) == 64  # sha256 hex

    changed = [_article("A completely different headline", _fresh_date(1))]
    with patched(gdelt, "gdelt_context", _fake_ctx(changed)):
        c = EP.build_evidence_pack(_MARKET, cfg)
    assert c.content_hash != a.content_hash, "hash must change when the gathered items change"

    # adding an item also changes the hash
    with patched(gdelt, "gdelt_context", _fake_ctx(arts + changed)):
        d = EP.build_evidence_pack(_MARKET, cfg)
    assert d.content_hash != a.content_hash


# ── (5) to_dict JSON-serializable ─────────────────────────────────────────────
def test_to_dict_json_serializable():
    cfg = _Cfg()
    arts = [_article("Iran United States peace deal nears", _fresh_date(1))]
    wblock = "[Background facts — Wikipedia]\n- Iran: Iran is a country in Western Asia."
    with patched(signals, "compute_signals", _fake_signals(["insider_alert"])), \
         patched(gdelt, "gdelt_context", _fake_ctx(arts)), \
         patched(wiki, "wiki_context", _fake_wiki(wblock)):
        pack = EP.build_evidence_pack(_MARKET, cfg)
    d = pack.to_dict()
    s = json.dumps(d)  # must not raise
    round_trip = json.loads(s)
    assert round_trip["content_hash"] == pack.content_hash
    assert round_trip["n_sources"] == 3
    assert round_trip["text"] == pack.text
    assert {src["kind"] for src in round_trip["sources"]} == {"signals", "news", "facts"}
    # per-source dicts carry the full schema
    for src in d["sources"]:
        assert set(src) == {"name", "kind", "items", "item_count",
                            "freshness_score", "relevance_score", "quality", "raw_summary"}


TESTS = [
    ("text_byte_compat_full", test_text_byte_compat_full),
    ("text_byte_compat_partial_no_signals_no_wiki", test_text_byte_compat_partial_no_signals_no_wiki),
    ("text_byte_compat_gdelt_zero_articles_block_still_present", test_text_byte_compat_gdelt_zero_articles_block_still_present),
    ("text_byte_compat_empty", test_text_byte_compat_empty),
    ("evidence_quality_zero_when_empty", test_evidence_quality_zero_when_empty),
    ("quality_rises_with_more_fresher_relevant", test_quality_rises_with_more_fresher_relevant),
    ("content_hash_stable_and_sensitive", test_content_hash_stable_and_sensitive),
    ("to_dict_json_serializable", test_to_dict_json_serializable),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
