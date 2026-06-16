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
                                    [--window same_day|near_term|weekly]
"""
from __future__ import annotations
import os, sys, time, contextlib

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness import gamma, classifier, sizing, wallet, journal, challenger, scanner, event_portfolio
from harness import event_safety as _esafe   # Plan 6: event-basket EXECUTION safety policy
# P6 — skill-weighted forecaster blend + gated calibration + versioned forecast record.
# All three are HARD cold-start passthroughs (see the blend/calibrate site in predict_one).
from harness import calibration_apply, forecast_versions
from harness import forecaster_weights as forecaster_weights_mod
from harness import provenance as _provenance
from harness.loop import LoopConfig, _build_enrichment, _forecast, _days_until, build_pack

# ── observability (W1: control-flow / lifecycle / classify / skips / errors) ──
# Guarded import so a broken/missing obs package can NEVER break a forecast pass.
# Every obs call below is gated on `if obs:` and obs is OBSERVATION-ONLY: it never
# changes a return value, alters logic, or raises.
try:
    from harness import obs
except Exception:
    obs = None

# ── Plan 1: FAIL-CLOSED money gates ──────────────────────────────────────────
# safety_gate holds the canonical fail-closed reason vocabulary + the coerce()
# validator. It is a HARD dependency of the bet-decision path: a money gate that
# cannot be evaluated MUST block the bet, never allow it. (Pure module, no cycle.)
from harness import safety_gate as _sg
# ── Plan 2: SWARM-DEGRADATION safety ─────────────────────────────────────────
# swarm_health holds the minimum-surviving-agent policy. A degraded / aborted /
# under-strength swarm forecast must never be bet on. (Pure module, no cycle.)
from core import swarm_health as _swarm_health

# ── P7: EV-after-costs gate + per-theme adaptive min_edge + experiment tagging ──
# Additive + guarded: a broken/missing P7 module can NEVER break a forecast pass.
# Every wired effect is a pure TIGHTENING or a passive recording (see the helpers
# below). On ANY import/runtime fault we fall back to the EXACT pre-P7 behavior
# (the prior gates), so cold-start / thin-data behavior is never LOOSER than pre-P7.
try:
    from harness import profitability as _profitability
except Exception:
    _profitability = None
try:
    from harness import adaptive as _adaptive
    from harness import scoreboard as _scoreboard_p7
except Exception:
    _adaptive = None
    _scoreboard_p7 = None
try:
    from harness import experiments as _experiments
except Exception:
    _experiments = None
# P8/P9 money-gate modules — held at MODULE level (mirroring _profitability) so the
# wrappers can detect an unavailable module and FAIL CLOSED, and so tests can
# simulate "module unavailable" by patching these to None.
try:
    from harness import risk_guards as _risk_guards
except Exception:
    _risk_guards = None
try:
    from harness import bankroll as _bankroll
except Exception:
    _bankroll = None


def _p7_adaptive_min_edge(question, base_min_edge):
    """P7 (B3): per-theme adaptive min_edge, FLOORED at the live base (never looser).

    Returns ``max(adaptive_min_edge(theme, floor=base), base)``. Cold start / thin
    data / any error -> ``base_min_edge`` EXACTLY (the pre-P7 value), so today's
    behavior is unchanged. FLOOR-ONLY-UP: the result is never below base_min_edge,
    so wiring this in can only demand MORE edge, never less."""
    if _adaptive is None or _scoreboard_p7 is None:
        return base_min_edge
    try:
        theme = _scoreboard_p7.theme_of(question)
        me = _adaptive.adaptive_min_edge(theme, floor=base_min_edge)
        return max(float(me), float(base_min_edge))
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="predict_today._p7_adaptive_min_edge", exc=e, action="skip")
            except Exception:
                pass
        return base_min_edge


def _p7_ev_gate(model_p, market_p, side, m=None, confidence=None):
    """P7 (B1): EV-after-costs HARD GATE. Returns ``(ok, reason)``.

    PURE TIGHTENING: a healthy +EV bet passes; the gate can only REJECT. ``ok`` is
    True only when the slippage-worsened wallet fill (plus, when a market ``m`` is
    supplied, the spread/liquidity/uncertainty/exit-risk penalties) keeps this
    share's after-cost EV above MIN_EV_AFTER_COSTS, using the wallet's OWN cost model.

    FAIL-CLOSED (Plan 1): the EV gate is a money gate. If the profitability module
    is missing, the inputs are malformed (bad/NaN price, prob out of range, bad
    side), the call raises, or it returns a malformed result, we BLOCK the bet — we
    never assume positive EV on a fault."""
    if _profitability is None:
        return False, _sg.EV_UNAVAILABLE
    # Validate inputs UP FRONT: a malformed price/probability/side can never be a
    # pass. market_p must be a tradable price strictly in (0,1); model_p in [0,1].
    try:
        mp = float(market_p)
        pp = float(model_p)
    except (TypeError, ValueError):
        return False, _sg.EV_INVALID
    side_u = str(side).upper() if side is not None else ""
    if not (_sg.finite(mp) and _sg.finite(pp)) or not (0.0 < mp < 1.0) \
            or not (0.0 <= pp <= 1.0) or side_u not in ("YES", "NO"):
        return False, _sg.EV_INVALID
    try:
        spread = liquidity = exit_risk = None
        if m is not None:
            try:
                spread = scanner._spread(m)
                liquidity = scanner._f(m.get("liquidity"), None)
                exit_risk = scanner.exit_risk(m)
            except Exception:
                pass   # optional penalty signals only; their absence never loosens
        res = _profitability.ev_gate(model_p, market_p, side, spread=spread,
                                     liquidity=liquidity, confidence=confidence,
                                     exit_risk=exit_risk)
    except Exception as e:
        _sg.log_error("predict_today._p7_ev_gate", e)
        return False, _sg.EV_ERROR
    return _sg.coerce(res, gate="ev_gate", block_reason=_sg.EV_INVALID)


def _p8_risk_guards(m, side, q):
    """P8: unified adaptive risk guards (market-quality: stale/low-liquidity/
    high-spread; portfolio: correlation/bad-theme) under a drawdown-derived
    `tighten` (stricter when the book is losing). Returns ``(allow, reason)``.

    PURE TIGHTENING: a clean market in a healthy book passes; the guards can only
    SKIP. FAIL-CLOSED (Plan 1): if the risk_guards module is missing, ``evaluate``
    raises, or it returns a malformed verdict, we BLOCK the bet. Only an EXPLICIT
    ``allow is True`` from a well-formed verdict passes — unknown risk = no bet."""
    if _risk_guards is None:
        return False, _sg.RISK_UNAVAILABLE
    try:
        rg = _risk_guards.evaluate(m, side, q)
    except Exception as e:
        _sg.log_error("predict_today._p8_risk_guards", e)
        return False, _sg.RISK_ERROR
    if not isinstance(rg, dict) or "allow" not in rg:
        return False, _sg.RISK_INVALID
    allow = (rg.get("allow") is True)   # ONLY explicit True passes
    reason = rg.get("blocking_reason") or ("ok" if allow else "risk_guards_blocked")
    return allow, reason


def _p9_can_trade():
    """P9: pre-bet bankroll KILL SWITCH (drawdown pause / loss limit / losing-streak
    cooldown). Returns ``(ok, reason)``. A pause withholds only the BET — the
    forecast is still computed + logged + frozen for scoring (observe-only).

    FAIL-CLOSED (Plan 1): the kill switch is a money gate. If the bankroll module is
    missing, ``can_trade`` raises, or it returns a malformed result, we BLOCK the
    bet — we never assume the book is healthy on a fault."""
    if _bankroll is None:
        return False, _sg.BANKROLL_UNAVAILABLE
    try:
        res = _bankroll.can_trade()
    except Exception as e:
        _sg.log_error("predict_today._p9_can_trade", e)
        return False, _sg.BANKROLL_ERROR
    return _sg.coerce(res, gate="bankroll", block_reason=_sg.BANKROLL_INVALID)


def _p9_exposure_ok(q, event, stake):
    """P9: per-theme / per-event STAKE exposure cap. Returns ``(ok, reason)``.
    Pure tightening — refuses a bet that would over-concentrate the book.

    FAIL-CLOSED (Plan 1): if the bankroll module (or the theme tagger) is missing,
    the call raises, or it returns a malformed result, we BLOCK the bet — an
    unevaluable concentration check is unsafe."""
    if _bankroll is None or _scoreboard_p7 is None:
        # bankroll module or the theme tagger missing -> cannot evaluate the cap -> block
        return False, _sg.EXPOSURE_UNAVAILABLE
    try:
        theme = _scoreboard_p7.theme_of(q)
        res = _bankroll.exposure_ok(theme, event, stake)
    except Exception as e:
        _sg.log_error("predict_today._p9_exposure_ok", e)
        return False, _sg.EXPOSURE_ERROR
    return _sg.coerce(res, gate="exposure", block_reason=_sg.EXPOSURE_INVALID)


def _p_swarm_health(meta, prefix="swarm"):
    """P-Plan2: block a bet on a DEGRADED / ABORTED / under-strength swarm forecast.

    Returns ``(allow, reason)``. The forecast itself is already computed + logged +
    frozen by the swarm; this only WITHHOLDS the bet (observe-only). A healthy swarm
    passes untouched and the later quality gates still apply.

    FAIL-CLOSED on missing health metadata: if the swarm did not report its health
    (no ``allow_bet`` / ``n_agents_succeeded``), we BLOCK — an unknown-strength
    forecast is never treated as healthy. ``prefix`` namespaces the reason code
    (predict_today: 'swarm'; sameday: 'sameday_swarm')."""
    def _r(key):
        return f"{prefix}_{key}_no_bet"
    if not isinstance(meta, dict) or "allow_bet" not in meta or "n_agents_succeeded" not in meta:
        return False, _r("missing_health_metadata")
    # probability came from the all-agents-failed fallback (a default 0.5, not a signal)
    if meta.get("method") == _swarm_health.ALL_AGENTS_FAILED_METHOD:
        return False, _r("fallback_probability")
    if meta.get("aborted") is True:
        return False, _r("aborted")
    n_succ = meta.get("n_agents_succeeded")
    if not isinstance(n_succ, int):
        return False, _r("missing_health_metadata")
    if n_succ < _swarm_health.MIN_SWARM_AGENTS_FOR_BET:
        return False, _r("insufficient_agents")
    if meta.get("allow_bet") is not True:
        return False, _r("degraded")
    # consensus unusable because too few agents survived (defensive; assess already blocks)
    if meta.get("consensus") is None and meta.get("consensus_status") == _swarm_health.CONSENSUS_INSUFFICIENT:
        return False, _r("insufficient_agents")
    return True, "ok"


def _swarm_health_skip_reason(sh_reason, meta):
    """Enrich a swarm-health block reason with the agent counts for the decision log."""
    try:
        return (f"{sh_reason} (agents {meta.get('n_agents_succeeded')}/"
                f"{meta.get('n_agents_requested')}, {meta.get('degradation_reason')})")
    except Exception:
        return sh_reason


def _p7_experiment_tag():
    """P7 (B4): the active parameter experiment, materialized + returned best-effort.

    Calling ``active_experiment()`` lazily creates the 'baseline' experiment (whose
    params ARE the current live defaults) so a forecast always runs under a known,
    recorded tag. PURELY INFORMATIONAL — there is NO write path back into
    sizing/gating, so this can never loosen a gate or change a bet. Returns the
    exp_key or None."""
    if _experiments is None:
        return None
    try:
        exp = _experiments.active_experiment()
        return exp.get("exp_key") if isinstance(exp, dict) else None
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="predict_today._p7_experiment_tag", exc=e, action="skip")
            except Exception:
                pass
        return None


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

# ── data-sufficiency gate (B-series WIRE): "no data, no bet" + thin-evidence observe-only ──
# These DO NOT touch the forecast: it is ALWAYS computed + logged + frozen (so the calibration
# backtest keeps learning). They only decide whether the BET is allowed to proceed:
#   * no_data       — the evidence pack gathered NO real items / no sources -> HARD skip (never bet).
#   * low_evidence  — 0 < evidence_quality < MIN_EVIDENCE_QUALITY -> observe-only (forecast logged, no bet).
MIN_EVIDENCE_QUALITY = 0.25

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


def _conviction(swarm_p, challenger_p, consensus, edge, had_data, evidence_quality=None):
    """0..1 conviction from independent reliability signals. Bets that reach this already
    cleared the guards (so divergence is < threshold, consensus >= MIN, label ok).

    `evidence_quality` (optional, 0..1) refines the data term: when supplied, a STRONGER
    evidence base raises conviction and a thin one lowers it — monotonic in quality and
    bounded to the same [0.5, 1.0] band the legacy `had_data` flag produced, so weak
    evidence can NEVER inflate a bet above the had-data baseline. Omitting it preserves the
    exact legacy behavior (1.0 with data, 0.5 without)."""
    if challenger_p is not None:
        agree = 1.0 - min(1.0, abs(swarm_p - challenger_p) / max(1e-9, MAX_SWARM_CHALLENGER_DIVERGENCE))
    else:
        agree = 0.5
    cons = min(1.0, max(0.0, consensus)) if consensus is not None else 0.5
    edge_conf = min(1.0, abs(edge) / CONVICTION_EDGE_FULL)
    if evidence_quality is not None:
        data = 0.5 + 0.5 * min(1.0, max(0.0, evidence_quality))
    else:
        data = 1.0 if had_data else 0.5
    return round(0.40 * agree + 0.30 * cons + 0.20 * edge_conf + 0.10 * data, 3)


def _conviction_sizing(conviction):
    """Map conviction (0..1) -> (lambda, cap) for sizing.size_bet — bigger when surer."""
    lam = CONVICTION_LAM_MIN + conviction * (CONVICTION_LAM_MAX - CONVICTION_LAM_MIN)
    cap = CONVICTION_CAP_MIN + conviction * (CONVICTION_CAP_MAX - CONVICTION_CAP_MIN)
    return round(lam, 4), round(cap, 4)


def _mirofish_report(question: str, market_id: str, price, max_wait: int = None, run_id=None):
    """Run MiroFish FRESH for THIS market, validate it, record the run, and return a
    validated MiroFishResult. It NEVER returns a stale/weak report as usable — the caller
    appends it to the swarm only when result.usable is True (else degraded/no-bet)."""
    from harness import mirofish_signal, mirofish_validate as mfv
    max_wait = max_wait if max_wait is not None else MF_WAIT
    cfg = mfv.config()
    requested_at = mfv._now()
    print("\n[2.5] REPORT — MiroFish crowd sim (FRESH per market, validated)…", flush=True)
    t0 = time.time()
    raw, sid = {}, None
    try:
        from harness import mirofish
        # Phase 2: a UNIQUE project name per market+run+timestamp so the backend can NOT
        # reuse an old (e.g. June-13) completed project for this question.
        pname = mfv.fresh_project_name(market_id, run_id) if cfg["FORCE_FRESH"] else None
        raw = mirofish.forecast_market(question, base=os.getenv("MIROFISH_BASE", "http://localhost:5001"),
                                       max_wait=max_wait, project_name=pname)
        sid = raw.get("simulation_id")
    except Exception as e:
        raw = {"ok": False, "error": f"backend unavailable: {str(e)[:80]}"}
    sig = {}
    try:
        if sid:
            sig = mirofish_signal.crowd_signal(sid, question)
            mirofish_signal.save_signal(market_id, sig, market_odds=price, sim_id=sid)
    except Exception as e:
        if obs:
            try:
                obs.hooks.on_error(where="predict_today._mirofish_report.signal", exc=e, action="skip")
            except Exception:
                pass
    result = mfv.build_result(raw, market_id, question, requested_at, sig, started_at=requested_at)
    mfv.validate(result, cfg)
    mfv.record_run(result, forecast_id=(obs.current().get("forecast_id") if obs else None))
    print(f"      MiroFish: {mfv.status_label(result)} — sim={result.simulation_id or '-'} "
          f"posts={result.n_posts} age={result.report_age_seconds}s match={result.question_match_score:.2f} "
          f"usable={result.usable} ({time.time()-t0:.0f}s)", flush=True)
    if result.warnings:
        print(f"        reason: {'; '.join(result.warnings)[:160]}", flush=True)
    return result


def _render_mf_text(result) -> str:
    """The swarm-context block for a USABLE MiroFish result (never called for stale/weak)."""
    lines = ["[MiroFish multi-agent crowd report — independent simulated agents, NOT an LLM forecast]"]
    if result.crowd_probability is not None:
        lines.append(f"Crowd's implied YES probability: {result.crowd_probability:.0%} "
                     f"(distilled from {result.n_posts} crowd posts)")
    if result.report_markdown:
        lines.append("Crowd report excerpt: " + " ".join(result.report_markdown.split())[:700])
    return "\n".join(lines)


# minimum evidence-pack quality to allow a bet in MIROFISH_MODE=degraded when MiroFish is unusable
MIROFISH_DEGRADED_MIN_EVIDENCE = float(os.getenv("MIROFISH_DEGRADED_MIN_EVIDENCE", "0.40"))


def _p_mirofish_gate(m, pack):
    """Phase 4: enforce MIROFISH_MODE. Returns (ok, reason).
      off / mirofish disabled / mirofish usable -> ok.
      required  + unusable -> NO BET.
      degraded  + unusable -> bet ONLY if the evidence pack is strong enough, else no bet."""
    try:
        from harness import mirofish_validate as _mfv
        mode = _mfv.config()["MODE"]
    except Exception:
        return True, "ok"
    if not USE_MIROFISH or mode == "off":
        return True, "ok"
    if m.get("_mf_usable"):
        return True, "ok"
    status = m.get("_mf_status") or "unusable"
    if mode == "required":
        return False, f"mirofish_required_unusable:{status}"
    if mode == "degraded":
        eq = getattr(pack, "evidence_quality", None) if pack is not None else None
        if eq is not None and eq >= MIROFISH_DEGRADED_MIN_EVIDENCE:
            return True, f"mirofish_degraded_ok(evidence {eq:.2f})"
        return False, f"mirofish_degraded_weak_evidence:{status}"
    return True, "ok"


def _hours_left(m):
    d = _days_until(m.get("end_date"))
    return None if d is None else d * 24.0


# ── window selector (P2) — same_day default; near_term / weekly are the new capability ──
# Env SCANNER_WINDOW or CLI --window picks the time-to-resolution band scanned. The daemon
# stays SAME_DAY by default (resolves TODAY); near_term (1–3d) / weekly (3–7d) exist so the
# SAME pipeline can be pointed further out without any code change.
def _window_name(window=None) -> str:
    """Resolve the scan window: explicit arg > env SCANNER_WINDOW > 'same_day'.
    Anything outside {same_day, near_term, weekly} falls back to same_day."""
    name = str(window or os.getenv("SCANNER_WINDOW") or scanner.SAME_DAY).strip().lower()
    return name if name in scanner.WINDOWS else scanner.SAME_DAY


def _observe_only_for(question):
    """(fine_label, observe_only) for a question. fine_label is the granular theme;
    observe_only consults the P4B label backtest. Fully guarded — a missing table /
    DB never breaks candidate selection (defaults to (theme_or_'other', False))."""
    fine = "other"
    try:
        from harness import scoreboard
        fine = scoreboard.theme_of(question)
    except Exception:
        pass
    try:
        from harness import label_perf
        return fine, bool(label_perf.should_observe_only(fine))
    except Exception:
        return fine, False


def find_candidates(max_hours=24.0, max_n=3, include_mechanical=False, window=None):
    """Markets the AI can predict in the selected resolution WINDOW: liquid, tradeable price,
    not already held, not stale. Opinion (forecastable) markets first.

    P2 wiring: candidates are SOURCED via scanner.scan(window) (default 'same_day' → resolves
    TODAY) and ORDERED via scanner.rank_candidates — a transparent composite that already folds
    in exit-risk / liquidity / forecastability — while PRESERVING the same-day contract:
    opinion-first, held excluded, liquidity floor, untradeable prices dropped. Stale / degenerate
    rows are DROPPED (observe-only — never bet). Each returned candidate keeps the legacy
    _label/_hl/_price keys predict_one consumes, plus scanner's _rank_score / _exit_risk / _why
    for transparency. Sizing and the betting guards are UNCHANGED.
    """
    win_name = _window_name(window)
    same_day = win_name == scanner.SAME_DAY
    # Same-day preserves the legacy contract exactly: 0.5h .. max_hours (default 24), so
    # --max-hours still tightens/loosens the today window. Other windows use their fixed band.
    scan_window = scanner.Window("same_day", 0.5, float(max_hours)) if same_day else scanner.WINDOWS[win_name]

    held = {p["market_id"] for p in wallet.get_open_positions()}
    cands = []
    for m in scanner.scan(scan_window, limit=200):
        mid = m.get("market_id")
        if not mid or mid in held:
            if obs and mid:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, "held")
            continue
        hl = m.get("_hours_left")
        if hl is None:
            hl = _hours_left(m)
        if hl is None or hl < 0.5:        # scanner.scan already bounded the upper end per window
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
        # P2: drop stale / degenerate rows — there's nothing to trade or exit against, so the
        # bettor must never see them (scanner.is_stale also catches expired / never-traded rows).
        stale, why_stale = scanner.is_stale(m)
        if stale:
            if obs:
                obs.hooks.on_classify(mid, m.get("question"), None, None, False, f"stale: {why_stale}")
            continue
        cls = classifier.tag_market(m)
        m["_label"] = cls.label
        m["_hl"] = hl
        m["_price"] = price
        # P4B — consult the label backtest. MARK (never drop) observe-only labels:
        # a fine_label that historically lost money / didn't beat the market is still
        # forecast + logged (so the backtest keeps learning) but is never bet.
        fine, obs_only = _observe_only_for(m.get("question"))
        m["_fine_label"] = fine
        m["_observe_only"] = obs_only
        if obs:
            obs.hooks.on_classify(mid, m.get("question"), cls.label,
                                  getattr(cls, "signals", None),
                                  cls.label != "mechanical", "candidate")
        cands.append(m)

    # P2: transparent composite rank (folds in exit-risk / liquidity / forecastability / …),
    # producing annotated COPIES (_rank_score / _exit_risk / _subscores / _why). rank_candidates
    # recomputes _label/_price/_hours_left identically; we re-attach the legacy _hl key so the
    # rest of the pipeline (predict_one / run_once) is untouched.
    ranked = scanner.rank_candidates(cands)
    for c in ranked:
        c["_hl"] = c.get("_hours_left")
    if same_day:
        # SAME-DAY CONTRACT preserved: opinion (forecastable) strictly FIRST; the exit-risk-aware
        # rank score breaks ties within a label (replacing the old raw-liquidity tiebreak), then
        # soonest. Non-same-day windows keep the pure composite-rank order.
        ranked.sort(key=lambda c: (
            c.get("_label") != "opinion",
            -(c.get("_rank_score") or 0.0),
            c["_hl"] if c.get("_hl") is not None else 1e9,
        ))
    cands = ranked

    if not include_mechanical:
        # Forecastable = anything NOT clearly mechanical. The regex classifier tags genuine
        # news/geopolitical markets (Iran peace deal, Israel airspace) as "unknown" — they are
        # exactly what the swarm should forecast, so keep opinion + unknown and only drop the
        # clearly-mechanical sports/crypto/weather. (sort above already puts opinion first.)
        forecastable = [c for c in cands if c.get("_label") != "mechanical"]
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


# ── P3: event-portfolio wiring (shared by predict_one + sameday.place_sameday) ─────────
# When the current market is a leg of a MULTI-LEG mutually-exclusive event, we stop
# blindly enforcing one-bet-per-event (Guard D) and instead evaluate the WHOLE event as a
# portfolio via harness.event_portfolio.evaluate_event, then act ONLY on this leg's slot in
# the accepted portfolio. Single-leg / non-event markets never reach here — they keep the
# existing per-market guard + conviction-sizing path unchanged.
def build_event_legs(m, swarm_p, price, group_legs):
    """Assemble the event leg set for event_portfolio.evaluate_event from the CURRENT
    forecast leg (this market) plus held SIBLING positions in the same event, and decide
    whether this is a MULTI-LEG mutually-exclusive event (the only case routed to the
    portfolio engine). Returns (is_me_multi, legs, event_key).

    Mutual-exclusivity reuses scanner.group_events (the single source of truth for the ME
    heuristic); as a backward-compatible fallback, any market with a real event_slug that
    already has >=1 held sibling in that slug is treated as a one-winner event (this is
    exactly the set Guard D's one-YES rule used to fire on).

    NOTE on probabilities/prices: the current leg's model_p (``swarm_p`` arg) is the P6
    DECISION probability — the blended + (gated-)calibrated value, which is NUMERICALLY
    IDENTICAL to the raw swarm probability until resolved data accrues; sibling legs carry
    the model_p stored at open and their price is the price AT OPEN (market_p) — we
    deliberately do NOT refetch live prices here (this path stays network-free, e.g. under
    the no-network test suite)."""
    mid = m.get("market_id")
    slug = m.get("event_slug")
    # market-like dicts so scanner.group_events can apply its ME heuristic unchanged
    cur_like = {"market_id": mid, "question": m.get("question"),
                "event_slug": slug, "outcomes": m.get("outcomes")}
    sib_likes = [{"market_id": o.get("market_id"), "question": o.get("question"),
                  "event_slug": o.get("event_slug"), "outcomes": None} for o in (group_legs or [])]
    try:
        events = scanner.group_events([cur_like] + sib_likes)
    except Exception:
        events = []
    key = slug or mid
    event = next((e for e in events if e.get("key") == key), (events[0] if events else None))
    event_me = bool(event and event.get("mutually_exclusive") and event.get("n_legs", 0) >= 2)
    shared_slug = bool(slug) and len(group_legs or []) >= 1
    is_me_multi = event_me or shared_slug

    legs = [{"leg_id": mid, "market_id": mid, "model_p": swarm_p, "price": price,
             "liquidity": m.get("liquidity"), "exit_risk": m.get("_exit_risk"), "has_data": True}]
    for o in (group_legs or []):
        legs.append({"leg_id": o.get("market_id"), "market_id": o.get("market_id"),
                     "model_p": o.get("model_p"), "price": o.get("market_p"),
                     "liquidity": None, "exit_risk": None,
                     "has_data": o.get("model_p") is not None})
    return is_me_multi, legs, key


def run_event_portfolio(mid, legs, event_key, bankroll):
    """Evaluate the whole event as a portfolio and return (ep, my_pos) where ep is the
    EventPortfolio and my_pos is THIS leg's accepted position dict (or None if the leg was
    not selected / the event was rejected). Emits the 'event.portfolio' obs event. The
    event is treated as mutually-exclusive (one winner) — that is why it was routed here."""
    ep = event_portfolio.evaluate_event(
        legs, bankroll, cfg=event_portfolio.Config(mutually_exclusive=True))
    my_pos = None
    safety_reason = None
    if ep.accept:
        my_pos = next((p for p in ep.positions if p.get("leg_id") == mid), None)
        # ── Plan 6: EXECUTION safety. A multi-leg / arbitrage basket is recommendation-
        #    ONLY (no atomic multi-leg executor; we open one leg at a time over an
        #    incomplete, stale legset) → DISABLED. Only a single-leg edge opportunity
        #    may execute, and only if it is coherent with the open book (one-YES). ──
        _label, _executable, _reason = _esafe.classify_event_execution(ep, my_pos)
        if not _executable:
            my_pos, safety_reason = None, _reason
        else:
            # FAIL-CLOSED (consistent with the Plan-1 money gates): if coherence cannot be
            # verified, do NOT open the leg — never silently skip the one-YES check.
            try:
                _coh = _esafe.check_event_position_coherence(
                    event_key, (my_pos or {}).get("side"), mid, wallet.get_open_positions())
            except Exception as _e:
                _coh = {"ok": False, "reason": _esafe.INCOHERENT_POSITION}
                if obs:
                    try:
                        obs.hooks.on_error(where="predict_today.run_event_portfolio.coherence",
                                           exc=_e, action="fail-closed-block")
                    except Exception:
                        pass
            if not _coh["ok"]:
                my_pos, safety_reason = None, _coh["reason"]
    # stash the Plan-6 reason so event_leg_reject_reason can surface the exact code
    try:
        ep._event_safety_reason = safety_reason
    except Exception:
        pass
    if obs:
        obs.hooks.on_event_portfolio(
            forecast_id=(obs.current().get("forecast_id") if obs else None),
            event_key=event_key,
            market_id=mid,
            accept=ep.accept,
            positions=ep.positions,
            rejected=ep.rejected,
            portfolio_ev=ep.portfolio_ev,
            worst_case_loss=ep.worst_case_loss,
            max_exposure=ep.max_exposure,
            losing_outcome=ep.losing_outcome,
            reject_reason=ep.reject_reason,
            mutually_exclusive=ep.mutually_exclusive,
            is_arbitrage=ep.is_arbitrage,
            explanation=ep.explanation,
        )
    return ep, my_pos


def event_leg_reject_reason(ep, mid):
    """Human-readable reason THIS leg is not being bet under the event portfolio."""
    # Plan 6: a multi-leg / arbitrage basket or an incoherent add surfaces its exact code.
    sr = getattr(ep, "_event_safety_reason", None)
    if sr:
        return sr
    if ep.reject_reason:
        return f"event portfolio rejected: {ep.reject_reason}"
    for r in (ep.rejected or []):
        if r.get("leg_id") == mid:
            return f"event portfolio skipped this leg: {r.get('reason')}"
    return "leg not selected in the accepted event portfolio"


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


def _evidence_guard(pack):
    """Data-sufficiency gate (pure; no I/O). Decides whether the evidence behind a forecast
    is enough to allow a BET (the forecast itself is logged/frozen regardless). Returns
    (ok, reason):
      * (False, "no_data")           — the pack gathered NO real items / no sources at all.
      * (False, "low_evidence:X.XX") — 0 < evidence_quality < MIN_EVIDENCE_QUALITY (thin).
      * (True,  "ok")                — enough evidence; the bet may proceed.
    """
    if pack is None:
        return False, "no_data"
    total = getattr(pack, "total_items", 0) or 0
    nsrc = getattr(pack, "n_sources", 0) or 0
    if total <= 0 or nsrc <= 0:
        return False, "no_data"
    q = getattr(pack, "evidence_quality", 0.0) or 0.0
    if 0.0 < q < MIN_EVIDENCE_QUALITY:
        return False, f"low_evidence:{q:.2f}"
    return True, "ok"


def _emit_evidence_pack(mid, pack):
    """Best-effort: freeze the EXACT evidence bundle behind this forecast for replay
    (obs 'evidence.pack'). Observation-only — never changes logic and never raises; the
    heavy pack text is stored once in a blob, the JSONL line carries hashes + a summary."""
    if not obs or pack is None:
        return
    try:
        import json as _json
        summary = "; ".join(f"{s.name}:{s.item_count}(q{s.quality:.2f})"
                             for s in getattr(pack, "sources", [])) or "(none)"
        obs.hooks.on_evidence_pack(
            forecast_id=(obs.current().get("forecast_id") if obs else None),
            market_id=mid,
            content_hash=pack.content_hash,
            n_sources=pack.n_sources,
            total_items=pack.total_items,
            evidence_quality=pack.evidence_quality,
            sources_summary=summary,
            pack_json=_json.dumps(pack.to_dict(), ensure_ascii=False, default=str),
        )
    except Exception as e:
        if obs:
            obs.hooks.on_error(where="predict_today._emit_evidence_pack", exc=e, action="skip")


def _decision_probability(swarm_p, challenger_p):
    """P6 — fold the raw swarm probability and the challenger ensemble into the
    SINGLE decision probability, in the order the spec fixes:

        1. WEIGHTED BLEND   — weight swarm vs challenger by their realized per-forecaster
                              Brier skill (harness.forecaster_weights).
        2. GATED CALIBRATE  — calibrate the blend against resolved swarm history, but only
                              once enough has accrued (harness.calibration_apply).

    Returns ``(final_p, blended, weights, cal)`` so the caller can both SIZE on
    ``final_p`` and RECORD the full context. Pure-ish: no network / no LLM (only
    local-DB reads behind best-effort guards).

    COLD-START INVARIANT (today: 0 resolved opinion markets): ``forecaster_weights``
    returns ``{"swarm":1.0,"challenger":0.0}`` until BOTH forecasters have a real
    track record, so ``blend_forecasters`` returns ``swarm_p`` EXACTLY (object
    identity, no float math); ``apply_calibration`` is an exact passthrough below
    its n>=30 floor, so ``calibrated_p == swarm_p``. Hence ``final_p == swarm_p``
    to full float precision — NUMERICALLY IDENTICAL to the pre-P6 decision p."""
    w = forecaster_weights_mod.forecaster_weights()
    blended = forecaster_weights_mod.blend_forecasters(swarm_p, challenger_p, w)
    cal = calibration_apply.apply_calibration(blended)
    return cal["calibrated_p"], blended, w, cal


def _record_forecast_version(mid, q, swarm_p, ens, blended, final_p, weights, cal):
    """Best-effort append of the FULL forecast context (raw swarm p, the challenger
    ensemble + per-model probs, the blended p, the calibrated decision p, the
    per-forecaster weights, the calibration method/data-count) to the
    forecast_versions audit table. PASSIVE — never feeds back into a decision and
    never raises into the forecast path. This is the P6 'OBS' step: the blend /
    calibration / weights are stashed here (no new canonical obs event needed)."""
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
                obs.hooks.on_error(where="predict_today._record_forecast_version", exc=e, action="skip")
            except Exception:
                pass


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

        # 2 — GATHER — ONE canonical evidence pack. `enr` (= pack.text) is BYTE-IDENTICAL to
        #    the legacy _build_enrichment join, so the swarm sees exactly the same context text.
        print("\n[2/4] GATHER — pulling GDELT news/sentiment + microstructure signals…", flush=True)
        t0 = time.time()
        pack = build_pack(m, cfg)
        enr = pack.text
        print(f"      gathered {len(enr)} chars of real context in {time.time()-t0:.0f}s "
              f"({pack.n_sources} sources, {pack.total_items} items, evidence quality {pack.evidence_quality:.2f})")
        for line in (enr.splitlines()[:8] if enr else ["(no external context found for this market)"]):
            print("      | " + line[:100])
        # freeze the EXACT evidence bundle behind this forecast for replay (best-effort).
        _emit_evidence_pack(mid, pack)

        # 2.5 — REPORT (MiroFish crowd sim — validated; a stale/weak report is NEVER fed to the
        #       swarm, and MIROFISH_MODE decides whether an unusable report blocks the bet).
        from harness import mirofish_validate as _mfv
        _mf_mode = _mfv.config()["MODE"]
        m["_mf_status"], m["_mf_usable"], m["_mf_reason"] = "skipped", None, "not run"
        m["_mf_sim_id"], m["_mf_report_hash"], m["_mf_n_posts"], m["_mf_age"] = None, None, 0, None
        if not USE_MIROFISH or getattr(cfg, "dry_run", False) or _mf_mode == "off":
            if _mf_mode == "off":
                m["_mf_status"], m["_mf_reason"] = "disabled", "mirofish_disabled (MIROFISH_MODE=off)"
                print("\n[2.5] REPORT — MiroFish DISABLED (MIROFISH_MODE=off)", flush=True)
        else:
            _mfres = _mirofish_report(q, mid, price, run_id=(obs.current().get("run_id") if obs else None))
            m["_mf_status"] = _mfv.status_label(_mfres)
            m["_mf_usable"] = _mfres.usable
            m["_mf_reason"] = "; ".join(_mfres.warnings) if _mfres.warnings else ("usable" if _mfres.usable else "")
            m["_mf_sim_id"], m["_mf_report_hash"] = _mfres.simulation_id, _mfres.report_markdown_hash
            m["_mf_n_posts"], m["_mf_age"] = _mfres.n_posts, _mfres.report_age_seconds
            if _mfres.usable:                     # ONLY a fresh, market-specific report feeds the swarm
                mf = _render_mf_text(_mfres)
                enr = (enr + "\n\n" + mf) if enr else mf
            # a stale/weak/failed report is NEVER appended (no silent pretend-success).

        # 3 — THINK (the swarm LLM processes the gathered data + the crowd report)
        print(f"\n[3/4] THINK — {cfg.swarm_size}-persona swarm forecasting WITH that data (slow on CPU)…", flush=True)
        t0 = time.time()
        p, meta = _forecast(m, price, cfg, enr)
        print(f"      swarm: {p:.1%} YES vs market {price:.0%} · regime={meta.get('regime')} "
              f"· consensus={meta.get('consensus')} · {time.time()-t0:.0f}s")
        # ── P6 step 1: CHALLENGER ENSEMBLE (replaces the single challenger call). The DEFAULT
        #    1-model roster makes ens["mean"] NUMERICALLY IDENTICAL to today's single bp (no
        #    extra LLM load on the CPU box). bp keeps feeding the divergence/consensus guards
        #    UNCHANGED. Extra models activate only when CHALLENGER_MODELS is set. ──
        bp = None
        ens = {"per_model": {}, "probs": [], "mean": None, "n": 0, "models": []}
        try:
            if not getattr(cfg, "dry_run", False):   # --dry-run skips the LLM challenger call
                ens = challenger.ensemble_forecast(q, price, enr)
            bp = ens.get("mean")
            if bp is not None:
                challenger.save_baseline(mid, q, bp, price)
                roster = ens.get("models") or []
                tag = f"[{ens.get('n', 0)}/{len(roster)} model(s)]" if len(roster) > 1 else "(A/B)"
                print(f"      challenger ensemble {tag}: {bp:.1%} YES")
        except Exception as e:
            if obs:
                obs.hooks.on_error(where="predict_today.predict_one.challenger", exc=e, action="skip")

        # ── P6 steps 2+3: WEIGHTED BLEND (swarm vs challenger by realized Brier) → GATED
        #    CALIBRATION → the SINGLE decision probability `final_p`. ORDER MATTERS. Both are
        #    HARD cold-start passthroughs: today (0 resolved opinion markets) final_p == p to
        #    full float precision, so sizing is NUMERICALLY IDENTICAL to the pre-P6 value. The
        #    new machinery only activates as resolved per-forecaster / calibration data accrues.
        final_p, blended, weights, cal = _decision_probability(p, bp)
        if final_p != p:
            print(f"      P6 decision p: swarm {p:.1%} → blended {blended:.1%} → "
                  f"calibrated {final_p:.1%} (method={cal.get('method')}, n_hist={cal.get('n_history')})")
        # P6 step 5/6 (VERSION + OBS): stash the full context (swarm p, ensemble, blend, calibrated
        # decision p, weights, calibration method/n) in the forecast_versions audit table. Passive.
        _record_forecast_version(mid, q, p, ens, blended, final_p, weights, cal)
        # P7 (B4): tag this forecast with the ACTIVE parameter experiment (baseline today;
        # no auto-switch). Materializes + logs the tag so the resolved outcome can be
        # attributed at settle. Passive/best-effort — never feeds back into the decision.
        _p7_exp_key = _p7_experiment_tag()
        if _p7_exp_key:
            print(f"      experiment: running under '{_p7_exp_key}' parameter set")

        # ── Plan 2: SWARM-HEALTH gate. A degraded / aborted / under-strength swarm
        #    forecast must never be bet on (and must never look like a healthy
        #    high-consensus signal). The forecast above is already logged/frozen; this
        #    only WITHHOLDS the bet. Runs BEFORE sizing AND before BOTH the
        #    event-portfolio and single-market bet paths, so neither can bet on it. ──
        sh_ok, sh_reason = _p_swarm_health(meta)
        if not sh_ok:
            print("\n[4/4] BET — swarm-health gate (no bet)…", flush=True)
            return _skip(mid, q, _swarm_health_skip_reason(sh_reason, meta), p=p, price=price)

        # ── post-forecast reliability guards B/C/D(b): need swarm p, challenger bp, consensus.
        #    (group re-read here too — a sibling leg may have been bet during the long forecast.) ──
        group_legs = _event_group_legs(mid, group_key)
        # P3: is this a MULTI-LEG mutually-exclusive event? If so, Guard D's one-YES rule is
        # REPLACED by the event-portfolio engine — so we pass NO group legs to _betting_guards
        # (Guard D is then a no-op) and run the reliability guards B/C only; the portfolio owns
        # event coherence. Single-leg / non-event markets keep the full per-market guard path.
        # The event-portfolio leg sizes on the DECISION probability `final_p` (== p today).
        is_me_multi, ep_legs, event_key = build_event_legs(m, final_p, price, group_legs)
        guard_legs = [] if is_me_multi else group_legs
        # P6: the divergence/consensus guards stay on the RAW swarm p vs the RAW challenger bp,
        # so a downstream blend/calibration step can NEVER mask genuine model disagreement.
        ok, reason = _betting_guards(label, p, bp, meta.get("consensus"), guard_legs, price)
        if not ok:
            print("\n[4/4] BET — reliability guard…", flush=True)
            return _skip(mid, q, reason, p=p, price=price)

        # ── data-sufficiency gate ("no data, no bet"): the forecast above is already computed +
        #    logged + frozen; here we only WITHHOLD the bet. no_data (no real evidence at all) is a
        #    HARD skip; low_evidence (0 < quality < MIN_EVIDENCE_QUALITY) is observe-only. Runs
        #    BEFORE sizing AND before the multi-leg event path, so neither path bets without data. ──
        ev_ok, ev_reason = _evidence_guard(pack)
        if not ev_ok:
            print("\n[4/4] BET — evidence gate (no bet)…", flush=True)
            return _skip(mid, q, ev_reason, p=p, price=price)

        # ── MiroFish gate (Phase 4): enforce MIROFISH_MODE. required + unusable -> no bet;
        #    degraded + unusable -> bet only if OTHER evidence is strong, else observe-only.
        #    The forecast above is already logged/frozen; this only withholds the BET. ──
        mf_ok, mf_reason = _p_mirofish_gate(m, pack)
        if not mf_ok:
            print("\n[4/4] BET — MiroFish gate (no bet)…", flush=True)
            return _skip(mid, q, mf_reason, p=p, price=price)

        # ── P4B: OBSERVE-ONLY label — the forecast above is saved + logged, but this
        #    fine_label historically lost money / didn't beat the market, so NO bet is
        #    placed. We mark, not drop: the forecast keeps feeding the label backtest. ──
        if m.get("_observe_only"):
            fine = m.get("_fine_label") or _observe_only_for(q)[0]
            print("\n[4/4] BET — observe-only label (no bet)…", flush=True)
            return _skip(mid, q, f"observe_only:{fine}", p=p, price=price)

        # ── P9: bankroll KILL SWITCH — pause NEW bets under drawdown / loss-limit /
        #    losing-streak cooldown. The forecast above is already logged + frozen for
        #    scoring; this only WITHHOLDS the bet (observe-only). Fail-open. ──
        ct_ok, ct_reason = _p9_can_trade()
        if not ct_ok:
            print("\n[4/4] BET — bankroll kill switch (no bet)…", flush=True)
            return _skip(mid, q, ct_reason, p=p, price=price)

        # 4 — BET — MULTI-LEG ME event → evaluate the WHOLE event as a portfolio, act on THIS leg.
        if is_me_multi:
            print("\n[4/4] BET — multi-leg event: evaluating the whole event as a portfolio…", flush=True)
            ep, my_pos = run_event_portfolio(mid, ep_legs, event_key, wallet.bankroll_for_sizing())
            if not (ep.accept and my_pos is not None):
                return _skip(mid, q, event_leg_reject_reason(ep, mid), p=p, price=price)
            side, stake, edge = my_pos["side"], my_pos["stake"], my_pos["edge"]
            # P7 (B1): EV-after-costs HARD GATE on the accepted event leg. Side is now
            # known; reject if the slippage-worsened fill is non-positive-EV (pure
            # tightening — a healthy +edge leg is unaffected). Uses the wallet cost model.
            ev_ok, ev_reason = _p7_ev_gate(final_p, price, side, m=m, confidence=meta.get("consensus"))
            if not ev_ok:
                return _skip(mid, q, ev_reason, p=p, price=price)
            # P8: unified adaptive risk guards (market-quality + correlation + bad-theme),
            # STRICTER under book drawdown. Fail-open. Pure tightening — a clean market in
            # a healthy book is unaffected.
            rg_ok, rg_reason = _p8_risk_guards(m, side, q)
            if not rg_ok:
                return _skip(mid, q, rg_reason, p=p, price=price)
            # P9: per-theme / per-event stake exposure cap (concentration limit).
            ex_ok, ex_reason = _p9_exposure_ok(q, m.get("event_slug"), stake)
            if not ex_ok:
                return _skip(mid, q, ex_reason, p=p, price=price)
            regime = meta.get("regime", "")
            sig = "LONG (YES)" if side == "YES" else "SHORT (NO)"
            print(f"      event portfolio ACCEPTS this leg → {side} ${stake:.2f} "
                  f"(portfolio EV ${ep.portfolio_ev:+.2f}, worst-case ${ep.worst_case_loss:+.2f}, "
                  f"max exposure ${ep.max_exposure:.2f})")
            # P6: size/record on the DECISION probability final_p (== raw swarm p today).
            fr = wallet.open_position(mid, q, side, final_p, price, edge, stake,
                                      cfg=wallet.WalletConfig(max_bet_frac=CONVICTION_CAP_MAX, max_exposure_frac=0.95),
                                      end_date=m.get("end_date"), event_slug=m.get("event_slug"))
            if fr.opened:
                why = (f"Event-portfolio (multi-leg mutually-exclusive): swarm {p:.0%} vs market {price:.0%}. "
                       f"Portfolio EV ${ep.portfolio_ev:+.2f}, worst-case ${ep.worst_case_loss:+.2f}. "
                       f"Bought {fr.side} @ {fr.fill_price:.3f} with ${fr.stake:.2f}. Resolves in {hl:.1f}h (today).")
                journal.record_decision(mid, q, final_p, price, edge, fr.side, fr.stake, fr.fill_price, regime, sig, "bet", why)
                print(f"      BET PLACED: {fr.side} ${fr.stake:.2f} @ {fr.fill_price:.3f} — resolves in {hl:.1f}h")
                return True
            print(f"      bet rejected by wallet: {fr.reason}")
            if obs:
                obs.hooks.on_trade_skip(
                    forecast_id=(obs.current().get("forecast_id") if obs else None),
                    reason=f"wallet_rejected: {fr.reason}",
                    inputs={"market_id": mid, "side": side, "stake": stake, "layer": "wallet"},
                )
            return False

        # 4 — BET — conviction-scaled stake (bigger when surer); size_bet mechanics UNCHANGED.
        # P6: conviction + Kelly size on the DECISION probability final_p (== raw swarm p today).
        # The divergence guard above already ran on RAW p vs bp, so blend/calibration can't sneak
        # a bet past it; final_p only refines the magnitude once calibration/weighting activate.
        print("\n[4/4] BET — sizing on the swarm's edge vs the market…", flush=True)
        conv = _conviction(final_p, bp, meta.get("consensus"), final_p - price, bool(enr),
                           evidence_quality=pack.evidence_quality)
        lam, cap = _conviction_sizing(conv)
        print(f"      conviction {conv:.2f} → {lam:g}x Kelly, cap {cap:.0%}")
        # P7 (B3): per-theme adaptive min_edge, FLOORED at cfg.min_edge (cold start ==
        # cfg.min_edge exactly, so sizing is unchanged today; only RAISES for a theme
        # with a real losing track record). Replaces the hardcoded cfg.min_edge.
        me = _p7_adaptive_min_edge(q, cfg.min_edge)
        sz = sizing.size_bet(final_p, price, wallet.bankroll_for_sizing(), lam=lam, cap=cap, min_edge=me)
        print(f"      edge {sz.edge:+.1%} → {sz.reason}")
        regime = meta.get("regime", "")
        sig = "LONG (YES)" if sz.edge > 0 else "SHORT (NO)"
        if sz.side is None:
            why = (f"AI pipeline: gathered GDELT+signals, swarm {p:.0%} vs market {price:.0%} "
                   f"(edge {sz.edge:+.1%}). No bet — {sz.reason}.")
            journal.record_decision(mid, q, final_p, price, sz.edge, None, 0.0, None, regime, "no edge", "no_bet", why)
            print("      DECISION: NO BET — edge below threshold")
            if obs:
                obs.hooks.on_trade_skip(
                    forecast_id=(obs.current().get("forecast_id") if obs else None),
                    reason="edge_below_threshold",
                    inputs={"market_id": mid, "p": p, "price": price, "edge": sz.edge, "layer": "sizer"},
                )
            return False
        # P7 (B1): EV-after-costs HARD GATE. Side + stake are known and the edge cleared
        # min_edge; reject a bet whose slippage-worsened fill is non-positive-EV (pure
        # tightening — a healthy +edge bet passes untouched). Same cost model as the wallet.
        ev_ok, ev_reason = _p7_ev_gate(final_p, price, sz.side, m=m, confidence=meta.get("consensus"))
        if not ev_ok:
            return _skip(mid, q, ev_reason, p=p, price=price)
        # P8: unified adaptive risk guards (stale/low-liquidity/high-spread + correlation
        # + bad-theme), STRICTER under book drawdown. Fail-open; pure tightening so a clean
        # liquid market in a healthy book is never blocked here.
        rg_ok, rg_reason = _p8_risk_guards(m, sz.side, q)
        if not rg_ok:
            return _skip(mid, q, rg_reason, p=p, price=price)
        # P9: per-theme / per-event stake exposure cap (concentration limit).
        ex_ok, ex_reason = _p9_exposure_ok(q, m.get("event_slug"), sz.stake)
        if not ex_ok:
            return _skip(mid, q, ex_reason, p=p, price=price)
        # Give the precise AI pipeline its own exposure headroom so the daemon's price-rule
        # positions don't crowd out its (small, Kelly-capped) data-driven bets.
        fr = wallet.open_position(mid, q, sz.side, final_p, price, sz.edge, sz.stake,
                                  cfg=wallet.WalletConfig(max_bet_frac=CONVICTION_CAP_MAX, max_exposure_frac=0.95),
                                  end_date=m.get("end_date"), event_slug=m.get("event_slug"))
        if fr.opened:
            why = (f"AI pipeline (data-driven): gathered GDELT news + microstructure, the {cfg.swarm_size}-persona "
                   f"swarm forecast {p:.0%} vs market {price:.0%} → {sz.edge:+.1%} edge. Bought {fr.side} @ "
                   f"{fr.fill_price:.3f} with ${fr.stake:.2f} ({sz.reason}). Resolves in {hl:.1f}h (today).")
            journal.record_decision(mid, q, final_p, price, sz.edge, fr.side, fr.stake, fr.fill_price, regime, sig, "bet", why)
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


def run_once(cfg, max_n=3, max_hours=24.0, include_mech=False, window=None):
    win = _window_name(window)
    with contextlib.ExitStack() as _es:
        if obs:
            _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
            obs.hooks.on_run_start(_obs_run_config(cfg), _obs_bankroll())
        _provenance.record_config_snapshot()  # P12: config-change event (idempotent)
        print(f"[1/4] FIND — scanning {win} markets the AI can predict…", flush=True)
        cands = find_candidates(max_hours=max_hours, max_n=max_n, include_mechanical=include_mech, window=win)
        if not cands:
            print(f"      No {win} markets fit (in window + liquid + tradeable + not stale).")
            if obs:
                obs.hooks.on_run_end({"bets": 0, "candidates": 0})
            return 0
        print(f"      Picked {len(cands)} {win} market(s):")
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


def daemon(cfg, max_hours=24.0, interval=60, include_mech=False, window=None):
    """Continuous PRECISE pipeline: settle -> ONE deep find/gather/think/bet per cycle -> repeat.
    One forecast per cycle because each is slow (minutes) on the CPU. No price rule."""
    win = _window_name(window)
    done: set[str] = set()
    last_key = None
    idle_interval = max(interval, IDLE_INTERVAL)
    print(f"\n  PRECISE AI DAEMON — find->gather->think->bet, {win} window, no price rule. No stopping.\n", flush=True)
    while True:
        cands = []
        worked = False
        try:
            with contextlib.ExitStack() as _es:
                if obs:
                    _es.enter_context(obs.run_ctx(run_id=obs.mint("run")))
                    obs.hooks.on_run_start(_obs_run_config(cfg), _obs_bankroll())
                    _provenance.record_config_snapshot()  # P12: config-change event
                worked = bool(_settle())              # a settlement is real work (P&L moved)
                cands = [c for c in find_candidates(max_hours=max_hours, max_n=12,
                                                    include_mechanical=include_mech, window=win)
                         if c["market_id"] not in done]
                if not cands:
                    print(f"[predict] no fresh {win} market to forecast right now — waiting…", flush=True)
                else:
                    m = cands[0]                      # one deep forecast per cycle (slow)
                    done.add(m["market_id"])
                    print("=" * 78)
                    # crash-safety: one bad market must never kill the cycle, so the
                    # snapshot/heartbeat/run_end below still run for this cycle.
                    try:
                        predict_one(m, cfg)
                    except Exception as e:
                        if obs:
                            obs.hooks.on_error(where="predict_today.daemon.market", exc=e, action="skip")
                        print(f"[predict] market error ({type(e).__name__}): {e}", flush=True)
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


def _print_config_summary(cfg, win, args):
    """Phase-5 startup config summary (no secrets) so the operator sees how the
    daemon is configured before it runs."""
    print("  ── config ────────────────────────────────────────────────")
    print(f"   command={args.command}  window={win}  max_hours={args.max_hours:.0f}h  interval={args.interval}s")
    print(f"   provider={os.getenv('LLM_PROVIDER', 'ollama')}  model={os.getenv('MODEL_FAST', '(default)')}  "
          f"swarm_size={cfg.swarm_size}  rounds={cfg.rounds}  min_edge={cfg.min_edge}")
    print(f"   mirofish={'on' if USE_MIROFISH else 'off'}  dry_run={getattr(cfg, 'dry_run', False)}  "
          f"trading=PAPER (real-money execution disabled)")
    print("  ──────────────────────────────────────────────────────────")


def main(argv=None):
    import argparse
    argv = argv if argv is not None else sys.argv[1:]
    global USE_MIROFISH, MF_WAIT
    p = argparse.ArgumentParser(
        prog="harness.predict_today",
        description="Precise AI pipeline: find -> gather -> think -> bet (PAPER only).")
    p.add_argument("command", nargs="?", default="once", choices=["once", "daemon"],
                   help="once = one pass; daemon = continuous loop")
    p.add_argument("--max", type=int, default=3, dest="max_n")
    p.add_argument("--size", type=int, default=6)
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--min-edge", type=float, default=0.03, dest="min_edge")
    p.add_argument("--max-hours", type=float, default=24.0, dest="max_hours")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--include-mechanical", action="store_true", dest="include_mech")
    p.add_argument("--window", default=None)
    p.add_argument("--with-mirofish", action="store_true")
    p.add_argument("--mf-wait", type=int, default=None, dest="mf_wait")
    p.add_argument("--dry-run", action="store_true",
                   help="stub forecast — NO LLM/MiroFish; exercises the pipeline fast")
    # argparse now errors loudly on a missing flag value / unknown flag / bad command,
    # instead of IndexError-crashing or silently swallowing --dry-run (audit #11).
    args = p.parse_args(argv)

    if args.with_mirofish:
        USE_MIROFISH = True
    if args.mf_wait is not None:
        MF_WAIT = args.mf_wait

    win = _window_name(args.window)
    from core.calibration import init_db
    init_db(); wallet.init_wallet(1000.0); journal.init_journal()
    cfg = LoopConfig(swarm_size=args.size, rounds=args.rounds, min_edge=args.min_edge,
                     use_gdelt=True, use_signals=True, challenger=True, dry_run=args.dry_run)
    _print_config_summary(cfg, win, args)

    if args.command == "daemon":
        daemon(cfg, max_hours=args.max_hours, interval=args.interval, include_mech=args.include_mech, window=win)
    else:
        print(f"\n  PRECISE {win} AI pipeline — find -> gather -> think -> bet ({win}, <{args.max_hours:.0f}h)\n")
        run_once(cfg, max_n=args.max_n, max_hours=args.max_hours, include_mech=args.include_mech, window=win)


if __name__ == "__main__":
    main()
