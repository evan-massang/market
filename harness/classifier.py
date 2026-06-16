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

P4 — RICHER LABELS
==================
Beyond the legacy 3-way ``label`` ("opinion" | "mechanical" | "unknown"),
``tag_market`` also returns a finer ``fine_label`` and a short, stable
``reason_code`` — both DERIVED from the SAME fired regex signals (no new model,
no network):

  fine_label             meaning                                  reason_code examples
  ---------------------  ---------------------------------------  -----------------------------
  opinion_forecastable   crowd / geopolitics / policy outcome     elections_kw, party_control,
                         WITH a pollable base rate -> forecast    candidate_kw, approval_poll
  opinion_unforecastable opinion-ish but no base rate / pure      awards_virality, attention_metric,
                         speculation / very-low confidence        popularity, low_conf
  mechanical_skip        sports / price / weather / counts /      price_threshold, magnitude,
                         court ruling / launch — objective        crypto_asset, equity_index,
                         non-crowd process                        sports_kw, scoreline, match_date,
                                                                  weather, court_ruling, launch,
                                                                  tweet_count
  data_release_skip      scheduled data / announcement / ship     central_bank, data_release,
                         event: CPI/FOMC/Fed/jobs report,         release_date, product_version
                         "released by <date>", product version
  ambiguous_review       no signal, or an exact opinion/mech tie  no_signal, tie

LEGACY-COMPAT MAPPING — ``.label`` is DERIVED from ``fine_label`` so every
existing caller (predict_today.find_candidates, scanner, sameday, backtest,
scoreboard and their ``label != "opinion"`` / ``== "mechanical"`` gates) keeps
working with NO change:

  opinion_forecastable                                       -> "opinion"
  ambiguous_review                                           -> "unknown"
  mechanical_skip | data_release_skip | opinion_unforecastable -> "mechanical"

The new skip-only fine_labels (data_release_skip, opinion_unforecastable)
collapse onto the legacy "mechanical" bucket, so the existing "drop mechanical"
filters skip them automatically.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict

# ── liquidity floor defaults (USD) ───────────────────────────────────────────
DEFAULT_MIN_VOLUME = 5_000.0
DEFAULT_MIN_LIQUIDITY = 1_000.0

