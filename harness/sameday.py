"""
Continuous SAME-DAY trading daemon. Keeps ONLY bets resolving within ~24h, and
never stops searching.

Each cycle: settle resolved -> cash out any bet resolving beyond today -> find
same-day OPINION markets -> run the AI swarm (PolySwarm + single-LLM challenger, and a
best-effort MiroFish crowd-sim) on them -> place a bet ONLY where the swarm sees a real
edge vs the market, at most one position per event. Loops until killed.

The bet is driven by the swarm forecast, not a hardcoded price assumption, and mechanical
markets (sports, crypto, weather) are skipped. Without the AI, no bets are placed.

    python -m harness.sameday close     # one-off: cash out every bet resolving >24h
    python -m harness.sameday once      # one cycle
    python -m harness.sameday daemon    # continuous (no stopping)
"""
from __future__ import annotations
import os, subprocess, sys, time, contextlib
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, wallet, journal, sizing, challenger, scanner
# P6 — same skill-weighted blend + gated calibration + versioned record as predict_today.
# All three are HARD cold-start passthroughs (see _ai_scout): final_p == swarm p today.
from harness import calibration_apply, forecast_versions
from harness import forecaster_weights as forecaster_weights_mod

# ── observability (W1: control-flow / lifecycle / classify / skips / errors) ──
# Guarded import: a broken/missing obs package can NEVER break a cycle. Every obs
# call is gated on `if obs:` and is OBSERVATION-ONLY (no return/logic/exception change).
try:
    from harness import obs
except Exception:
    obs = None

MAX_HOURS = 24.0          # "same day" window
INTERVAL = 1200           # 20 min between cycles
MIN_EDGE = 0.04           # only bet when the SWARM disagrees with the market by >= 4 points
MAX_SCOUT = 12            # cap swarm forecasts per cycle (each one is CPU-heavy)
_mf_launched: set[str] = set()   # market_ids we've kicked a MiroFish sim for


def _obs_run_config():
    """Effective config snapshot for run.start (observation-only)."""
    return {
        "entry": "sameday",
        "MAX_HOURS": MAX_HOURS,
        "INTERVAL": INTERVAL,
        "MIN_EDGE": MIN_EDGE,
        "MAX_SCOUT": MAX_SCOUT,
        "swarm_size": 5,
        "rounds": 1,
        "MODEL_FAST": os.getenv("MODEL_FAST"),
        "MODEL_DEEP": os.getenv("MODEL_DEEP"),
        "DEBATE_ROUNDS": os.getenv("DEBATE_ROUNDS"),
        "LLM_PROVIDER": os.getenv("LLM_PROVIDER"),
    }


def _obs_bankroll():
    try:
        return wallet.get_state().get("cash") or 1000.0
    except Exception:
        return 1000.0


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
        if obs:
            obs.hooks.on_error(where="sameday.settle_resolved", exc=e, action="skip")
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
            if obs:
                obs.hooks.on_error(where="sameday.close_long_dated", exc=e, action="skip")
            print("[sameday] close error", str(p.get("market_id"))[:14], e)
    if closed:
        print(f"[sameday] cashed out {closed} long-dated bets (>{max_hours:.0f}h), realized ${pnl:+.2f}")
    return closed


def _record_forecast_version(mid, q, swarm_p, ens, blended, final_p, weights, cal):
    """Best-effort append of the full P6 forecast context to forecast_versions (mirror of
    predict_today._record_forecast_version). PASSIVE audit only — never feeds back into a
    decision and never raises into the cycle."""
    try:
        fid = obs.current().get("forecast_id") if obs else None
        forecast_versions.record_forecast_version(
            forecast_id=fid, market_id=mid, question=q, swarm_p=swarm_p,
            challenger_models=(ens or {}).get("models") or [],
            challenger_ps=(ens or {}).get("probs") or [],
            blended_p=blended, calibrated_p=final_p, weights=weights,
            calibration_method=(cal or {}).get("method"),
            n_calib_history=(cal or {}).get("n_history"),
        )
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="sameday._record_forecast_version", exc=e, action="skip")
            except Exception:
                pass


