"""
PRECISE same-day AI prediction pipeline — the real chain, on markets resolving TODAY:

  1. FIND   — same-day markets the AI can actually predict (liquid, tradeable, resolves <24h)
  2. GATHER — real data: GDELT news/sentiment (14d) + WhoIsSharp microstructure signals
  3. THINK  — the multi-persona swarm (qwen2.5:7b) processes that data -> probability + edge
  4. BET    — if the LLM sees an edge vs the market, place a Kelly-sized PAPER bet (data-driven,
              NOT the mechanical favorite-longshot rule)

Slow on a CPU box by design (a real multi-agent LLM forecast per market). Paper-only, $0.
Reuses loop.py's _build_enrichment / _forecast so it shares the exact swarm + data path.

    python -m harness.predict_today [--max 3] [--size 6] [--rounds 1] [--min-edge 0.03]
                                    [--max-hours 24] [--include-mechanical]
"""
from __future__ import annotations
import os, sys, time, contextlib

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, sizing, wallet, journal, challenger
from harness.loop import LoopConfig, _build_enrichment, _forecast, _days_until

# ── observability (W1: control-flow / lifecycle / classify / skips / errors) ──
# Guarded import so a broken/missing obs package can NEVER break a forecast pass.
# Every obs call below is gated on `if obs:` and obs is OBSERVATION-ONLY: it never
# changes a return value, alters logic, or raises.
try:
    from harness import obs
except Exception:
    obs = None


def _obs_run_config(cfg):
    """Effective config snapshot for run.start (observation-only)."""
    return {
        "entry": "predict_today",
        "swarm_size": getattr(cfg, "swarm_size", None),
        "rounds": getattr(cfg, "rounds", None),
        "min_edge": getattr(cfg, "min_edge", None),
        "use_gdelt": getattr(cfg, "use_gdelt", None),
        "use_signals": getattr(cfg, "use_signals", None),
        "MODEL_FAST": os.getenv("MODEL_FAST"),
        "MODEL_DEEP": os.getenv("MODEL_DEEP"),
        "DEBATE_ROUNDS": os.getenv("DEBATE_ROUNDS"),
        "LLM_PROVIDER": os.getenv("LLM_PROVIDER"),
        "guards": {
            "SKIP_MECHANICAL": SKIP_MECHANICAL,
            "MAX_SWARM_CHALLENGER_DIVERGENCE": MAX_SWARM_CHALLENGER_DIVERGENCE,
            "MIN_SWARM_CONSENSUS": MIN_SWARM_CONSENSUS,
            "MAX_GROUP_PROB_SUM": MAX_GROUP_PROB_SUM,
            "ONE_YES_PER_EVENT": ONE_YES_PER_EVENT,
        },
    }


def _obs_bankroll():
    try:
        return wallet.get_state().get("cash") or 1000.0
    except Exception:
        return 1000.0

# Set by --with-mirofish: run the FULL MiroFish crowd pipeline (builds the Zep knowledge
# graph + runs the multi-agent crowd sim + writes a report) and feed that report INTO the
# swarm LLM as context — the user's pipeline: gather -> MiroFish report -> LLM -> decide.
USE_MIROFISH = False
MF_WAIT = 1200          # seconds budget for the full MiroFish run per market (CPU is slow)

# ── betting reliability guards (tunable) — applied in predict_one() BEFORE sizing ─────
# The swarm->edge->Kelly->cap->min-edge path is unchanged; these only DECIDE WHETHER to
# let a market reach it. The bot was placing its biggest bets exactly where the swarm is
# least trustworthy (mechanical markets; swarm wildly disagreeing with the challenger;
# low internal consensus; stacked mutually-exclusive legs). Each guard below closes one.
SKIP_MECHANICAL = True                    # Guard A: never bet markets the classifier tags "mechanical"
MAX_SWARM_CHALLENGER_DIVERGENCE = 0.15    # Guard B: skip if |swarm_p - challenger_p| > this (15 pts) — unreliable swarm number
MIN_SWARM_CONSENSUS = 0.50                # Guard C: skip if the swarm's internal agreement (consensus) is below this
MAX_GROUP_PROB_SUM = 1.20                 # Guard D(b): skip a mutually-exclusive event group whose YES-probs sum above this
ONE_YES_PER_EVENT = True                  # Guard D(a): at most ONE YES (winner) bet per event group — NO/fade bets are unlimited

