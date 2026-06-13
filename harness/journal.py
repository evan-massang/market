"""
Dashboard journal — time-series equity snapshots + a decision transcript.

The wallet/calibration tables hold current state; the dashboard also needs HISTORY
(for the P&L curve) and the REASONING behind each bet (the transcript). The loop
writes here every pass. Shares ./polyswarm.db.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_journal():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, cash REAL, equity REAL, realized_pnl REAL,
            open_exposure REAL, n_open INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, market_id TEXT, question TEXT,
            model_p REAL, market_p REAL, edge REAL,
            side TEXT, stake REAL, fill_price REAL,
            regime TEXT, signal TEXT, status TEXT, why TEXT
        )
    """)
    conn.commit(); conn.close()


def record_snapshot(cash: float, equity: float, realized_pnl: float,
                    open_exposure: float, n_open: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO equity_snapshots (ts, cash, equity, realized_pnl, open_exposure, n_open) "
        "VALUES (?,?,?,?,?,?)", (_now(), cash, equity, realized_pnl, open_exposure, n_open))
    conn.commit(); conn.close()


def record_decision(market_id: str, question: str, model_p: float, market_p: float,
                    edge: float, side, stake: float, fill_price, regime: str,
                    signal: str, status: str, why: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO decisions (ts, market_id, question, model_p, market_p, edge, side, stake, "
        "fill_price, regime, signal, status, why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), market_id, question, model_p, market_p, edge, side, stake, fill_price,
         regime, signal, status, why))
    conn.commit(); conn.close()


def get_snapshots(limit: int = 500) -> list[dict]:
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


def get_decisions(limit: int = 100) -> list[dict]:
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(r) for r in rows]
