"""
MiroFish QUICK — run the crowd simulation on a market only until it generates posts,
then distill the crowd probability (skip the slow full report pipeline that can't
finish on CPU). Saves a MiroFish forecast that shows on the dashboard.

    python -m harness.mirofish_quick "<question substring>"
"""
from __future__ import annotations
import os, sys, time

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, mirofish, mirofish_signal


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    needle = argv[0] if argv else "ten million Switzerland"
    market = None
    for m in gamma.fetch_active_markets(limit=1500):
        if needle.lower() in (m.get("question") or "").lower():
            market = m; break
    if not market:
        print(f"[mf-quick] market matching {needle!r} not found"); return
    q, mid, price = market["question"], market["market_id"], gamma.yes_price(market)
    print(f"[mf-quick] MiroFish crowd sim on: {q}  (market {price:.3f})", flush=True)

    # drive graph -> personas -> start (returns at sim_running on timeout — that's fine,
    # the crowd generates posts while running)
    res = mirofish.forecast_market(q, max_wait=1500, verbose=True)
    sid = res.get("simulation_id")
    print(f"[mf-quick] sim={sid} stage={res.get('stage_reached')}", flush=True)
    if not sid:
        print("[mf-quick] no simulation id — abort"); return

    # wait for the crowd to produce posts, then distill
    sig = {"n_posts": 0}
    for _ in range(12):
        sig = mirofish_signal.crowd_signal(sid, q)
        print(f"[mf-quick] posts so far: {sig.get('n_posts', 0)}", flush=True)
        if sig.get("n_posts", 0) >= 2 and sig.get("probability") is not None:
            break
        time.sleep(45)
    sig = mirofish_signal.crowd_signal(sid, q)
    mirofish_signal.save_signal(mid, sig, market_odds=price, sim_id=sid)
    print(f"[mf-quick] DONE -> MiroFish crowd probability={sig.get('probability')} "
          f"from {sig.get('n_posts')} posts (market {price:.3f})", flush=True)


if __name__ == "__main__":
    main()
