"""
Calibration tracker using Brier scores.
Brier score = mean((forecast - outcome)^2), lower is better.
Perfect calibration = 0.0, random = 0.25.
"""

from __future__ import annotations
import sqlite3
import os
from datetime import datetime

try:
    from harness import obs
except Exception:
    obs = None


DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Idempotently add a column to an existing table (SQLite ALTER TABLE).

    HARNESS PATCH (P0.5): lets the market_id column land on a DB created before
    this change without dropping accumulated calibration history.
    """
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            market_id TEXT,
            agent_id TEXT,
            probability REAL NOT NULL,
            outcome REAL,
            brier_score REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swarm_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            market_id TEXT,
            final_probability REAL NOT NULL,
            consensus_score REAL,
            outcome REAL,
            brier_score REAL,
            market_odds REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
    """)
    # Migrate pre-P0.5 databases in place.
    _ensure_column(conn, "forecasts", "market_id", "TEXT")
    _ensure_column(conn, "swarm_forecasts", "market_id", "TEXT")
    # Plan 2 — degraded-forecast honesty: store WHY a swarm forecast is degraded so a
    # reduced/partial run never looks like a healthy one in Gate-1 / calibration. All
    # nullable + backward-compatible (legacy rows stay NULL = "unknown", not "healthy").
    _ensure_column(conn, "swarm_forecasts", "degraded", "INTEGER")
    _ensure_column(conn, "swarm_forecasts", "n_agents_succeeded", "INTEGER")
    _ensure_column(conn, "swarm_forecasts", "n_agents_requested", "INTEGER")
    _ensure_column(conn, "swarm_forecasts", "degradation_reason", "TEXT")
    # Index market_id for fast keyed resolution / dedupe.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forecasts_market ON forecasts(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_swarm_market ON swarm_forecasts(market_id)")
    conn.commit()
    conn.close()


def save_forecast(question: str, agent_id: str, probability: float,
                  market_odds: float | None = None, market_id: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO forecasts (question, market_id, agent_id, probability) VALUES (?, ?, ?, ?)",
        (question, market_id, agent_id, probability),
    )
    conn.commit()
    conn.close()


