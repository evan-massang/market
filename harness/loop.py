"""
P3 — autonomous PAPER-trading loop. Scheduled, simulated, $0.

NO real money, NO keys, NO execution. One pass:
  1. fetch active Polymarket markets (read-only Gamma)
  2. classify -> keep only OPINION markets above the liquidity floor
  3. dedupe -> skip markets we already hold an open paper position on
  4. enrich context upstream (GDELT news/tone + WhoIsSharp microstructure signals)
  5. forecast with the PolySwarm swarm -> probability p, blended with market price
  6. size a SIMULATED bet = min(0.25*Kelly, cap) * paper_bankroll
  7. record a realistic fill (price + slippage) and open a paper position
  8. (separately) on resolution: settle the paper position + score Brier

Forecasters run in PARALLEL and are A/B'd, never chained; signals are FEATURES,
not stages. Here we drive PolySwarm; the single-LLM challenger is added at P4.

CLI (run from polyswarm/ with PYTHONUTF8=1):
  python -m harness.loop run     [--max-markets N --size N --rounds N --dry-run ...]
  python -m harness.loop settle
  python -m harness.loop status
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass

# Load polyswarm/.env (LLM_PROVIDER=ollama, MODEL_FAST=...) — running as
# `python -m harness.loop` bypasses main.py, which is what normally loads it.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, sizing, wallet, challenger, journal
from harness import gdelt as gdelt_mod
from harness import signals as signals_mod

# ── observability (W1: control-flow / lifecycle / classify / resolution / errors) ──
# Guarded import: a broken/missing obs package can NEVER break a pass. Every obs call
# is gated on `if obs:` and is OBSERVATION-ONLY (no return/logic/exception change).
try:
    from harness import obs
except Exception:
    obs = None


def _obs_run_config(cfg):
    """Effective config snapshot for run.start (observation-only)."""
    try:
        import dataclasses
        base = dataclasses.asdict(cfg)
    except Exception:
        base = {}
    base.update({
        "entry": "loop",
        "MODEL_FAST": os.getenv("MODEL_FAST"),
        "MODEL_DEEP": os.getenv("MODEL_DEEP"),
        "DEBATE_ROUNDS": os.getenv("DEBATE_ROUNDS"),
        "LLM_PROVIDER": os.getenv("LLM_PROVIDER"),
    })
    return base


def _obs_bankroll():
    try:
        return wallet.get_state().get("cash") or 1000.0
    except Exception:
        return 1000.0


@dataclass
class LoopConfig:
    max_markets: int = 5          # opinion markets to forecast per pass (CPU-bound)
    fetch_limit: int = 150        # how many active markets to pull before filtering
    swarm_size: int | None = 6    # agents (None = all 12). 6 keeps CPU passes tractable.
    rounds: int = 1               # debate rounds (1 for speed on CPU)
    min_volume: float = 5_000.0
    min_liquidity: float = 1_000.0
    min_edge: float = 0.02
    max_days_to_resolution: int = 180   # skip markets resolving further out, so the
                                        # gate actually accrues resolutions (the top
                                        # opinion markets are multi-year 2028 races)
    lam: float = 0.25             # quarter-Kelly
    cap: float = 0.02             # hard per-bet cap
    starting_bankroll: float = 1_000.0
    use_gdelt: bool = True
    use_signals: bool = True
    use_llm_classifier: bool = False
    challenger: bool = False      # also run a single-LLM baseline per market (parallel A/B)
    dry_run: bool = False         # stub the forecast (test the pipeline without the slow LLM)


# ── context enrichment (fed UPSTREAM to the forecaster) ──────────────────────
def _build_enrichment(market: dict, cfg: LoopConfig) -> str:
    """Backwards-compatible GATHER entry point used by the loop / predict_today / place_bet.

    Delegates to the ONE canonical gather path (evidence_pack.build_evidence_pack) and
    returns its swarm-input text, which is byte-identical to the historical join (same
    header, per-block headers, ``"\n\n"`` join, ``""`` when no blocks). Signature is
    unchanged so every existing caller keeps working. Callers that want the structured
    pack (per-source scores + content hash) should use :func:`build_pack`.
    """
    return build_pack(market, cfg).text


def build_pack(market: dict, cfg: LoopConfig):
    """Structured GATHER: the full EvidencePack (sources + freshness/relevance/quality
    scores + a stable content hash). ``build_pack(m, cfg).text`` == ``_build_enrichment(m, cfg)``."""
    from harness.evidence_pack import build_evidence_pack
    return build_evidence_pack(market, cfg)


# ── forecast (real swarm or dry-run stub) ─────────────────────────────────────
def _days_until(end_date) -> float | None:
    """Days from now until an ISO end_date string, or None if unparseable."""
    if not end_date:
        return None
    from datetime import datetime, timezone
    s = str(end_date).replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,):
        try:
            dt = parse(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except Exception:
            pass
    return None


def _stub_probability(market_id: str, price: float) -> float:
    """Deterministic stub for --dry-run: a varied offset off the price so the
    pipeline (sizing/fills/both sides) gets exercised without the slow LLM."""
    h = int(hashlib.sha256((market_id or "x").encode()).hexdigest(), 16)
    offset = ((h % 21) - 10) / 100.0   # -0.10 .. +0.10
    return min(max(price + offset, 0.01), 0.99)


def _forecast(market: dict, price: float, cfg: LoopConfig, enrichment: str = "") -> tuple[float, dict]:
    """Returns (probability, meta). meta carries the swarm's regime/consensus for the
    dashboard transcript."""
    if cfg.dry_run:
        return _stub_probability(market["market_id"], price), {"regime": "(dry-run)"}
    from core.swarm import Swarm
    from agents.personas import build_swarm
    os.environ["DEBATE_ROUNDS"] = str(cfg.rounds)
    swarm = Swarm(agents=build_swarm(cfg.swarm_size) if cfg.swarm_size else None)
    result = swarm.forecast(market["question"], market_odds=price,
                            market_id=market["market_id"], extra_context=enrichment)
    rg = result.get("regime")
    regime = rg.get("regime") if isinstance(rg, dict) else (str(rg) if rg else "")
    return float(result["probability"]), {"regime": regime, "consensus": result.get("consensus_score")}


# ── one pass ──────────────────────────────────────────────────────────────────
def run_once(cfg: LoopConfig) -> dict:
    from core.calibration import init_db
    init_db()
    wallet.init_wallet(cfg.starting_bankroll)
    challenger.init_baseline_db()
    journal.init_journal()

    with contextlib.ExitStack() as _es:
        if obs:
            _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
            obs.hooks.on_run_start(_obs_run_config(cfg), _obs_bankroll())
        held = {p["market_id"] for p in wallet.get_open_positions()}
        summary = {"scanned": 0, "opinion": 0, "forecast": 0, "opened": 0, "skipped_reasons": {}}

        def skip(reason):
            summary["skipped_reasons"][reason] = summary["skipped_reasons"].get(reason, 0) + 1

        print(f"[loop] fetching up to {cfg.fetch_limit} active markets…")
        markets = gamma.fetch_active_markets(limit=cfg.fetch_limit)
        # Prefer SOONEST-resolving markets so watchable near-term bets get placed first.
        markets.sort(key=lambda m: (_days_until(m.get("end_date")) if _days_until(m.get("end_date")) is not None else 1e9))
        summary["scanned"] = len(markets)
        print(f"[loop] {len(markets)} markets fetched. dry_run={cfg.dry_run} "
              f"size={cfg.swarm_size} rounds={cfg.rounds}\n")

        picked = 0
        for m in markets:
            if picked >= cfg.max_markets:
                break
            try:
                mid = m["market_id"]
                ok, cls = classifier.should_forecast(
                    m, use_llm=cfg.use_llm_classifier, min_volume=cfg.min_volume, min_liquidity=cfg.min_liquidity)
                if cls.label == "opinion":
                    summary["opinion"] += 1
                if not ok:
                    _reason = "not_opinion" if cls.label != "opinion" else "below_liquidity_floor"
                    if obs:
                        obs.hooks.on_classify(mid, m.get("question"), cls.label, getattr(cls, "signals", None), False, _reason)
                    skip(_reason)
                    continue
                if mid in held:
                    if obs:
                        obs.hooks.on_classify(mid, m.get("question"), cls.label, getattr(cls, "signals", None), False, "already_held")
                    skip("already_held"); continue
                price = gamma.yes_price(m)
                if price is None or not (0.0 < price < 1.0):
                    if obs:
                        obs.hooks.on_classify(mid, m.get("question"), cls.label, getattr(cls, "signals", None), False, "no_yes_price")
                    skip("no_yes_price"); continue
                if cfg.max_days_to_resolution:
                    days = _days_until(m.get("end_date"))
                    if days is not None and days > cfg.max_days_to_resolution:
                        if obs:
                            obs.hooks.on_classify(mid, m.get("question"), cls.label, getattr(cls, "signals", None), False, "resolves_too_far_out")
                        skip("resolves_too_far_out"); continue

                picked += 1
                if obs:
                    obs.hooks.on_classify(mid, m.get("question"), cls.label, getattr(cls, "signals", None), True, "candidate")
                with contextlib.ExitStack() as _mes:
                    if obs:
                        _mes.enter_context(obs.market_ctx(market_id=mid, question=m["question"]))
                        _mes.enter_context(obs.forecast_ctx(forecast_id=obs.mint('f')))
                    print(f"[{picked}/{cfg.max_markets}] {m['question'][:70]}")
                    print(f"      market_id={mid[:18]}…  yes_price={price:.3f}  vol=${m['volume']:,.0f}")
                    try:
                        enrichment = "" if cfg.dry_run else _build_enrichment(m, cfg)
                        p, meta = _forecast(m, price, cfg, enrichment)
                        summary["forecast"] += 1
                        # CHALLENGER — independent single-LLM forecast on the SAME market (parallel
                        # A/B, never chained). Does not drive betting; pure calibration control.
                        if cfg.challenger and not cfg.dry_run:
                            bp = challenger.single_llm_forecast(m["question"], price, enrichment)
                            if bp is not None:
                                challenger.save_baseline(mid, m["question"], bp, price)
                                print(f"      challenger single-LLM p={bp:.3f}")
                        bankroll = wallet.bankroll_for_sizing()
                        sz = sizing.size_bet(p, price, bankroll, lam=cfg.lam, cap=cfg.cap, min_edge=cfg.min_edge)
                        print(f"      model_p={p:.3f}  market_p={price:.3f}  edge={sz.edge:+.3f}  -> {sz.reason}")
                        regime, sig = meta.get("regime", ""), ("LONG (YES)" if sz.edge > 0 else "SHORT (NO)")
                        if sz.side is None:
                            why = f"Swarm {p:.0%} vs market {price:.0%} (edge {sz.edge:+.1%}). No bet — {sz.reason}."
                            journal.record_decision(mid, m["question"], p, price, sz.edge, None, 0.0, None,
                                                    regime, "no edge", "no_bet", why)
                            skip("no_edge_or_below_min"); print()
                            if obs:
                                obs.hooks.on_trade_skip(
                                    forecast_id=(obs.current().get("forecast_id") if obs else None),
                                    reason="no_edge_or_below_min",
                                    inputs={"market_id": mid, "p": p, "price": price, "edge": sz.edge, "layer": "sizer"},
                                )
                            continue
                        fr = wallet.open_position(mid, m["question"], sz.side, p, price, sz.edge, sz.stake,
                                                  end_date=m.get("end_date"))
                        if fr.opened:
                            summary["opened"] += 1
                            why = (f"Swarm sees {p:.0%} vs the market's {price:.0%} — a {sz.edge:+.1%} edge. "
                                   f"Buying {fr.side} @ {fr.fill_price:.3f} with ${fr.stake:.2f} ({sz.reason}).")
                            journal.record_decision(mid, m["question"], p, price, sz.edge, fr.side, fr.stake,
                                                    fr.fill_price, regime, sig, "bet", why)
                            print(f"      OPENED {fr.side} stake=${fr.stake:.2f} fill={fr.fill_price:.3f} "
                                  f"shares={fr.shares:.2f}  bankroll now ${wallet.bankroll_for_sizing():.2f}")
                        else:
                            why = f"Sized {sz.side} ${sz.stake:.2f} but wallet rejected: {fr.reason}"
                            journal.record_decision(mid, m["question"], p, price, sz.edge, sz.side, 0.0, None,
                                                    regime, sig, "rejected", why)
                            skip("wallet_rejected"); print(f"      wallet rejected: {fr.reason}")
                            if obs:
                                obs.hooks.on_trade_skip(
                                    forecast_id=(obs.current().get("forecast_id") if obs else None),
                                    reason=f"wallet_rejected: {fr.reason}",
                                    inputs={"market_id": mid, "side": sz.side, "stake": sz.stake, "layer": "wallet"},
                                )
                    except Exception as e:
                        skip("error")
                        if obs:
                            obs.hooks.on_error(where="loop.run_once", exc=e, action="skip",
                                               context={"market_id": mid})
                        print(f"      ERROR: {type(e).__name__}: {e}")
                        if cfg.dry_run:
                            traceback.print_exc()
                    print()
            except Exception as e:
                # crash-safety: one malformed market must never abort the whole pass.
                skip("error")
                if obs:
                    obs.hooks.on_error(where="loop.run_once.market", exc=e, action="skip",
                                       context={"market_id": m.get("market_id")})
                print(f"      ERROR (market): {type(e).__name__}: {e}")
                continue

        st = wallet.get_state()
        journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
        print(f"[loop] pass done. {summary}")
        print(f"[wallet] cash=${st['cash']:.2f} equity=${st['equity']:.2f} "
              f"open={st['n_open']} realized_pnl=${st['realized_pnl']:.2f}")
        # heartbeat: real file mtime + summary that the dashboard health probe reads
        try:
            with open(os.getenv("HARNESS_HEARTBEAT", ".heartbeat.json"), "w") as _hb:
                json.dump({"ts": time.time(), "cycle": {"phase": "loop", **summary,
                           "cash": round(st["cash"], 2), "equity": round(st["equity"], 2),
                           "realized_pnl": round(st["realized_pnl"], 2)}}, _hb)
        except Exception:
            pass
        if obs:
            obs.hooks.on_run_end({**summary, "open": st["n_open"]})
        return summary


# ── settlement ────────────────────────────────────────────────────────────────
def _latest_baseline_p(market_id):
    """Latest stored challenger (baseline) probability for a market, or None.

    Settlement fallback for the challenger per-forecaster Brier when no
    forecast_versions row was recorded. DATABASE_URL-aware (resolved at call time).
    Best-effort: never raises."""
    try:
        import sqlite3
        raw = os.getenv("DATABASE_URL")
        if raw:
            db = raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
        else:
            try:
                from harness.obs import config as _cfg
                db = str(_cfg.resolve_db_path())
            except Exception:
                db = "polyswarm.db"
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT probability FROM baseline_forecasts WHERE market_id=? "
                "ORDER BY id DESC LIMIT 1", (market_id,)).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _record_forecaster_outcomes(market_id, outcome):
    """P6 settlement hook — attribute a resolved market's Brier to each FORECASTER
    (the raw SWARM probability and the challenger ENSEMBLE mean) so
    harness.forecaster_weights can build a per-forecaster track record.

    Prefers the EXACT probabilities stashed in the forecast_versions audit row for
    this market (swarm_p, mean of the challenger ensemble probs); falls back to the
    swarm_forecasts / baseline_forecasts tables if no version row exists. Best-effort
    and never raises into settlement; recording does NOT affect the cold-start decision
    probability (forecaster_weights stays swarm-only until BOTH forecasters qualify)."""
    try:
        from harness import forecaster_weights, forecast_versions
        swarm_p = challenger_p = None
        ver = forecast_versions.get_forecast_version_by_market(market_id)
        if ver:
            swarm_p = ver.get("swarm_p")
            ps = ver.get("challenger_ps") or []
            if ps:
                try:
                    challenger_p = sum(ps) / len(ps)
                except Exception:
                    challenger_p = None
        if swarm_p is None:
            try:
                from harness import label_perf
                swarm_p, _mkt = label_perf.latest_forecast_pq(market_id)
            except Exception:
                swarm_p = None
        if challenger_p is None:
            challenger_p = _latest_baseline_p(market_id)
        if swarm_p is not None:
            forecaster_weights.record_forecaster_outcome("swarm", market_id, swarm_p, outcome)
        if challenger_p is not None:
            forecaster_weights.record_forecaster_outcome("challenger", market_id, challenger_p, outcome)
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="loop.settle_resolved.forecaster_weights",
                                   exc=e, action="skip", context={"market_id": market_id})
            except Exception:
                pass


def settle_resolved(cfg: LoopConfig | None = None) -> list[dict]:
    from core.calibration import resolve_forecast
    journal.init_journal()
    positions = wallet.get_open_positions()
    seen, results = {}, []
    # P7 (B2): snapshot the OPEN positions per market BEFORE they settle — each row
    # still carries side / fill_price / market_p, which the CLV recorder needs (the
    # post-settle rows are still present but we capture them here for a clean read).
    pos_by_market: dict = {}
    for p in positions:
        seen.setdefault(p["market_id"], p["question"])
        pos_by_market.setdefault(p["market_id"], []).append(p)
    print(f"[settle] {len(seen)} markets with open paper positions to check…")
    for mid, question in seen.items():
        with contextlib.ExitStack() as _mes:
            if obs:
                _mes.enter_context(obs.market_ctx(market_id=mid, question=question))
            try:
                m = gamma.fetch_market_by_condition_id(mid)
                if m is None:
                    print(f"  {mid[:18]}… not found on Gamma — skip"); continue
                outcome = gamma.resolution_outcome(m)
                if outcome is None:
                    print(f"  {mid[:18]}… not resolved yet"); continue
                if obs:
                    obs.hooks.on_resolution(mid, outcome, "gamma")
                settled = wallet.settle_market(mid, outcome)
                n = resolve_forecast(question, outcome, market_id=mid)
                challenger.resolve_baseline(outcome, mid)   # score the A/B baseline too
                # P6 — record the SWARM and CHALLENGER per-forecaster realized Brier so the
                # skill-weighted blend can ACTIVATE once BOTH have a track record. Best-effort:
                # never breaks settlement (cold-start identity is unaffected — this only feeds
                # forecaster_weights, which stays {"swarm":1,"challenger":0} until both qualify).
                _record_forecaster_outcomes(mid, outcome)
                pnl = sum(s["realized_pnl"] for s in settled)
                # P4B — feed the label backtest (best-effort; never break settlement).
                # fine_label = theme, coarse label = classifier verdict; model_p/market_p
                # come from the just-resolved swarm forecast, pnl from the settled position.
                try:
                    from harness import label_perf, scoreboard
                    model_p, market_p = label_perf.latest_forecast_pq(mid)
                    label_perf.record_classification_outcome(
                        mid, question, scoreboard.theme_of(question),
                        classifier.tag_market(m).label, model_p, market_p, outcome, pnl)
                except Exception as _e:
                    if obs:
                        try:
                            obs.hooks.on_error(where="loop.settle_resolved.label_perf",
                                               exc=_e, action="skip", context={"market_id": mid})
                        except Exception:
                            pass
                # P7 — CLV + experiment-outcome attribution (best-effort; NEVER breaks
                # settlement, NEVER touches a gate/threshold/bet). Pure recording.
                try:
                    from harness import clv as _clv, experiments as _experiments
                    from harness import scoreboard as _sb, label_perf as _lp
                    _theme = _sb.theme_of(question)
                    # CLV: entry = our actual fill_price; closing-line PROXY = the
                    # pre-resolution market price stored on the position (no live closing
                    # snapshot exists at on-chain resolution — documented proxy).
                    for _pos in pos_by_market.get(mid, []):
                        _clv.record_clv(mid, _pos.get("side"),
                                        entry_price=_pos.get("fill_price"),
                                        closing_price=_pos.get("market_p"),
                                        theme=_theme)
                    # Experiment outcome: attribute this resolved market's Brier + realized
                    # P&L to the currently-ACTIVE experiment (baseline today; no auto-switch).
                    _mp, _kp = _lp.latest_forecast_pq(mid)
                    _model_brier = None if _mp is None else (_mp - outcome) ** 2
                    _market_brier = None if _kp is None else (_kp - outcome) ** 2
                    _exp = _experiments.active_experiment()
                    _experiments.record_experiment_outcome(
                        _exp.get("exp_key") if isinstance(_exp, dict) else None,
                        mid, _model_brier, _market_brier, pnl)
                except Exception as _e:
                    if obs:
                        try:
                            obs.hooks.on_error(where="loop.settle_resolved.p7",
                                               exc=_e, action="skip", context={"market_id": mid})
                        except Exception:
                            pass
                results.append({"market_id": mid, "outcome": outcome, "positions": len(settled),
                                "brier_rows": n, "realized_pnl": pnl})
                print(f"  {mid[:18]}… RESOLVED outcome={outcome} settled={len(settled)} "
                      f"brier_rows={n} pnl=${pnl:+.2f}")
            except Exception as e:
                if obs:
                    obs.hooks.on_error(where="loop.settle_resolved", exc=e, action="skip",
                                       context={"market_id": mid})
                print(f"  {mid[:18]}… ERROR {type(e).__name__}: {e}")
    st = wallet.get_state()
    journal.record_snapshot(st["cash"], st["equity"], st["realized_pnl"], st["open_exposure"], st["n_open"])
    print(f"[wallet] cash=${st['cash']:.2f} equity=${st['equity']:.2f} "
          f"open={st['n_open']} realized_pnl=${st['realized_pnl']:.2f}")
    return results


def daemon(cfg: LoopConfig, interval_sec: int = 10_800) -> None:
    """Scheduled autonomous operation: settle resolved -> run a pass -> sleep, repeat.
    Default interval 3h (CPU forecasts are slow). Ctrl-C to stop. The single source
    of truth (paper wallet + calibration DB) persists across passes."""
    import time
    print(f"[daemon] starting. interval={interval_sec}s ({interval_sec/3600:.1f}h). Ctrl-C to stop.")
    while True:
        try:
            settle_resolved(cfg)
            run_once(cfg)
        except KeyboardInterrupt:
            print("[daemon] stopped."); return
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="loop.daemon", exc=e, action="retry")
            print(f"[daemon] pass error: {type(e).__name__}: {e}")
        print(f"[daemon] sleeping {interval_sec}s…\n")
        time.sleep(interval_sec)


def status() -> None:
    wallet.init_wallet()
    st = wallet.get_state()
    print("=== PAPER WALLET ===")
    for k, v in st.items():
        print(f"  {k:18s}: {v}")
    print("\n=== OPEN POSITIONS ===")
    for p in wallet.get_open_positions():
        print(f"  {p['side']:3s} ${p['stake']:.2f} @ {p['fill_price']:.3f}  "
              f"model_p={p['model_p']:.3f} mkt_p={p['market_p']:.3f}  {p['question'][:50]}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse(argv) -> tuple[str, LoopConfig]:
    ap = argparse.ArgumentParser(prog="harness.loop", description="Autonomous paper-trading loop")
    ap.add_argument("command", choices=["run", "settle", "status", "daemon"])
    ap.add_argument("--interval", type=int, default=10_800, help="daemon pass interval (seconds)")
    ap.add_argument("--max-markets", type=int, default=5)
    ap.add_argument("--fetch-limit", type=int, default=150)
    ap.add_argument("--size", type=int, default=6, help="swarm size (0 = all 12)")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--min-volume", type=float, default=5_000.0)
    ap.add_argument("--min-liquidity", type=float, default=1_000.0)
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--max-days", type=int, default=180, help="skip markets resolving further out (gate accrual)")
    ap.add_argument("--bankroll", type=float, default=1_000.0)
    ap.add_argument("--no-gdelt", action="store_true")
    ap.add_argument("--no-signals", action="store_true")
    ap.add_argument("--llm-classifier", action="store_true")
    ap.add_argument("--challenger", action="store_true", help="also run a single-LLM baseline per market (A/B)")
    ap.add_argument("--dry-run", action="store_true", help="stub the forecast (test pipeline, no LLM)")
    a = ap.parse_args(argv)
    cfg = LoopConfig(
        max_markets=a.max_markets, fetch_limit=a.fetch_limit,
        swarm_size=(a.size or None), rounds=a.rounds,
        min_volume=a.min_volume, min_liquidity=a.min_liquidity, min_edge=a.min_edge,
        max_days_to_resolution=a.max_days,
        starting_bankroll=a.bankroll, use_gdelt=not a.no_gdelt, use_signals=not a.no_signals,
        use_llm_classifier=a.llm_classifier, challenger=a.challenger, dry_run=a.dry_run,
    )
    return a.command, cfg, a.interval


def main(argv=None):
    command, cfg, interval = _parse(argv if argv is not None else sys.argv[1:])
    if command == "run":
        run_once(cfg)
    elif command == "settle":
        settle_resolved(cfg)
    elif command == "status":
        status()
    elif command == "daemon":
        daemon(cfg, interval_sec=interval)


if __name__ == "__main__":
    main()
