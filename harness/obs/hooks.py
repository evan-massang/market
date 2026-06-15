"""obs.hooks — thin, fully-guarded payload builders for live call sites.

Each hook builds an event dict, calls eventlog.emit(...), and (where noted)
writes frozen evidence via evidence.*. Every hook is wrapped so it degrades to
a no-op on any internal failure — a running daemon must never crash because of
a logging hook. All hooks also short-circuit to None when OBS is disabled.
"""

import functools
import traceback
from datetime import datetime, timezone

from . import config
from . import eventlog
from . import evidence
from . import blobs
from . import redact
from . import codeversion
from . import ids as _ids


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hook(fn):
    """Decorator: no-op when disabled; swallow every exception -> return None."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not config.enabled():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    return wrapper


# ── run lifecycle ─────────────────────────────────────────────────────────────
@_hook
def on_run_start(config_dict, bankroll):
    repro = codeversion.reproducibility()
    cfg = redact.scrub_obj(config_dict) if config_dict is not None else None
    return eventlog.emit(
        "run.start", config=cfg, bankroll=bankroll, reproducibility=repro
    )


@_hook
def on_run_end(counters):
    return eventlog.emit("run.end", counters=counters)


# ── data ────────────────────────────────────────────────────────────────────--
@_hook
def on_data_fetch(source, endpoint, params, raw_text, item_count, latency_ms):
    raw_hash, blob_ref = blobs.store_blob(raw_text if raw_text is not None else "")
    return eventlog.emit(
        "data.fetch",
        source=source,
        endpoint=endpoint,
        params=params,
        raw_hash=raw_hash,
        blob_ref=blob_ref,
        item_count=item_count,
        latency_ms=latency_ms,
    )


# ── classification ─────────────────────────────────────────────────────────────
@_hook
def on_classify(market_id, question, label, signals, included, reason):
    return eventlog.emit(
        "classify.decision",
        market_id=market_id,
        question=question,
        label=label,
        signals=signals,
        included=included,
        reason=reason,
    )


# ── forecasting ────────────────────────────────────────────────────────────────
@_hook
def on_forecast_start(forecast_id, market_id, question, market_price):
    return eventlog.emit(
        "forecast.start",
        forecast_id=forecast_id,
        market_id=market_id,
        question=question,
        market_price=market_price,
    )


@_hook
def on_llm_call(
    provider,
    model,
    system,
    user,
    completion,
    tokens_in,
    tokens_out,
    latency_ms,
    role,
    retries=0,
    error=None,
):
    prompt_text = (system or "") + "\n\n" + (user or "")
    prompt_hash, prompt_ref = blobs.store_blob(prompt_text)
    completion_hash, completion_ref = blobs.store_blob(completion or "")
    return eventlog.emit(
        "llm.call",
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        prompt_ref=prompt_ref,
        completion_hash=completion_hash,
        completion_ref=completion_ref,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        role=role,
        retries=retries,
        error=error,
        cost_usd=0,
    )


@_hook
def on_agent_estimate(
    agent_id, forecast_id, persona, probability, confidence, reasoning, round
):
    return eventlog.emit(
        "agent.estimate",
        agent_id=agent_id,
        forecast_id=forecast_id,
        persona=persona,
        probability=probability,
        confidence=confidence,
        reasoning=reasoning,
        round=round,
    )


@_hook
def on_debate_round(forecast_id, round_n, estimates):
    return eventlog.emit(
        "debate.round",
        forecast_id=forecast_id,
        round_n=round_n,
        estimates=estimates,
    )


@_hook
def on_blend(forecast_id, method, prior, output_probability, consensus_score):
    return eventlog.emit(
        "blend.compute",
        forecast_id=forecast_id,
        method=method,
        prior=prior,
        output_probability=output_probability,
        consensus_score=consensus_score,
    )


@_hook
def on_forecast_final(
    forecast_id,
    market_id,
    model_probability,
    market_probability,
    edge,
    consensus,
    reasoning_summary,
):
    ctx = _ids.current()
    run_id = ctx.get("run_id") or "norun"
    question = ctx.get("question")  # carried through ctx if a call site set it
    ts = _now_iso()
    canonical = {
        "forecast_id": forecast_id,
        "market_id": market_id,
        "model_probability": model_probability,
        "market_probability": market_probability,
        "edge": edge,
        "consensus": consensus,
    }
    rh = evidence.record_hash(canonical)
    ev = eventlog.emit(
        "forecast.final",
        forecast_id=forecast_id,
        market_id=market_id,
        model_probability=model_probability,
        market_probability=market_probability,
        edge=edge,
        consensus=consensus,
        reasoning_summary=reasoning_summary,
        record_hash=rh,
    )
    evidence.freeze_forecast(
        forecast_id,
        market_id,
        run_id,
        question,
        model_probability,
        market_probability,
        edge,
        consensus,
        ts,
        rh,
    )
    return ev


# ── evidence pack (B3 — replayable evidence) ────────────────────────────────────
@_hook
def on_evidence_pack(
    forecast_id,
    market_id,
    content_hash,
    n_sources,
    total_items,
    evidence_quality,
    sources_summary,
    pack_json,
):
    """Freeze the EXACT evidence bundle that backed a forecast so it is replayable.

    Stores the full ``pack_json`` once via ``blobs.store_blob`` (content-addressed
    and secret-scrubbed before it ever touches disk) and emits an ``evidence.pack``
    event referencing that blob (``blob_ref`` + ``blob_hash``) alongside the
    caller's integrity ``content_hash``, a compact per-source ``sources_summary``,
    the overall ``evidence_quality`` score, the ``n_sources`` / ``total_items``
    counts, and the ``forecast_id`` / ``market_id`` it belongs to. The heavy pack
    text lives ONLY in the blob; the JSONL line carries just the hashes + summary
    so explain()/replay() can join the full evidence back on demand — making the
    exact evidence used at decision time reconstructable.

    Observation-only; degrades to a no-op on any internal failure.
    """
    blob_hash, blob_ref = blobs.store_blob(pack_json if pack_json is not None else "")
    return eventlog.emit(
        "evidence.pack",
        forecast_id=forecast_id,
        market_id=market_id,
        content_hash=content_hash,
        blob_hash=blob_hash,
        blob_ref=blob_ref,
        n_sources=n_sources,
        total_items=total_items,
        evidence_quality=evidence_quality,
        sources_summary=sources_summary,
    )


# ── sizing & trading ───────────────────────────────────────────────────────────
@_hook
def on_sizing(
    forecast_id,
    trade_id,
    bankroll,
    edge,
    side,
    kelly_f_star,
    lam,
    cap,
    final_fraction,
    stake,
    p,
    c,
):
    return eventlog.emit(
        "sizing.decision",
        forecast_id=forecast_id,
        trade_id=trade_id,
        bankroll=bankroll,
        edge=edge,
        side=side,
        kelly_f_star=kelly_f_star,
        lam=lam,
        cap=cap,
        final_fraction=final_fraction,
        stake=stake,
        p=p,
        c=c,
    )


@_hook
def on_trade_open(
    trade_id, market_id, forecast_id, side, stake, fill_price, slippage, fee
):
    ev = eventlog.emit(
        "trade.open",
        trade_id=trade_id,
        market_id=market_id,
        forecast_id=forecast_id,
        side=side,
        stake=stake,
        fill_price=fill_price,
        slippage=slippage,
        fee=fee,
        mode="paper",
    )
    evidence.append_trade(
        trade_id, forecast_id, market_id, side, stake, fill_price, "paper", _now_iso()
    )
    return ev


@_hook
def on_trade_skip(forecast_id, reason, inputs):
    return eventlog.emit(
        "trade.skip", forecast_id=forecast_id, reason=reason, inputs=inputs
    )


# ── event portfolio (P3) ────────────────────────────────────────────────────────
@_hook
def on_event_portfolio(
    forecast_id,
    event_key,
    market_id,
    accept,
    positions,
    rejected,
    portfolio_ev,
    worst_case_loss,
    max_exposure,
    losing_outcome,
    reject_reason=None,
    mutually_exclusive=None,
    is_arbitrage=None,
    explanation=None,
):
    """Emit the event-level portfolio decision: the legs chosen + rejected, the
    portfolio EV / worst-case / max-exposure / losing-outcome, and the full
    human-readable explanation. Observation-only; degrades to a no-op on failure."""
    return eventlog.emit(
        "event.portfolio",
        forecast_id=forecast_id,
        # NB: emit as `event_id` (not `event_key`): obs.redact DROPS any field whose
        # name ends in `_KEY` as a suspected secret, which would silently delete this.
        event_id=event_key,
        market_id=market_id,
        accept=accept,
        positions=positions,
        rejected=rejected,
        portfolio_ev=portfolio_ev,
        worst_case_loss=worst_case_loss,
        max_exposure=max_exposure,
        losing_outcome=losing_outcome,
        reject_reason=reject_reason,
        mutually_exclusive=mutually_exclusive,
        is_arbitrage=is_arbitrage,
        explanation=explanation,
    )


# ── resolution & scoring ───────────────────────────────────────────────────────
@_hook
def on_resolution(market_id, outcome, source):
    ev = eventlog.emit(
        "resolution.observed", market_id=market_id, outcome=outcome, source=source
    )
    forecast_id = _ids.current().get("forecast_id")
    evidence.append_resolution(market_id, outcome, source, forecast_id=forecast_id)
    return ev


@_hook
def on_trade_settle(
    trade_id, market_id, outcome, payout, realized_pnl, bankroll_before, bankroll_after
):
    return eventlog.emit(
        "trade.settle",
        trade_id=trade_id,
        market_id=market_id,
        outcome=outcome,
        payout=payout,
        realized_pnl=realized_pnl,
        bankroll_before=bankroll_before,
        bankroll_after=bankroll_after,
    )


@_hook
def on_score(forecast_id, market_id, model_brier, market_brier):
    ev = eventlog.emit(
        "score.brier",
        forecast_id=forecast_id,
        market_id=market_id,
        model_brier=model_brier,
        market_brier=market_brier,
    )
    evidence.append_score(forecast_id, market_id, model_brier, market_brier)
    return ev


@_hook
def on_gate(
    n_resolved,
    model_brier_mean,
    market_brier_mean,
    paper_pnl,
    gate1_pass,
    gate2_pass,
    overall_pass,
):
    ev = eventlog.emit(
        "gate.eval",
        n_resolved=n_resolved,
        model_brier_mean=model_brier_mean,
        market_brier_mean=market_brier_mean,
        paper_pnl=paper_pnl,
        gate1_pass=gate1_pass,
        gate2_pass=gate2_pass,
        overall_pass=overall_pass,
    )
    evidence.append_gate(
        n_resolved,
        model_brier_mean,
        market_brier_mean,
        paper_pnl,
        gate1_pass,
        gate2_pass,
        overall_pass,
    )
    return ev


# ── errors ──────────────────────────────────────────────────────────────────--
@_hook
def on_error(where, exc, action, context=None):
    return eventlog.emit(
        "error",
        level="ERROR",
        where=where,
        error=repr(exc),
        action=action,
        context=context,
        traceback=traceback.format_exc(),
    )
