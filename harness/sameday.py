"""
Continuous SAME-DAY trading daemon. Keeps ONLY bets resolving within ~24h, and
never stops searching.

Each cycle: settle resolved -> cash out any bet resolving beyond today -> find
same-day markets -> run the AI agents (PolySwarm + single-LLM challenger, and a
best-effort MiroFish crowd-sim) on the same-day OPINION markets -> place same-day
+EV favorite-longshot bets. Loops until killed.

    python -m harness.sameday close     # one-off: cash out every bet resolving >24h
    python -m harness.sameday once      # one cycle
    python -m harness.sameday daemon    # continuous (no stopping)
"""
from __future__ import annotations
import os, subprocess, sys, time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, wallet, journal, strategy, challenger

MAX_HOURS = 24.0          # "same day" window
INTERVAL = 1200           # 20 min between cycles
_mf_launched: set[str] = set()   # market_ids we've kicked a MiroFish sim for


def _hours_left(ed):
    if not ed:
        return None
    try:
        dt = datetime.fromisoformat(str(ed).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def settle_resolved():
    from harness import loop
    try:
        return loop.settle_resolved(loop.LoopConfig())
    except Exception as e:
        print("[sameday] settle error:", e)
        return []


def close_long_dated(max_hours=MAX_HOURS):
    """Cash out (at current price) every open bet resolving beyond the same-day window."""
    closed, pnl = 0, 0.0
    for p in wallet.get_open_positions():
        hl = _hours_left(p.get("end_date"))
        if hl is None or hl <= max_hours:
            continue
        try:
            m = gamma.fetch_market_by_condition_id(p["market_id"])
            cur = gamma.yes_price(m) if m else None
            if cur is None:
                cur = p["market_p"]
            for r in wallet.close_at_price(p["market_id"], cur):
                closed += 1; pnl += r["realized_pnl"]
        except Exception as e:
            print("[sameday] close error", str(p.get("market_id"))[:14], e)
    if closed:
        print(f"[sameday] cashed out {closed} long-dated bets (>{max_hours:.0f}h), realized ${pnl:+.2f}")
    return closed


def _ai_scout(market, price):
    """Run the AI agents (PolySwarm + single-LLM challenger) on a same-day OPINION
    market so their forecasts show on the dashboard. Also fires a best-effort MiroFish
    crowd-sim (subprocess) the first time we see a given market."""
    mid = market["market_id"]
    # ONE forecast per market: re-scouting the same market every cycle wastes minutes
    # of CPU and double-counts it toward the gate (and cross-joins the A/B panel).
    # Skip if we already have an unresolved swarm forecast for it.
    try:
        from core.calibration import get_open_market_ids
        if mid in get_open_market_ids():
            print(f"[sameday] already forecast — skip re-scout: {market['question'][:44]}")
            return None
    except Exception:
        pass
    # MiroFish crowd-sim (slow; fire-and-forget so it doesn't block the daemon)
    if mid not in _mf_launched:
        _mf_launched.add(mid)
        try:
            subprocess.Popen([sys.executable, "-m", "harness.mirofish_quick", market["question"][:60]],
                             cwd=os.path.dirname(os.path.dirname(__file__)),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[sameday] 🐟 MiroFish crowd-sim launched for: {market['question'][:44]}")
        except Exception:
            pass
    # PolySwarm + challenger (the AI agents)
    try:
        from core.swarm import Swarm
        from agents.personas import build_swarm
        os.environ["DEBATE_ROUNDS"] = "1"
        res = Swarm(agents=build_swarm(5)).forecast(market["question"], market_odds=price, market_id=mid)
        sp = float(res["probability"])
        bp = challenger.single_llm_forecast(market["question"], price)
        if bp is not None:
            challenger.save_baseline(mid, market["question"], bp, price)
        print(f"[sameday] 🤖 swarm {sp:.0%} | single-LLM {bp if bp is not None else '-'} | market {price:.0%}")
        return sp
    except Exception as e:
        print("[sameday] AI scout error:", e)
        return None


def place_sameday(max_new=8, use_ai=True):
    held = {p["market_id"] for p in wallet.get_open_positions()}
    ms = gamma.fetch_markets_ending_within(MAX_HOURS, limit=150)
    ms.sort(key=lambda m: (_hours_left(m.get("end_date")) if _hours_left(m.get("end_date")) is not None else 1e9))
    opened = 0
    for m in ms:
        if opened >= max_new:
            break
        mid = m["market_id"]
        if mid in held:
            continue
        hl = _hours_left(m.get("end_date"))
        if hl is None or hl < 0.3:
            continue
        price = gamma.yes_price(m)
        if price is None:
            continue
        # AI agents scout same-day OPINION markets (rare, but this is "the AI searching")
        if use_ai and classifier.tag_market(m).label == "opinion":
            print(f"[sameday] AI agents scouting: {m['question'][:50]}")
            _ai_scout(m, price)
        # place a +EV favorite-longshot bet if in the edge bands + liquid
        if not ((0.05 <= price < 0.20) or (0.88 <= price < 0.99)) or m.get("liquidity", 0) < 1000:
            continue
        d = strategy.decide_bet(price)
        if d.side is None:
            continue
        stake = round(d.fraction * wallet.bankroll_for_sizing(), 6)
        fr = wallet.open_position(mid, m["question"], d.side, d.est_true_yes, price,
                                  round(d.est_true_yes - price, 4), stake,
                                  cfg=wallet.WalletConfig(max_bet_frac=0.05, max_exposure_frac=0.85),
                                  end_date=m.get("end_date"), event_slug=m.get("event_slug"))
        if fr.opened:
            opened += 1; held.add(mid)
            journal.record_decision(mid, m["question"], d.est_true_yes, price, round(d.est_true_yes - price, 4),
                                    fr.side, fr.stake, fr.fill_price, "same-day",
                                    "LONG (YES)" if d.side == "YES" else "SHORT (NO)", "bet",
                                    f"Same-day ({hl:.1f}h) favorite-longshot {d.reason}.")
            print(f"  [sameday] BET {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f} ({hl:.1f}h) {m['question'][:38]}")
    return opened


def run_once(use_ai=True):
    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    settle_resolved()
    close_long_dated()
    n = place_sameday(use_ai=use_ai)
    st = wallet.get_state()
    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
    print(f"[sameday] cycle done: +{n} same-day bets | open={st['n_open']} "
          f"cash=${st['cash']:.0f} equity=${st['equity']:.0f} realized=${st['realized_pnl']:+.2f}\n")


def daemon(interval=INTERVAL, use_ai=True):
    print(f"[sameday] DAEMON — same-day only, AI agents ON, every {interval/60:.0f} min. No stopping.\n")
    while True:
        try:
            run_once(use_ai=use_ai)
        except KeyboardInterrupt:
            print("[sameday] stopped."); return
        except Exception as e:
            print("[sameday] cycle error:", e)
        time.sleep(interval)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "once"
    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    if cmd == "close":
        close_long_dated()
        st = wallet.get_state()
        journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
        print(f"[sameday] after close: open={st['n_open']} cash=${st['cash']:.0f} realized=${st['realized_pnl']:+.2f}")
    elif cmd == "once":
        run_once()
    elif cmd == "daemon":
        daemon()


if __name__ == "__main__":
    main()