def save_swarm_forecast(question: str, probability: float, consensus_score: float,
                        market_odds: float | None = None, market_id: str | None = None,
                        degraded: bool | None = None, n_agents_succeeded: int | None = None,
                        n_agents_requested: int | None = None, degradation_reason: str | None = None):
    """Persist a swarm forecast. Plan 2: the optional ``degraded`` / ``n_agents_*`` /
    ``degradation_reason`` fields record WHEN a forecast was produced by a reduced or
    partial swarm, so a degraded run is stored as degraded (not healthy). Omitting them
    (legacy callers) stores NULL — backward-compatible."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO swarm_forecasts (question, market_id, final_probability, consensus_score, "
        "market_odds, degraded, n_agents_succeeded, n_agents_requested, degradation_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (question, market_id, probability, consensus_score, market_odds,
         (None if degraded is None else int(bool(degraded))),
         n_agents_succeeded, n_agents_requested, degradation_reason),
    )
    conn.commit()
    conn.close()


def resolve_forecast(question: str, outcome: float, market_id: str | None = None) -> int:
    """Resolve a forecast and write model Brier = (p - outcome)**2.

    outcome: 1.0 = YES resolved, 0.0 = NO resolved.

    HARNESS PATCH (P0.5) — keying: when market_id is provided, forecasts are
    matched on the STABLE Polymarket market/condition id, NOT the raw question
    text. Distinct markets can share identical wording and question text drifts
    between fetch and resolution, so question-text keying silently mis-resolves.
    Falls back to question-text matching only when market_id is None (manual CLI
    resolves of question-only forecasts). Returns the number of rows resolved.
    """
    conn = sqlite3.connect(DB_PATH)
    # `where` is a hardcoded literal (no user input) -> safe to interpolate.
    if market_id is not None:
        where, param = "market_id = ?", (market_id,)
    else:
        where, param = "question = ?", (question,)

    # update individual agent forecasts
    rows = conn.execute(
        f"SELECT id, probability FROM forecasts WHERE {where} AND outcome IS NULL",
        param
    ).fetchall()
    for row_id, prob in rows:
        brier = (prob - outcome) ** 2
        conn.execute(
            "UPDATE forecasts SET outcome=?, brier_score=?, resolved_at=? WHERE id=?",
            (outcome, brier, datetime.utcnow().isoformat(), row_id),
        )
        if obs:
            try:
                obs.hooks.on_score(forecast_id=None, market_id=market_id,
                                   model_brier=brier, market_brier=None)
            except Exception:
                pass
    # update swarm forecast
    swarm_rows = conn.execute(
        f"SELECT id, final_probability FROM swarm_forecasts WHERE {where} AND outcome IS NULL",
        param
    ).fetchall()
    for row_id, prob in swarm_rows:
        brier = (prob - outcome) ** 2
        conn.execute(
            "UPDATE swarm_forecasts SET outcome=?, brier_score=?, resolved_at=? WHERE id=?",
            (outcome, brier, datetime.utcnow().isoformat(), row_id),
        )
        if obs:
            try:
                obs.hooks.on_score(forecast_id=None, market_id=market_id,
                                   model_brier=brier, market_brier=None)
            except Exception:
                pass
    conn.commit()
    conn.close()
    return len(rows) + len(swarm_rows)


def get_open_market_ids() -> set[str]:
    """market_ids with an unresolved swarm forecast.

    HARNESS PATCH (P0.5): the autonomous loop calls this to dedupe — it must not
    re-forecast / double-bet a market it already holds an open paper position on.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT market_id FROM swarm_forecasts "
        "WHERE market_id IS NOT NULL AND outcome IS NULL"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_agent_brier_scores() -> dict[str, float]:
    """Returns average Brier score per agent (lower = better calibrated)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT agent_id, AVG(brier_score) FROM forecasts WHERE brier_score IS NOT NULL GROUP BY agent_id"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def get_swarm_brier_score() -> float | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT AVG(brier_score) FROM swarm_forecasts WHERE brier_score IS NOT NULL"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_market_brier_score() -> float | None:
    """Market-PRICE Brier = AVG((market_odds - outcome)^2) over resolved forecasts.

    HARNESS PATCH (P4) — NET-NEW: PolySwarm only Briers the model, never the market
    price it is betting against. This is the benchmark half of GATE 1 (is the swarm
    actually better-calibrated than the market?). Compared on the SAME resolved rows
    that have a stored market_odds, so it is a fair head-to-head with the swarm Brier.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT AVG((market_odds - outcome) * (market_odds - outcome)) "
        "FROM swarm_forecasts WHERE outcome IS NOT NULL AND market_odds IS NOT NULL"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_calibration_weights() -> dict[str, float]:
    """Convert Brier scores to weights — better calibrated agents get higher weight."""
    scores = get_agent_brier_scores()
    if not scores:
        return {}
    # invert: lower brier = higher weight, normalize
    inverted = {k: 1.0 / (v + 0.01) for k, v in scores.items()}
    max_w = max(inverted.values())
    return {k: v / max_w for k, v in inverted.items()}


def get_forecast_history(limit: int = 50) -> list[dict]:
    """Retrieve past swarm forecasts."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT question, final_probability, consensus_score,
                  market_odds, outcome, brier_score, created_at, resolved_at
           FROM swarm_forecasts ORDER BY created_at DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [
        {
            "question": r[0],
            "probability": r[1],
            "consensus_score": r[2],
            "market_odds": r[3],
            "outcome": r[4],
            "brier_score": r[5],
            "created_at": r[6],
            "resolved_at": r[7],
            "status": "resolved" if r[4] is not None else "pending",
        }
        for r in rows
    ]


def export_calibration(format: str = "json") -> str:
    """Export calibration data as JSON or CSV."""
    agent_scores = get_agent_brier_scores()
    swarm_score = get_swarm_brier_score()
    history = get_forecast_history(limit=1000)

    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["question", "probability", "consensus_score", "market_odds",
                         "outcome", "brier_score", "created_at", "resolved_at", "status"])
        for h in history:
            writer.writerow([h["question"], h["probability"], h["consensus_score"],
                             h["market_odds"], h["outcome"], h["brier_score"],
                             h["created_at"], h["resolved_at"], h["status"]])
        return output.getvalue()
    else:
        import json
        return json.dumps({
            "swarm_brier_score": swarm_score,
            "agent_brier_scores": agent_scores,
            "forecasts": history,
        }, indent=2)
