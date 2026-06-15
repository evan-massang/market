"""
P2 — Market SCANNER: window selection, event grouping, and transparent ranking.

This is the candidate-sourcing brain that sits in front of the forecasting swarm.
Today the live scanner is `predict_today.find_candidates` (single same-day window,
hard filters, opinion-first sort). This module GENERALISES that into:

  * three resolution WINDOWS (same-day / near-term / weekly) plus 'all',
  * EVENT GROUPING so the bot can see every leg of a multi-leg / mutually-exclusive
    event together (feeds the P3 group-coherence guards),
  * per-candidate QUALITY signals (theme, exit-risk, staleness), and
  * a TRANSPARENT composite RANK score with per-candidate sub-scores and a
    one-line 'why ranked' string (auditable — every number is explainable).

Design principles (mirrors gamma.py / classifier.py):
  * Pure where possible. Every Gamma network call is isolated behind a tiny
    module-level wrapper (`_gamma_within`, `_gamma_active`) so unit tests
    monkeypatch ONE name and make ZERO HTTP requests.
  * No LLM, no DB writes, no real-money paths. Read-only / paper.
  * Defensive: never raises on a single malformed field; missing data degrades
    to a neutral score rather than an exception.

NOTE: this module is intentionally NOT wired into predict_today yet (a later
agent does that to avoid an edit conflict). It self-tests on synthetic markets.

    python -m harness.scanner          # prints API + synthetic self-test result
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from harness import gamma, classifier

# theme_of is reused from the scoreboard (single source of truth for the theme
# keyword map). Guarded so scanner stays importable even if scoreboard breaks.
try:
    from harness.scoreboard import theme_of as _theme_of_impl
except Exception:  # pragma: no cover - defensive fallback
    _theme_of_impl = None

# Fallback theme map — kept identical to scoreboard._THEMES so behaviour matches
# even on the (defensive) import-failure path.
_FALLBACK_THEMES = [
    ("elections", ("election", "nomination", "nominee", "primary", "caucus", "senate",
                   "president", "presidential", "governor", "gubernatorial", "vote",
                   "democrat", "republican", "gop", "ballot", "reelection", "mayor")),
    ("approval",  ("approval", "rating", "poll", "favorability")),
    ("geopolitics", ("war", "ceasefire", "invasion", "sanctions", "treaty", "nuclear",
                     "missile", "troops", "ukraine", "russia", "israel", "gaza", "china",
                     "taiwan", "iran", "coup", "summit")),
    ("culture", ("oscar", "grammy", "emmy", "award", "viral", "person of the year",
                 "box office", "movie", "song", "chart", "tiktok", "celebrity", "number one")),
]


def theme_of(question: str) -> str:
    """Coarse theme of a market question (elections/approval/geopolitics/culture/other).

    Reuses scoreboard.theme_of (so theme P&L lookups line up) with a local fallback.
    """
    if _theme_of_impl is not None:
        try:
            return _theme_of_impl(question)
        except Exception:
            pass
    q = (question or "").lower()
    for name, kws in _FALLBACK_THEMES:
        if any(k in q for k in kws):
            return name
    return "other"


# ── resolution windows ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Window:
    """A time-to-resolution band, in hours from now (inclusive of both bounds)."""
    name: str
    lo_hours: float
    hi_hours: float


# WINDOWS (the const/enum the task asks for). Bounds overlap at the edges by
# design (24h is the same-day/near-term seam, 72h the near-term/weekly seam).
WINDOWS: dict[str, Window] = {
    "same_day":  Window("same_day", 0.5, 24.0),     # 0.5h – 24h  (resolves TODAY)
    "near_term": Window("near_term", 24.0, 72.0),   # 24h – 72h   (1–3 days out)
    "weekly":    Window("weekly", 72.0, 168.0),     # 3d  – 7d
}
ALL_WINDOW = Window("all", 0.5, 168.0)              # full 0.5h – 7d sweep

# Ergonomic string constants.
SAME_DAY = "same_day"
NEAR_TERM = "near_term"
WEEKLY = "weekly"
ALL = "all"

MAX_FETCH = 500  # hard cap on rows pulled from Gamma per scan (politeness + cost)

# ── ranking weights (sum to 1.0) and scales — ALL tunable, ALL documented ──────
W = {
    "forecastability": 0.22,   # classifier says this is the kind of market we can forecast
    "liquidity":       0.16,   # can we get filled / exit
    "volume":          0.10,   # is the market real / actively traded
    "exit":            0.16,   # inverse of exit_risk (easy to get out = good)
    "freshness":       0.08,   # is the data live (vs a stale/never-traded row)
    "disagreement":    0.10,   # PROXY for model-vs-market edge (price extremity, pre-forecast)
    "time_fit":        0.08,   # resolves in a sweet spot (time to act, fast to learn)
    "theme_profit":    0.10,   # have we historically made money on this theme (P7 fills this)
}
assert abs(sum(W.values()) - 1.0) < 1e-9, "rank weights must sum to 1.0"

LIQ_SCALE = 20_000.0     # half-saturation point for liquidity quality (v/(v+scale))
VOL_SCALE = 100_000.0    # half-saturation point for volume quality
EXIT_LIQ_SCALE = 20_000.0
EXIT_VOL_SCALE = 100_000.0
SPREAD_CAP = 0.08        # a 0.08 (8c) bid/ask spread => maximal exit risk from spread
STALE_PENALTY = 0.10     # stale candidates keep their place in output but score ~0 (observe-only)
LIQ_EPS = 1.0            # liquidity <= this (USD) is treated as effectively zero
MIN_AFFIX = 12           # shared question prefix/suffix length that flags a candidate family
NEUTRAL_THEME = 0.5      # default theme-profit score when theme_pnl has no entry


# ── tiny numeric helpers ───────────────────────────────────────────────────────
def _f(value, default=None):
    """Coerce to float; return `default` on missing/uncoercible. Never raises."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quality(value: float, scale: float) -> float:
    """Saturating 0..1 quality curve: value/(value+scale).

    0 at value=0, 0.5 at value=scale, asymptotes to 1. Used for liquidity/volume so
    a market at the liquidity floor scores low and a deep market scores high without
    a hard cliff.
    """
    if value is None or value <= 0:
        return 0.0
    return round(value / (value + scale), 4)


