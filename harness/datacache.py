"""harness/datacache.py — B1: persistent SQLite key/value cache.

A tiny, best-effort key/value cache living in the SAME polyswarm.db as the rest
of the harness. It lets the data-gathering layer (gdelt, wiki) survive a process
restart without re-hitting rate-limited, keyless free APIs: the in-memory gdelt
cache is lost on every Python restart, this one is not.

Design contract
---------------
* Self-contained sqlite table ``data_cache`` created idempotently in the SAME
  polyswarm.db. The DB path honors DATABASE_URL (the EXACT normalization
  harness.label_perf uses, so a test that points DATABASE_URL at a temp file
  hits the same file) and otherwise defers to obs.config.resolve_db_path() —
  the canonical polyswarm/polyswarm.db, regardless of cwd. The production DB is
  never touched by tests.
* EVERY public function is best-effort and import-safe: any error degrades to a
  safe default (None / False) and NEVER raises. A cache failure must never break
  a fetch, so a caller can wrap a live fetch around cache_get / cache_set and
  rely on the live path whenever the cache misbehaves.
* Values are JSON-serializable (stored as JSON text, returned parsed). A value
  that will not serialize simply fails to cache (cache_set -> False) rather than
  raising.
* Expiry is computed on READ from fetched_at + ttl_s, so an expired row reads
  back as a miss (None) — no background sweeper, no clock writes on read.

Public API:
  init_db(conn=None)                 -> None     (idempotent table create)
  make_key(source, *parts)           -> str      (stable sha256 key)
  cache_get(key)                     -> value|None (None if missing OR expired)
  cache_set(key, value, source, ttl_s) -> bool    (True iff stored)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime

_TABLE = "data_cache"


# ── db path / connection ──────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path.

    Honors DATABASE_URL with the EXACT normalization harness.label_perf uses (so
    a test pointing DATABASE_URL at a temp file hits the same file). Otherwise
    defers to obs.config.resolve_db_path(), which anchors to the canonical
    polyswarm/polyswarm.db regardless of cwd.
    """
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


# ── schema ──────────────────────────────────────────────────────────────────--
def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create data_cache (+ source index) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                key TEXT PRIMARY KEY,
                source TEXT,
                payload_json TEXT,
                fetched_at TEXT,
                ttl_s INTEGER
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_source ON {_TABLE}(source)")
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── key ───────────────────────────────────────────────────────────────────────
def make_key(source, *parts) -> str:
    """Stable cache key from ``source`` + ``parts``: ``"<source>:<sha256hex>"``.

    Deterministic across processes (sha256 over a unit-separator-joined string
    of str(source) and each str(part)). Never raises.
    """
    try:
        joined = "\x1f".join([str(source)] + [str(p) for p in parts])
        digest = hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()
        return f"{source}:{digest}"
    except Exception:
        try:
            digest = hashlib.sha256(repr(parts).encode("utf-8", "replace")).hexdigest()
        except Exception:
            digest = "0"
        return "datacache:" + digest


# ── freshness ───────────────────────────────────────────────────────────────--
def _is_fresh(fetched_at, ttl_s) -> bool:
    """True iff a row's age (now - fetched_at) does not exceed ttl_s.

    ttl_s <= 0, an unparseable fetched_at, or any arithmetic error -> not fresh
    (treated as an expired miss). Negative elapsed (clock skew) counts as fresh.
    """
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        return False
    if ttl <= 0:
        return False
    try:
        then = datetime.fromisoformat(str(fetched_at))
    except Exception:
        return False
    try:
        elapsed = (datetime.utcnow() - then).total_seconds()
    except Exception:
        return False
    return elapsed <= ttl


# ── get ─────────────────────────────────────────────────────────────────────--
def cache_get(key):
    """Return the cached JSON value for ``key``, or None if missing OR expired.

    Best-effort: a missing table, a corrupt payload, a locked DB, or any other
    error all degrade to None (a cache miss). Never raises.
    """
    if not key:
        return None
    try:
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT payload_json, fetched_at, ttl_s FROM {_TABLE} "
                f"WHERE key=? LIMIT 1",
                (key,),
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not row:
            return None
        payload_json, fetched_at, ttl_s = row[0], row[1], row[2]
        if not _is_fresh(fetched_at, ttl_s):
            return None
        return json.loads(payload_json)
    except Exception:
        return None


# ── set ─────────────────────────────────────────────────────────────────────--
def cache_set(key, value, source, ttl_s) -> bool:
    """Upsert a JSON-serializable ``value`` under ``key``. Returns True on store.

    Best-effort: a non-serializable value, a bad ttl, a locked DB, or any other
    error -> False (and the row is left untouched). Never raises.
    """
    if not key:
        return False
    try:
        payload_json = json.dumps(value)
    except (TypeError, ValueError):
        return False
    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError):
        return False
    try:
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            conn.execute(
                f"INSERT OR REPLACE INTO {_TABLE} "
                f"(key, source, payload_json, fetched_at, ttl_s) VALUES (?,?,?,?,?)",
                (key, source, payload_json, datetime.utcnow().isoformat(), ttl),
            )
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False
