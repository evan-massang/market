"""
GDELT DOC 2.0 client — free, keyless news / sentiment context for opinion markets.

PolySwarm forecasts OPINION markets (elections, approval, virality, geopolitics).
For those, *what the press is saying and how loud it is saying it* is real signal.
GDELT's DOC 2.0 API (https://api.gdeltproject.org/api/v2/doc/doc) is a $0, keyless
window onto the global news stream: article lists, an average-tone timeline, and a
news-volume timeline. This module wraps it with the three things you must get right
to use GDELT without getting blocked:

  1. RATE LIMIT (sticky!).  GDELT throttles to ~1 request / 5 s per IP, and the
     throttle is sticky — hammer it and the penalty lingers. We enforce a *global*
     (module-level) minimum spacing of 5 s between calls using time.monotonic().
  2. NON-JSON THROTTLE BODY.  When throttled, GDELT replies HTTP 200 with a PLAIN-TEXT
     body ("Please limit requests to one every 5 seconds...") — not JSON. We guard
     every r.json() (Content-Type check + try/except) and on a non-JSON body we treat
     it as throttled, back off, and return empty rather than crashing.
  3. TTL CACHE.  A small in-memory ~15 min cache keyed by the request params means
     repeated/again-same-topic lookups (very common in a forecasting loop) don't
     re-hit the API at all.

Read-only / paper only. No keys, no wallet, no execution — just news context.

Public API:
  gdelt_articles(query, timespan, max_records)  -> list[dict]
  gdelt_tone_timeline(query, timespan)          -> list[{date, value}]
  gdelt_volume_timeline(query, timespan)        -> list[{date, value}]
  gdelt_context(query, timespan, max_records)   -> dict
  build_query(question)                         -> str
  format_context_for_llm(ctx)                   -> str
"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx

try:
    from harness import obs
except Exception:
    obs = None

# ── endpoint / client config ─────────────────────────────────────────────────
BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "PolymarketHarness/1.0"
HEADERS = {"User-Agent": USER_AGENT}
HTTP_TIMEOUT = 30.0

# ── rate limiting (sticky ~1 req / 5 s per IP) ───────────────────────────────
MIN_REQUEST_INTERVAL = 6.0   # min spacing between GDELT calls (>=5s, with margin —
                             # exactly 5.0 sits on the boundary and GDELT still 429s)
THROTTLE_BACKOFF = 6.0       # extra spacing imposed after we *see* a throttle body
CACHE_TTL = 15 * 60.0        # in-memory cache lifetime (seconds)
PERSIST_TTL = 6 * 3600.0     # persistent (cross-restart) cache lifetime for news (~6h)

# Module-global throttle state. We use time.monotonic() (never wall-clock) so the
# spacing is immune to clock changes. A lock keeps it correct if the harness ever
# calls GDELT from more than one thread.
_throttle_lock = threading.Lock()
_next_allowed_monotonic = 0.0   # earliest monotonic time at which a call may be sent

# Tiny in-memory TTL cache: key -> (stored_at_monotonic, value)
_cache: dict[tuple, tuple[float, object]] = {}


# ── rate-limit primitives ────────────────────────────────────────────────────
def _wait_for_slot() -> None:
    """Block until the global 5 s spacing has elapsed, then reserve the next slot."""
    global _next_allowed_monotonic
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_allowed_monotonic - now
        if wait > 0:
            time.sleep(wait)
        # Reserve: the *next* request may not go out until MIN_REQUEST_INTERVAL later.
        _next_allowed_monotonic = time.monotonic() + MIN_REQUEST_INTERVAL


def _register_throttle() -> None:
    """We were throttled — push the next-allowed time further into the future."""
    global _next_allowed_monotonic
    with _throttle_lock:
        _next_allowed_monotonic = max(
            _next_allowed_monotonic,
            time.monotonic() + MIN_REQUEST_INTERVAL + THROTTLE_BACKOFF,
        )


# ── cache primitives ─────────────────────────────────────────────────────────
def _cache_key(params: dict) -> tuple:
    """Stable cache key from request params (covers query, mode, timespan, etc.).

    'format' is excluded because it is constant ('json') and never changes results.
    """
    return tuple(sorted((k, str(v)) for k, v in params.items() if k != "format"))


def _cache_get(key: tuple):
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: tuple, value) -> None:
    _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    """Drop all cached responses (handy for tests / forcing a refresh)."""
    _cache.clear()


# ── rate-limited, JSON-guarded GET ───────────────────────────────────────────
def _looks_like_json(resp: httpx.Response) -> bool:
    """Heuristic: does this response actually carry a JSON object body?

    GDELT's throttle reply is HTTP 200 with a plain-text body, so a 200 status is
    NOT enough — we check the Content-Type and that the body opens with '{'.
    """
    ctype = resp.headers.get("content-type", "").lower()
    body = resp.text.lstrip()
    if "json" in ctype:
        return body.startswith("{") or body.startswith("[")
    # Content-Type missing/wrong but body might still be JSON — accept only if it
    # clearly opens as a JSON object (the throttle text starts with "Please ...").
    return body.startswith("{")


def _get(params: dict) -> dict:
    """Rate-limited GET against GDELT DOC with a TTL cache and a hard JSON guard.

    Returns the parsed JSON dict on success, or {} on throttle / non-JSON / error.
    Never raises — callers treat {} as "no data this window".
    """
    key = _cache_key(params)
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    # Persistent (cross-restart) cache — belt-and-suspenders BEHIND the in-memory
    # one. The in-memory _cache is lost on every Python restart; this survives it.
    # Best-effort: ANY error here falls through to the unchanged live path below.
    _pkey = None
    try:
        from harness import datacache as _dc
        _pkey = _dc.make_key("gdelt", repr(key))
        _pval = _dc.cache_get(_pkey)
        if isinstance(_pval, dict):
            _cache_set(key, _pval)   # warm the in-memory cache too
            return _pval
    except Exception:
        _pkey = None

    _wait_for_slot()

    _t0 = time.perf_counter()
    try:
        resp = httpx.get(BASE_URL, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        # Network hiccup / timeout — back off a touch and report empty.
        _register_throttle()
        return {}
    _latency_ms = (time.perf_counter() - _t0) * 1000.0

    # obs (leaf emit): capture the raw body BEFORE the _looks_like_json throttle guard
    # so throttle plaintext bodies (HTTP 200) are preserved for the audit trail. This
    # does NOT change the {}/data return or the throttle handling below.
    if obs:
        try:
            _raw_text = resp.text
            try:
                _parsed = resp.json()
            except Exception:
                _parsed = None
            _item_count = len(_parsed) if hasattr(_parsed, "__len__") else 0
            obs.hooks.on_data_fetch(
                source="gdelt", endpoint=BASE_URL, params=dict(params),
                raw_text=_raw_text, item_count=(_item_count or 0),
                latency_ms=_latency_ms,
            )
        except Exception:
            pass

    # Guard #1: is the body even JSON? A plain-text body == we were throttled.
    if not _looks_like_json(resp):
        _register_throttle()
        return {}

    # Guard #2: parse defensively (truncated/invalid JSON also means "no data").
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        _register_throttle()
        return {}

    if not isinstance(data, dict):
        return {}

    _cache_set(key, data)  # only cache genuine successes (never throttle/empty)
    # Mirror the genuine success into the persistent cache (best-effort, never raises).
    if _pkey is not None:
        try:
            from harness import datacache as _dc
            _dc.cache_set(_pkey, data, "gdelt", int(PERSIST_TTL))
        except Exception:
            pass
    return data


# ── timeline parsing helper ──────────────────────────────────────────────────
def _parse_timeline(data: dict) -> list[dict]:
    """Extract [{date, value}, ...] from a GDELT Timeline* JSON response.

    Shape: {"timeline": [{"series": "...", "data": [{"date": "...", "value": n}]}]}.
    We take the first series (Average Tone / Volume Intensity) and coerce defensively.
    """
    out: list[dict] = []
    timeline = data.get("timeline") if isinstance(data, dict) else None
    if not isinstance(timeline, list) or not timeline:
        return out
    series = timeline[0] if isinstance(timeline[0], dict) else {}
    for point in series.get("data", []) or []:
        if not isinstance(point, dict):
            continue
        date = str(point.get("date", ""))
        try:
            value = float(point.get("value"))
        except (TypeError, ValueError):
            continue
        out.append({"date": date, "value": value})
    return out


# ── public: article list ─────────────────────────────────────────────────────
def gdelt_articles(query: str, timespan: str = "14d", max_records: int = 25) -> list[dict]:
    """Recent articles matching `query`, newest first.

    Returns a list of {title, url, domain, seendate, language, sourcecountry}.
    Empty list on throttle / no coverage.
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
        "maxrecords": str(max_records),
        "sort": "DateDesc",
    }
    data = _get(params)
    articles = data.get("articles") if isinstance(data, dict) else None
    out: list[dict] = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        out.append({
            "title": str(a.get("title", "")),
            "url": str(a.get("url", "")),
            "domain": str(a.get("domain", "")),
            "seendate": str(a.get("seendate", "")),
            "language": str(a.get("language", "")),
            "sourcecountry": str(a.get("sourcecountry", "")),
        })
    return out