# ── signal patterns: (compiled_regex, weight, label, reason_code) ────────────
# Weights are small ints; the winning label is the higher summed weight. The
# 4th element is a short STABLE reason_code that also drives the P4 fine_label
# (see _DATA_RELEASE_CODES / _OPINION_UNFORECASTABLE_CODES below).
_RAW_SIGNALS: list[tuple[str, int, str, str]] = [
    # ---- MECHANICAL ----
    # price / numeric thresholds (require $, %, or magnitude suffix so bare years
    # like "2026" do NOT match)
    (r"\$\s?\d", 3, "mechanical", "price_threshold"),
    (r"\b\d+(?:\.\d+)?\s?(?:k|m|bn?|million|billion|trillion)\b", 3, "mechanical", "magnitude"),
    (r"\b(?:above|below|over|under|reach|exceed|hit|surpass|cross|close (?:above|below)|"
     r"greater than|less than|at least|more than)\b.{0,20}(?:\$|\d|%)", 3, "mechanical", "price_threshold"),
    (r"\b(?:price|all-?time high|market ?cap|trading at)\b", 2, "mechanical", "price_threshold"),
    # crypto / stocks / indices
    (r"\b(?:btc|eth|sol|xrp|doge|bitcoin|ethereum|solana|crypto|altcoin)\b", 2, "mechanical", "crypto_asset"),
    (r"\b(?:s&p|nasdaq|dow jones|stock|share price|\$[A-Z]{1,5}\b)", 2, "mechanical", "equity_index"),
    # sports
    (r"\b(?:nfl|nba|mlb|nhl|premier league|la liga|serie a|bundesliga|super bowl|"
     r"world cup|champions league|stanley cup|world series|playoff|championship|"
     r"win the (?:game|match|title|cup|series|final)|defeat|vs\.?|ucl|ufc|"
     r"grand prix|formula ?1|\bf1\b|olympic|gold medal|relegat)\b", 3, "mechanical", "sports_kw"),
    # sports — scorelines / dated match results / in-game props (national-team football
    # etc. that the league-name list above misses). "Exact Score: A x - y B?",
    # "Will <team> win on 2026-06-13?", "Both Teams to Score", "to score first".
    (r"\bexact score\b", 4, "mechanical", "scoreline"),
    (r"\bwin on \d{4}-\d{2}-\d{2}\b", 3, "mechanical", "match_date"),
    (r"\b(?:both teams to score|neither team to score|to score first|"
     r"clean sheet|first half|halftime|full[- ]?time)\b", 3, "mechanical", "scoreline"),
    # weather / natural
    (r"\b(?:temperature|hurricane|earthquake|rainfall|snowfall|degrees|celsius|"
     r"fahrenheit|magnitude|tornado|wildfire)\b", 3, "mechanical", "weather"),
    # central bank / rates
    (r"\b(?:fed|federal reserve|fomc|interest rate|rate (?:cut|hike|decision)|"
     r"basis points|\bbps\b|\becb\b|central bank|jerome powell|rate by)\b", 3, "mechanical", "central_bank"),
    # economic data releases
    (r"\b(?:cpi|inflation rate|\bgdp\b|unemployment|jobs report|nonfarm|payrolls|"
     r"\bpce\b|initial claims|retail sales|recession)\b", 3, "mechanical", "data_release"),
    # court / regulatory rulings (official process, not crowd-driven)
    (r"\b(?:supreme court|court (?:rule|ruling)|sec (?:approve|reject|sue)|verdict|"
     r"convicted|indicted|sentenced|found guilty|appeals court)\b", 2, "mechanical", "court_ruling"),
    # launches / scientific
    (r"\b(?:spacex|nasa launch|rocket launch|satellite|starship)\b", 2, "mechanical", "launch"),
    # counts / numeric-quantity thresholds the crowd doesn't "drive" (tweet/post/video counts):
    # "post 65-89 tweets", "post <40 tweets", "40-64 tweets". These arrived as 'unknown' and the
    # swarm hallucinated a bucket probability — tag mechanical so they're skipped/divergence-gated.
    (r"\b\d+\s*(?:-|–|to)\s*\d+\s+(?:tweets?|posts?|times|videos?|goals?|points?|hours?)\b", 3, "mechanical", "tweet_count"),
    (r"\b(?:post|tweet|publish|send)\w*\s+(?:<|>|≤|≥|\d|fewer|less|more|at least|at most|under|over|between|exactly)\b", 3, "mechanical", "tweet_count"),
    (r"\b(?:fewer|less|more|greater|at least|at most|no more|under|over|exactly)\s+(?:than\s+)?\d+\s+(?:tweets?|posts?|times|videos?)\b", 3, "mechanical", "tweet_count"),
    # product / software released-by-a-date — an objective ship event, not crowd sentiment:
    # "GPT-5.6 released by June 15", "launched on …", plus versioned product names.
    (r"\b(?:released?|launch(?:ed|es)?|ship(?:ped|s)?|unveiled?|drop(?:ped|s)?)\s+(?:by|before|on)\b", 3, "mechanical", "release_date"),
    (r"\b(?:gpt|llama|claude|gemini|grok|chatgpt|ios|android|windows)[\s-]?\d+(?:\.\d+)?\b", 2, "mechanical", "product_version"),

    # ---- OPINION ----
    # elections / political contests (outcome driven by voter/crowd behavior) — weight 4
    # so a numeric threshold on a crowd-driven quantity doesn't flip it to mechanical.
    (r"\b(?:election|elected|re-?elect|win the (?:presidency|primary|nomination|"
     r"election|seat|race|senate|house)|win control|win a majority|take control|"
     r"control of (?:the )?(?:senate|house|congress)|nominee|nomination|presidential|"
     r"gubernatorial|senate race|house race|primary|caucus|electoral|ballot|"
     r"win .{0,15}(?:state|district|county|senate|house|majority)|"
     r"become (?:president|prime minister|chancellor|nominee))\b", 4, "opinion", "elections_kw"),
    (r"\b(?:democrats?|republicans?|gop|labour|tory|maga)\b.{0,30}\b(?:win|control|"
     r"majority|flip|sweep)\b", 2, "opinion", "party_control"),
    # 'candidate' only counts as a POLITICAL opinion signal in political context —
    # otherwise a 'vaccine candidate' / 'candidate gene' market would be mislabeled
    # opinion. Real election markets independently fire elections_kw above. (audit #25)
    (r"\bcandidate\b.{0,40}\b(?:elect|primary|nomin|party|senate|house|governor|"
     r"president|congress|republican|democrat|gop|ballot|caucus|runoff)\b|"
     r"\b(?:elect|primary|nomin|party|republican|democrat|gop|ballot|caucus)\b.{0,40}\bcandidate\b",
     1, "opinion", "candidate_kw"),
    # approval / polling / sentiment — weight 4 (opinion subject dominates a threshold)
    (r"\b(?:approval rating|disapproval|favorability|poll(?:s|ing)?|popular vote|"
     r"net approval|approval of)\b", 4, "opinion", "approval_poll"),
    # approval-RATING markets phrased as a bare 'approval' + a threshold/percentage
    # ("X's approval be above 50%", "approval exceed 45%", "more than 60% approve of X").
    # Threshold-scoped so a MECHANICAL 'FDA approval'/'drug approval' (no %) is NOT
    # caught here. Weight 4 so it dominates the +3 numeric-threshold signal. (audit #2)
    (r"\bapproval\b.{0,25}(?:\d+\s*%|above|below|exceed|over\b|under\b|higher|lower|reach|hit|stay)",
     4, "opinion", "approval_poll"),
    (r"\bapprove of\b|\d+\s*%\s+(?:approve|approval)", 4, "opinion", "approval_poll"),
    # cultural awards / attention / virality — opinion but NO pollable base rate
    (r"\b(?:oscars?|grammys?|emmys?|golden globe|nobel|person of the year|"
     r"best (?:picture|actor|actress|director)|man of the (?:match|year)|"
     r"\baward\b|go viral|trending|most (?:popular|streamed|watched|talked|"
     r"discussed|searched|admired)|number one|#1\b|top of the chart|tiktok)\b", 2, "opinion", "awards_virality"),
    # attention metrics (audience growth = crowd attention)
    (r"\b(?:followers|subscribers|streams|most liked|going viral)\b", 3, "opinion", "attention_metric"),
    # public-opinion / popularity framing
    (r"\b(?:public opinion|sentiment|most admired|popularity)\b", 2, "opinion", "popularity"),
]