# Daemon idle cadence: when there's no fresh market to forecast, sleep this long instead of
# spinning every `interval`s (which flooded the log + equity_snapshots with identical rows).
IDLE_INTERVAL = 600

# ── conviction-scaled sizing: bet BIGGER when the model is genuinely sure ──────────────
# Only bets that already PASSED the guards reach here. A 0..1 conviction score (swarm vs
# challenger agreement, swarm consensus, edge magnitude, whether real data was gathered)
# scales BOTH the Kelly fraction and the per-bet cap. Max conviction -> half-Kelly at a
# CAP_MAX stake ("huge"); marginal conviction stays quarter-Kelly at 2%. Kelly still sizes
# by edge, so a tiny edge stays small even at high conviction — only a big, agreed-upon edge
# becomes a huge bet. Hard wallet exposure rails still apply on top. ALL tunable.
CONVICTION_LAM_MIN = 0.25      # quarter-Kelly floor (the previous fixed value)
CONVICTION_LAM_MAX = 0.50      # half-Kelly on a maximum-conviction bet
CONVICTION_CAP_MIN = 0.02      # 2% of bankroll floor
CONVICTION_CAP_MAX = 0.10      # up to 10% of bankroll on a maximum-conviction bet ("huge" = 5x the old cap)
CONVICTION_EDGE_FULL = 0.15    # an absolute edge this large counts as full edge-confidence


def _conviction(swarm_p, challenger_p, consensus, edge, had_data):
    """0..1 conviction from independent reliability signals. Bets that reach this already
    cleared the guards (so divergence is < threshold, consensus >= MIN, label ok)."""
    if challenger_p is not None:
        agree = 1.0 - min(1.0, abs(swarm_p - challenger_p) / max(1e-9, MAX_SWARM_CHALLENGER_DIVERGENCE))
    else:
        agree = 0.5
    cons = min(1.0, max(0.0, consensus)) if consensus is not None else 0.5
    edge_conf = min(1.0, abs(edge) / CONVICTION_EDGE_FULL)
    data = 1.0 if had_data else 0.5
    return round(0.40 * agree + 0.30 * cons + 0.20 * edge_conf + 0.10 * data, 3)


def _conviction_sizing(conviction):
    """Map conviction (0..1) -> (lambda, cap) for sizing.size_bet — bigger when surer."""
    lam = CONVICTION_LAM_MIN + conviction * (CONVICTION_LAM_MAX - CONVICTION_LAM_MIN)
    cap = CONVICTION_CAP_MIN + conviction * (CONVICTION_CAP_MAX - CONVICTION_CAP_MIN)
    return round(lam, 4), round(cap, 4)


