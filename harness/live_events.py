"""harness/live_events.py — live telemetry event bus for the dashboard cockpit.

The AI work runs in the daemon processes (predict_today / sameday) while the dashboard runs in a
SEPARATE process. So live events are persisted to a small SQLite RING BUFFER in its OWN database
(``LIVE_EVENTS_DB`` — NOT polyswarm.db), which both the daemon (writer) and the dashboard
(SSE reader) can see. The same store doubles as the replay source for the initial page load.

HARD GUARANTEES:
  * Every helper is BEST-EFFORT and fully guarded — emitting/reading telemetry can NEVER raise
    into, slow, or change the forecast / gate / wallet / decision path. A telemetry failure is a
    silent no-op.
  * Every event carries ``ts``, ``source`` and ``paper_only=True``. Events that fail validation
    are dropped (logged-as-dropped), never crash the bot.
  * No secrets / API keys / private keys are ever stored (callers must not put them in ``data``;
    we additionally never read env secrets here).
  * This module NEVER touches polyswarm.db and NEVER places or sizes a bet.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

PAPER_ONLY = True

# keep the ring buffer bounded (newest N kept; older pruned best-effort)
MAX_EVENTS = 5000
# a stream with no new event within this many seconds is "stale"
STALE_AFTER_SECONDS = 300.0
# "connected"/live if the newest event is younger than this
LIVE_WITHIN_SECONDS = 20.0

VALID_TYPES = {
    "agent.started", "agent.token", "agent.finished", "agent.parse_failed",
    "mirofish.stage", "mirofish.state", "swarm.started", "swarm.vote", "swarm.degraded",
    "challenger.vote", "gate.result", "candidate.ranked", "decision.no_bet", "decision.bet",
    "wallet.update", "position.opened", "position.settled", "pnl.tick", "heartbeat",
    "evidence.pack", "forecast.final", "log", "error",
}
VALID_SOURCES = {
    "predict_today", "sameday", "mirofish", "swarm", "challenger", "wallet", "dashboard",
    "accounting", "gate", "profit_intel", "agent", "obs", "system",
}

_LOCAL = threading.local()
_CLIENTS = {"n": 0}            # in-process SSE client counter (this process only)
_CLIENTS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db_path() -> str:
    """Telemetry ring-buffer DB — its OWN file, never polyswarm.db. Override with LIVE_EVENTS_DB."""
    p = os.getenv("LIVE_EVENTS_DB")
    if p:
        return p
    # default: sit next to the configured runtime, NOT inside polyswarm.db
    base = os.getenv("SUPERVISOR_RUNTIME_DIR") or os.path.join(os.getcwd(), ".runtime")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.getcwd()
    return os.path.join(base, "live_events.db")


def _connect(path=None):
    conn = sqlite3.connect(path or db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=4000")
        conn.execute("PRAGMA journal_mode=WAL")   # many readers + a few writers across processes
    except Exception:
        pass
    return conn


def init_db(path=None) -> None:
    conn = _connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, type TEXT, source TEXT, market_id TEXT, question TEXT,
                stage TEXT, status TEXT, message TEXT, data TEXT,
                paper_only INTEGER DEFAULT 1, replay INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def validate_event(event: dict):
    """Return (ok: bool, reason: str). Requires ts, source, type, paper_only. Unknown type/source
    is allowed but flagged (we never crash on an odd event — we keep telemetry permissive)."""
    if not isinstance(event, dict):
        return False, "not_a_dict"
    if not event.get("type"):
        return False, "missing_type"
    if not event.get("source"):
        return False, "missing_source"
    if not event.get("ts"):
        return False, "missing_ts"
    if event.get("paper_only") is not True:
        return False, "missing_paper_only"
    return True, "ok"


def _coerce(event: dict) -> dict:
    """Fill defaults so a partially-specified event is still valid + safe. Never raises."""
    e = dict(event or {})
    e.setdefault("ts", _now_iso())
    e.setdefault("paper_only", True)
    e["paper_only"] = True if e.get("paper_only", True) else True   # always paper-only
    e.setdefault("source", "system")
    e.setdefault("type", "log")
    e.setdefault("status", "unknown")
    return e


def emit_event(event: dict, *, path=None) -> bool:
    """Persist a validated event to the ring buffer. BEST-EFFORT: returns False on any problem and
    NEVER raises (it must not perturb the trading/decision path). Drops invalid events safely."""
    try:
        e = _coerce(event)
        ok, _reason = validate_event(e)
        if not ok:
            return False
        data = e.get("data")
        if not isinstance(data, (dict, list)):
            data = {} if data is None else {"value": data}
        init_db(path)
        conn = _connect(path)
        try:
            cur = conn.execute(
                "INSERT INTO live_events (ts, type, source, market_id, question, stage, status, "
                "message, data, paper_only, replay) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (e.get("ts"), str(e.get("type"))[:64], str(e.get("source"))[:32],
                 e.get("market_id"), (e.get("question") or "")[:300] or None,
                 e.get("stage"), e.get("status"), (e.get("message") or "")[:500] or None,
                 json.dumps(data, default=str)[:4000], 1, int(bool(e.get("replay")))))
            new_id = cur.lastrowid
            # prune the ring buffer (best-effort)
            if new_id and new_id % 50 == 0:
                conn.execute("DELETE FROM live_events WHERE id < ?", (new_id - MAX_EVENTS,))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def emit(event_type: str, source: str, *, market_id=None, question=None, stage=None,
         status=None, message=None, data=None, replay=False) -> bool:
    """Convenience emitter. Best-effort."""
    return emit_event({
        "ts": _now_iso(), "type": event_type, "source": source, "market_id": market_id,
        "question": question, "stage": stage, "status": status, "message": message,
        "data": data or {}, "paper_only": True, "replay": bool(replay),
    })


# broadcast_event is an alias: SSE/REST readers pull from the same ring buffer, so persisting IS
# the broadcast (cross-process). Kept as a named helper per the telemetry contract.
def broadcast_event(event: dict) -> bool:
    return emit_event(event)


def recent_events(limit: int = 200, *, since_id=None, type=None, path=None) -> list[dict]:
    """Newest-first recent events (best-effort, [] on any error). ``since_id`` returns only events
    with id > since_id (ascending, for incremental SSE)."""
    try:
        conn = _connect(path)
        try:
            if since_id is not None:
                rows = conn.execute(
                    "SELECT * FROM live_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (int(since_id), int(limit))).fetchall()
            elif type:
                rows = conn.execute(
                    "SELECT * FROM live_events WHERE type=? ORDER BY id DESC LIMIT ?",
                    (type, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except Exception:
            d["data"] = {}
        d["id"] = str(d["id"])
        d["paper_only"] = True
        d["replay"] = bool(d.get("replay"))
        out.append(d)
    return out


def _parse_ts(s):
    if not s:
        return None
    try:
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def event_status(*, path=None) -> dict:
    """Stream state for /api/live/status. Honest: no events ever → unknown; recent → connected;
    older than STALE_AFTER → stale. Never raises."""
    last_id = last_ts = None
    age = None
    state = "unknown"
    try:
        conn = _connect(path)
        try:
            row = conn.execute("SELECT id, ts FROM live_events ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row is not None:
            last_id = str(row["id"])
            last_ts = row["ts"]
            dt = _parse_ts(last_ts)
            if dt is not None:
                age = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
                if age <= LIVE_WITHIN_SECONDS:
                    state = "connected"
                elif age <= STALE_AFTER_SECONDS:
                    state = "idle"
                else:
                    state = "stale"
            else:
                state = "unknown"
    except Exception:
        state = "unknown"
    return {
        "paper_only": True, "state": state, "last_event_id": last_id, "last_event_ts": last_ts,
        "last_event_age_seconds": (round(age, 3) if age is not None else None),
        "client_count": client_count(), "transport": "sse", "generated_at": _now_iso(),
    }


# ── in-process SSE client accounting (this process only) ────────────────────────
def register_client() -> int:
    with _CLIENTS_LOCK:
        _CLIENTS["n"] += 1
        return _CLIENTS["n"]


def unregister_client() -> int:
    with _CLIENTS_LOCK:
        _CLIENTS["n"] = max(0, _CLIENTS["n"] - 1)
        return _CLIENTS["n"]


def client_count() -> int:
    with _CLIENTS_LOCK:
        return _CLIENTS["n"]