_SIGNALS = [(re.compile(p, re.IGNORECASE), w, label, code) for p, w, label, code in _RAW_SIGNALS]

# ── P4 fine_label derivation ─────────────────────────────────────────────────
# reason_codes whose winning side is mechanical AND that denote a SCHEDULED
# data/announcement/ship event -> fine_label "data_release_skip" (everything
# else mechanical -> "mechanical_skip").
_DATA_RELEASE_CODES = frozenset({"central_bank", "data_release", "release_date", "product_version"})
# opinion reason_codes with NO pollable base rate (pure attention/speculation)
# -> fine_label "opinion_unforecastable" (other opinion codes -> forecastable).
_OPINION_UNFORECASTABLE_CODES = frozenset({"awards_virality", "attention_metric", "popularity"})
# an opinion winner this thin is treated as unforecastable (reason_code 'low_conf').
_LOW_CONFIDENCE_FLOOR = 0.10

# fine_label -> legacy 3-way label (the backward-compat contract; see module docstring).
_LABEL_FROM_FINE = {
    "opinion_forecastable":   "opinion",
    "opinion_unforecastable": "mechanical",
    "mechanical_skip":        "mechanical",
    "data_release_skip":      "mechanical",
    "ambiguous_review":       "unknown",
}

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
    label: str                       # "opinion" | "mechanical" | "unknown" (DERIVED from fine_label)
    confidence: float                # 0..1, margin / total weight
    reason: str
    opinion_score: int = 0
    mechanical_score: int = 0
    signals: list[str] = field(default_factory=list)   # human-readable fired signals
    method: str = "rules"            # "rules" | "llm" | "rules+llm"
    ambiguous: bool = False
    # ── P4 richer labels (label above is DERIVED from fine_label) ──────────────
    fine_label: str = "ambiguous_review"   # opinion_forecastable | opinion_unforecastable
                                           # | mechanical_skip | data_release_skip | ambiguous_review
    reason_code: str = "no_signal"         # short stable code, e.g. elections_kw / scoreline /
                                           # price_threshold / release_date / no_signal / tie / low_conf

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
def _score(text: str) -> tuple[int, int, list[tuple[str, int, str, str]]]:
    """Return (opinion_score, mechanical_score, fired_records) where each record
    is (label, weight, reason_code, matched_text)."""
    op = me = 0
    fired: list[tuple[str, int, str, str]] = []
    for rx, w, label, code in _SIGNALS:
        m = rx.search(text)
        if m:
            if label == "opinion":
                op += w
            else:
                me += w
            fired.append((label, w, code, m.group(0).strip()[:40]))
    return op, me, fired