def _mirofish_report(question: str, market_id: str, price, max_wait: int = None) -> str:
    """Run MiroFish (the multi-agent crowd, NOT the swarm LLM) on this market and return a
    text REPORT block to feed the LLM. Drives the real :5001 backend so the Zep knowledge
    graph the dashboard renders is built for THIS market; falls back to the local $0 crowd
    if the backend is down. Never raises — a failed crowd run just returns ''."""
    from harness import mirofish_signal
    max_wait = max_wait if max_wait is not None else MF_WAIT
    sid, report_md = None, ""
    print(f"\n[2.5] REPORT — MiroFish crowd sim builds a knowledge graph + debates (full, slow)…", flush=True)
    t0 = time.time()
    try:
        from harness import mirofish
        res = mirofish.forecast_market(question, base=os.getenv("MIROFISH_BASE", "http://localhost:5001"),
                                       max_wait=max_wait)
        sid = res.get("simulation_id")
        report_md = res.get("report_markdown") or res.get("report") or ""
        print(f"      MiroFish backend: stage={res.get('stage_reached')} sim={sid} "
              f"graph={res.get('graph_id')} ({time.time()-t0:.0f}s)", flush=True)
    except Exception as e:
        print(f"      MiroFish backend unavailable ({str(e)[:70]}); local crowd fallback", flush=True)
        try:
            from harness import crowd_local
            sid = crowd_local.run_local_crowd(question, rounds=int(os.getenv("CROWD_ROUNDS", "1")))
        except Exception as e2:
            print(f"      local crowd failed too ({str(e2)[:60]}) — skipping MiroFish", flush=True)
            return ""
    # distill the crowd's collective view from the posts it generated
    sig = {}
    try:
        sig = mirofish_signal.crowd_signal(sid, question)
        mirofish_signal.save_signal(market_id, sig, market_odds=price, sim_id=sid)
    except Exception as e:
        print(f"      crowd distill error: {str(e)[:60]}", flush=True)
    cp, n_posts, posts = sig.get("probability"), sig.get("n_posts", 0), sig.get("posts", [])[:6]
    if cp is None and not posts and not report_md:
        print("      MiroFish produced no usable report this market.", flush=True)
        return ""
    lines = ["[MiroFish multi-agent crowd report — independent simulated agents, NOT an LLM forecast]"]
    if cp is not None:
        lines.append(f"Crowd's implied YES probability: {cp:.0%}  (distilled from {n_posts} crowd posts)")
    if report_md:
        lines.append("Crowd report excerpt: " + " ".join(report_md.split())[:700])
    for p in posts:
        lines.append(f"- crowd voice: {p[:160]}")
    print(f"      MiroFish report ready: crowd P(YES)={cp}, {n_posts} posts → feeding to the swarm.", flush=True)
    return "\n".join(lines)


def _hours_left(m):
    d = _days_until(m.get("end_date"))
    return None if d is None else d * 24.0


def find_candidates(max_hours=24.0, max_n=3, include_mechanical=False):
    """Same-day markets the AI can predict: resolving within the window (TODAY, not days out),
    liquid, tradeable price, not already held. Opinion (forecastable) markets first."""
    held = {p["market_id"] for p in wallet.get_open_positions()}
    cands = []
    for m in gamma.fetch_markets_ending_within(max_hours, limit=200):
        mid = m.get("market_id")
        if not mid or mid in held:
            if obs and mid:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, "held")
            continue
        hl = _hours_left(m)
        if hl is None or hl < 0.5 or hl > max_hours:        # resolves today, not a couple days out
            if obs:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, "hours")
            continue
        price = gamma.yes_price(m)
        if price is None or not (0.02 < price < 0.98):
            if obs:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, "untradeable_price")
            continue
        if not classifier.passes_liquidity_floor(m):
            if obs:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, "illiquid")
            continue
        cls = classifier.tag_market(m)
        m["_label"] = cls.label
        m["_hl"] = hl
        m["_price"] = price
        if obs:
            obs.hooks.on_classify(mid, m.get("question"), cls.label,
                                  getattr(cls, "signals", None),
                                  cls.label != "mechanical", "candidate")
        cands.append(m)
    # opinion (forecastable) first; then most-liquid/substantial markets; then soonest
    cands.sort(key=lambda m: (m["_label"] != "opinion", -(m.get("liquidity") or 0.0), m["_hl"]))
    if not include_mechanical:
        # Forecastable = anything NOT clearly mechanical. The regex classifier tags genuine
        # news/geopolitical markets (Iran peace deal, Israel airspace) as "unknown" — they
        # are exactly what the swarm should forecast, so keep opinion + unknown and only drop
        # the clearly-mechanical sports/crypto/weather. (sort above already puts opinion first.)
        forecastable = [c for c in cands if c["_label"] != "mechanical"]
        if forecastable:
            cands = forecastable
    return cands[:max_n]