def _ai_scout(market, price, evidence_text: str = ""):
    """Run the AI agents (PolySwarm + challenger ENSEMBLE) on a same-day OPINION market.

    Plan 5: ``evidence_text`` is the SAME canonical evidence-pack text predict_today
    feeds its forecast; it is passed to BOTH the swarm (extra_context) and the challenger
    ensemble (extra_context) so same-day no longer forecasts blind. The caller builds the
    pack BEFORE calling this.

    Returns ``(swarm_p, challenger_bp, consensus, final_p, health)`` where ``final_p`` is
    the P6 DECISION probability and ``health`` is the Plan-2 swarm-health dict plus
    ``evidence_used`` and ``mirofish_used`` (the MiroFish sim is fire-and-forget and is
    NOT read into this decision — mirofish_used is always False here)."""
    mid = market["market_id"]
    # ONE forecast per market: re-scouting the same market every cycle wastes minutes
    # of CPU and double-counts it toward the gate (and cross-joins the A/B panel).
    # Skip if we already have an unresolved swarm forecast for it.
    try:
        from core.calibration import get_open_market_ids
        if mid in get_open_market_ids():
            print(f"[sameday] already forecast — skip re-scout: {market['question'][:44]}")
            return None, None, None, None, None
    except Exception as e:
        if obs:
            obs.hooks.on_error(where="sameday._ai_scout.open_ids", exc=e, action="skip")
    # MiroFish crowd-sim (slow; fire-and-forget). HONESTY (Plan 5): this is launched but
    # its result is NOT read into the same-day decision — it is mirofish_launched_not_used.
    if mid not in _mf_launched:
        _mf_launched.add(mid)
        try:
            subprocess.Popen([sys.executable, "-m", "harness.mirofish_quick", market["question"][:60]],
                             cwd=os.path.dirname(os.path.dirname(__file__)),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[sameday] 🐟 MiroFish crowd-sim launched (mirofish_launched_not_used — "
                  f"NOT read into this decision): {market['question'][:40]}")
            if obs:
                try:
                    obs.hooks.on_trade_skip(
                        forecast_id=(obs.current().get("forecast_id") if obs else None),
                        reason="mirofish_launched_not_used",
                        inputs={"market_id": mid, "mirofish_used": False, "layer": "mirofish"})
                except Exception:
                    pass
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="sameday._ai_scout.mirofish_launch", exc=e, action="skip")
    # PolySwarm + challenger ENSEMBLE (the AI agents) — BOTH see the evidence pack (Plan 5).
    try:
        from core.swarm import Swarm
        from agents.personas import build_swarm
        os.environ["DEBATE_ROUNDS"] = "1"
        res = Swarm(agents=build_swarm(5)).forecast(market["question"], market_odds=price,
                                                    market_id=mid, extra_context=evidence_text)
        sp = float(res["probability"])
        cons = res.get("consensus_score")          # swarm internal agreement (for Guard C)
        # P6 step 1: challenger ENSEMBLE mean (default 1-model roster == today's single bp),
        # now with the SAME evidence context the swarm saw (Plan 5 parity).
        ens = challenger.ensemble_forecast(market["question"], price, evidence_text)
        bp = ens.get("mean")
        if bp is not None:
            challenger.save_baseline(mid, market["question"], bp, price)
        # P6 steps 2+3: weighted blend (swarm vs challenger by realized Brier) → gated
        # calibration → the decision probability final_p. Cold-start: weights are
        # {"swarm":1,"challenger":0} and calibration is passthrough, so final_p == sp EXACTLY.
        weights = forecaster_weights_mod.forecaster_weights()
        blended = forecaster_weights_mod.blend_forecasters(sp, bp, weights)
        cal = calibration_apply.apply_calibration(blended)
        final_p = cal["calibrated_p"]
        # P6 step 5: stash the full forecast context (passive audit).
        _record_forecast_version(mid, market["question"], sp, ens, blended, final_p, weights, cal)
        extra = f" | decision {final_p:.0%}" if final_p != sp else ""
        print(f"[sameday] 🤖 swarm {sp:.0%} | challenger {bp if bp is not None else '-'} | "
              f"market {price:.0%}{extra}")
        # Plan 2: surface the swarm-health metadata so place_sameday can block a
        # degraded / aborted / under-strength swarm BEFORE sizing/betting.
        health = {
            "consensus": res.get("consensus_score"),
            "consensus_status": res.get("consensus_status"),
            "aborted": res.get("aborted"), "degraded": res.get("degraded"),
            "allow_bet": res.get("allow_bet"), "method": res.get("method"),
            "n_agents_requested": res.get("n_agents_requested"),
            "n_agents_succeeded": res.get("n_agents_succeeded"),
            "n_agents_failed": res.get("n_agents_failed"),
            "degradation_reason": res.get("degradation_reason"),
            # Plan 5 honesty: the forecast saw the evidence pack; MiroFish was launch-only.
            "evidence_used": bool(evidence_text),
            "mirofish_used": False,
        }
        return sp, bp, cons, final_p, health
    except Exception as e:
        if obs:
            obs.hooks.on_error(where="sameday._ai_scout", exc=e, action="skip")
        print("[sameday] AI scout error:", e)
        return None, None, None, None, None