def _dominant(records: list[tuple[str, int, str, str]], side: str):
    """Highest-weight fired signal for one side ('opinion'|'mechanical'); a
    data-release code wins weight ties (it explains WHY we skip more specifically
    than a bare numeric threshold). Returns the record or None."""
    same = [r for r in records if r[0] == side]
    if not same:
        return None
    return max(same, key=lambda r: (r[1], r[2] in _DATA_RELEASE_CODES))


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
    conf = round(margin / total, 3) if (total and op != me) else 0.0

    # ── fine_label + reason_code (then DERIVE the legacy 3-way label) ─────────
    if total == 0:
        fine_label, reason_code = "ambiguous_review", "no_signal"
        reason = "no rule signals fired"
    elif op == me:
        fine_label, reason_code = "ambiguous_review", "tie"
        reason = f"tie ({op}={me})"
    elif op > me:
        dom = _dominant(fired, "opinion")
        reason_code = dom[2] if dom else "low_conf"
        if reason_code in _OPINION_UNFORECASTABLE_CODES:
            fine_label = "opinion_unforecastable"
        elif conf < _LOW_CONFIDENCE_FLOOR:
            # opinion wins, but the margin is too thin to trust as a base rate.
            fine_label, reason_code = "opinion_unforecastable", "low_conf"
        else:
            fine_label = "opinion_forecastable"
        reason = f"opinion signals outweigh mechanical ({op} vs {me})"
    else:  # me > op
        dom = _dominant(fired, "mechanical")
        reason_code = dom[2] if dom else "price_threshold"
        fine_label = "data_release_skip" if reason_code in _DATA_RELEASE_CODES else "mechanical_skip"
        reason = f"mechanical signals outweigh opinion ({me} vs {op})"

    label = _LABEL_FROM_FINE[fine_label]
    signals = [f"{lbl}+{w}: '{txt}'" for lbl, w, _code, txt in fired]

    result = Classification(
        label=label, confidence=conf, reason=reason,
        opinion_score=op, mechanical_score=me, signals=signals,
        method="rules", ambiguous=ambiguous,
        fine_label=fine_label, reason_code=reason_code,
    )

    if use_llm and ambiguous:
        llm_label = _llm_tiebreak(question, description, model=llm_model)
        if llm_label in ("opinion", "mechanical"):
            # keep fine_label / .label consistent with the LLM verdict
            result.fine_label = "opinion_forecastable" if llm_label == "opinion" else "mechanical_skip"
            result.reason_code = "llm"
            result.label = _LABEL_FROM_FINE[result.fine_label]
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
    _saved_mf = os.environ.get("MODEL_FAST")   # restore after — don't leak the override
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
    finally:
        # scope the MODEL_FAST override to THIS call — never permanently mutate the
        # process env (which would silently re-point every later LLM call). (audit #26)
        if model:
            if _saved_mf is None:
                os.environ.pop("MODEL_FAST", None)
            else:
                os.environ["MODEL_FAST"] = _saved_mf


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
