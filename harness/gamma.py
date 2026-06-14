"""
P0.5 — Polymarket Gamma market-data fetcher (standalone, READ-ONLY, $0, keyless).

Pulls live market data from Polymarket's public Gamma API and normalizes the
quirky wire format into clean Python dicts the rest of the harness can rely on.

Why this is a standalone module:
  The polymarket-agents repo's clients drag in web3 / py-clob-client / wallet /
  private-key machinery on import. This harness is PAPER / read-only, so we talk
  to the public Gamma REST endpoint directly with httpx and import NOTHING from
  that repo. No API key, no wallet, no signing — just HTTP GET.

Gamma wire quirks handled here (so callers never see them):
  * `outcomes`, `outcomePrices`, `clobTokenIds` arrive as JSON-ENCODED STRINGS,
    e.g. '["Yes", "No"]' and '["0.0195", "0.9805"]' — we json.loads them.
  * numeric fields (`volume`, `liquidity`) arrive as strings — we coerce to float.
  * the STABLE id is `conditionId` (the on-chain condition id), which is robust to
    question-wording drift; the integer `id` can churn. We key on conditionId and
    fall back to `id` only if conditionId is missing.

Design mirrors harness/classifier.py: dataclass-free thin functions, type hints,
defensive parsing that never raises on a single malformed field, clear docstrings.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

# ── endpoint config (public, keyless) ────────────────────────────────────────
GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKETS_URL = f"{GAMMA_BASE}/markets"
EVENTS_URL = f"{GAMMA_BASE}/events"

DEFAULT_TIMEOUT = 30.0
# Polite UA so we look like a well-behaved read-only client, not an anonymous bot.
_HEADERS = {"User-Agent": "polyswarm-harness/0.1 (paper, read-only)"}


# ── tolerant low-level parsers ────────────────────────────────────────────────
def _loads_list(value: Any) -> list:
    """Gamma encodes list-valued fields as JSON STRINGS ('["Yes","No"]').

    Return a real list regardless of whether we got a JSON string, an actual
    list, or something malformed/empty. Never raises.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _to_float(value: Any) -> float | None:
    """Coerce a Gamma numeric (often a string like '66242210.56') to float.

    Returns None when the value is missing or uncoercible, so callers can tell
    'genuinely absent' apart from a real 0.0 price.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_list(value: Any) -> list[float]:
    """Parse a JSON-string list of numbers into a list[float], dropping bad entries."""
    out: list[float] = []
    for item in _loads_list(value):
        f = _to_float(item)
        if f is not None:
            out.append(f)
    return out


def _first_float(market: dict, *keys: str) -> float:
    """First coercible float across candidate keys; 0.0 if none (mirrors classifier)."""
    for k in keys:
        f = _to_float(market.get(k))
        if f is not None:
            return f
    return 0.0


# ── normalization ─────────────────────────────────────────────────────────────
def normalize_market(market: dict) -> dict:
    """Normalize a raw Gamma market dict into the harness's canonical shape.

    Returned keys:
      market_id       STABLE id — conditionId (on-chain) preferred, else id.
      question        the market question text.
      description     resolution criteria text (Gamma 'description').
      outcomes        list[str], e.g. ['Yes', 'No'].
      outcome_prices  list[float], index-aligned with `outcomes`.
      volume          float USD.
      liquidity       float USD.
      end_date        ISO end timestamp (str) or None.
      clob_token_ids  list[str] CLOB token ids, index-aligned with `outcomes`.
      raw             the original unmodified market dict.
    """
    # STABLE id: prefer on-chain conditionId; fall back to the integer id.
    cond = market.get("conditionId")
    market_id = cond if cond else str(market.get("id") or "")

    return {
        "market_id": market_id,
        "question": str(market.get("question") or "").strip(),
        "description": str(market.get("description") or "").strip(),
        "outcomes": [str(o) for o in _loads_list(market.get("outcomes"))],
        "outcome_prices": _float_list(market.get("outcomePrices")),
        "volume": _first_float(market, "volume", "volumeNum", "volumeClob"),
        "liquidity": _first_float(market, "liquidity", "liquidityNum", "liquidityClob"),
        "end_date": market.get("endDate") or market.get("endDateIso") or None,
        "clob_token_ids": [str(t) for t in _loads_list(market.get("clobTokenIds"))],
        "event_slug": ((market.get("events") or [{}])[0].get("slug") if market.get("events")
                       else market.get("slug")),
        "raw": market,
    }


# ── fetchers ──────────────────────────────────────────────────────────────────
def fetch_active_markets(
    limit: int = 50,
    order: str = "volumeNum",
    *,
    ascending: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
) -> list[dict]:
    """Fetch active, open, non-archived markets sorted by `order` (desc by default).

    Optional `end_date_min`/`end_date_max` (ISO datetimes, e.g. '2026-06-14T23:59:59Z')
    use Gamma's SERVER-SIDE end-date filter — far cheaper than fetching everything and
    filtering client-side. Read-only GET; no key, no wallet.
    """
    extra = {}
    if end_date_min:
        extra["end_date_min"] = end_date_min
    if end_date_max:
        extra["end_date_max"] = end_date_max
    return _fetch_paginated(
        {"active": "true", "closed": "false", "archived": "false"},
        limit, order, ascending, timeout, extra)


def fetch_markets_ending_within(hours: float, limit: int = 200, order: str = "volumeNum",
                                timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Markets resolving within the next `hours` — uses Gamma's end_date_min/max filter.
    e.g. fetch_markets_ending_within(24) -> everything resolving by this time tomorrow."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return fetch_active_markets(limit=limit, order=order, timeout=timeout,
                                end_date_min=fmt(now), end_date_max=fmt(now + timedelta(hours=hours)))


def fetch_closed_markets(limit: int = 200, order: str = "volumeNum", *,
                         ascending: bool = False, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Fetch CLOSED (resolved) markets — used for the $0 historical calibration read.
    Read-only, keyless, paginated. Returns normalized market dicts (resolved
    outcome via resolution_outcome())."""
    return _fetch_paginated(
        {"active": "false", "closed": "true", "archived": "true"},
        limit, order, ascending, timeout)