def _parse_dt(s):
    if not s:
        return None
    try:
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _hours_left(candidate, now=None):
    """Hours from now until the candidate's end_date, or None if unparseable.

    Same semantics as loop._days_until * 24 (kept independent to avoid importing
    the heavy loop module into the scanner).
    """
    dt = _parse_dt(candidate.get("end_date"))
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 3600.0


# ── gamma call isolation (monkeypatch THESE in tests — no live HTTP) ───────────
def _gamma_within(hours: float, limit: int) -> list[dict]:
    """ONLY path for 'markets ending within N hours'. Wraps gamma.fetch_markets_ending_within."""
    return gamma.fetch_markets_ending_within(hours, limit=limit)


def _gamma_active(limit: int) -> list[dict]:
    """Alternate fetch path (active by volume), filtered to a window client-side.
    Wraps gamma.fetch_active_markets. Used by scan(..., source='active')."""
    return gamma.fetch_active_markets(limit=limit)


def _resolve_window(mode) -> Window:
    if mode is None:
        return ALL_WINDOW
    if isinstance(mode, Window):
        return mode
    key = str(mode).strip().lower()
    if key in ("all", "*", ""):
        return ALL_WINDOW
    if key in WINDOWS:
        return WINDOWS[key]
    raise ValueError(f"unknown scan mode {mode!r}; use one of {list(WINDOWS) + ['all']}")