def _sd_skip(mid, q, reason, p=None, price=None, layer="guard"):
    """Record a sameday no-bet decision EVERYWHERE predict_today does: print + obs
    trade.skip + journal.decisions — so the dashboard's decisions transcript shows
    why the live sameday daemon declined (audit #5). Returns False."""
    print(f"  [sameday] DECISION: NO BET — {reason}")
    if obs:
        try:
            obs.hooks.on_trade_skip(
                forecast_id=(obs.current().get("forecast_id") if obs else None),
                reason=reason, inputs={"market_id": mid, "question": q, "p": p, "price": price, "layer": layer})
        except Exception:
            pass
    try:
        journal.record_decision(mid, q, p, price, None, None, 0.0, None, "", layer, "no_bet",
                                f"Guard skip: {reason}.")
    except Exception:
        pass
    return False


def place_sameday(max_new=6, use_ai=True, max_scout=MAX_SCOUT, min_edge=MIN_EDGE):
    """Place same-day paper bets DRIVEN BY THE SWARM forecast (not a hardcoded edge).

    Per market we: (1) keep only genuine OPINION markets — mechanical ones (sports
    scores, crypto/price levels, weather, view counts) are skipped; (2) allow at most ONE
    position per event, so mutually-exclusive families (every exact score, every Nobel
    candidate, each party's 'most seats') can't be stacked; (3) run the AI swarm to get a
    probability; (4) bet only when the swarm disagrees with the market by >= min_edge,
    sized with fractional Kelly + caps. Without the AI no bets are placed: the decision now
    requires a real forecast, not "assume every longshot is 3 cents overpriced."
    """
    opens = wallet.get_open_positions()
    held = {p["market_id"] for p in opens}
    # We now allow MORE than one bet per event, but only ONE YES (one winner) and unlimited NO
    # (fade the losers). Track the held YES legs per event (value = sum of their model_p, for the
    # incoherence cap). NO legs don't constrain anything.
    yes_events: dict = {}
    for o in opens:
        if str(o.get("side") or "").upper() == "YES" and o.get("event_slug"):
            yes_events[o["event_slug"]] = yes_events.get(o["event_slug"], 0.0) + (o.get("model_p") or 0.0)
    ms = gamma.fetch_markets_ending_within(MAX_HOURS, limit=150)
    ms.sort(key=lambda m: (_hours_left(m.get("end_date")) if _hours_left(m.get("end_date")) is not None else 1e9))
    opened = scouted = 0
    for m in ms:
        if opened >= max_new or scouted >= max_scout:
            break
        mid, q = m["market_id"], m["question"]
        try:
            with contextlib.ExitStack() as _es:
                if obs:
                    _es.enter_context(obs.market_ctx(market_id=mid, question=q))
                    _es.enter_context(obs.forecast_ctx(forecast_id=obs.mint('f')))
                if mid in held:
                    continue
                hl = _hours_left(m.get("end_date"))
                if hl is None or hl < 0.3:
                    continue
                price = gamma.yes_price(m)
                if price is None or not (0.02 <= price <= 0.98) or m.get("liquidity", 0) < 1000:
                    continue
                # (1) OPINION markets only — the swarm forecasts crowd-driven outcomes, not
                #     sports/crypto/weather (those are MECHANICAL and get skipped here).
                _cls = classifier.tag_market(m, use_llm=False)
                if _cls.label != "opinion":
                    # mechanical/classifier skip — recorded via obs.on_classify (parity with
                    # predict_today's loop; NOT journaled, to avoid flooding decisions each cycle).
                    if obs:
                        obs.hooks.on_classify(mid, q, _cls.label, getattr(_cls, "signals", None),
                                              False, "mechanical_not_opinion")
                    continue
                # (1b) Plan 5 STALE/QUALITY parity — skip a stale book BEFORE the slow forecast
                #      using the SAME scanner.is_stale predict_today's market-quality guard uses.
                #      If staleness cannot even be evaluated -> no bet (never forecast on unknown).
                try:
                    _stale, _why = scanner.is_stale(m)
                except Exception as e:
                    if obs:
                        obs.hooks.on_error(where="sameday.is_stale", exc=e, action="skip", context={"market_id": mid})
                    _sd_skip(mid, q, "sameday_market_quality_unknown_no_bet", price=price, layer="stale")
                    continue
                if _stale:
                    _sd_skip(mid, q, f"sameday_stale_market_no_bet ({_why})", price=price, layer="stale")
                    continue
                # (2) event coherence is checked AFTER the forecast now (so we can bet the winning side
                #     per leg): one YES per event, NO/fade unlimited. ev is defined here for that check.
                ev = m.get("event_slug") or mid
                if not use_ai:
                    continue
                # (3) Plan 5 EVIDENCE PARITY — build the SAME canonical pack predict_today uses
                #     BEFORE forecasting, so the swarm + challenger SEE it. A build ERROR -> no bet
                #     (never forecast blind). A built-but-thin pack still forecasts; the evidence
                #     GUARD below then gates the BET — matching predict_today's order.
                from harness import loop as _loop
                try:
                    pack = _loop.build_pack(m, _loop.LoopConfig())
                    evidence_text = (pack.text or "") if pack else ""
                except Exception as e:
                    pack = None
                    if obs:
                        obs.hooks.on_error(where="sameday.build_pack", exc=e, action="skip", context={"market_id": mid})
                    _sd_skip(mid, q, "sameday_evidence_build_error_no_bet", price=price, layer="evidence")
                    continue
                # (4) the SWARM + challenger forecast (now EVIDENCE-AWARE) drives the bet.
                print(f"[sameday] AI scouting ({hl:.1f}h, evidence {len(evidence_text)} chars): {q[:44]}")
                scouted += 1
                # p = RAW swarm probability (used by the reliability guards); final_p = the P6
                # decision probability (skill-weighted blend → gated calibration) used for sizing.
                p, bp, cons, final_p, health = _ai_scout(m, price, evidence_text)
                if p is None:
                    continue
                # ── Plan 2: SWARM-HEALTH gate. Block a degraded / aborted / under-strength
                #    swarm BEFORE sizing/betting (mirror of predict_today). The forecast is
                #    already logged/frozen; this only WITHHOLDS the bet. ──
                from harness.predict_today import (_p_swarm_health as _p_sh,
                                                   _swarm_health_skip_reason as _sh_reason)
                _sh_ok, _sh_r = _p_sh(health, prefix="sameday_swarm")
                if not _sh_ok:
                    _sd_skip(mid, q, _sh_reason(_sh_r, health or {}), p=p, price=price, layer="swarm_health")
                    continue
                # P4B OBSERVE-ONLY (audit #4): the forecast above is logged for scoring,
                # but if this market's label has a real losing track record, WITHHOLD the
                # bet — mirror predict_today so both daemons freeze a losing label.
                try:
                    from harness.predict_today import _observe_only_for as _oo
                    _fine, _obs_only = _oo(q)
                    if _obs_only:
                        _sd_skip(mid, q, f"observe_only:{_fine}", p=p, price=price, layer="observe_only")
                        continue
                except Exception:
                    pass
                # P7 (B4): tag this forecast with the ACTIVE parameter experiment (baseline
                # today; no auto-switch). Materializes + logs the tag so the resolved outcome
                # can be attributed at settle. Passive — never feeds back into the decision.
                from harness.predict_today import _p7_experiment_tag as _p7_exp_tag
                _p7_exp_key = _p7_exp_tag()
                if _p7_exp_key:
                    print(f"  [sameday] experiment: '{_p7_exp_key}'")
                # reliability guards B/C (Guard A = opinion-only filter above, Guard D = bet_events above):
                # skip when the swarm number is untrustworthy. Thresholds shared with predict_today.
                # P6: divergence/consensus guards stay on RAW swarm p vs RAW challenger bp.
                from harness.predict_today import (MAX_SWARM_CHALLENGER_DIVERGENCE as _MAXDIV,
                                                   MIN_SWARM_CONSENSUS as _MINCONS)
                if bp is not None and abs(p - bp) > _MAXDIV:
                    _sd_skip(mid, q, f"sameday_divergence_no_bet (swarm {p:.2f} vs challenger {bp:.2f}, "
                             f"|delta|={abs(p - bp):.2f} > {_MAXDIV})", p=p, price=price, layer="divergence")
                    continue
                if cons is not None and cons < _MINCONS:
                    _sd_skip(mid, q, f"sameday_consensus_no_bet (consensus {cons:.2f} < {_MINCONS:.2f})",
                             p=p, price=price, layer="consensus")
                    continue
                # data-sufficiency gate (mirror of predict_today): the pack was already built BEFORE
                # the forecast (above) and fed to the swarm + challenger. Freeze it for replay, then
                # gate the BET on evidence quality with sameday_-prefixed, JOURNALED reasons.
                from harness.predict_today import _emit_evidence_pack as _ev_emit, _evidence_guard as _ev_guard
                _ev_emit(mid, pack)
                ev_ok, ev_reason = _ev_guard(pack)
                if not ev_ok:
                    _sd_reason = ("sameday_no_evidence_no_bet" if ev_reason == "no_data"
                                  else f"sameday_low_evidence_quality_no_bet ({ev_reason})")
                    _sd_skip(mid, q, _sd_reason, p=p, price=price, layer="evidence")
                    continue
                # P3 — MULTI-LEG mutually-exclusive event: evaluate the WHOLE event as a portfolio
                # (replaces the one-YES Guard D below for ME events) and act ONLY on this leg's slot.
                # Single-market opinions fall through to the existing conviction-sized per-market path.
                from harness.predict_today import (build_event_legs as _build_event_legs,
                                                   run_event_portfolio as _run_event_portfolio,
                                                   event_leg_reject_reason as _event_leg_reject_reason,
                                                   CONVICTION_CAP_MAX as _CAPMAX0)
                _ev_siblings = [o for o in wallet.get_open_positions()
                                if m.get("event_slug") and o.get("event_slug") == m.get("event_slug")
                                and o.get("market_id") != mid]
                _is_me_multi, _ep_legs, _event_key = _build_event_legs(m, final_p, price, _ev_siblings)
                if _is_me_multi:
                    ep, my_pos = _run_event_portfolio(mid, _ep_legs, _event_key, wallet.bankroll_for_sizing())
                    if not (ep.accept and my_pos is not None):
                        reason = _event_leg_reject_reason(ep, mid)
                        _sd_skip(mid, q, f"sameday_event_portfolio_no_bet ({reason})",
                                 p=final_p, price=price, layer="event_portfolio")
                        continue
                    side, stake, edge = my_pos["side"], my_pos["stake"], my_pos["edge"]
                    # P7 (B1): EV-after-costs HARD GATE on the accepted event leg — Plan 5 parity:
                    # pass m + confidence so the spread/liquidity/uncertainty/exit-risk penalties run.
                    from harness.predict_today import _p7_ev_gate as _p7_ev_gate_ep
                    _ev_ok, _ev_reason = _p7_ev_gate_ep(final_p, price, side, m=m, confidence=cons)
                    if not _ev_ok:
                        # FAIL-CLOSED money gate (EV). Record EVERYWHERE (print+obs+journal).
                        _sd_skip(mid, q, _ev_reason, p=final_p, price=price, layer="ev_gate")
                        continue
                    # P8: unified adaptive risk guards (market-quality + correlation +
                    # bad-theme), STRICTER under drawdown. Fail-open; pure tightening.
                    from harness.predict_today import (_p8_risk_guards as _p8_rg_ep,
                                                        _p9_can_trade as _p9_ct_ep,
                                                        _p9_exposure_ok as _p9_ex_ep)
                    _rg_ok, _rg_reason = _p8_rg_ep(m, side, q)
                    if not _rg_ok:
                        _sd_skip(mid, q, _rg_reason, p=final_p, price=price, layer="risk_guards")
                        continue
                    # P9: bankroll kill switch + per-theme/event stake exposure cap.
                    _ct_ok, _ct_reason = _p9_ct_ep()
                    if not _ct_ok:
                        _sd_skip(mid, q, _ct_reason, p=final_p, price=price, layer="bankroll")
                        continue
                    _ex_ok, _ex_reason = _p9_ex_ep(q, ev, stake)
                    if not _ex_ok:
                        _sd_skip(mid, q, _ex_reason, p=final_p, price=price, layer="exposure")
                        continue
                    print(f"  [sameday] event portfolio ACCEPTS this leg → {side} ${stake:.2f} "
                          f"(EV ${ep.portfolio_ev:+.2f}, worst ${ep.worst_case_loss:+.2f})")
                    # P6: size/record on the DECISION probability final_p (== raw swarm p today).
                    fr = wallet.open_position(mid, q, side, final_p, price, edge, stake,
                                              cfg=wallet.WalletConfig(max_bet_frac=_CAPMAX0, max_exposure_frac=0.85),
                                              end_date=m.get("end_date"), event_slug=ev)
                    if fr.opened:
                        opened += 1; held.add(mid)
                        if fr.side == "YES":
                            yes_events[ev] = yes_events.get(ev, 0.0) + final_p
                        journal.record_decision(mid, q, final_p, price, edge, fr.side, fr.stake, fr.fill_price, "swarm",
                                                "LONG (YES)" if side == "YES" else "SHORT (NO)", "bet",
                                                f"Event-portfolio (multi-leg ME): swarm {p:.0%} vs market {price:.0%}, "
                                                f"portfolio EV ${ep.portfolio_ev:+.2f}, worst ${ep.worst_case_loss:+.2f} -> {fr.side}.")
                        print(f"  [sameday] BET {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f} "
                              f"(event portfolio, {hl:.1f}h) {q[:34]}")
                    else:
                        _sd_skip(mid, q, f"sameday_wallet_rejected_no_bet ({fr.reason})", p=final_p, price=price, layer="wallet")
                    continue
                # (4) bet ONLY on a real edge — conviction-scaled stake (bigger when surer).
                from harness.predict_today import (_conviction, _conviction_sizing, CONVICTION_CAP_MAX as _CAPMAX,
                                                   _p7_adaptive_min_edge as _p7_min_edge, _p7_ev_gate as _p7_ev_gate_reg)
                # sameday now gathers the same evidence pack (above) -> feed its quality into
                # conviction so a stronger evidence base raises (and a thin one lowers) the stake.
                # P6: conviction + Kelly size on the DECISION probability final_p (== raw p today);
                # the divergence/consensus guards above already ran on RAW p vs bp.
                conv = _conviction(final_p, bp, cons, final_p - price,
                                   had_data=bool(pack and pack.text),
                                   evidence_quality=(pack.evidence_quality if pack else None))
                lam, cap = _conviction_sizing(conv)
                print(f"  [sameday] conviction {conv:.2f} → {lam:g}x Kelly, cap {cap:.0%}")
                # P7 (B3): per-theme adaptive min_edge, FLOORED at min_edge (cold start ==
                # min_edge exactly; only RAISES for a theme with a real losing track record).
                me = _p7_min_edge(q, min_edge)
                sz = sizing.size_bet(final_p, price, wallet.bankroll_for_sizing(), lam=lam, cap=cap, min_edge=me)
                if sz.side is None or sz.stake <= 0:
                    _sd_skip(mid, q, f"sameday_no_edge_no_bet ({sz.reason})", p=final_p, price=price, layer="sizer")
                    continue
                # P7 (B1): EV-after-costs HARD GATE — Plan 5 parity: pass m + confidence so the
                # spread/liquidity/uncertainty/exit-risk penalties run (a thin raw-edge bet whose
                # after-cost EV fails is now rejected, exactly as in predict_today).
                _ev_ok, _ev_reason = _p7_ev_gate_reg(final_p, price, sz.side, m=m, confidence=cons)
                if not _ev_ok:
                    # FAIL-CLOSED money gate (EV). Record EVERYWHERE (print+obs+journal).
                    _sd_skip(mid, q, _ev_reason, p=final_p, price=price, layer="ev_gate")
                    continue
                # P8: unified adaptive risk guards (stale/low-liquidity/high-spread +
                # correlation + bad-theme), STRICTER under drawdown. Fail-open; tightening.
                from harness.predict_today import (_p8_risk_guards as _p8_rg_reg,
                                                    _p9_can_trade as _p9_ct_reg,
                                                    _p9_exposure_ok as _p9_ex_reg)
                _rg_ok, _rg_reason = _p8_rg_reg(m, sz.side, q)
                if not _rg_ok:
                    _sd_skip(mid, q, _rg_reason, p=final_p, price=price, layer="risk_guards")
                    continue
                # P9: bankroll kill switch + per-theme/event stake exposure cap.
                _ct_ok, _ct_reason = _p9_ct_reg()
                if not _ct_ok:
                    _sd_skip(mid, q, _ct_reason, p=final_p, price=price, layer="bankroll")
                    continue
                _ex_ok, _ex_reason = _p9_ex_reg(q, ev, sz.stake)
                if not _ex_ok:
                    _sd_skip(mid, q, _ex_reason, p=final_p, price=price, layer="exposure")
                    continue
                # Guard D — one YES (winner) per mutually-exclusive event; NO/fade unlimited.
                from harness.predict_today import ONE_YES_PER_EVENT as _ONEYES, MAX_GROUP_PROB_SUM as _MAXSUM
                if sz.side == "YES":
                    if _ONEYES and ev in yes_events:
                        _sd_skip(mid, q, "sameday_event_already_hold_yes_no_bet",
                                 p=final_p, price=price, layer="event_coherence")
                        continue
                    if yes_events.get(ev, 0.0) + p > _MAXSUM:
                        _sd_skip(mid, q, f"sameday_event_incoherent_no_bet "
                                 f"(YES-sum {yes_events.get(ev, 0.0) + p:.2f} > {_MAXSUM})",
                                 p=final_p, price=price, layer="event_coherence")
                        continue
                fr = wallet.open_position(mid, q, sz.side, final_p, price, sz.edge, sz.stake,
                                          cfg=wallet.WalletConfig(max_bet_frac=_CAPMAX, max_exposure_frac=0.85),
                                          end_date=m.get("end_date"), event_slug=ev)
                if fr.opened:
                    opened += 1; held.add(mid)
                    if fr.side == "YES":
                        yes_events[ev] = yes_events.get(ev, 0.0) + final_p
                    journal.record_decision(mid, q, final_p, price, sz.edge,
                                            fr.side, fr.stake, fr.fill_price, "swarm",
                                            "LONG (YES)" if sz.side == "YES" else "SHORT (NO)", "bet",
                                            f"Swarm sees {p:.0%} vs market {price:.0%} ({hl:.1f}h) -> {sz.side}, edge {sz.edge:+.1%}.")
                    print(f"  [sameday] BET {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f} "
                          f"(swarm {p:.0%} vs mkt {price:.0%}, {hl:.1f}h) {q[:34]}")
                else:
                    _sd_skip(mid, q, f"sameday_wallet_rejected_no_bet ({fr.reason})", p=final_p, price=price, layer="wallet")
        except Exception as e:
            # crash-safety: one bad market must never kill the daemon cycle.
            if obs:
                obs.hooks.on_error(where="sameday.place_sameday", exc=e, action="skip",
                                   context={"market_id": m.get("market_id")})
            print(f"[sameday] market error ({type(e).__name__}): {e}")
            continue
    return opened


