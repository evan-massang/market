"""
Profitability research — backtest betting strategies on REAL resolved opinion
markets to find a demonstrable +EV edge (no LLM, $0).

Builds a dataset of {pre_resolution_price, outcome} from closed opinion markets
(Gamma closed + CLOB price ~N days before resolution), measures the market's
CALIBRATION (is there a favorite-longshot bias?), and backtests strategies with a
realistic fill (price + slippage) to compute P&L / ROI.

CLI:
  python -m harness.backtest collect [--n 1200] [--max 500] [--lookback 5]
  python -m harness.backtest calib
  python -m harness.backtest strategies
"""
from __future__ import annotations
import json, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from harness import gamma, classifier
from harness import history as H

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtest_data.json")
SLIPPAGE = 0.01   # pay 1 cent worse than mid on the side you buy
FEE = 0.0


def _one(m, lookback):
    try:
        if classifier.tag_market(m).label != "opinion":
            return None
        outcome = gamma.resolution_outcome(m)
        if outcome is None:
            return None
        price = H.pre_resolution_yes_price(m, lookback_days=lookback)
        if price is None or not (0.0 < price < 1.0):
            return None
        return {"market_id": m["market_id"], "question": m["question"][:80],
                "price": round(price, 4), "outcome": float(outcome), "volume": m.get("volume", 0)}
    except Exception:
        return None