# ── public: tone timeline ────────────────────────────────────────────────────
def gdelt_tone_timeline(query: str, timespan: str = "21d") -> list[dict]:
    """Daily average-tone timeline for `query` (GDELT tone scale ~ -10..+10).

    Returns [{date, value}, ...]; positive = more positive coverage. Empty on throttle.
    """
    params = {
        "query": query,
        "mode": "TimelineTone",
        "format": "json",
        "timespan": timespan,
    }
    return _parse_timeline(_get(params))


# ── public: volume timeline ──────────────────────────────────────────────────
def gdelt_volume_timeline(query: str, timespan: str = "21d") -> list[dict]:
    """Daily news-volume (attention intensity) timeline for `query`.

    Returns [{date, value}, ...]; value = % of all monitored coverage. Empty on throttle.
    """
    params = {
        "query": query,
        "mode": "TimelineVol",
        "format": "json",
        "timespan": timespan,
    }
    return _parse_timeline(_get(params))


# ── trend / tone summarization ───────────────────────────────────────────────
def _latest_tone(tone_timeline: list[dict]) -> float | None:
    """Smoothed most-recent tone: mean of the last few points (None if no data)."""
    vals = [p["value"] for p in tone_timeline if isinstance(p.get("value"), (int, float))]
    if not vals:
        return None
    tail = vals[-3:]
    return round(sum(tail) / len(tail), 3)


