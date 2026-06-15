"""obs.evidence — append-only, frozen evidence tables in polyswarm.db.

Forecasts are frozen BEFORE their outcomes are known; resolutions/trades/
scores/gate rows are appended later. UPDATE/DELETE triggers make the core
evidence tables tamper-evident at the DB layer. We ONLY ever create obs_*
tables and triggers on obs_* tables — existing harness tables are never
touched.

All writes are best-effort: sqlite OperationalError/IntegrityError (and any
other error) are swallowed. The JSONL event log remains the canonical record.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import config

# Set of resolved DB paths that have already been init()'d. Keying by path
# (rather than a single bool) means that if DATABASE_URL changes mid-process the
# new DB still gets its obs_* tables/triggers created. Production runs a single
# DB, so this set holds one entry and behavior is identical to the old flag.
_inited_paths = set()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect():
    """sqlite3 connection to the canonical DB. busy_timeout set; journal_mode untouched."""
    conn = sqlite3.connect(str(config.resolve_db_path()), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


_TABLES = (
    """CREATE TABLE IF NOT EXISTS obs_forecasts (
        forecast_id TEXT PRIMARY KEY,
        market_id TEXT,
        run_id TEXT,
        question TEXT,
        model_probability REAL,
        market_probability REAL,
        edge REAL,
        consensus REAL,
        ts TEXT,
        record_hash TEXT,
        frozen_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS obs_resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        forecast_id TEXT,
        market_id TEXT,
        outcome REAL,
        source TEXT,
        ts TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS obs_trades (
        trade_id TEXT PRIMARY KEY,
        forecast_id TEXT,
        market_id TEXT,
        side TEXT,
        stake REAL,
        fill_price REAL,
        mode TEXT,
        ts TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS obs_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        forecast_id TEXT,
        market_id TEXT,
        model_brier REAL,
        market_brier REAL,
        ts TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS obs_gate (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        n_resolved INTEGER,
        model_brier_mean REAL,
        market_brier_mean REAL,
        paper_pnl REAL,
        gate1_pass INTEGER,
        gate2_pass INTEGER,
        overall_pass INTEGER
    )""",
)

# Append-only triggers — ONLY on obs_ evidence tables.
_FROZEN_TABLES = ("obs_forecasts", "obs_trades", "obs_scores")


def init():
    """Create obs_* tables + append-only triggers (idempotent, lazy). Never raises.

    Idempotency is keyed by the resolved DB path so a mid-process DATABASE_URL
    change re-runs init() against the new database.
    """
    try:
        db_path = str(config.resolve_db_path())
    except Exception:
        db_path = None
    if db_path is not None and db_path in _inited_paths:
        return
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            for ddl in _TABLES:
                cur.execute(ddl)
            for t in _FROZEN_TABLES:
                cur.execute(
                    "CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                    "BEGIN SELECT RAISE(ABORT,'append-only: {t} is frozen evidence'); "
                    "END".format(t=t)
                )
                cur.execute(
                    "CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                    "BEGIN SELECT RAISE(ABORT,'append-only: {t} is frozen evidence'); "
                    "END".format(t=t)
                )
            conn.commit()
            if db_path is not None:
                _inited_paths.add(db_path)
        finally:
            conn.close()
    except Exception:
        # Leave the path unmarked so a later call can retry.
        pass


def record_hash(d):
    """sha256 over json.dumps(d, sort_keys=True) of the canonical forecast fields."""
    try:
        return hashlib.sha256(
            json.dumps(d, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return None


def freeze_forecast(
    forecast_id,
    market_id,
    run_id,
    question,
    model_probability,
    market_probability,
    edge,
    consensus,
    ts,
    record_hash,
):
    """Freeze a forecast before its outcome is known. INSERT OR IGNORE (idempotent)."""
    try:
        init()
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO obs_forecasts "
                "(forecast_id, market_id, run_id, question, model_probability, "
                "market_probability, edge, consensus, ts, record_hash, frozen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    forecast_id,
                    market_id,
                    run_id,
                    question,
                    model_probability,
                    market_probability,
                    edge,
                    consensus,
                    ts,
                    record_hash,
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    except Exception:
        pass


def append_resolution(market_id, outcome, source, forecast_id=None, ts=None):
    """Append a market resolution (outcome observed)."""
    try:
        init()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO obs_resolutions "
                "(forecast_id, market_id, outcome, source, ts) VALUES (?,?,?,?,?)",
                (forecast_id, market_id, outcome, source, ts or _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    except Exception:
        pass


def append_trade(
    trade_id, forecast_id, market_id, side, stake, fill_price, mode, ts=None
):
    """Append a (paper) trade record. INSERT-only."""
    try:
        init()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO obs_trades "
                "(trade_id, forecast_id, market_id, side, stake, fill_price, mode, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    trade_id,
                    forecast_id,
                    market_id,
                    side,
                    stake,
                    fill_price,
                    mode,
                    ts or _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    except Exception:
        pass


def append_score(forecast_id, market_id, model_brier, market_brier, ts=None):
    """Append Brier scores for a resolved forecast. INSERT-only."""
    try:
        init()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO obs_scores "
                "(forecast_id, market_id, model_brier, market_brier, ts) "
                "VALUES (?,?,?,?,?)",
                (forecast_id, market_id, model_brier, market_brier, ts or _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    except Exception:
        pass


def append_gate(
    n_resolved,
    model_brier_mean,
    market_brier_mean,
    paper_pnl,
    gate1_pass,
    gate2_pass,
    overall_pass,
    ts=None,
):
    """Append a gate-evaluation snapshot. INSERT-only."""
    try:
        init()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO obs_gate "
                "(ts, n_resolved, model_brier_mean, market_brier_mean, paper_pnl, "
                "gate1_pass, gate2_pass, overall_pass) VALUES (?,?,?,?,?,?,?,?)",
                (
                    ts or _now_iso(),
                    n_resolved,
                    model_brier_mean,
                    market_brier_mean,
                    paper_pnl,
                    int(gate1_pass) if gate1_pass is not None else None,
                    int(gate2_pass) if gate2_pass is not None else None,
                    int(overall_pass) if overall_pass is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.IntegrityError):
        pass
    except Exception:
        pass