def run_once(use_ai=True):
    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    with contextlib.ExitStack() as _es:
        if obs:
            _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
            obs.hooks.on_run_start(_obs_run_config(), _obs_bankroll())
        settle_resolved()
        close_long_dated()
        n = place_sameday(use_ai=use_ai)
        st = wallet.get_state()
        journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
        print(f"[sameday] cycle done: +{n} same-day bets | open={st['n_open']} "
              f"cash=${st['cash']:.0f} equity=${st['equity']:.0f} realized=${st['realized_pnl']:+.2f}\n")
        if obs:
            obs.hooks.on_run_end({"bets": n, "open": st["n_open"]})


def daemon(interval=INTERVAL, use_ai=True):
    print(f"[sameday] DAEMON — same-day only, SWARM-DRIVEN bets (opinion-only, 1/event), "
          f"every {interval/60:.0f} min. No stopping.\n")
    while True:
        try:
            run_once(use_ai=use_ai)
        except KeyboardInterrupt:
            print("[sameday] stopped."); return
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="sameday.daemon", exc=e, action="retry")
            print("[sameday] cycle error:", e)
        time.sleep(interval)


def main(argv=None):
    import argparse
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="harness.sameday",
                                description="Same-day favorite-longshot + AI scout daemon (PAPER only).")
    p.add_argument("command", nargs="?", default="once", choices=["once", "daemon", "close"])
    p.add_argument("--interval", type=int, default=None, help="daemon cycle seconds (default INTERVAL)")
    # argparse rejects an unknown command/flag loudly instead of a silent no-op (audit #12).
    args = p.parse_args(argv)
    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    print(f"  [sameday] command={args.command}  interval={args.interval or INTERVAL}s  trading=PAPER (real-money disabled)")
    if args.command == "close":
        close_long_dated()
        st = wallet.get_state()
        journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
        print(f"[sameday] after close: open={st['n_open']} cash=${st['cash']:.0f} realized=${st['realized_pnl']:+.2f}")
    elif args.command == "once":
        run_once()
    elif args.command == "daemon":
        daemon(interval=args.interval if args.interval is not None else INTERVAL)


if __name__ == "__main__":
    main()