# ── 1) SCAN ─────────────────────────────────────────────────────────────────--
def scan(mode="all", limit: int = 200, *, source: str = "within") -> list[dict]:
    """Return normalized candidate markets whose time-to-resolution falls in `mode`'s window.

    mode   : 'same_day' | 'near_term' | 'weekly' | 'all' (or a Window instance).
    limit  : max candidates returned (after window filtering).
    source : 'within' (default; Gamma end-date server filter) or 'active' (by volume,
             filtered client-side) — both isolated behind injectable helpers.

    Each returned market gains two annotations: '_hours_left' and '_window'.
    Markets with an unparseable end_date are dropped (no window to place them in).
    """
    win = _resolve_window(mode)
    # Over-fetch: near-term/weekly windows discard everything ending sooner than lo,
    # so pull a multiple of `limit` to still fill the window after filtering.
    fetch_limit = min(MAX_FETCH, max(int(limit), 50) * 3)
    if source == "active":
        raw = _gamma_active(fetch_limit)
    else:
        raw = _gamma_within(win.hi_hours, fetch_limit)

    out: list[dict] = []
    for m in raw or []:
        hl = _hours_left(m)
        if hl is None:
            continue
        if win.lo_hours <= hl <= win.hi_hours:
            m["_hours_left"] = hl
            m["_window"] = win.name
            out.append(m)
    return out[: int(limit)]


# ── 2) EVENT GROUPING ──────────────────────────────────────────────────────────
def _is_yes_no(m: dict) -> bool:
    outs = [str(o).strip().lower() for o in (m.get("outcomes") or [])]
    return len(outs) == 2 and set(outs) == {"yes", "no"}


def _common_prefix_len(strs: list[str]) -> int:
    if not strs:
        return 0
    s0 = strs[0]
    for i, ch in enumerate(s0):
        for s in strs[1:]:
            if i >= len(s) or s[i] != ch:
                return i
    return len(s0)


def _common_suffix_len(strs: list[str]) -> int:
    return _common_prefix_len([s[::-1] for s in strs])


def _looks_like_family(legs: list[dict]) -> bool:
    """Heuristic: do these legs share a question template (candidate / scoreline family)?

    e.g. 'Will <X> win the 2028 nomination?' across many X share a long common SUFFIX;
    'Team A vs Team B - Exact Score: 1-0 / 2-1 / ...' share a long common PREFIX. A
    shared affix >= MIN_AFFIX chars (after lowercasing) implies one underlying event
    split into competing legs => mutually exclusive.
    """
    qs = [(l.get("question") or "").strip().lower() for l in legs if l.get("question")]
    if len(qs) < 2:
        return False
    return _common_prefix_len(qs) >= MIN_AFFIX or _common_suffix_len(qs) >= MIN_AFFIX


def _make_event(key: str, legs: list[dict]) -> dict:
    n = len(legs)
    slug0 = legs[0].get("event_slug")
    has_real_slug = bool(slug0) and all(l.get("event_slug") == slug0 for l in legs)
    yes_no = sum(1 for l in legs if _is_yes_no(l))
    exact_score = any("exact score" in (l.get("question") or "").lower() for l in legs)
    family = _looks_like_family(legs)

    # Mutually-exclusive heuristic (one winner among the legs):
    #   - several Yes/No legs sharing a real event_slug (the classic "who wins" split), OR
    #   - an exact-score family, OR a candidate/scoreline template family, OR
    #   - a single categorical market with >2 outcomes (its outcomes are ME by construction).
    mutually_exclusive = bool((has_real_slug and yes_no > 1) or exact_score or family)
    if n == 1 and len(legs[0].get("outcomes") or []) > 2:
        mutually_exclusive = True

    return {
        "key": key,
        "event_slug": slug0,
        "legs": legs,
        "n_legs": n,
        "mutually_exclusive": mutually_exclusive,
        "theme": theme_of(legs[0].get("question") or ""),
    }


