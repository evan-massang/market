"""
P-signals — faithful Python port of the market-SNAPSHOT signal math from the
WhoIsSharp repo (Rust:  src/signals.rs).

Scope of THIS port (network-free, single-market snapshot only):
  - insider_alert  (find_insider_alerts, signals.rs L200-247)
  - near_fifty     (find_near_fifty,     signals.rs L253-281)
  - thin_market    (find_thin_markets,   signals.rs L329-359)
  - vol_spike      (find_vol_spikes,     signals.rs L285-325)
  - momentum       (find_momentum,       signals.rs L368-403)

DELIBERATELY NOT ported here (see DEFERRED note at the bottom of the file):
  - find_arb_pairs            — needs cross-platform (PM↔KL) title pairs, out of scope.
  - build_wallet_profile / compute_suspicion / scan_too_smart_wallets
                              — the per-wallet "smart-money" engine (tools.rs).
  - kelly_size / kelly_correlated — the harness keeps ONE sizing engine elsewhere.

These signals are produced as FORECASTING FEATURES: `compute_signals(market)`
returns a flat, auditable dict that the harness feeds to the forecaster as
*context* about a market's microstructure. They are NOT trade triggers, and this
module performs NO order / execution / wallet / private-key work — it is pure,
synchronous arithmetic over an already-loaded market dict.

Style matches harness/classifier.py: dataclasses, type hints, tolerant field
normalization (works on Gamma market dicts using either `outcomePrices` /
`outcome_prices`, `volume` / `volumeNum`, `liquidity` / `liquidityNum`,
`volume24hr` / `volume_24hr`), and a transparent record of which signals fired.

Run the synthetic self-test (no network):
    export PYTHONUTF8=1
    ./.venv/Scripts/python.exe -m harness.signals
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

# ─── Ported threshold constants (cite: WhoIsSharp/src/signals.rs) ─────────────
# Every constant below is copied verbatim from the Rust source. Line numbers are
# from signals.rs at the commit read for this port.

# insider_alert — find_insider_alerts (L200-247)
INSIDER_VOL_LIQ_RATIO = 15.0   # signals.rs L197: volume >= 15x liquidity pool
INSIDER_PRICE_EXTREME = 0.25   # signals.rs L198: flag when price < 0.25 OR > 0.75
INSIDER_MIN_VOLUME = 1_000.0   # signals.rs L207: need real volume to compute ratio
INSIDER_MIN_LIQUIDITY = 1.0    # signals.rs L207: need a real pool to compare against
INSIDER_STAR_3 = 50.0          # signals.rs L223: ratio >= 50 -> 3 stars
INSIDER_STAR_2 = 25.0          # signals.rs L223: ratio >= 25 -> 2 stars

# near_fifty — find_near_fifty (L253-281)
NEAR_FIFTY_RANGE = 0.06        # signals.rs L251: 44%-56% band, |price-0.5| <= 0.06
NEAR_FIFTY_MIN_VOLUME = 10_000.0  # signals.rs L257: only surface if volume > 10_000
NEAR_FIFTY_STAR_3 = 0.01       # signals.rs L261: dist < 0.01 -> 3 stars
NEAR_FIFTY_STAR_2 = 0.03       # signals.rs L261: dist < 0.03 -> 2 stars

# thin_market — find_thin_markets (L329-359)
THIN_LIQUIDITY_CEILING = 10_000.0  # signals.rs L334: liquidity < 10_000 (and > 0)
THIN_PRICE_LOW = 0.05          # signals.rs L336: exclude price <= 0.05
THIN_PRICE_HIGH = 0.95         # signals.rs L336: exclude price >= 0.95

# vol_spike — find_vol_spikes (L285-325)
VOL_SPIKE_MULTIPLIER = 3.0     # signals.rs L296: spike_threshold = baseline * 3.0
VOL_SPIKE_STAR_3 = 10.0        # signals.rs L304: ratio >= 10 -> 3 stars
VOL_SPIKE_STAR_2 = 5.0         # signals.rs L304: ratio >= 5  -> 2 stars

# momentum — find_momentum (L361-403)
MOMENTUM_THRESHOLD = 0.04      # signals.rs L366: >= 4 percentage-point move fires
MOMENTUM_STAR_3 = 0.12         # signals.rs L381: |delta| >= 0.12 -> 3 stars
MOMENTUM_STAR_2 = 0.07         # signals.rs L381: |delta| >= 0.07 -> 2 stars


# ─── Result containers ───────────────────────────────────────────────────────
@dataclass
class InsiderDetail:
    """Detail payload for an insider_alert firing (mirrors the Rust Signal fields
    set in find_insider_alerts, signals.rs L215-244)."""
    vol_liq_ratio: float        # gap field in Rust (L236): volume / liquidity
    extreme: bool               # price in the extreme band (L211-212)
    direction: str              # "YES" if price > 0.5 else "NO" (L225)
    price_extremity: float      # abs(price - 0.5) * 2.0, 0..1 (L221)
    ev_score: float             # ratio * price_extremity * 10.0 (L222)
    stars: int                  # 1..3 (L223)


@dataclass
class SignalSnapshot:
    """All snapshot signals for a single market, designed as forecaster features."""
    fired: list[str] = field(default_factory=list)
    insider_alert: bool = False
    insider_detail: InsiderDetail | None = None
    near_fifty: bool = False
    thin_market: bool = False
    vol_spike: bool = False
    momentum: float | None = None          # signed 24h delta if derivable, else None
    raw_metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Field normalization (tolerant of Gamma / harness-normalized dicts) ──────
def _first_float(market, *keys) -> float | None:
    """Return the first parseable float among `keys`, else None."""
    if not isinstance(market, dict):
        return None
    for k in keys:
        v = market.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _yes_price(market) -> float:
    """Extract the YES implied probability (0.0-1.0).

    Mirrors WhoIsSharp parse_outcome_prices (polymarket.rs L623-626): YES is the
    first element of `outcomePrices`. Tolerant of:
      - an explicit yes-price key (yes_price / yesPrice / price),
      - `outcomePrices` / `outcome_prices` as a real list OR a JSON-encoded string
        ("[\\"0.65\\",\\"0.35\\"]"),
    and defaults to 0.5 when nothing usable is present (same default as Rust).
    """
    explicit = _first_float(market, "yes_price", "yesPrice", "price", "yesProbability")
    if explicit is not None:
        return explicit
    if isinstance(market, dict):
        raw = market.get("outcomePrices")
        if raw is None:
            raw = market.get("outcome_prices")
        prices = _coerce_price_list(raw)
        if prices:
            try:
                return float(prices[0])
            except (TypeError, ValueError):
                pass
    return 0.5


def _coerce_price_list(raw) -> list:
    """Accept a real list or a JSON-encoded string array; return a list (maybe empty)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return list(parsed) if isinstance(parsed, (list, tuple)) else []
        except (ValueError, TypeError):
            return []
    return []


