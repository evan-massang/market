"""
P1 — Market classifier:  tag_market(market) -> "opinion" | "mechanical".

Taxonomy (from the plan):
  OPINION    = resolution CAUSED by crowd sentiment / behavior / attention:
               elections, approval ratings, polls, cultural awards, virality,
               "most popular", public-opinion. -> we FORECAST these.
  MECHANICAL = resolution by an objective external process the crowd doesn't drive:
               price/number thresholds, crypto/stock levels, sports results,
               weather, central-bank rate decisions, economic data releases,
               court/regulatory rulings, launches. -> we SKIP these.

Design: a transparent, deterministic, $0 weighted-regex engine (auditable — it
returns the exact signals that fired). Ambiguous cases (small score margin) can
optionally be broken by the local LLM. Works on BOTH Gamma market dicts and
PolyBench market records (tolerant field normalization), so the same classifier
is reused for the P2.5 historical read.

No network, no LLM unless use_llm=True is explicitly passed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict

# ── liquidity floor defaults (USD) ───────────────────────────────────────────
DEFAULT_MIN_VOLUME = 5_000.0
DEFAULT_MIN_LIQUIDITY = 1_000.0

# ── signal patterns: (compiled_regex, weight, label) ─────────────────────────
# Weights are small ints; the winning label is the higher summed weight.
_RAW_SIGNALS: list[tuple[str, int, str]] = [
    # ---- MECHANICAL ----
    # price / numeric thresholds (require $, %, or magnitude suffix so bare years
    # like "2026" do NOT match)
    (r"\$\s?\d", 3, "mechanical"),
    (r"\b\d+(?:\.\d+)?\s?(?:k|m|bn?|million|billion|trillion)\b", 3, "mechanical"),
    (r"\b(?:above|below|over|under|reach|exceed|hit|surpass|cross|close (?:above|below)|"
     r"greater than|less than|at least|more than)\b.{0,20}(?:\$|\d|%)", 3, "mechanical"),
    (r"\b(?:price|all-?time high|market ?cap|trading at)\b", 2, "mechanical"),
    # crypto / stocks / indices
    (r"\b(?:btc|eth|sol|xrp|doge|bitcoin|ethereum|solana|crypto|altcoin)\b", 2, "mechanical"),
    (r"\b(?:s&p|nasdaq|dow jones|stock|share price|\$[A-Z]{1,5}\b)", 2, "mechanical"),
    # sports
    (r"\b(?:nfl|nba|mlb|nhl|premier league|la liga|serie a|bundesliga|super bowl|"
     r"world cup|champions league|stanley cup|world series|playoff|championship|"
     r"win the (?:game|match|title|cup|series|final)|defeat|vs\.?|ucl|ufc|"
     r"grand prix|formula ?1|\bf1\b|olympic|gold medal|relegat)\b", 3, "mechanical"),
    # sports — scorelines / dated match results / in-game props (national-team football
    # etc. that the league-name list above misses). "Exact Score: A x - y B?",
    # "Will <team> win on 2026-06-13?", "Both Teams to Score", "to score first".
    (r"\bexact score\b", 4, "mechanical"),
    (r"\bwin on \d{4}-\d{2}-\d{2}\b", 3, "mechanical"),
    (r"\b(?:both teams to score|neither team to score|to score first|"
     r"clean sheet|first half|halftime|full[- ]?time)\b", 3, "mechanical"),
    # weather / natural
    (r"\b(?:temperature|hurricane|earthquake|rainfall|snowfall|degrees|celsius|"
     r"fahrenheit|magnitude|tornado|wildfire)\b", 3, "mechanical"),
    # central bank / rates
    (r"\b(?:fed|federal reserve|fomc|interest rate|rate (?:cut|hike|decision)|"
     r"basis points|\bbps\b|\becb\b|central bank|jerome powell|rate by)\b", 3, "mechanical"),
    # economic data releases
    (r"\b(?:cpi|inflation rate|\bgdp\b|unemployment|jobs report|nonfarm|payrolls|"
     r"\bpce\b|initial claims|retail sales|recession)\b", 3, "mechanical"),
    # court / regulatory rulings (official process, not crowd-driven)
    (r"\b(?:supreme court|court (?:rule|ruling)|sec (?:approve|reject|sue)|verdict|"
     r"convicted|indicted|sentenced|found guilty|appeals court)\b", 2, "mechanical"),
    # launches / scientific
    (r"\b(?:spacex|nasa launch|rocket launch|satellite|starship)\b", 2, "mechanical"),
    # counts / numeric-quantity thresholds the crowd doesn't "drive" (tweet/post/video counts):
    # "post 65-89 tweets", "post <40 tweets", "40-64 tweets". These arrived as 'unknown' and the
    # swarm hallucinated a bucket probability — tag mechanical so they're skipped/divergence-gated.
    (r"\b\d+\s*(?:-|–|to)\s*\d+\s+(?:tweets?|posts?|times|videos?|goals?|points?|hours?)\b", 3, "mechanical"),
    (r"\b(?:post|tweet|publish|send)\w*\s+(?:<|>|≤|≥|\d|fewer|less|more|at least|at most|under|over|between|exactly)\b", 3, "mechanical"),
    (r"\b(?:fewer|less|more|greater|at least|at most|no more|under|over|exactly)\s+(?:than\s+)?\d+\s+(?:tweets?|posts?|times|videos?)\b", 3, "mechanical"),
    # product / software released-by-a-date — an objective ship event, not crowd sentiment:
    # "GPT-5.6 released by June 15", "launched on …", plus versioned product names.
    (r"\b(?:released?|launch(?:ed|es)?|ship(?:ped|s)?|unveiled?|drop(?:ped|s)?)\s+(?:by|before|on)\b", 3, "mechanical"),
    (r"\b(?:gpt|llama|claude|gemini|grok|chatgpt|ios|android|windows)[\s-]?\d+(?:\.\d+)?\b", 2, "mechanical"),

    # ---- OPINION ----
    # elections / political contests (outcome driven by voter/crowd behavior) — weight 4
    # so a numeric threshold on a crowd-driven quantity doesn't flip it to mechanical.
    (r"\b(?:election|elected|re-?elect|win the (?:presidency|primary|nomination|"
     r"election|seat|race|senate|house)|win control|win a majority|take control|"
     r"control of (?:the )?(?:senate|house|congress)|nominee|nomination|presidential|"
     r"gubernatorial|senate race|house race|primary|caucus|electoral|ballot|"
     r"win .{0,15}(?:state|district|county|senate|house|majority)|"
     r"become (?:president|prime minister|chancellor|nominee))\b", 4, "opinion"),
    (r"\b(?:democrats?|republicans?|gop|labour|tory|maga)\b.{0,30}\b(?:win|control|"
     r"majority|flip|sweep)\b", 2, "opinion"),
    (r"\bcandidate\b", 1, "opinion"),
    # approval / polling / sentiment — weight 4 (opinion subject dominates a threshold)
    (r"\b(?:approval rating|disapproval|favorability|poll(?:s|ing)?|popular vote|"
     r"net approval|approval of)\b", 4, "opinion"),
    # cultural awards / attention / virality
    (r"\b(?:oscars?|grammys?|emmys?|golden globe|nobel|person of the year|"
     r"best (?:picture|actor|actress|director)|man of the (?:match|year)|"
     r"\baward\b|go viral|trending|most (?:popular|streamed|watched|talked|"
     r"discussed|searched|admired)|number one|#1\b|top of the chart|tiktok)\b", 2, "opinion"),
    # attention metrics (audience growth = crowd attention)
    (r"\b(?:followers|subscribers|streams|most liked|going viral)\b", 3, "opinion"),
    # public-opinion / popularity framing
    (r"\b(?:public opinion|sentiment|most admired|popularity)\b", 2, "opinion"),
]

_SIGNALS = [(re.compile(p, re.IGNORECASE), w, label) for p, w, label in _RAW_SIGNALS]

# Polymarket resolution boilerplate that pollutes keyword matching — notably
# "primary resolution source" (matched our political 'primary' signal on EVERY
# market). Stripped before the description is ever scored.
_BOILERPLATE = re.compile(
    r"primary resolution source|resolution source|this market will resolve|"
    r"will resolve according to|resolves? to|resolution will be|resolve immediately|"
    r"according to the rules|\buma\w*", re.IGNORECASE)


def _strip_boilerplate(text: str) -> str:
    return _BOILERPLATE.sub(" ", text)


@dataclass
class Classification:
    label: str                       # "opinion" | "mechanical" | "unknown"
    confidence: float                # 0..1, margin / total weight
    reason: str
    opinion_score: int = 0
    mechanical_score: int = 0
    signals: list[str] = field(default_factory=list)   # human-readable fired signals
    method: str = "rules"            # "rules" | "llm" | "rules+llm"
    ambiguous: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ── field normalization (Gamma & PolyBench tolerant) ─────────────────────────
def _market_text(market) -> tuple[str, str]:
    """Return (question, description) from a market dict OR a raw string."""
    if isinstance(market, str):
        return market, ""
    q = market.get("question") or market.get("title") or ""
    d = (market.get("description") or market.get("resolution")
         or market.get("resolutionSource") or market.get("rules") or "")
    return str(q), str(d)


def _market_floats(market) -> tuple[float, float]:
    """Return (volume, liquidity), tolerant of Gamma/PolyBench key names & str values."""
    def f(*keys):
        if not isinstance(market, dict):
            return 0.0
        for k in keys:
            v = market.get(k)
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0
    volume = f("volume", "volumeNum", "volume24hr", "volume_24hr")
    liquidity = f("liquidity", "liquidityNum", "liquidity_num")
    return volume, liquidity


# ── core rule engine ─────────────────────────────────────────────────────────
def _score(text: str) -> tuple[int, int, list[str]]:
    op = me = 0
    fired: list[str] = []
    for rx, w, label in _SIGNALS:
        m = rx.search(text)
        if m:
            if label == "opinion":
                op += w
            else:
                me += w
            fired.append(f"{label}+{w}: '{m.group(0).strip()[:40]}'")
    return op, me, fired


def tag_market(market, *, use_llm: bool = False, ambiguity_margin: int = 2,
               llm_model: str | None = None) -> Classification:
    """Classify a market as 'opinion' or 'mechanical'.

    market: dict (Gamma or PolyBench) or a raw question string.
    use_llm: if True, ambiguous cases (|opinion-mechanical| <= ambiguity_margin,
             or no signals at all) are broken by the local LLM.
    Returns a Classification with the exact signals that fired.
    """
    question, description = _market_text(market)
    # The QUESTION is the reliable signal; the resolution DESCRIPTION is mostly
    # boilerplate ("primary resolution source…") that pollutes matching, so only
    # fall back to it (boilerplate-stripped) when the question yields no signals.
    op, me, fired = _score(question)
    scored_from = "question"
    if op + me == 0 and description:
        op, me, fired = _score(_strip_boilerplate(description))
        scored_from = "description"
    total = op + me
    margin = abs(op - me)
    ambiguous = (total == 0) or (margin <= ambiguity_margin)

    if total == 0:
        label, conf, reason = "unknown", 0.0, "no rule signals fired"
    elif op > me:
        label, conf = "opinion", round(margin / total, 3)
        reason = f"opinion signals outweigh mechanical ({op} vs {me})"
    elif me > op:
        label, conf = "mechanical", round(margin / total, 3)
        reason = f"mechanical signals outweigh opinion ({me} vs {op})"
    else:
        label, conf, reason = "unknown", 0.0, f"tie ({op}={me})"

    result = Classification(
        label=label, confidence=conf, reason=reason,
        opinion_score=op, mechanical_score=me, signals=fired,
        method="rules", ambiguous=ambiguous,
    )

    if use_llm and ambiguous:
        llm_label = _llm_tiebreak(question, description, model=llm_model)
        if llm_label in ("opinion", "mechanical"):
            result.label = llm_label
            result.method = "rules+llm" if total else "llm"
            result.reason += f"; LLM tiebreak -> {llm_label}"
            if result.confidence == 0.0:
                result.confidence = 0.5
    return result


def _llm_tiebreak(question: str, description: str, model: str | None = None) -> str:
    """Ask the local LLM to classify an ambiguous market. Uses PolySwarm's existing
    keyless Ollama client. Returns 'opinion'|'mechanical'|'unknown'. Never raises."""
    try:
        from core.agent import _get_llm_client, _call_llm  # type: ignore
    except Exception:
        return "unknown"
    system = "You classify prediction markets. Reply with ONLY the requested JSON."
    user = (
        "Classify this prediction market as OPINION or MECHANICAL.\n"
        "OPINION = the outcome is caused by crowd sentiment/behavior/attention "
        "(elections, approval ratings, polls, awards, virality, popularity).\n"
        "MECHANICAL = the outcome is an objective external process the crowd does not "
        "drive (price/number thresholds, crypto/stock levels, sports results, weather, "
        "central-bank rate decisions, economic data, court rulings, launches).\n\n"
        f"Question: {question}\nResolution: {description[:400]}\n\n"
        'Reply with ONLY JSON: {"label": "opinion" | "mechanical"}'
    )
    try:
        if model:
            os.environ["MODEL_FAST"] = model
        provider, client = _get_llm_client()
        raw = _call_llm(provider, client, system, user)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        label = json.loads(m.group(0))["label"].strip().lower() if m else ""
        return label if label in ("opinion", "mechanical") else "unknown"
    except Exception:
        return "unknown"


# ── liquidity floor ──────────────────────────────────────────────────────────
def passes_liquidity_floor(market, min_volume: float = DEFAULT_MIN_VOLUME,
                           min_liquidity: float = DEFAULT_MIN_LIQUIDITY) -> bool:
    """True if the market is liquid enough to bother forecasting/paper-trading."""
    volume, liquidity = _market_floats(market)
    return volume >= min_volume and liquidity >= min_liquidity


def should_forecast(market, *, use_llm: bool = False,
                    min_volume: float = DEFAULT_MIN_VOLUME,
                    min_liquidity: float = DEFAULT_MIN_LIQUIDITY) -> tuple[bool, Classification]:
    """Combined gate for the loop: forecast only OPINION markets above the floor."""
    cls = tag_market(market, use_llm=use_llm)
    ok = (cls.label == "opinion") and passes_liquidity_floor(market, min_volume, min_liquidity)
    return ok, cls