def group_events(markets: list[dict]) -> list[dict]:
    """Group candidate markets into events so all legs of one event are seen together.

    Groups by event_slug (falling back to market_id for slug-less singletons),
    preserving first-seen order. Each Event = {key, event_slug, legs, n_legs,
    mutually_exclusive, theme}. The P3 group-coherence guards consume `legs` +
    `mutually_exclusive`.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in markets or []:
        key = m.get("event_slug") or m.get("market_id") or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    return [_make_event(k, groups[k]) for k in order]


# ── 3) QUALITY SIGNALS ─────────────────────────────────────────────────────────
def _spread(candidate: dict):
    """Best-effort bid/ask spread (0..1 price units) from the raw Gamma row, or None.

    Tries the 'spread' field first, then bestAsk-bestBid. None => no microstructure
    signal available (caller falls back to a liquidity/volume proxy).
    """
    raw = candidate.get("raw") or {}
    sv = _f(raw.get("spread"))
    if sv is not None and sv >= 0:
        return sv
    bid = _f(raw.get("bestBid"))
    ask = _f(raw.get("bestAsk"))
    if bid is not None and ask is not None and ask >= bid:
        return ask - bid
    return None


def exit_risk(candidate: dict) -> float:
    """0..1 difficulty-of-exit estimate (higher = harder to exit a paper position).

    PROXY DEFINITION:
      * If a spread signal exists (raw 'spread' or bestAsk-bestBid): exit risk is
        dominated by the spread (spread/SPREAD_CAP, capped at 1) blended 60/40 with
        a depth proxy.
      * Otherwise (the common Gamma case, no quoted book): a pure DEPTH PROXY —
        low liquidity (and secondarily low volume) => high exit risk. We invert the
        saturating liquidity/volume quality curves, weighting liquidity 70 / volume 30,
        because liquidity is the more direct 'can I unwind' signal.
    """
    liq = _f(candidate.get("liquidity"), 0.0) or 0.0
    vol = _f(candidate.get("volume"), 0.0) or 0.0
    liq_risk = 1.0 - _quality(liq, EXIT_LIQ_SCALE)
    vol_risk = 1.0 - _quality(vol, EXIT_VOL_SCALE)
    depth_risk = 0.7 * liq_risk + 0.3 * vol_risk

    spread = _spread(candidate)
    if spread is not None:
        spread_risk = min(1.0, spread / SPREAD_CAP)
        return round(0.6 * spread_risk + 0.4 * depth_risk, 4)
    return round(depth_risk, 4)


def _has_freshness(candidate: dict) -> bool:
    """Is there any signal the market is live (traded recently / has a book)?"""
    if (_f(candidate.get("volume"), 0.0) or 0.0) > 0:
        return True
    raw = candidate.get("raw") or {}
    for k in ("updatedAt", "lastTradeTime", "lastTradePrice",
              "acceptingOrdersTimestamp", "volume24hr", "volumeNum"):
        v = raw.get(k)
        if v not in (None, "", 0, "0"):
            return True
    return False


def is_stale(candidate: dict):
    """(stale: bool, reason: str). Stale/suspicious markets are OBSERVE-ONLY (don't bet).

    Flags, in priority order:
      1. liquidity ~= 0           — there is nothing to trade against / exit into.
      2. missing/unparseable end_date — no resolution time; can't reason about it.
      3. end_date already passed  — should have resolved; the row is stale.
      4. degenerate price + no freshness signal — price pinned at 0/1 (or no price)
         AND no volume/recent-trade signal => never-traded or already-settled row.
    """
    liq = _f(candidate.get("liquidity"), 0.0) or 0.0
    prices = candidate.get("outcome_prices") or []
    price = gamma.yes_price(candidate)
    fresh = _has_freshness(candidate)
    hl = _hours_left(candidate)

    if liq <= LIQ_EPS:
        return True, "liquidity ~= 0 (cannot exit a paper position)"
    if hl is None:
        return True, "missing/unparseable end_date (no resolution time)"
    if hl < 0:
        return True, "end_date already passed (market should have resolved)"

    degenerate = (
        (not prices)
        or (price is not None and (price <= 0.0 or price >= 1.0))
        or (price is None and len(prices) >= 2 and (max(prices) >= 0.999 or min(prices) <= 0.001))
    )
    if degenerate and not fresh:
        return True, "degenerate price with no freshness signal (never-traded / settled)"
    return False, "ok"


# ── 4) RANK ─────────────────────────────────────────────────────────────────--
def _forecastability(label: str, conf: float) -> float:
    """0..1: how much this is the KIND of market the swarm can forecast (classifier-driven)."""
    c = min(1.0, max(0.0, conf or 0.0))
    if label == "opinion":
        return round(0.60 + 0.40 * c, 4)     # crowd-driven: our wheelhouse
    if label == "mechanical":
        return round(0.10 + 0.10 * c, 4)      # objective process: swarm has little edge
    return 0.50                               # unknown: genuine news/geo markets — keep, but uncertain


def _time_fit(hl) -> float:
    """0..1 fit on time-to-resolution: a tent that peaks in the 6h–48h sweet spot.

    Too soon (<6h) = little time to gather data / act. Too far (>48h) = capital tied
    up and more uncertainty; decays toward 0.35 at the 7-day edge.
    """
    if hl is None or hl <= 0:
        return 0.2
    if hl < 6.0:
        return round(0.5 + 0.5 * (hl / 6.0), 4)        # 0.50 -> 1.00 ramp up to 6h
    if hl <= 48.0:
        return 1.0                                      # sweet spot
    if hl <= 168.0:
        return round(max(0.35, 1.0 - 0.65 * (hl - 48.0) / (168.0 - 48.0)), 4)
    return 0.35


def _theme_profit_score(theme: str, theme_pnl) -> float:
    """0..1 historical-profitability score for a theme. NEUTRAL when absent (P7 fills it).

    Accepts either a value already in 0..1 (used directly) or a raw P&L number
    (logistic-squashed to 0..1 around 0). Missing/None => NEUTRAL_THEME.
    """
    if not theme_pnl:
        return NEUTRAL_THEME
    v = theme_pnl.get(theme)
    if v is None:
        return NEUTRAL_THEME
    v = _f(v)
    if v is None:
        return NEUTRAL_THEME
    if 0.0 <= v <= 1.0:
        return round(v, 4)
    return round(1.0 / (1.0 + math.exp(-v)), 4)


def _annotate(cand: dict, theme_pnl) -> dict:
    """Score one candidate. Returns a SHALLOW COPY annotated with sub-scores + why (pure)."""
    c = dict(cand)  # copy so we never mutate the caller's dict ('raw' is shared read-only)
    q = c.get("question") or ""
    cls = classifier.tag_market(c)
    label, conf = cls.label, cls.confidence
    price = gamma.yes_price(c)
    liq = _f(c.get("liquidity"), 0.0) or 0.0
    vol = _f(c.get("volume"), 0.0) or 0.0
    hl = _hours_left(c)
    theme = theme_of(q)
    er = exit_risk(c)
    stale, stale_reason = is_stale(c)

    sub = {
        "forecastability": _forecastability(label, conf),
        "liquidity": _quality(liq, LIQ_SCALE),
        "volume": _quality(vol, VOL_SCALE),
        "exit": round(1.0 - er, 4),
        "freshness": 1.0 if _has_freshness(c) else 0.3,
        # disagreement PROXY: pre-forecast we have no model number, so price extremity
        # (2*|price-0.5|) stands in for 'how much room is there to disagree'. P3 replaces
        # this with the real |model_p - market_price| once the swarm has run.
        "disagreement": 0.0 if price is None else round(min(1.0, 2.0 * abs(price - 0.5)), 4),
        "time_fit": _time_fit(hl),
        "theme_profit": _theme_profit_score(theme, theme_pnl),
    }
    base = sum(W[k] * sub[k] for k in W)
    observe_only = bool(stale)
    score = base * (STALE_PENALTY if stale else 1.0)

    contrib = sorted(((W[k] * sub[k], k) for k in W), reverse=True)
    drivers = ", ".join(f"{k}={sub[k]:.2f}" for _, k in contrib[:3])
    flags = []
    if observe_only:
        flags.append(f"OBSERVE-ONLY(stale: {stale_reason})")
    if label == "mechanical":
        flags.append("mechanical")
    price_s = "n/a" if price is None else f"{price:.2f}"
    hl_s = "n/a" if hl is None else f"{hl:.1f}h"
    why = (f"{theme}/{label} rank={score:.3f} | top: {drivers} | "
           f"liq=${liq:,.0f} vol=${vol:,.0f} exit_risk={er:.2f} price={price_s} ttl={hl_s}")
    if flags:
        why += " | " + "; ".join(flags)

    c["_theme"] = theme
    c["_label"] = label
    c["_conf"] = conf
    c["_price"] = price
    c["_hours_left"] = hl
    c["_exit_risk"] = er
    c["_stale"] = stale
    c["_stale_reason"] = stale_reason
    c["_observe_only"] = observe_only
    c["_subscores"] = sub
    c["_rank_score"] = round(score, 4)
    c["_why"] = why
    return c


def rank_candidates(candidates: list[dict], theme_pnl: dict | None = None) -> list[dict]:
    """Rank candidates by a transparent composite score (highest first).

    theme_pnl: optional {theme -> score|pnl} map (P7 fills it; defaults NEUTRAL).

    Returns NEW annotated copies (inputs are not mutated). Each carries:
      _rank_score, _subscores (the 8 weighted components), _theme, _label, _conf,
      _price, _hours_left, _exit_risk, _stale/_stale_reason, _observe_only, _why.
    Stale candidates are kept (so callers can observe them) but scored ~0 so they
    sink to the bottom and are flagged OBSERVE-ONLY.
    """
    ranked = [_annotate(c, theme_pnl) for c in (candidates or [])]
    ranked.sort(key=lambda c: (
        -c["_rank_score"],
        -(_f(c.get("liquidity"), 0.0) or 0.0),
        c["_hours_left"] if c["_hours_left"] is not None else 1e9,
    ))
    return ranked


# ── public API surface (for docs / introspection) ─────────────────────────────
__all__ = [
    "Window", "WINDOWS", "ALL_WINDOW", "SAME_DAY", "NEAR_TERM", "WEEKLY", "ALL",
    "scan", "group_events", "theme_of", "exit_risk", "is_stale", "rank_candidates",
]


# ── self-test (synthetic markets, NO network) ─────────────────────────────────-
def _mk(market_id, question, price, hours, *, volume=200_000.0, liquidity=40_000.0,
        event_slug=None, outcomes=None, prices=None, raw=None):
    """Build a NORMALIZED market dict (gamma.normalize_market shape) for testing."""
    end = (datetime.now(timezone.utc).timestamp() + hours * 3600.0)
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    outs = outcomes or ["Yes", "No"]
    prc = prices if prices is not None else [price, round(1.0 - price, 4)]
    return {
        "market_id": market_id,
        "question": question,
        "description": "",
        "outcomes": outs,
        "outcome_prices": prc,
        "volume": volume,
        "liquidity": liquidity,
        "end_date": end_iso,
        "clob_token_ids": ["t1", "t2"],
        "event_slug": event_slug,
        "raw": raw or {},
    }


def _selftest():
    """Exercise every public function on synthetic data. Returns (passed, total)."""
    checks = []

    def chk(name, cond):
        checks.append((name, bool(cond)))

    # --- scan: window filtering (monkeypatch the gamma wrapper -> NO HTTP) ---
    universe = [
        _mk("SD-1", "Will the Senate flip in the election?", 0.50, 6),     # same-day
        _mk("SD-2", "Will the incumbent win re-election?", 0.40, 20),      # same-day
        _mk("NT-1", "Will the nominee win the primary?", 0.55, 48),        # near-term
        _mk("WK-1", "Will the candidate win the race?", 0.45, 120),        # weekly
        _mk("FAR-1", "Will this resolve way out?", 0.50, 400),             # beyond all windows
        _mk("BAD-1", "No end date here", 0.50, 6),                          # unparseable end_date
    ]
    universe[-1]["end_date"] = None  # force unparseable

    global _gamma_within, _gamma_active
    _orig_within, _orig_active = _gamma_within, _gamma_active
    _gamma_within = lambda hours, limit: list(universe)   # noqa: E731
    _gamma_active = lambda limit: list(universe)          # noqa: E731
    try:
        sd = {m["market_id"] for m in scan(SAME_DAY, limit=50)}
        nt = {m["market_id"] for m in scan(NEAR_TERM, limit=50)}
        wk = {m["market_id"] for m in scan(WEEKLY, limit=50)}
        al = {m["market_id"] for m in scan(ALL, limit=50)}
        chk("scan same_day", sd == {"SD-1", "SD-2"})
        chk("scan near_term", nt == {"NT-1"})
        chk("scan weekly", wk == {"WK-1"})
        chk("scan all", al == {"SD-1", "SD-2", "NT-1", "WK-1"})  # FAR-1 + BAD-1 excluded
        chk("scan limit", len(scan(ALL, limit=2)) == 2)
        chk("scan active source", {m["market_id"] for m in scan(SAME_DAY, limit=50, source="active")} == {"SD-1", "SD-2"})
        chk("scan annotates window+ttl", all("_window" in m and "_hours_left" in m for m in scan(SAME_DAY, limit=50)))
    finally:
        _gamma_within, _gamma_active = _orig_within, _orig_active

    # --- group_events ---
    family = [
        _mk("F-1", "Will Alice win the 2028 nomination contest?", 0.3, 48, event_slug="nom-2028"),
        _mk("F-2", "Will Bob win the 2028 nomination contest?", 0.3, 48, event_slug="nom-2028"),
        _mk("F-3", "Will Carol win the 2028 nomination contest?", 0.3, 48, event_slug="nom-2028"),
    ]
    singleton = [_mk("S-1", "Totally standalone question?", 0.5, 48)]  # no slug -> keyed by id
    cat = [_mk("C-1", "Who wins?", 0.3, 48, outcomes=["A", "B", "C"], prices=[0.3, 0.3, 0.4])]

    evs = group_events(family + singleton + cat)
    by_key = {e["key"]: e for e in evs}
    chk("group: family one event", by_key.get("nom-2028") is not None and by_key["nom-2028"]["n_legs"] == 3)
    chk("group: family mutually_exclusive", by_key["nom-2028"]["mutually_exclusive"] is True)
    chk("group: singleton not ME", by_key["S-1"]["n_legs"] == 1 and by_key["S-1"]["mutually_exclusive"] is False)
    chk("group: categorical ME", by_key["C-1"]["mutually_exclusive"] is True)

    # --- theme_of ---
    chk("theme elections", theme_of("Will the Senate flip?") == "elections")
    chk("theme other", theme_of("totally unrelated question") == "other")

    # --- exit_risk ---
    deep = _mk("D", "q?", 0.5, 12, volume=2_000_000.0, liquidity=500_000.0)
    thin = _mk("T", "q?", 0.5, 12, volume=100.0, liquidity=50.0)
    wide = _mk("W", "q?", 0.5, 12, volume=200_000.0, liquidity=40_000.0, raw={"spread": 0.07})
    chk("exit_risk deep < thin", exit_risk(deep) < exit_risk(thin))
    chk("exit_risk thin high", exit_risk(thin) > 0.8)
    chk("exit_risk spread raises", exit_risk(wide) > exit_risk(_mk("W2", "q?", 0.5, 12, raw={"spread": 0.0})))
    chk("exit_risk in [0,1]", all(0.0 <= exit_risk(x) <= 1.0 for x in (deep, thin, wide)))

    # --- is_stale ---
    healthy = _mk("H", "Will the candidate win?", 0.5, 12)
    zero_liq = _mk("Z", "q?", 0.5, 12, liquidity=0.0)
    expired = _mk("E", "q?", 0.5, -3)
    pinned = _mk("P", "q?", 0.0, 12, volume=0.0, prices=[0.0, 1.0])
    chk("stale: healthy ok", is_stale(healthy)[0] is False)
    chk("stale: zero liquidity", is_stale(zero_liq)[0] is True)
    chk("stale: expired end", is_stale(expired)[0] is True)
    chk("stale: pinned+no-fresh", is_stale(pinned)[0] is True)

    # --- rank_candidates ---
    cands = [
        _mk("OPN", "Will the Republicans win control of the Senate?", 0.50, 12,
            volume=500_000.0, liquidity=120_000.0),                       # opinion, deep, fresh
        _mk("MECH", "Will Bitcoin close above $100,000?", 0.50, 12,
            volume=500_000.0, liquidity=120_000.0),                       # mechanical
        _mk("THIN", "Will the governor win re-election?", 0.50, 12,
            volume=300.0, liquidity=0.0),                                 # opinion but STALE (liquidity ~= 0)
    ]
    ranked = rank_candidates(cands)
    order = [c["market_id"] for c in ranked]
    chk("rank: opinion-deep first", order[0] == "OPN")
    chk("rank: stale/thin last", order[-1] == "THIN")
    chk("rank: thin observe-only", next(c for c in ranked if c["market_id"] == "THIN")["_observe_only"] is True)
    chk("rank: opinion > mechanical", next(c for c in ranked if c["market_id"] == "OPN")["_rank_score"]
        > next(c for c in ranked if c["market_id"] == "MECH")["_rank_score"])
    chk("rank: annotations present", all(
        all(k in c for k in ("_rank_score", "_subscores", "_why", "_theme", "_exit_risk"))
        for c in ranked))
    chk("rank: scores in [0,1]", all(0.0 <= c["_rank_score"] <= 1.0 for c in ranked))
    # purity: inputs not mutated
    chk("rank: inputs not mutated", all("_rank_score" not in c for c in cands))
    # theme_pnl shifts score
    base_opn = next(c for c in rank_candidates(cands) if c["market_id"] == "OPN")["_rank_score"]
    boosted = next(c for c in rank_candidates(cands, theme_pnl={"elections": 1.0})
                   if c["market_id"] == "OPN")["_rank_score"]
    suppressed = next(c for c in rank_candidates(cands, theme_pnl={"elections": 0.0})
                      if c["market_id"] == "OPN")["_rank_score"]
    chk("rank: theme_pnl boosts", boosted > base_opn > suppressed)

    passed = sum(1 for _, ok in checks if ok)
    return passed, len(checks), checks


def _main():
    print("harness.scanner — public API")
    print("  WINDOWS:", {k: (w.lo_hours, w.hi_hours) for k, w in WINDOWS.items()},
          "| all:", (ALL_WINDOW.lo_hours, ALL_WINDOW.hi_hours))
    print("  scan(mode='all', limit=200, source='within') -> list[normalized candidate]")
    print("  group_events(markets) -> list[Event{key,event_slug,legs,n_legs,mutually_exclusive,theme}]")
    print("  theme_of(question) -> str")
    print("  exit_risk(candidate) -> float 0..1 (higher = harder to exit)")
    print("  is_stale(candidate) -> (bool, reason)")
    print("  rank_candidates(candidates, theme_pnl=None) -> sorted annotated copies")
    print("  rank weights:", W)
    print()

    passed, total, checks = _selftest()
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\nSELF-TEST: {passed}/{total} checks passed")

    # show a sample ranked candidate's 'why'
    sample = rank_candidates([
        _mk("DEMO", "Will the Democrats win the Senate majority?", 0.42, 10,
            volume=750_000.0, liquidity=150_000.0)])[0]
    print("\nsample ranked candidate:")
    print("  _why:", sample["_why"])
    print("  _subscores:", sample["_subscores"])
    return 0 if passed == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_main())
