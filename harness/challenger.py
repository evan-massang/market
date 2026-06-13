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
import sqlite3
from datetime import datetime

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
    raw = _hosted_raw(system, user) if _hosted_configured() else _local_raw(system, user, model)
    if not raw:
        return None
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        p = float(json.loads(m.group(0))["probability"]) if m else float(re.search(r'[01]?\.?\d+', raw).group(0))
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
        r = client.chat.completions.create(
            model=os.getenv("CHALLENGER_MODEL"), max_tokens=300,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content.strip()
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