def _event_group_legs(mid, group_key):
    """Other OPEN positions in the same mutually-exclusive event group (by event_slug,
    falling back to the market id when a market has no event). Used by Guard D."""
    try:
        return [o for o in wallet.get_open_positions()
                if (o.get("event_slug") or o.get("market_id")) == group_key
                and o.get("market_id") != mid]
    except Exception:
        return []


def _betting_guards(label, swarm_p, challenger_p, consensus, group_legs, price):
    """Pure reliability gate evaluated BEFORE sizing. Returns (ok, reason). Pure function so
    it can be replayed deterministically (see replay_guards.py) — no I/O, no LLM. `group_legs`
    are the OPEN positions in the same mutually-exclusive event (each has 'side' + 'model_p')."""
    # Guard A — clearly-mechanical markets (sports/price/count/release-date): the swarm has no edge.
    if SKIP_MECHANICAL and label == "mechanical":
        return False, "mechanical"
    # Guard B (the main one) — big swarm/challenger disagreement = untrustworthy swarm number.
    if challenger_p is not None and abs(swarm_p - challenger_p) > MAX_SWARM_CHALLENGER_DIVERGENCE:
        return False, f"swarm/challenger divergence {abs(swarm_p - challenger_p):.2f}"
    # Guard C — the swarm doesn't even agree with itself.
    if consensus is not None and consensus < MIN_SWARM_CONSENSUS:
        return False, f"consensus {consensus:.2f} < {MIN_SWARM_CONSENSUS:.2f}"
    # Guard D — mutually-exclusive event coherence. We CAN bet several legs of one event, but a
    # mutually-exclusive event has exactly ONE winner: bet the side the swarm believes (YES if
    # swarm_p>price else NO), allow UNLIMITED NO (fade the losers — a NO wins when that leg loses),
    # but at most ONE YES (you can only back one winner). Plus an incoherence cap: the YES-probs
    # of the YES legs must not sum past MAX_GROUP_PROB_SUM (the swarm contradicting itself).
    side = "YES" if (price is not None and swarm_p > price) else "NO"
    held_yes = [o for o in group_legs if str(o.get("side") or "").upper() == "YES"]
    if ONE_YES_PER_EVENT and side == "YES" and held_yes:
        return False, "already hold the YES (winner) leg in this event"
    yes_sum = sum((o.get("model_p") or 0.0) for o in held_yes) + (swarm_p if side == "YES" else 0.0)
    if yes_sum > MAX_GROUP_PROB_SUM:
        return False, f"incoherent group (YES-prob sum {yes_sum:.2f})"
    return True, "ok"


def _skip(mid, q, reason, *, p=None, price=None):
    """Log a guard skip in the existing '[4/4] BET' transcript style AND record it to the
    decisions table so it shows in the dashboard transcript. Returns False (no bet placed)."""
    print(f"      DECISION: NO BET — {reason}")
    if obs:
        obs.hooks.on_trade_skip(
            forecast_id=(obs.current().get("forecast_id") if obs else None),
            reason=reason,
            inputs={"market_id": mid, "question": q, "p": p, "price": price, "layer": "guard"},
        )
    try:
        why = f"Guard skip: {reason}."
        if p is not None and price is not None:
            why += f" (swarm {p:.0%} vs market {price:.0%})"
        journal.record_decision(mid, q, p, price, None, None, 0.0, None, "", "guard", "no_bet", why)
    except Exception:
        pass
    return False


