"""
Forecast ONE specific near-term market and place a paper bet AFTER processing.

Honors the processing-time constraint: the swarm forecast is slow (~10-25 min on
CPU), so AFTER it finishes we RE-FETCH the market and only bet if it's still open
with time left — the slow forecast never lands a bet on an already-resolved market.
Stores the market's end_date so the dashboard shows a live resolution countdown.

    python -m harness.place_bet "<question substring>" [--size 12] [--rounds 1]
"""
from __future__ import annotations
import os, sys, time
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, sizing, wallet, journal, challenger
from harness.loop import _build_enrichment, LoopConfig


def _hours_left(m) -> float | None:
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


def _find(needle: str):
    for m in gamma.fetch_active_markets(limit=1500):
        if needle.lower() in (m.get("question") or "").lower():
            return m
    return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    needle = argv[0] if argv else "ten million Switzerland"
    size = 12
    rounds = 1
    for i, a in enumerate(argv):
        if a == "--size" and i + 1 < len(argv): size = int(argv[i + 1])
        if a == "--rounds" and i + 1 < len(argv): rounds = int(argv[i + 1])

    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal(); challenger.init_baseline_db()

    m = _find(needle)
    if not m:
        print(f"[bet] market matching {needle!r} not found"); return
    mid, q = m["market_id"], m["question"]
    price, end = gamma.yes_price(m), m.get("end_date")
    hl = _hours_left(m)
    print(f"[bet] TARGET: {q}")
    print(f"[bet] market_id={mid[:20]}…  yes_price={price:.3f}  resolves in {hl:.1f}h  (end {end})")
    if price is None or not (0.02 < price < 0.98):
        print("[bet] price not tradeable — abort"); return

    # ── FORECAST (slow) ──
    enr = _build_enrichment(m, LoopConfig())
    os.environ["DEBATE_ROUNDS"] = str(rounds)
    from core.swarm import Swarm
    from agents.personas import build_swarm
    t0 = time.time()
    print(f"[bet] forecasting with {size}-agent swarm (this is the slow part)…")
    res = Swarm(agents=build_swarm(size)).forecast(q, market_odds=price, market_id=mid, extra_context=enr)
    p = float(res["probability"])
    rg = res.get("regime"); regime = rg.get("regime") if isinstance(rg, dict) else (str(rg) if rg else "")
    print(f"[bet] forecast done in {(time.time()-t0)/60:.1f} min -> model_p={p:.3f}  (regime {regime})")

    bp = challenger.single_llm_forecast(q, price, enr)
    if bp is not None:
        challenger.save_baseline(mid, q, bp, price); print(f"[bet] challenger single-LLM p={bp:.3f}")

    # ── RE-CHECK the market is still open AFTER the slow forecast ──
    m2 = _find(needle)
    if not m2:
        print("[bet] market is CLOSED/gone after processing — NOT betting (this is the guard you asked for)"); return
    price2, end2, hl2 = gamma.yes_price(m2), m2.get("end_date"), _hours_left(m2)
    if hl2 is not None and hl2 < 0.1:
        print(f"[bet] only {hl2:.2f}h left after processing — too close, NOT betting"); return
    print(f"[bet] re-check OK: still open, yes_price={price2:.3f}, {hl2:.1f}h left")

    # ── SIZE + PLACE ──
    sz = sizing.size_bet(p, price2, wallet.bankroll_for_sizing())
    sig = "LONG (YES)" if sz.edge > 0 else "SHORT (NO)"
    if sz.side is None:
        why = f"Swarm {p:.0%} vs market {price2:.0%} (edge {sz.edge:+.1%}). No bet — {sz.reason}."
        journal.record_decision(mid, q, p, price2, sz.edge, None, 0.0, None, regime, "no edge", "no_bet", why)
        print(f"[bet] NO BET: {sz.reason}")
    else:
        # Plan 3: route this manual bet through the SHARED safety stack
        # (swarm-health → EV → risk → bankroll → exposure) instead of opening directly.
        from harness import safe_bet
        fmeta = {
            "consensus": res.get("consensus_score"), "consensus_status": res.get("consensus_status"),
            "allow_bet": res.get("allow_bet"), "aborted": res.get("aborted"),
            "degraded": res.get("degraded"), "method": res.get("method"),
            "n_agents_succeeded": res.get("n_agents_succeeded"),
            "n_agents_requested": res.get("n_agents_requested"),
            "degradation_reason": res.get("degradation_reason"),
        }
        out = safe_bet.open_position_if_safe(source="place_bet", market=m2, side=sz.side,
                                             probability=p, price=price2, stake=sz.stake,
                                             confidence=res.get("consensus_score"), forecast_meta=fmeta)
        print(f"[bet] {'OPENED' if out['opened'] else 'NO BET'}: {sz.side} ${sz.stake:.2f} — {out['reason']}")

    st = wallet.get_state()
    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
    print(f"[bet] DONE. wallet: cash=${st['cash']:.2f} equity=${st['equity']:.2f} open={st['n_open']}")


if __name__ == "__main__":
    main()
