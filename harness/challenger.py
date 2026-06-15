"""
P4 — single-LLM CHALLENGER (the MiroFish replacement).

MiroFish turned out to be a social-simulation app that emits no probability, so
it can't be A/B'd. The right, free, architecturally-correct challenger is a
SINGLE plain LLM call -> one probability. Run in PARALLEL with the swarm (never
chained) on the SAME market, so the scoreboard can answer the real question:
does the 12-agent swarm machinery actually beat one LLM call (and the market)?

Stored in its own baseline_forecasts table, keyed by market_id (P0.5 keying),
scored with the same Brier. Does NOT drive betting — the swarm does the sizing;
this is purely the calibration control.
"""
from __future__ import annotations

import json
import os
import re
import contextlib
import sqlite3
from datetime import datetime
from time import perf_counter

try:
    from harness import obs
except Exception:
    obs = None

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")


def init_baseline_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baseline_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            question TEXT NOT NULL,
            probability REAL NOT NULL,
            market_odds REAL,
            outcome REAL,
            brier_score REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_baseline_market ON baseline_forecasts(market_id)")
    conn.commit(); conn.close()


def single_llm_forecast(question: str, market_odds: float | None = None,
                        extra_context: str = "", model: str | None = None) -> float | None:
    """One plain LLM call -> calibrated YES probability in (0,1). Never raises;
    returns None on failure. Uses PolySwarm's keyless Ollama client."""
    system = "You are a careful, calibrated forecaster. Reply with ONLY the requested JSON."
    user = (
        "Estimate the probability that the following binary prediction-market question "
        "resolves YES. Consider base rates and evidence, then give one probability.\n\n"
        f"Question: {question}\n"
        + (f"Current market-implied probability: {market_odds:.3f}\n" if market_odds is not None else "")
        + (f"\nContext:\n{extra_context[:1500]}\n" if extra_context else "")
        + '\nReply with ONLY JSON: {"probability": <number between 0 and 1>}'
    )
    with (obs.agent_ctx(role="challenger") if obs else contextlib.nullcontext()):
        raw = _hosted_raw(system, user) if _hosted_configured() else _local_raw(system, user, model)
    if not raw:
        return None
    try:
        from core.agent import _coerce_prob   # robust % / prose / null coercion
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                p = _coerce_prob(json.loads(m.group(0)).get("probability"))
            except Exception:
                p = None
        else:
            # prose fallback: "60 percent"/"75%" -> 0.6/0.75 (NOT a forced 0.99);
            # an unparseable reply returns None (reject) rather than a fake max-confidence.
            p = _coerce_prob(raw)
        if p is None:
            return None
        return min(max(p, 0.01), 0.99)
    except Exception:
        return None


def _hosted_configured() -> bool:
    """A hosted challenger (e.g. Google AI Studio / Gemini) is wired when the three
    CHALLENGER_* env vars are set. The swarm is UNAFFECTED — it stays on Ollama."""
    return bool(os.getenv("CHALLENGER_API_KEY") and os.getenv("CHALLENGER_BASE_URL")
                and os.getenv("CHALLENGER_MODEL"))