def predict_one(m, cfg):
    q, price, mid, hl = m["question"], m["_price"], m["market_id"], m["_hl"]
    label = m.get("_label", "unknown")
    group_key = m.get("event_slug") or mid
    with contextlib.ExitStack() as _es:
        if obs:
            _es.enter_context(obs.market_ctx(market_id=mid, question=q))
            # forecast_id spans forecast->size->trade so sizing.decision/trade.* chain to the forecast
            _es.enter_context(obs.forecast_ctx(forecast_id=obs.mint('f')))
        print("=" * 78)
        print(f"MARKET: {q}")
        print(f"  resolves in {hl:.1f}h (today) · market YES {price:.0%} · class={label} · {mid[:16]}…")

        # ── cheap pre-forecast guard: skip clearly-mechanical markets BEFORE the slow
        #    GATHER/MiroFish/THINK. Event-coherence now needs the forecast (we FORECAST every leg
        #    of an event so we can bet the winning side on each), so it runs after THINK. ──
        if SKIP_MECHANICAL and label == "mechanical":
            return _skip(mid, q, "mechanical", price=price)

        # 2 — GATHER
        print("\n[2/4] GATHER — pulling GDELT news/sentiment + microstructure signals…", flush=True)
        t0 = time.time()
        enr = _build_enrichment(m, cfg)
        print(f"      gathered {len(enr)} chars of real context in {time.time()-t0:.0f}s")
        for line in (enr.splitlines()[:8] if enr else ["(no external context found for this market)"]):
            print("      | " + line[:100])

        # 2.5 — REPORT (MiroFish + crowd agents build a report; it is fed to the LLM, not run as one)
        if USE_MIROFISH:
            mf = _mirofish_report(q, mid, price)
            if mf:
                enr = (enr + "\n\n" + mf) if enr else mf

        # 3 — THINK (the swarm LLM processes the gathered data + the crowd report)
        print(f"\n[3/4] THINK — {cfg.swarm_size}-persona swarm forecasting WITH that data (slow on CPU)…", flush=True)
        t0 = time.time()
        p, meta = _forecast(m, price, cfg, enr)
        print(f"      swarm: {p:.1%} YES vs market {price:.0%} · regime={meta.get('regime')} "
              f"· consensus={meta.get('consensus')} · {time.time()-t0:.0f}s")
        bp = None
        try:
            bp = challenger.single_llm_forecast(q, price, enr)
            if bp is not None:
                challenger.save_baseline(mid, q, bp, price)
                print(f"      single-LLM challenger (A/B): {bp:.1%} YES")
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="predict_today.predict_one.challenger", exc=e, action="skip")

        # ── post-forecast reliability guards B/C/D(b): need swarm p, challenger bp, consensus.
        #    (group re-read here too — a sibling leg may have been bet during the long forecast.) ──
        ok, reason = _betting_guards(label, p, bp, meta.get("consensus"),
                                     _event_group_legs(mid, group_key), price)
        if not ok:
            print("\n[4/4] BET — reliability guard…", flush=True)
            return _skip(mid, q, reason, p=p, price=price)

        # 4 — BET — conviction-scaled stake (bigger when surer); size_bet mechanics UNCHANGED
        print("\n[4/4] BET — sizing on the swarm's edge vs the market…", flush=True)
        conv = _conviction(p, bp, meta.get("consensus"), p - price, bool(enr))
        lam, cap = _conviction_sizing(conv)
        print(f"      conviction {conv:.2f} → {lam:g}x Kelly, cap {cap:.0%}")
        sz = sizing.size_bet(p, price, wallet.bankroll_for_sizing(), lam=lam, cap=cap, min_edge=cfg.min_edge)
        print(f"      edge {sz.edge:+.1%} → {sz.reason}")
        regime = meta.get("regime", "")
        sig = "LONG (YES)" if sz.edge > 0 else "SHORT (NO)"
        if sz.side is None:
            why = (f"AI pipeline: gathered GDELT+signals, swarm {p:.0%} vs market {price:.0%} "
                   f"(edge {sz.edge:+.1%}). No bet — {sz.reason}.")
            journal.record_decision(mid, q, p, price, sz.edge, None, 0.0, None, regime, "no edge", "no_bet", why)
            print("      DECISION: NO BET — edge below threshold")
            if obs:
                obs.hooks.on_trade_skip(
                    forecast_id=(obs.current().get("forecast_id") if obs else None),
                    reason="edge_below_threshold",
                    inputs={"market_id": mid, "p": p, "price": price, "edge": sz.edge, "layer": "sizer"},
                )
            return False
        # Give the precise AI pipeline its own exposure headroom so the daemon's price-rule
        # positions don't crowd out its (small, Kelly-capped) data-driven bets.
        fr = wallet.open_position(mid, q, sz.side, p, price, sz.edge, sz.stake,
                                  cfg=wallet.WalletConfig(max_bet_frac=CONVICTION_CAP_MAX, max_exposure_frac=0.95),
                                  end_date=m.get("end_date"), event_slug=m.get("event_slug"))
        if fr.opened:
            why = (f"AI pipeline (data-driven): gathered GDELT news + microstructure, the {cfg.swarm_size}-persona "
                   f"swarm forecast {p:.0%} vs market {price:.0%} → {sz.edge:+.1%} edge. Bought {fr.side} @ "
                   f"{fr.fill_price:.3f} with ${fr.stake:.2f} ({sz.reason}). Resolves in {hl:.1f}h (today).")
            journal.record_decision(mid, q, p, price, sz.edge, fr.side, fr.stake, fr.fill_price, regime, sig, "bet", why)
            print(f"      BET PLACED: {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f} — resolves in {hl:.1f}h")
            return True
        print(f"      bet rejected by wallet: {fr.reason}")
        if obs:
            obs.hooks.on_trade_skip(
                forecast_id=(obs.current().get("forecast_id") if obs else None),
                reason=f"wallet_rejected: {fr.reason}",
                inputs={"market_id": mid, "side": sz.side, "stake": sz.stake, "layer": "wallet"},
            )
        return False