def _volume(market) -> float:
    v = _first_float(market, "volume", "volumeNum", "volume_num")
    return v if v is not None else 0.0


def _liquidity(market) -> float:
    v = _first_float(market, "liquidity", "liquidityNum", "liquidity_num")
    return v if v is not None else 0.0


def _volume_24hr(market) -> float | None:
    """24h volume if the snapshot carries it, else None."""
    return _first_float(market, "volume24hr", "volume_24hr", "volume24Hr", "oneDayVolume")


def _momentum_delta(market, prev_price: float | None) -> float | None:
    """Signed 24h YES-price change if derivable, else None.

    The Rust find_momentum (L368-403) diffs the current price against the price
    from the previous snapshot (prev_prices map). A single-market snapshot has no
    history, so we accept the change from whichever 24h field the Gamma dict
    carries, OR an explicit `prev_price` supplied by the caller:
      - a direct change field (oneDayPriceChange / one_day_price_change /
        priceChange24h) is already a signed delta, used as-is;
      - otherwise delta = current_yes - prev_price.
    Returns None when no 24h information is available (so momentum is opt-in).
    """
    direct = _first_float(market, "oneDayPriceChange", "one_day_price_change",
                          "priceChange24h", "price_change_24h")
    if direct is not None:
        return direct
    prior = prev_price
    if prior is None:
        prior = _first_float(market, "prev_yes_price", "yes_price_24h_ago",
                             "price_24h_ago")
    if prior is None:
        return None
    return _yes_price(market) - prior


