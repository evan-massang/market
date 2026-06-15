"""Keyless Wikipedia factual context for the forecaster.

GDELT tells the model what the press is *saying*; Wikipedia tells it the underlying *facts*
(who/what the entities are). Both free, keyless, read-only.

NOTE (2026-06-15): the MediaWiki action API (w/api.php search) now returns HTTP 403 to a
generic User-Agent, so we DON'T use search. Instead we hit the REST summary endpoint
DIRECTLY with the entity name (it normalizes case + follows redirects, so "iran",
"donald trump" -> the right page) and use a Wikimedia-policy-compliant User-Agent.

Public API:
  wiki_summary(term)            -> str   (lead-section extract, '' on miss)
  wiki_context(entities, n=2)   -> str   (compact LLM-ready block for 1-2 entities)
"""
from __future__ import annotations

import time

import httpx

try:
    from harness import obs
except Exception:
    obs = None

_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
# Wikimedia's UA policy wants tool name + version + contact; a generic UA gets 403'd.
_HEADERS = {"User-Agent": "PolymarketResearchHarness/1.0 (paper trading research; harness@example.org)"}
_TIMEOUT = 15.0
_PERSIST_TTL = 7 * 86400  # persistent (cross-restart) cache lifetime for facts (~7d)


def wiki_summary(term: str, max_chars: int = 600) -> str:
    """Lead-section extract for an entity, trimmed. '' on 404 / error / disambiguation."""
    if not term:
        return ""
    # Persistent (cross-restart) cache — facts change slowly, so a 7d TTL spares
    # the live Wikipedia REST call. Best-effort: ANY error falls through to the
    # unchanged live fetch below, so the returned value stays byte-identical.
    _pkey = None
    try:
        from harness import datacache as _dc
        _pkey = _dc.make_key("wiki", term.strip().lower(), max_chars)
        _hit = _dc.cache_get(_pkey)
        if isinstance(_hit, str) and _hit:
            return _hit
    except Exception:
        _pkey = None
    try:
        _url = _SUMMARY_URL + term.strip().replace(" ", "_")
        _t0 = time.perf_counter()
        r = httpx.get(_url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        _latency_ms = (time.perf_counter() - _t0) * 1000.0
        if obs:
            try:
                obs.hooks.on_data_fetch(
                    source="wiki", endpoint=_url, params={"term": term},
                    raw_text=r.text,
                    item_count=(1 if r.status_code == 200 else 0),
                    latency_ms=_latency_ms,
                )
            except Exception:
                pass
        if r.status_code != 200:
            return ""
        d = r.json() or {}
        if d.get("type") == "disambiguation":
            return ""
        extract = " ".join((d.get("extract") or "").split())[:max_chars]
        # Mirror a genuine non-empty extract into the persistent cache (best-effort).
        if _pkey is not None and extract:
            try:
                _dc.cache_set(_pkey, extract, "wiki", _PERSIST_TTL)
            except Exception:
                pass
        return extract
    except Exception:
        return ""


def wiki_context(entities, n: int = 2, max_chars: int = 600) -> str:
    """Compact Wikipedia block for up to `n` entities. '' if nothing usable, so callers skip
    it cleanly. Generic one-letter/country abbreviations ('US') often redirect fine; misses
    are simply dropped."""
    if isinstance(entities, str):
        entities = [entities]
    blocks, seen = [], set()
    for term in (entities or []):
        key = term.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        summary = wiki_summary(term, max_chars=max_chars)
        if summary:
            blocks.append(f"- {term}: {summary}")
        if len(blocks) >= n:
            break
    if not blocks:
        return ""
    return "[Background facts — Wikipedia]\n" + "\n".join(blocks)


if __name__ == "__main__":
    import sys
    from harness import gdelt
    q = " ".join(sys.argv[1:]) or "Will Iran and the United States agree to a peace deal by 2026?"
    ents = gdelt.extract_entities(q)
    print(f"question : {q}")
    print(f"entities : {ents}")
    print("\n" + (wiki_context(ents) or "(no Wikipedia context found)"))