def _settle():
    """Book resolved/cashed-out bets (uses gamma.resolution_outcome, incl. UMA-proposed snap)."""
    from harness import loop
    try:
        return loop.settle_resolved(loop.LoopConfig())
    except Exception as e:
        if obs:
            obs.hooks.on_error(where="predict_today._settle", exc=e, action="skip")
        print("[predict] settle error:", e)
        return []


def _heartbeat(st):
    import json as _json
    try:
        with open(os.getenv("HARNESS_HEARTBEAT", ".heartbeat.json"), "w") as hb:
            _json.dump({"ts": time.time(), "cycle": {"phase": "predict_today", "open": st["n_open"],
                       "equity": round(st["equity"], 2), "realized_pnl": round(st["realized_pnl"], 2)}}, hb)
    except Exception:
        pass


def run_once(cfg, max_n=3, max_hours=24.0, include_mech=False):
    with contextlib.ExitStack() as _es:
        if obs:
            _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
            obs.hooks.on_run_start(_obs_run_config(cfg), _obs_bankroll())
        print("[1/4] FIND — scanning same-day markets the AI can predict…", flush=True)
        cands = find_candidates(max_hours=max_hours, max_n=max_n, include_mechanical=include_mech)
        if not cands:
            print("      No same-day markets fit (resolving today + liquid + tradeable).")
            if obs:
                obs.hooks.on_run_end({"bets": 0, "candidates": 0})
            return 0
        print(f"      Picked {len(cands)} market(s) resolving today:")
        for c in cands:
            print(f"        - [{c['_label']}] {c['_hl']:.1f}h - {c['question'][:62]}")
        bets = 0
        for m in cands:
            try:
                bets += 1 if predict_one(m, cfg) else 0
            except Exception as e:
                if obs:
                    obs.hooks.on_error(where="predict_today.run_once", exc=e, action="skip")
                print(f"      ERROR on this market: {type(e).__name__}: {e}")
        st = wallet.get_state()
        journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
        print("\n" + "=" * 78)
        print(f"DONE — {bets} data-driven bet(s) placed. wallet: cash ${st['cash']:.2f} · "
              f"equity ${st['equity']:.2f} · realized ${st['realized_pnl']:+.2f} · {st['n_open']} open")
        if obs:
            obs.hooks.on_run_end({"bets": bets, "open": st["n_open"], "candidates": len(cands)})
        return bets