# ─── Individual signal computations (faithful ports) ─────────────────────────
def _insider_alert(yes: float, vol: float, liq: float) -> InsiderDetail | None:
    """Port of find_insider_alerts (signals.rs L200-247).

    Informed-flow heuristic: a strongly directional price (>75% or <25% YES) that
    is consuming far more cumulative volume than its standing liquidity pool can
    explain. Fires when vol/liq >= 15x (L197) AND price is in the extreme band
    (L211-212), with vol >= 1_000 and liq >= 1.0 (L207).
    """
    # filter (L203-213)
    if vol < INSIDER_MIN_VOLUME or liq < INSIDER_MIN_LIQUIDITY:
        return None
    ratio = vol / liq
    extreme = (yes > (1.0 - INSIDER_PRICE_EXTREME)) or (yes < INSIDER_PRICE_EXTREME)
    if not (ratio >= INSIDER_VOL_LIQ_RATIO and extreme):
        return None
    # detail (L221-225)
    price_extremity = abs(yes - 0.5) * 2.0            # 0.0 .. 1.0  (L221)
    ev_score = ratio * price_extremity * 10.0          # (L222)
    if ratio >= INSIDER_STAR_3:
        stars = 3
    elif ratio >= INSIDER_STAR_2:
        stars = 2
    else:
        stars = 1
    direction = "YES" if yes > 0.5 else "NO"           # (L225)
    return InsiderDetail(
        vol_liq_ratio=ratio,
        extreme=extreme,
        direction=direction,
        price_extremity=price_extremity,
        ev_score=ev_score,
        stars=stars,
    )


def _near_fifty(yes: float, vol: float) -> tuple[bool, float, int]:
    """Port of find_near_fifty (signals.rs L253-281).

    Returns (fired, dist_from_fifty, stars). Fires when |price-0.5| <= 0.06 (L251,
    L256) AND volume > 10_000 (L257). The volume gate is the Rust surfacing
    condition; the raw `dist_from_fifty` (price-only) is always reported in
    raw_metrics for callers that want the pure-price feature.
    """
    dist = abs(yes - 0.5)
    in_band = dist <= NEAR_FIFTY_RANGE
    fired = in_band and (vol > NEAR_FIFTY_MIN_VOLUME)
    if dist < NEAR_FIFTY_STAR_3:
        stars = 3
    elif dist < NEAR_FIFTY_STAR_2:
        stars = 2
    else:
        stars = 1
    return fired, dist, stars


def _thin_market(yes: float, liq: float) -> bool:
    """Port of find_thin_markets (signals.rs L329-359).

    Low-liquidity / adverse-selection flag: liquidity in (0, 10_000) (L333-334)
    and price strictly inside (0.05, 0.95) (L336) — extremes are excluded because
    a near-certain market being thin is unremarkable.
    """
    if not (0.0 < liq < THIN_LIQUIDITY_CEILING):
        return False
    return THIN_PRICE_LOW < yes < THIN_PRICE_HIGH


def _vol_spike(vol: float, liq: float, vol24: float | None,
               peer_mean_volume: float | None) -> tuple[bool, float | None, int]:
    """Port of find_vol_spikes (signals.rs L285-325), adapted for single markets.

    The Rust version compares a market's volume against the CROSS-MARKET mean
    (spike_threshold = mean * 3.0, L296) — which requires the whole population.
    Two modes:
      1. If `peer_mean_volume` is supplied (caller has the population), we apply
         the faithful Rust logic: ratio = volume / max(mean, 1.0), fire when
         volume >= mean * 3.0.
      2. Otherwise, in pure single-market snapshot mode we substitute the standing
         liquidity pool as the per-market baseline and measure recent turnover:
         ratio = volume24hr / max(liquidity, 1.0), fire when ratio >= 3.0.
         Requires volume24hr (a recent-flow spike is undetectable without it).
    The 3.0 multiplier (L296) and the 10x/5x star bands (L304) are preserved in
    both modes. Returns (fired, ratio_or_None, stars).
    """
    if peer_mean_volume is not None:
        baseline = max(peer_mean_volume, 1.0)
        ratio = vol / baseline
        fired = vol >= peer_mean_volume * VOL_SPIKE_MULTIPLIER
    else:
        if vol24 is None:
            return False, None, 1
        baseline = max(liq, 1.0)
        ratio = vol24 / baseline
        fired = ratio >= VOL_SPIKE_MULTIPLIER
    if ratio >= VOL_SPIKE_STAR_3:
        stars = 3
    elif ratio >= VOL_SPIKE_STAR_2:
        stars = 2
    else:
        stars = 1
    return fired, ratio, stars


