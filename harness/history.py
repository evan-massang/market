"""
P2.5 (fallback) — $0 historical market-calibration read, NO PolyBench download.

PolyBench's dataset is behind a browser-locked OneDrive link. This builds the same
key number — the MARKET-PRICE Brier on resolved OPINION markets (the BAR our swarm
must beat for Gate 1) — straight from free public Polymarket endpoints:
  * Gamma /markets?closed=true  -> recently resolved markets + their outcome
  * CLOB /prices-history        -> the YES price N days BEFORE resolution (the
                                   'forecast-time' price; the settled price is 0/1
                                   and useless for calibration)
We classify each to opinion (our P1 classifier) and score (price - outcome)^2.

This does NOT run our LLM over history (that would be thousands of slow forecasts);
it measures how well-calibrated the MARKET is — i.e. the bar. Keyless, $0.

CLI:  python -m harness.history [--n 120] [--lookback-days 7] [--max-scored 80]
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import httpx

from harness import gamma
from harness.classifier import tag_market

CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
_HEADERS = {"User-Agent": "polyswarm-harness/0.1 (paper, read-only)"}


def _to_ts(iso) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def clob_prices_history(token_id: str, start_ts: int, end_ts: int,
                        fidelity: int = 360, timeout: float = 30.0) -> list[tuple[int, float]]:
    """Return [(unix_ts, price), …] for a CLOB token over [start_ts, end_ts]. Keyless.
    CLOB requires an explicit time window and caps its span (~14d). Never raises."""
    if not token_id:
        return []
    try:
        r = httpx.get(CLOB_HISTORY, params={"market": token_id, "startTs": int(start_ts),
                      "endTs": int(end_ts), "fidelity": fidelity}, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return []
        hist = r.json().get("history", [])
        return [(int(pt["t"]), float(pt["p"])) for pt in hist if "t" in pt and "p" in pt]
    except Exception:
        return []


def pre_resolution_yes_price(market: dict, lookback_days: int = 7) -> float | None:
    """The YES price ~lookback_days before the market's end_date. Queries a 13-day
    CLOB window ending at resolution (within CLOB's span cap) and takes the last
    point at or before the lookback target."""
    tokens = market.get("clob_token_ids") or []
    end_ts = _to_ts(market.get("end_date"))
    if not tokens or end_ts is None:
        return None
    start_ts = end_ts - 13 * 86400          # stay under CLOB's ~14d window cap
    hist = clob_prices_history(tokens[0], start_ts, end_ts)  # tokens[0] = YES token
    if not hist:
        return None
    hist.sort(key=lambda x: x[0])
    target = end_ts - lookback_days * 86400
    before = [p for t, p in hist if t <= target]
    if before:
        return before[-1]
    return hist[0][1]   # market younger than the lookback — use its first price


def historical_read(n_markets: int = 120, lookback_days: int = 7,
                    max_scored: int | None = 80, pause: float = 0.2) -> dict:
    print(f"[history] fetching up to {n_markets} closed markets…")
    markets = gamma.fetch_closed_markets(limit=n_markets)
    opinion = [m for m in markets if tag_market(m).label == "opinion"]
    print(f"[history] {len(markets)} closed markets, {len(opinion)} classified OPINION. "
          f"Pricing up to {max_scored or len(opinion)} of them {lookback_days}d pre-resolution "
          f"via CLOB (this hits the network per market)…\n")

    briers, scored = [], 0
    for m in opinion:
        if max_scored and scored >= max_scored:
            break
        outcome = gamma.resolution_outcome(m)
        if outcome is None:
            continue
        price = pre_resolution_yes_price(m, lookback_days)
        if price is None or not (0.0 <= price <= 1.0):
            continue
        b = (price - outcome) ** 2
        briers.append(b)
        scored += 1
        print(f"  [{scored}] pre_price={price:.3f} outcome={outcome:.0f} brier={b:.3f}  {m['question'][:48]}")
        time.sleep(pause)   # be polite to CLOB

    market_brier = sum(briers) / len(briers) if briers else None
    print("\n" + "=" * 60)
    print(" P2.5 (fallback) — HISTORICAL MARKET CALIBRATION ($0, no PolyBench)")
    print("=" * 60)
    print(f"   closed markets fetched : {len(markets)}")
    print(f"   classified OPINION     : {len(opinion)}")
    print(f"   scored (price+outcome) : {scored}   (lookback {lookback_days}d)")
    if market_brier is not None:
        print(f"\n   MARKET-PRICE Brier on resolved OPINION markets: {market_brier:.4f}")
        print("   ^ THE BAR — our swarm's Brier must come in BELOW this for Gate 1.")
        print(f"   (random=0.25; the lower this is, the sharper the market, the harder to beat.)")
    else:
        print("\n   could not score any markets (CLOB history unavailable?) — try a larger --n.")
    print("=" * 60)
    return {"closed": len(markets), "opinion": len(opinion), "scored": scored, "market_brier": market_brier}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness.history")
    ap.add_argument("--n", type=int, default=120, help="closed markets to fetch")
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--max-scored", type=int, default=80)
    a = ap.parse_args(argv)
    historical_read(n_markets=a.n, lookback_days=a.lookback_days, max_scored=a.max_scored)


if __name__ == "__main__":
    main()