def _attention_trend(volume_timeline: list[dict]) -> str:
    """Is news attention rising? Compare the recent half vs the earlier half.

    Returns 'rising' | 'falling' | 'flat' | 'unknown'.
    """
    vals = [p["value"] for p in volume_timeline if isinstance(p.get("value"), (int, float))]
    if len(vals) < 4:
        return "unknown"
    mid = len(vals) // 2
    early, late = vals[:mid], vals[mid:]
    e = sum(early) / len(early) if early else 0.0
    l = sum(late) / len(late) if late else 0.0
    if e <= 0:
        return "rising" if l > 0 else "unknown"
    ratio = l / e
    if ratio >= 1.15:
        return "rising"
    if ratio <= 0.85:
        return "falling"
    return "flat"


# ── public: combined context ─────────────────────────────────────────────────
def gdelt_context(query: str, timespan: str = "14d", max_records: int = 25) -> dict:
    """One-shot news context: articles + tone + volume, summarized.

    Makes three GDELT calls (article list, tone timeline, volume timeline). The
    module-global 5 s spacing is enforced automatically between each, so this is the
    convenient single entry point for the forecasting loop. Returns:
      {query, articles, tone_timeline, volume_timeline, latest_tone, attention_trend}.
    """
    articles = gdelt_articles(query, timespan=timespan, max_records=max_records)
    used_query = query
    # Broaden once if the narrow AND-query found nothing: fall back to just the lead entity,
    # so we still get headlines + a tone timeline instead of "no coverage / no tone data".
    if not articles and query.split():
        broad = query[1:].split('"', 1)[0] if query.startswith('"') else query.split()[0]
        if broad and broad != query:
            retry = gdelt_articles(broad, timespan=timespan, max_records=max_records)
            if retry:
                articles, used_query = retry, broad
    tone_timeline = gdelt_tone_timeline(used_query, timespan=timespan)
    volume_timeline = gdelt_volume_timeline(used_query, timespan=timespan)
    return {
        "query": used_query,
        "articles": articles,
        "tone_timeline": tone_timeline,
        "volume_timeline": volume_timeline,
        "latest_tone": _latest_tone(tone_timeline),
        "attention_trend": _attention_trend(volume_timeline),
    }