def _momentum_stars(delta: float) -> int:
    """Star band from find_momentum (signals.rs L381)."""
    a = abs(delta)
    if a >= MOMENTUM_STAR_3:
        return 3
    if a >= MOMENTUM_STAR_2:
        return 2
    return 1


# ─── Public API ──────────────────────────────────────────────────────────────
def compute_signals(market, *, prev_price: float | None = None,
                    peer_mean_volume: float | None = None) -> dict:
    """Compute all snapshot signals for a single market.

    Args:
      market: a (harness-normalized) Gamma market dict. Tolerant of key variants
              for price (outcomePrices / outcome_prices / yes_price), volume
              (volume / volumeNum), liquidity (liquidity / liquidityNum), and
              24h volume (volume24hr / volume_24hr).
      prev_price: optional prior YES price for momentum; if omitted, momentum is
              derived from a 24h change field on the dict, else left as None.
      peer_mean_volume: optional cross-market mean volume enabling the FAITHFUL
              find_vol_spikes baseline (signals.rs L295-296); omit for the
              single-market 24h-vs-liquidity proxy.

    Returns a flat dict (forecaster features, NOT trade triggers):
      {
        "fired":          [str],          # which signals fired, for audit
        "insider_alert":  bool,
        "insider_detail": {...} | None,   # vol/liq ratio, direction, stars, ...
        "near_fifty":     bool,
        "thin_market":    bool,
        "vol_spike":      bool,
        "momentum":       float | None,   # signed 24h YES-price delta if derivable
        "raw_metrics":    {...},          # the underlying numbers + thresholds
      }
    """
    yes = _yes_price(market)
    vol = _volume(market)
    liq = _liquidity(market)
    vol24 = _volume_24hr(market)

    snap = SignalSnapshot()

    # insider_alert (L200)
    detail = _insider_alert(yes, vol, liq)
    if detail is not None:
        snap.insider_alert = True
        snap.insider_detail = detail
        snap.fired.append("insider_alert")

    # near_fifty (L253)
    nf_fired, dist, nf_stars = _near_fifty(yes, vol)
    snap.near_fifty = nf_fired
    if nf_fired:
        snap.fired.append("near_fifty")

    # thin_market (L329)
    snap.thin_market = _thin_market(yes, liq)
    if snap.thin_market:
        snap.fired.append("thin_market")

    # vol_spike (L285)
    vs_fired, vs_ratio, vs_stars = _vol_spike(vol, liq, vol24, peer_mean_volume)
    snap.vol_spike = vs_fired
    if vs_fired:
        snap.fired.append("vol_spike")

    # momentum (L368)
    delta = _momentum_delta(market, prev_price)
    snap.momentum = delta
    mom_stars = None
    if delta is not None and abs(delta) >= MOMENTUM_THRESHOLD:
        snap.fired.append("momentum")
        mom_stars = _momentum_stars(delta)

    snap.raw_metrics = {
        "yes_price": yes,
        "volume": vol,
        "liquidity": liq,
        "volume24hr": vol24,
        "vol_liq_ratio": (vol / liq) if liq > 0 else None,
        "price_extremity": abs(yes - 0.5) * 2.0,
        "dist_from_fifty": dist,
        "near_fifty_in_band": dist <= NEAR_FIFTY_RANGE,
        "near_fifty_stars": nf_stars if nf_fired else None,
        "vol_spike_ratio": vs_ratio,
        "vol_spike_stars": vs_stars if vs_fired else None,
        "vol_spike_baseline": "peer_mean" if peer_mean_volume is not None else "liquidity_24hr",
        "momentum_delta": delta,
        "momentum_stars": mom_stars,
        "thresholds": {
            "insider_vol_liq_ratio": INSIDER_VOL_LIQ_RATIO,
            "insider_price_extreme": INSIDER_PRICE_EXTREME,
            "near_fifty_range": NEAR_FIFTY_RANGE,
            "near_fifty_min_volume": NEAR_FIFTY_MIN_VOLUME,
            "thin_liquidity_ceiling": THIN_LIQUIDITY_CEILING,
            "vol_spike_multiplier": VOL_SPIKE_MULTIPLIER,
            "momentum_threshold": MOMENTUM_THRESHOLD,
        },
    }
    return snap.to_dict()


