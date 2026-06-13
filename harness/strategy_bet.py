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
        stake = round(d.fraction * wallet.bankroll_for_sizing(), 6)
        model_p = d.est_true_yes if d.est_true_yes is not None else price
        edge = round(model_p - price, 4)   # NO bets -> negative, YES bets -> positive
        fr = wallet.open_position(mid, m["question"], d.side, model_p, price, edge, stake,
                                  cfg=wallet.WalletConfig(max_bet_frac=0.05, max_exposure_frac=0.75),
                                  end_date=m.get("end_date"))
        if fr.opened:
            opened += 1
            sig = "LONG (YES)" if d.side == "YES" else "SHORT (NO)"
            why = (f"Favorite-longshot edge — {d.reason}. Bought {fr.side} @ {fr.fill_price:.3f} "
                   f"with ${fr.stake:.2f} (backtested +EV, ~95% win).")
            journal.record_decision(mid, m["question"], model_p, price, edge, fr.side, fr.stake,
                                    fr.fill_price, "edge", sig, "bet", why)
            hrs = f"{hl:.0f}h" if hl is not None else "?"
            print(f"  [{opened:2d}] {fr.side} ${fr.stake:5.2f} @ {fr.fill_price:.3f} | mkt {price:.0%} "
                  f"| resolves {hrs} | {m['question'][:50]}")

    st = wallet.get_state()
    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
    print(f"\n[strategy] PORTFOLIO PLACED: {opened} +EV bets. wallet: cash=${st['cash']:.2f} "
          f"equity=${st['equity']:.2f} exposure=${st['open_exposure']:.2f} open={st['n_open']}")


if __name__ == "__main__":
    main()