# ── question -> focused GDELT query ───────────────────────────────────────────
# Leading interrogative / auxiliary verbs that begin a market question.
_LEADING_AUX = re.compile(
    r"^\s*(?:will|would|could|should|can|did|does|do|is|are|was|were|has|have|had)\s+",
    re.IGNORECASE,
)
# Trailing temporal qualifier: "in 2026", "by the end of 2026", "before March 2026",
# "this year", "by Q1", "during 2025", etc. Strip it so the query stays on the entity.
_TEMPORAL_TAIL = re.compile(
    r"\s+(?:in|by|before|after|during|on|at|until|through|within|by the end of|"
    r"this|next|prior to)\s+(?:the\s+)?(?:end of\s+)?(?:q[1-4]\b|"
    r"january|february|march|april|may|june|july|august|september|october|november|"
    r"december|spring|summer|fall|autumn|winter|year|month|week|"
    r"\d{1,2}(?:st|nd|rd|th)?|\d{4}|[\w\s]{0,15}\d{4})\b.*$",
    re.IGNORECASE,
)
# Low-signal filler words dropped when condensing a long question.
_STOPWORDS = {
    "the", "a", "an", "be", "to", "of", "in", "on", "at", "by", "for", "and", "or",
    "with", "this", "that", "these", "those", "its", "their", "there", "it", "as",
    "than", "then", "from", "into", "out", "up", "down", "more", "most", "least",
    "any", "all", "some", "no", "not",
}
# Domain topic terms preferred as the salient keyword(s) paired with an entity.
_TOPIC_KEYWORDS = [
    "election", "approval", "primary", "caucus", "nominee", "nomination", "referendum",
    "vote", "impeach", "impeachment", "resign", "resignation", "ceasefire", "truce", "war",
    "invasion", "sanctions", "sanction", "ban", "recession", "rate", "inflation",
    "championship", "verdict", "indictment", "indict", "win", "reelection", "poll",
    "shutdown", "strike", "airstrike", "treaty", "summit",
    # geopolitical / event terms that were arriving as 'unknown' topics:
    "peace", "deal", "agreement", "airspace", "blockade", "hostage", "prisoner", "pardon",
    "arrest", "attack", "airspace", "tariff", "tariffs", "default", "bankruptcy", "merger",
    "acquisition", "lawsuit", "release", "released", "launch", "draw", "hormuz", "nuclear",
]
# Generic / low-specificity entities — deprioritized so a SPECIFIC entity (Iran, Hormuz)
# beats a generic one (US, United States) when building the query.
_GENERIC_ENTITIES = {
    "us", "u.s.", "usa", "america", "american", "uk", "eu", "un", "world", "global",
    "united", "states", "state", "national", "international", "government",
}


def extract_entities(question: str) -> list[str]:
    """Proper-noun runs (Capitalized, non-stopword, <=3 words) from a question, ordered
    SPECIFIC-first (generic country/government words deprioritized), de-duplicated. Reused
    by build_query AND the Wikipedia source to ground the forecast on the right subject."""
    q = (question or "").strip().rstrip(" ?.!")
    q = _LEADING_AUX.sub("", q, count=1)
    q = _TEMPORAL_TAIL.sub("", q).strip()
    runs, cur = [], []
    for w in q.split():
        tok = w.strip(",.;:'\"").removesuffix("'s").removesuffix("’s")
        if tok[:1].isupper() and tok.lower() not in _STOPWORDS and any(c.isalpha() for c in tok):
            cur.append(tok)
        else:
            if cur:
                runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    ents, seen = [], set()
    for r in runs:
        e = " ".join(r[:3])
        if e.lower() not in seen:
            seen.add(e.lower()); ents.append(e)
    ents.sort(key=lambda e: (all(w.lower() in _GENERIC_ENTITIES for w in e.split()),
                             -len(e.split()), -len(e)))
    return ents