# ─── DEFERRED: per-wallet smart-money engine (future follow-up) ──────────────
def scan_too_smart_wallets(*_args, **_kwargs):
    """DEFERRED — not implemented in this pass.

    The WhoIsSharp per-wallet "smart-money" engine
    (tools.rs: build_wallet_profile L1413, compute_suspicion L1820,
    scan_too_smart_wallets L854) profiles individual wallets by pulling their
    per-wallet TRADE/REDEEM history from data-api.polymarket.com
    (GET /positions, GET /trades; see polymarket.rs L329-331, L519-538) and scores
    each wallet's "too-smart" suspicion, then aggregates per-market.

    It is DEFERRED for this snapshot-signals pass because:

      1. SCOPE — it is a NETWORKED, per-wallet aggregation engine, not pure
         single-market snapshot arithmetic. This module is intentionally pure and
         network-free (matching the harness's read-only/paper posture); the wallet
         engine would add per-wallet HTTP fan-out and its own caching/rate-limit
         concerns that belong in a dedicated data-source module.

      2. EDGE MISMATCH — its strongest edge is on NEWS / MECHANICAL markets
         (insider/informed flow ahead of an objective external event: a data
         release, a court ruling, a sports result). The harness deliberately SKIPS
         mechanical markets and forecasts only OPINION markets (see
         harness/classifier.py: tag_market / should_forecast — opinion = outcomes
         driven by crowd sentiment, which a single smart wallet cannot pre-know).
         So the smart-money signal has little to add to the harness's current
         opinion-only universe, and porting it now would be speculative.

    When revisited it should live in its own module (e.g. harness/smartmoney.py)
    with explicit, cached, keyless httpx calls to data-api.polymarket.com — still
    READ-ONLY, still paper-only, no order/execution/wallet-write paths.
    """
    raise NotImplementedError(
        "per-wallet smart-money engine is deferred — see module docstring "
        "(strongest edge is on news/mechanical markets, which the harness skips)"
    )