def _fetch_paginated(status: dict, limit: int, order: str, ascending: bool,
                     timeout: float, extra: dict | None = None) -> list[dict]:
    PAGE = 100  # Gamma caps a single response at ~100 rows; paginate via offset.
    out: list[dict] = []
    with httpx.Client(timeout=timeout, headers=_HEADERS) as client:
        offset = 0
        while len(out) < limit:
            page = min(PAGE, limit - len(out))
            params = {**status, **(extra or {}), "limit": page, "offset": offset, "order": order,
                      "ascending": "true" if ascending else "false"}
            resp = client.get(MARKETS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            out.extend(normalize_market(m) for m in data if isinstance(m, dict))
            if len(data) < page:
                break
            offset += len(data)
    return out[:limit]


def fetch_active_events(
    limit: int = 50,
    order: str = "volume",
    *,
    ascending: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Fetch active events (each bundles one or more markets). Secondary to markets.

    Returns the RAW event dicts (events nest markets, so there is no single canonical
    flat shape) — use the nested 'markets' lists with normalize_market as needed.
    """
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": int(limit),
        "order": order,
        "ascending": "true" if ascending else "false",
    }
    with httpx.Client(timeout=timeout, headers=_HEADERS) as client:
        resp = client.get(EVENTS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


# ── derived helpers ───────────────────────────────────────────────────────────
def yes_price(market: dict) -> float | None:
    """Price of the 'Yes' outcome for a NORMALIZED market, or None if absent.

    Index-matches outcomes -> outcome_prices, matching 'yes' case-insensitively.
    Falls back to the first price for a binary market whose outcomes aren't labeled
    Yes/No but only has two sides? No — we stay strict: only an actual 'yes' label
    returns a price, so callers never mistake an unrelated outcome for 'Yes'.
    """
    outcomes = market.get("outcomes") or []
    prices = market.get("outcome_prices") or []
    for i, name in enumerate(outcomes):
        if str(name).strip().lower() == "yes" and i < len(prices):
            return prices[i]
    return None


# ── resolution lookup (for settling paper positions) ──────────────────────────
def fetch_market_by_condition_id(market_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """Fetch ONE market by its conditionId, INCLUDING closed/resolved markets
    (no active/closed filter). Returns a normalized market dict or None. Read-only."""
    if not market_id:
        return None
    params = {"condition_ids": market_id, "limit": 1}
    with httpx.Client(timeout=timeout, headers=_HEADERS) as client:
        resp = client.get(MARKETS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return normalize_market(data[0])
    return None


def resolution_outcome(market: dict) -> float | None:
    """Return 1.0 if the market resolved YES, 0.0 if NO, None if not (cleanly) resolved.

    A market is settled when Gamma marks it closed AND the prices have snapped to
    ~1/0. We require an unambiguous snap so a still-trading 'closed' edge case never
    settles a paper position at the wrong outcome.
    """
    raw = market.get("raw", {}) or {}
    closed = raw.get("closed")
    is_closed = (closed is True) or (str(closed).strip().lower() == "true")
    if not is_closed:
        return None
    yp = yes_price(market)
    if yp is not None:
        if yp >= 0.99:
            return 1.0
        if yp <= 0.01:
            return 0.0
        return None
    # No 'Yes' label: use the highest-priced outcome if it has clearly won.
    outcomes = market.get("outcomes") or []
    prices = market.get("outcome_prices") or []
    if prices:
        win = max(range(len(prices)), key=lambda i: prices[i])
        if prices[win] >= 0.99 and win < len(outcomes):
            name = str(outcomes[win]).strip().lower()
            if name == "yes":
                return 1.0
            if name == "no":
                return 0.0
    return None


# ── self-test / live smoke test ───────────────────────────────────────────────
def _main() -> None:
    """Fetch 5 live markets and print question + yes_price + volume + description."""
    markets = fetch_active_markets(limit=5)

    assert markets, "Gamma returned no active markets"
    for m in markets:
        assert m["question"], f"market {m['market_id']!r} has empty question"
        assert isinstance(m["volume"], float), f"non-numeric volume for {m['market_id']!r}"

    print(f"Fetched {len(markets)} live Polymarket markets (by volume desc):\n")
    for i, m in enumerate(markets, 1):
        yp = yes_price(m)
        yp_str = f"{yp:.4f}" if yp is not None else "n/a"
        desc = m["description"].replace("\n", " ")
        snippet = (desc[:140] + "…") if len(desc) > 140 else desc
        has_cond = bool(m["raw"].get("conditionId"))
        print(f"[{i}] {m['question']}")
        print(f"    market_id     : {m['market_id']}  (conditionId present: {has_cond})")
        print(f"    yes_price     : {yp_str}")
        print(f"    volume        : ${m['volume']:,.0f}   liquidity: ${m['liquidity']:,.0f}")
        print(f"    outcomes      : {m['outcomes']} -> {m['outcome_prices']}")
        print(f"    end_date      : {m['end_date']}")
        print(f"    description   : {snippet}")
        print()

    print("OK — all assertions passed (non-empty list, non-empty questions, numeric volumes).")


if __name__ == "__main__":
    _main()