def build_query(question: str) -> str:
    """Derive a focused GDELT query: the 1-2 most SPECIFIC entities + 1-2 salient topic
    terms, AND-joined (multi-word entities quoted for an exact-phrase match). Robust; never
    raises. The old version grabbed the first 1-word entity + longest leftover word, so
    'US x Iran permanent peace deal' became 'US permanent' (missed Iran AND the topic).

    Examples:
      "US x Iran permanent peace deal in 2026?"  -> 'Iran US peace'
      "Israel closes its airspace by June 15?"   -> 'Israel airspace'
    """
    q = (question or "").strip()
    if not q:
        return ""
    entities_top = extract_entities(q)[:2]
    entity_lc = set(" ".join(entities_top).lower().split())

    q2 = _TEMPORAL_TAIL.sub("", _LEADING_AUX.sub("", q.rstrip(" ?.!"), count=1)).strip()
    lowered = [w.strip(",.;:'\"").lower().removesuffix("'s").removesuffix("’s") for w in q2.split()]
    topics = [w for w in lowered if w in _TOPIC_KEYWORDS and w not in entity_lc]
    topics = list(dict.fromkeys(topics))          # de-dup, keep question order
    if not topics:
        cands = [w for w in lowered if w.isalpha() and len(w) >= 4
                 and w not in _STOPWORDS and w not in entity_lc]
        topics = sorted(dict.fromkeys(cands), key=lambda w: -len(w))[:1]

    parts = [f'"{e}"' if " " in e else e for e in entities_top] + topics
    parts = parts[:3]                              # keep the AND short enough to still match
    if not parts:
        parts = [w for w in lowered if w.isalpha() and w not in _STOPWORDS][:3]
    return " ".join(parts).strip()


# ── compact context block for the LLM ────────────────────────────────────────
def format_context_for_llm(ctx: dict) -> str:
    """Render a gdelt_context() dict into a compact text block for the forecaster.

    Includes the top ~8 headlines, the latest average tone (with a plain-language
    read), and whether news attention is rising — ready to inject into PolySwarm's
    forecasting context.
    """
    if not isinstance(ctx, dict):
        return "GDELT news context: unavailable."

    query = ctx.get("query", "")
    articles = ctx.get("articles") or []
    latest_tone = ctx.get("latest_tone")
    trend = ctx.get("attention_trend", "unknown")

    lines = [f"GDELT news context for: {query}"]

    if latest_tone is None:
        lines.append("Latest average tone: n/a (no tone data this window)")
    else:
        if latest_tone > 0.5:
            mood = "net-positive coverage"
        elif latest_tone < -0.5:
            mood = "net-negative coverage"
        else:
            mood = "roughly neutral coverage"
        lines.append(
            f"Latest average tone: {latest_tone:+.2f} ({mood}; "
            f"GDELT tone scale ~ -10 very negative .. +10 very positive)"
        )

    trend_note = {
        "rising": "news attention is RISING (story gaining momentum)",
        "falling": "news attention is FALLING (story cooling off)",
        "flat": "news attention is roughly flat",
        "unknown": "news attention trend unknown (insufficient data)",
    }.get(trend, f"news attention trend: {trend}")
    lines.append(f"Attention/volume: {trend_note}")

    lines.append(f"Recent matching articles: {len(articles)}")
    if articles:
        lines.append("Top headlines:")
        for a in articles[:12]:
            title = str(a.get("title", "")).strip()
            if not title:
                continue
            domain = a.get("domain", "")
            seendate = a.get("seendate", "")
            meta = ", ".join(x for x in (domain, seendate) if x)
            lines.append(f"  - {title}" + (f" ({meta})" if meta else ""))
    else:
        lines.append("No recent headlines (no coverage, or GDELT throttled this call).")

    return "\n".join(lines)


# ── live smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_query = '"Venezuela election"'
    print(f"[gdelt] live test — querying {test_query} (5s spacing enforced)...\n")

    ctx = gdelt_context(test_query, timespan="14d", max_records=25)

    articles = ctx["articles"]
    print(f"article count : {len(articles)}")
    print(f"latest_tone   : {ctx['latest_tone']}")
    print(f"attention     : {ctx['attention_trend']}")
    print(f"tone points   : {len(ctx['tone_timeline'])}")
    print(f"volume points : {len(ctx['volume_timeline'])}")
    print("\nfirst 3 headlines:")
    for a in articles[:3]:
        print(f"  - {a['title']}  [{a['domain']}, {a['seendate']}, {a['sourcecountry']}]")

    print("\n--- format_context_for_llm() ---")
    print(format_context_for_llm(ctx))

    assert len(articles) >= 1, "expected at least 1 article from GDELT (throttled or no coverage?)"
    print("\n[gdelt] OK — at least 1 article returned.")