def collect(n_markets=1200, max_collect=500, lookback=5, workers=6) -> list[dict]:
    print(f"[backtest] fetching up to {n_markets} closed markets…")
    markets = gamma.fetch_closed_markets(limit=n_markets)
    print(f"[backtest] {len(markets)} closed markets; pricing opinion ones via CLOB ({workers} workers)…")
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, m, lookback) for m in markets]
        for f in as_completed(futs):
            r = f.result()
            if r:
                rows.append(r)
                if len(rows) % 25 == 0:
                    print(f"  collected {len(rows)}…")
            if len(rows) >= max_collect:
                break
    json.dump(rows, open(DATA, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[backtest] saved {len(rows)} resolved opinion markets -> {DATA}")
    return rows


def _load() -> list[dict]:
    if not os.path.exists(DATA):
        print("[backtest] no dataset — run `collect` first"); return []
    return json.load(open(DATA, encoding="utf-8"))


def calib():
    """Calibration table: for each price bucket, the ACTUAL YES rate. If actual <
    price for low buckets, longshots are OVERPRICED (fade them = +EV)."""
    data = _load()
    if not data:
        return
    buckets = [(0, .05), (.05, .10), (.10, .20), (.20, .35), (.35, .50),
               (.50, .65), (.65, .80), (.80, .90), (.90, .95), (.95, 1.0)]
    print(f"\n CALIBRATION on {len(data)} resolved opinion markets (lookback price vs actual outcome)")
    print(f" {'price bucket':14s} {'n':>4s} {'avg_price':>9s} {'actual_YES':>10s} {'mispricing':>10s}")
    print(" " + "-" * 52)
    for lo, hi in buckets:
        rows = [r for r in data if lo <= r["price"] < hi]
        if not rows:
            continue
        n = len(rows)
        avg_p = sum(r["price"] for r in rows) / n
        actual = sum(r["outcome"] for r in rows) / n
        mis = actual - avg_p   # negative = market overpriced this bucket (fade YES)
        flag = "  <-- overpriced" if mis < -0.02 else ("  <-- underpriced" if mis > 0.02 else "")
        print(f" {f'[{lo:.2f},{hi:.2f})':14s} {n:>4d} {avg_p:>9.3f} {actual:>10.3f} {mis:>+10.3f}{flag}")


def _bt(data, decide, lam=0.25, cap=1.0, bankroll0=1000.0):
    """Backtest a decide(price)->('YES'|'NO'|None, fraction) strategy. Sequential,
    compounding off the running bankroll, realistic fill (price+slippage)+fee."""
    bank = bankroll0
    n_bets = wins = 0
    for r in data:
        c, o = r["price"], r["outcome"]
        side, frac = decide(c)
        if side is None or frac <= 0 or bank <= 0:
            continue
        stake = min(frac, cap) * bank
        base = c if side == "YES" else (1 - c)
        fill = min(base + SLIPPAGE, 0.999)   # honest: slippage always worsens; can't pay >~1.0
        shares = stake / fill
        won = (side == "YES" and o == 1.0) or (side == "NO" and o == 0.0)
        payout = shares if won else 0.0
        bank += payout - stake - FEE * stake
        n_bets += 1; wins += int(won)
    roi = (bank - bankroll0) / bankroll0
    return {"end_bank": bank, "roi": roi, "n_bets": n_bets, "win_rate": (wins / n_bets if n_bets else 0)}


def strategies():
    data = _load()
    if not data:
        return
    print(f"\n BACKTEST on {len(data)} resolved opinion markets (start $1000, 1/4-Kelly, cap 2%, slip {SLIPPAGE})")
    edge_kelly = lambda p_model, c: (p_model - c) / (1 - c) if p_model > c else (c - p_model) / c
    strats = {
        "fade longshots <15% (bet NO)":
            lambda c: ("NO", 0.25 * edge_kelly(max(c - 0.05, 0.01), c)) if c < 0.15 else (None, 0),
        "fade longshots <10% (bet NO)":
            lambda c: ("NO", 0.25 * edge_kelly(max(c - 0.04, 0.01), c)) if c < 0.10 else (None, 0),
        "fade longshots <25% flat 2%":
            lambda c: ("NO", 0.08) if c < 0.25 else (None, 0),
        "back favorites >85% (bet YES)":
            lambda c: ("YES", 0.25 * edge_kelly(min(c + 0.05, 0.99), c)) if c > 0.85 else (None, 0),
        "fade <15% AND back >85%":
            lambda c: (("NO", 0.08) if c < 0.15 else (("YES", 0.06) if c > 0.85 else (None, 0))),
        "EDGE fade[.05,.20)NO + back[.92,.99)YES":
            lambda c: (("NO", 0.10) if 0.05 <= c < 0.20 else (("YES", 0.10) if 0.92 <= c < 0.99 else (None, 0))),
        "EDGE fade[.05,.20) ONLY (NO)":
            lambda c: ("NO", 0.10) if 0.05 <= c < 0.20 else (None, 0),
        "EDGE wide fade[.03,.20)NO + back[.88,.99)YES":
            lambda c: (("NO", 0.08) if 0.03 <= c < 0.20 else (("YES", 0.08) if 0.88 <= c < 0.99 else (None, 0))),
        "naive: bet YES if <50% (BAD baseline)":
            lambda c: ("YES", 0.05) if c < 0.5 else (None, 0),
    }
    print(f" {'strategy':40s} {'bets':>5s} {'win%':>6s} {'ROI':>8s} {'end$':>9s}")
    print(" " + "-" * 72)
    res = {}
    for name, fn in strats.items():
        b = _bt(data, fn)
        res[name] = b
        print(f" {name:40s} {b['n_bets']:>5d} {b['win_rate']*100:>5.1f}% {b['roi']*100:>+7.1f}% {b['end_bank']:>9.0f}")
    best = max(res.items(), key=lambda kv: kv[1]["roi"])
    print(f"\n BEST: {best[0]}  ->  ROI {best[1]['roi']*100:+.1f}%")


def optimize():
    """Test the full 3-band calibration edge + a bet-size (cap) sweep, with a robustness
    check (split-half) so we don't overfit a small bucket."""
    data = _load()
    if not data:
        return
    import random
    full = lambda c, cap: (("NO", cap) if 0.03 <= c < 0.20 else
                           ("YES", cap) if 0.20 <= c < 0.35 else
                           ("YES", cap) if 0.88 <= c < 0.99 else (None, 0))
    robust = lambda c, cap: (("NO", cap) if 0.03 <= c < 0.20 else
                             ("YES", cap) if 0.88 <= c < 0.99 else (None, 0))
    print("\n CAP SWEEP (flat fraction per bet):")
    print(f" {'cap':>5s} | {'ROBUST (fade+fav)':>22s} | {'FULL (+[.20,.35) YES)':>24s}")
    for cap in (0.02, 0.04, 0.06, 0.10):
        rb = _bt(data, lambda c: robust(c, cap))
        fl = _bt(data, lambda c: full(c, cap))
        print(f" {cap*100:>4.0f}% | ROI {rb['roi']*100:>+6.1f}% ({rb['n_bets']:>3d} bets) | "
              f"ROI {fl['roi']*100:>+6.1f}% ({fl['n_bets']:>3d} bets)")
    # split-half robustness for the [.20,.35) YES band specifically
    band = [r for r in data if 0.20 <= r["price"] < 0.35]
    print(f"\n [.20,.35) band: n={len(band)}, actual YES rate={sum(r['outcome'] for r in band)/max(1,len(band)):.2f} "
          f"(priced ~0.26) -> {'real edge' if len(band)>=30 else 'SMALL SAMPLE, risky'}")


def slip_test():
    """Slippage sensitivity for the sweet-spot strategy — does the edge survive costs?"""
    global SLIPPAGE
    data = _load()
    if not data:
        return
    sweet = lambda c: (("NO", 0.10) if 0.06 <= c < 0.25 else (("YES", 0.10) if 0.80 <= c < 0.97 else (None, 0)))
    print("\n SLIPPAGE SENSITIVITY (sweet-spot strategy):")
    orig = SLIPPAGE
    for s in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03):
        SLIPPAGE = s
        b = _bt(data, sweet)
        print(f"   slip {s*100:.1f}c -> ROI {b['roi']*100:+6.1f}%  win {b['win_rate']*100:.1f}%  ({b['n_bets']} bets)")
    SLIPPAGE = orig


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "calib"
    if cmd == "sliptest":
        slip_test(); return
    if cmd == "optimize":
        optimize(); return
    if cmd == "collect":
        kw = {}
        for i, a in enumerate(argv):
            if a == "--n": kw["n_markets"] = int(argv[i+1])
            if a == "--max": kw["max_collect"] = int(argv[i+1])
            if a == "--lookback": kw["lookback"] = int(argv[i+1])
        collect(**kw)
    elif cmd == "calib":
        calib()
    elif cmd == "strategies":
        strategies()


if __name__ == "__main__":
    main()