def daemon(cfg, max_hours=24.0, interval=60, include_mech=False):
    """Continuous PRECISE pipeline: settle -> ONE deep find/gather/think/bet per cycle -> repeat.
    One forecast per cycle because each is slow (minutes) on the CPU. No price rule."""
    done: set[str] = set()
    last_key = None
    idle_interval = max(interval, IDLE_INTERVAL)
    print(f"\n  PRECISE AI DAEMON — find->gather->think->bet, same-day only, no price rule. No stopping.\n", flush=True)
    while True:
        cands = []
        worked = False
        try:
            with contextlib.ExitStack() as _es:
                if obs:
                    _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
                    obs.hooks.on_run_start(_obs_run_config(cfg), _obs_bankroll())
                worked = bool(_settle())              # a settlement is real work (P&L moved)
                cands = [c for c in find_candidates(max_hours=max_hours, max_n=12, include_mechanical=include_mech)
                         if c["market_id"] not in done]
                if not cands:
                    print("[predict] no fresh same-day market to forecast right now — waiting…", flush=True)
                else:
                    m = cands[0]                      # one deep forecast per cycle (slow)
                    done.add(m["market_id"])
                    print("=" * 78)
                    predict_one(m, cfg)
                    worked = True
                st = wallet.get_state()
                # Only snapshot/log when something actually changed — stops the idle spin from
                # flooding equity_snapshots with identical rows (which flat-lined the P&L chart).
                key = (round(st["equity"], 2), round(st["realized_pnl"], 2), st["n_open"])
                if worked or key != last_key:
                    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
                    print(f"[predict] cycle done · open={st['n_open']} equity=${st['equity']:.0f} "
                          f"realized=${st['realized_pnl']:+.2f}\n", flush=True)
                    last_key = key
                _heartbeat(st)
                if obs:
                    obs.hooks.on_run_end({"bets": (1 if worked else 0), "open": st["n_open"]})
        except KeyboardInterrupt:
            print("[predict] stopped."); return
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="predict_today.daemon", exc=e, action="retry")
            print("[predict] cycle error:", e, flush=True)
        time.sleep(interval if cands else idle_interval)   # don't spin when idle


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv and not argv[0].startswith("--") else "once"
    max_n, size, rounds, min_edge, max_hours, include_mech, interval = 3, 6, 1, 0.03, 24.0, False, 60
    global USE_MIROFISH, MF_WAIT
    for i, a in enumerate(argv):
        if a == "--max": max_n = int(argv[i + 1])
        elif a == "--size": size = int(argv[i + 1])
        elif a == "--rounds": rounds = int(argv[i + 1])
        elif a == "--min-edge": min_edge = float(argv[i + 1])
        elif a == "--max-hours": max_hours = float(argv[i + 1])
        elif a == "--interval": interval = int(argv[i + 1])
        elif a == "--include-mechanical": include_mech = True
        elif a == "--with-mirofish": USE_MIROFISH = True
        elif a == "--mf-wait": MF_WAIT = int(argv[i + 1])

    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    cfg = LoopConfig(swarm_size=size, rounds=rounds, min_edge=min_edge,
                     use_gdelt=True, use_signals=True, challenger=True)

    if cmd == "daemon":
        daemon(cfg, max_hours=max_hours, interval=interval, include_mech=include_mech)
    else:
        print(f"\n  PRECISE same-day AI pipeline — find -> gather -> think -> bet (today only, <{max_hours:.0f}h)\n")
        run_once(cfg, max_n=max_n, max_hours=max_hours, include_mech=include_mech)


if __name__ == "__main__":
    main()