# ─── Synthetic self-test (no network) ────────────────────────────────────────
def _run_self_test() -> None:
    print("== harness.signals — synthetic snapshot tests (no network) ==\n")

    # 1) Extreme price + high volume/liquidity -> insider_alert.
    #    Mirrors Rust test insider_alert_detected_high_vol_liq_extreme_yes
    #    (signals.rs L642-653): vol=200K, liq=5K -> ratio 40x at 80% YES.
    insider_mkt = {
        "question": "Suspicious directional market",
        "outcomePrices": "[\"0.80\", \"0.20\"]",
        "volume": "200000",
        "liquidity": "5000",
    }
    s1 = compute_signals(insider_mkt)
    print(f"  [insider]    fired={s1['fired']}")
    print(f"               ratio={s1['insider_detail']['vol_liq_ratio']:.1f}x "
          f"dir={s1['insider_detail']['direction']} stars={s1['insider_detail']['stars']}")
    assert s1["insider_alert"] is True, "extreme price + 40x vol/liq must fire insider_alert"
    assert abs(s1["insider_detail"]["vol_liq_ratio"] - 40.0) < 1e-6, "ratio should be ~40x"
    assert s1["insider_detail"]["stars"] == 2, "40x in [25,50) -> 2 stars (L223)"
    assert "insider_alert" in s1["fired"]

    # 2) Price ~0.50 with real volume -> near_fifty.
    #    Mirrors near_fifty_at_exactly_half (L554-561): price 0.50, vol 100K.
    coinflip_mkt = {
        "question": "Coin flip event",
        "outcomePrices": ["0.50", "0.50"],
        "volume": "120000",
        "liquidity": "60000",
    }
    s2 = compute_signals(coinflip_mkt)
    print(f"  [near_fifty] fired={s2['fired']} dist={s2['raw_metrics']['dist_from_fifty']:.3f}")
    assert s2["near_fifty"] is True, "price 0.50 with vol>10K must fire near_fifty"
    assert "near_fifty" in s2["fired"]

    # 3) Very low liquidity (mid price) -> thin_market.
    #    Mirrors thin_market_detected (L611-617): liq 500, price 0.50.
    thin_mkt = {
        "question": "Illiquid event",
        "outcomePrices": ["0.50", "0.50"],
        "liquidity": "500",
    }
    s3 = compute_signals(thin_mkt)
    print(f"  [thin]       fired={s3['fired']} liquidity={s3['raw_metrics']['liquidity']:.0f}")
    assert s3["thin_market"] is True, "liquidity 500 (<10K) at mid price must fire thin_market"
    assert "thin_market" in s3["fired"]

    # 4) vol_spike — single-market proxy: 24h volume = 5x liquidity (>= 3x).
    spike_mkt = {
        "question": "Recent burst of trading",
        "outcomePrices": ["0.62", "0.38"],
        "volume": "400000",
        "liquidity": "20000",
        "volume24hr": "100000",   # 100K / 20K = 5x liquidity -> spike (>=3x), 2 stars
    }
    s4 = compute_signals(spike_mkt)
    print(f"  [vol_spike]  fired={s4['fired']} ratio={s4['raw_metrics']['vol_spike_ratio']:.1f}x "
          f"baseline={s4['raw_metrics']['vol_spike_baseline']}")
    assert s4["vol_spike"] is True, "24h volume 5x liquidity must fire vol_spike (proxy mode)"
    assert s4["raw_metrics"]["vol_spike_stars"] == 2, "5x ratio -> 2 stars (L304)"

    # 4b) vol_spike — faithful cross-market mode via peer_mean_volume (L295-296).
    s4b = compute_signals(spike_mkt, peer_mean_volume=100_000.0)
    print(f"  [vol_spike*] fired={s4b['fired']} ratio={s4b['raw_metrics']['vol_spike_ratio']:.1f}x "
          f"baseline={s4b['raw_metrics']['vol_spike_baseline']}")
    assert s4b["vol_spike"] is True, "volume 400K vs mean 100K (4x >= 3x) must fire (faithful mode)"

    # 5) momentum — directional 24h move >= 4pp (L366).
    momentum_mkt = {
        "question": "Catalyst-driven mover",
        "outcomePrices": ["0.60", "0.40"],
        "volume": "80000",
        "liquidity": "30000",
        "oneDayPriceChange": "0.09",   # +9pp >= 4pp -> momentum fires, 2 stars
    }
    s5 = compute_signals(momentum_mkt)
    print(f"  [momentum]   fired={s5['fired']} delta={s5['momentum']:+.3f} "
          f"stars={s5['raw_metrics']['momentum_stars']}")
    assert s5["momentum"] is not None and abs(s5["momentum"] - 0.09) < 1e-9
    assert "momentum" in s5["fired"], "+9pp 24h move must fire momentum"
    assert s5["raw_metrics"]["momentum_stars"] == 2, "0.09 in [0.07,0.12) -> 2 stars (L381)"

    # 6) Negative controls — quiet, healthy, liquid market: nothing fires.
    #    price 0.70 is outside the near-fifty band (|0.70-0.5|=0.20 > 0.06) and
    #    not extreme (< 0.75), vol/liq = 1.5x (< 15x), liq > 10K, no 24h data.
    quiet_mkt = {
        "question": "Healthy liquid market",
        "outcomePrices": ["0.70", "0.30"],
        "volume": "300000",
        "liquidity": "200000",
    }
    s6 = compute_signals(quiet_mkt)
    print(f"  [control]    fired={s6['fired']} (expected empty)")
    assert s6["fired"] == [], "healthy liquid mid-price market should fire nothing"
    assert s6["momentum"] is None, "no 24h data -> momentum None"

    # 7) DEFERRED stub must raise (engine intentionally not implemented).
    try:
        scan_too_smart_wallets()
        raise AssertionError("scan_too_smart_wallets must raise NotImplementedError")
    except NotImplementedError:
        print("  [deferred]   scan_too_smart_wallets() correctly raises NotImplementedError")

    print("\nALL SYNTHETIC SIGNAL ASSERTIONS PASSED")


if __name__ == "__main__":
    _run_self_test()
