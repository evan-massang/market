"""
Deploy the backtested favorite-longshot strategy across active opinion markets —
a PORTFOLIO of +EV paper bets, placed fast (no LLM). Sorts soonest-resolving first
so the bets are watchable, stores each market's end_date for the dashboard countdown.

    python -m harness.strategy_bet [--max 40] [--max-days 365]
"""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, wallet, journal, strategy
from harness import safe_bet   # Plan 3: shared safety-gated opener + disabled-by-default switch


def _hours_left(m):
    ed = m.get("end_date")
    if not ed:
        return None
    try:
        dt = datetime.fromisoformat(str(ed).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    max_markets, max_days = 40, 365
    for i, a in enumerate(argv):
        if a == "--max": max_markets = int(argv[i + 1])
        if a == "--max-days": max_days = int(argv[i + 1])

    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()

    # ── Plan 3: DISABLED BY DEFAULT. The favorite-longshot strategy bets on a price
    #    pattern, not a real forecast, so it must not open paper positions unless the
    #    operator explicitly opts in. When opted in (ENABLE_STRATEGY_BET=true) every
    #    bet is routed through safe_bet (EV/risk/bankroll/exposure-gated). Returns
    #    BEFORE any network fetch — no wallet.open_position on the default path. ──
    if not safe_bet.strategy_bet_enabled():
        print(f"[strategy] {safe_bet.STRATEGY_DISABLED}: set ENABLE_STRATEGY_BET=true to opt in "
              f"(bets then pass EV/risk/bankroll/exposure via safe_bet). No paper bets placed.")
        return safe_bet.STRATEGY_DISABLED

    held = {p["market_id"] for p in wallet.get_open_positions()}

    print(f"[strategy] fetching markets…")
    markets = gamma.fetch_active_markets(limit=1500)
    markets.sort(key=lambda m: (_hours_left(m) if _hours_left(m) is not None else 1e9))
    print(f"[strategy] {len(markets)} markets. Applying favorite-longshot edge (fade .03-.20 NO, back .88-.99 YES)…\n")

    opened = scanned = 0
    for m in markets:
        if opened >= max_markets:
            break
        scanned += 1
        mid = m["market_id"]
        if mid in held:
            continue
        if classifier.tag_market(m).label != "opinion":
            continue
        if not classifier.passes_liquidity_floor(m):
            continue
        hl = _hours_left(m)
        if hl is not None and hl < 1.0:
            continue   # already ended or about to resolve — no edge to capture, skip
        if max_days and hl is not None and hl > max_days * 24:
            continue
        price = gamma.yes_price(m)
        if price is None:
            continue
        d = strategy.decide_bet(price)
        if d.side is None or d.fraction <= 0:
            continue
        # HONESTY (Plan 3): the rule must supply a REAL estimated true-YES to be
        # EV-gated. If it can't, it must NOT pretend the price is its forecast -> no bet.
        if d.est_true_yes is None:
            safe_bet.record_no_bet("strategy_bet", m, safe_bet.STRATEGY_MISSING_EV_PROB, price=price)
            continue
        stake = round(d.fraction * wallet.bankroll_for_sizing(), 6)
        model_p = float(d.est_true_yes)
        # Plan 3: route through the SHARED safety stack (EV/risk/bankroll/exposure).
        # No swarm-health — this is a price strategy, not an AI forecast (forecast_meta=None).
        res = safe_bet.open_position_if_safe(
            source="strategy_bet", market=m, side=d.side, probability=model_p, price=price,
            stake=stake, wallet_config=wallet.WalletConfig(max_bet_frac=0.05, max_exposure_frac=0.75),
            reason_context={"strategy_reason": d.reason})
        if res["opened"]:
            opened += 1
            hrs = f"{hl:.0f}h" if hl is not None else "?"
            print(f"  [{opened:2d}] {res['side']} ${res['stake']:5.2f} @ {res['fill_price']:.3f} | mkt {price:.0%} "
                  f"| resolves {hrs} | {m['question'][:50]}")
        else:
            print(f"  [skip] {safe_bet.STRATEGY_GATE_BLOCKED}: {res['reason']} | {m['question'][:46]}")

    st = wallet.get_state()
    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
    print(f"\n[strategy] PORTFOLIO PLACED: {opened} +EV bets. wallet: cash=${st['cash']:.2f} "
          f"equity=${st['equity']:.2f} exposure=${st['open_exposure']:.2f} open={st['n_open']}")


if __name__ == "__main__":
    main()