def _hosted_raw(system: str, user: str) -> str | None:
    """Call an OpenAI-compatible hosted endpoint (Gemini/Groq/etc.) via CHALLENGER_*."""
    try:
        import openai
        client = openai.OpenAI(api_key=os.getenv("CHALLENGER_API_KEY"),
                               base_url=os.getenv("CHALLENGER_BASE_URL"))
        t0 = perf_counter()
        r = client.chat.completions.create(
            model=os.getenv("CHALLENGER_MODEL"), max_tokens=300,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        ms = (perf_counter() - t0) * 1000.0
        text = r.choices[0].message.content.strip()
        if obs:
            try:
                ti = to = None
                try:
                    ti = r.usage.prompt_tokens
                    to = r.usage.completion_tokens
                except Exception:
                    pass
                obs.hooks.on_llm_call(
                    provider="hosted", model=os.getenv("CHALLENGER_MODEL"),
                    system=system, user=user, completion=text,
                    tokens_in=ti, tokens_out=to, latency_ms=ms, role="challenger",
                )
            except Exception:
                pass
        return text
    except Exception:
        return None


def _local_raw(system: str, user: str, model: str | None) -> str | None:
    try:
        from core.agent import _get_llm_client, _call_llm  # type: ignore
        if model:
            os.environ["MODEL_FAST"] = model
        provider, client = _get_llm_client()
        return _call_llm(provider, client, system, user)
    except Exception:
        return None


def challenger_model_label() -> str:
    """Which model the challenger uses now — for the dashboard A/B header."""
    if _hosted_configured():
        return os.getenv("CHALLENGER_MODEL")
    return os.getenv("MODEL_FAST") or "local-llm"


def save_baseline(market_id: str, question: str, probability: float, market_odds: float | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO baseline_forecasts (market_id, question, probability, market_odds) VALUES (?,?,?,?)",
        (market_id, question, probability, market_odds))
    conn.commit(); conn.close()


def resolve_baseline(outcome: float, market_id: str) -> int:
    """Resolve baseline forecasts for a market_id; write Brier=(p-outcome)^2."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, probability FROM baseline_forecasts WHERE market_id=? AND outcome IS NULL",
        (market_id,)).fetchall()
    for rid, p in rows:
        conn.execute("UPDATE baseline_forecasts SET outcome=?, brier_score=?, resolved_at=? WHERE id=?",
                     (outcome, (p - outcome) ** 2, datetime.utcnow().isoformat(), rid))
        if obs:
            obs.hooks.on_score(
                forecast_id=None, market_id=market_id,
                model_brier=(p - outcome) ** 2, market_brier=None,
            )
    conn.commit(); conn.close()
    return len(rows)


def get_baseline_brier() -> float | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT AVG(brier_score) FROM baseline_forecasts WHERE brier_score IS NOT NULL").fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else None


# ── B3: MULTI-CHALLENGER ENSEMBLE ────────────────────────────────────────────--
# ADDITIVE. Does NOT touch single_llm_forecast, auth, keys, or provider plumbing.
# DEFAULT roster has EXACTLY ONE element = today's single challenger model, so the
# default ensemble mean == today's single bp (numeric identity, no extra LLM load
# on the 16GB CPU box). Extra models activate ONLY when CHALLENGER_MODELS is set.

def _default_challenger_model() -> str:
    """The exact model `single_llm_forecast(model=None)` resolves to today.

    Used as the sole default-roster element so the one-element ensemble reproduces
    today's bp NUMERICALLY (not just structurally):
      - hosted  -> CHALLENGER_MODEL (_hosted_raw ignores the `model` arg anyway)
      - local + MODEL_FAST set   -> MODEL_FAST (re-passing it is a no-op)
      - local + MODEL_FAST unset -> core.agent._get_model_name() (the provider
        default model=None would have resolved to — same value, same result)
    """
    if _hosted_configured():
        return os.getenv("CHALLENGER_MODEL")
    mf = os.getenv("MODEL_FAST")
    if mf:
        return mf
    try:
        from core.agent import _get_model_name  # same resolution model=None uses
        return _get_model_name()
    except Exception:
        return challenger_model_label()


def challenger_models() -> list[str]:
    """The challenger model roster.

    CHALLENGER_MODELS (comma-separated) overrides; otherwise EXACTLY ONE element =
    today's single challenger model. So with no CHALLENGER_MODELS set the default
    list has one entry and the ensemble is byte-for-byte today's single challenger.
    """
    raw = os.getenv("CHALLENGER_MODELS")
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return [_default_challenger_model()]


def _log_model_skip(model, exc) -> None:
    """Best-effort obs log when a challenger model is dropped. Never raises."""
    if not (obs and obs.hooks):
        return
    try:
        obs.hooks.on_error(
            where="challenger.ensemble_forecast", exc=exc,
            action="skip_model", context={"model": model},
        )
    except Exception:
        pass


def ensemble_forecast(question: str, market_odds: float | None = None,
                      extra_context: str = "", models: list[str] | None = None) -> dict:
    """Run the single-LLM challenger across a roster of models and average them.

    ADDITIVE wrapper over single_llm_forecast (auth/provider plumbing UNCHANGED).
    `models` defaults to challenger_models() -> ONE model today, so mean == that
    single forecast == today's bp (numeric identity, no extra LLM load).

    Every model is called best-effort: a model that raises OR returns None is
    SKIPPED (logged via obs.hooks.on_error) and excluded from the mean — so a bad
    model can never inflate or corrupt the ensemble.

    Returns dict{
      per_model: {model: p}   # successful models only
      probs:     [p, ...]     # successful probabilities, roster order
      mean:      float | None # average of probs, or None if EVERY model failed
      n:         int          # len(probs)
      models:    [...]        # the roster that was attempted
    }
    """
    roster = list(models) if models is not None else challenger_models()
    per_model: dict[str, float] = {}
    probs: list[float] = []
    for m in roster:
        try:
            p = single_llm_forecast(question, market_odds, extra_context, model=m)
        except Exception as exc:  # single_llm_forecast shouldn't raise; stay safe
            _log_model_skip(m, exc)
            continue
        if p is None:
            _log_model_skip(m, RuntimeError(f"challenger model returned no probability: {m}"))
            continue
        per_model[str(m)] = float(p)
        probs.append(float(p))
    mean = (sum(probs) / len(probs)) if probs else None
    return {
        "per_model": per_model,
        "probs": probs,
        "mean": mean,
        "n": len(probs),
        "models": roster,
    }
