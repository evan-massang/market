"""harness/forecast_versions.py — B1: versioned forecast record (audit/replay).

A passive, append-only audit table that captures EVERYTHING that went into a
single forecast decision so a later run can be reproduced / diffed:

  * the raw swarm probability (``swarm_p``),
  * the per-model challenger ensemble (model labels + their probabilities),
  * the blended challenger probability,
  * the calibrated probability and the method/data-count that produced it,
  * the per-forecaster weights used,
  * and the version stamps (prompt / method / code) of the code that ran.

Design contract
---------------
* Self-contained sqlite table ``forecast_versions`` created idempotently in the
  SAME polyswarm.db as the rest of the harness. The DB path honors DATABASE_URL
  with the exact normalization core.calibration / harness.label_perf use (so a
  test that points DATABASE_URL at a temp file hits that file) and otherwise
  defers to obs.config.resolve_db_path() — the canonical polyswarm.db.
* This module NEVER influences a decision probability. It only RECORDS what was
  decided. There is no read path that feeds back into sizing/conviction, so it
  cannot change the cold-start numeric identity: with thin/empty history the
  recorded ``calibrated_p`` is whatever the (passthrough) calibrator returned
  and ``blended_p`` is whatever the (single-model) ensemble returned — this
  table just stores those values verbatim.
* Every public function is best-effort and import-safe. record_forecast_version
  returns False rather than raising on ANY error (bad/un-serializable input, a
  missing DB, a malformed value). get_forecast_version returns None on miss/err.
* JSON fields (challenger model list, challenger probability list, weights map)
  are stored as TEXT via json.dumps and parsed back on read.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

_TABLE = "forecast_versions"

# prompt_version is a plain module constant — bump when the swarm prompt changes.
PROMPT_VERSION = "swarm-v1"


# ── db path / connection ──────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path (DATABASE_URL-aware; copies label_perf)."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _f(x):
    """Coerce to float, mapping anything non-numeric (incl. None) to None."""
    try:
        return None if x is None else float(x)
    except Exception:
        return None


def _i(x):
    """Coerce to int, mapping anything non-integer (incl. None) to None."""
    try:
        return None if x is None else int(x)
    except Exception:
        return None


def _json_dump(x):
    """json.dumps(x) or None for a None input. Raises on un-serializable input
    so the caller's best-effort try/except can degrade to False."""
    if x is None:
        return None
    return json.dumps(x)


def _json_load(s):
    """json.loads(s) -> obj, else None. Never raises."""
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _version_stamps():
    """(method_version, code_version) defaults pulled from obs.codeversion.

    Maps the two reproducibility fields: method_version <- git_sha,
    code_version <- code_version (the source content hash). Defensive import:
    if codeversion is missing or raises, returns (None, None).
    """
    try:
        from harness.obs import codeversion as _cv
        repro = _cv.reproducibility() or {}
        return repro.get("git_sha"), repro.get("code_version")
    except Exception:
        return None, None


# ── schema ──────────────────────────────────────────────────────────────────--
def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create forecast_versions (+ indices) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_id TEXT,
                market_id TEXT,
                question TEXT,
                swarm_p REAL,
                challenger_models_json TEXT,
                challenger_ps_json TEXT,
                blended_p REAL,
                calibrated_p REAL,
                weights_json TEXT,
                calibration_method TEXT,
                n_calib_history INTEGER,
                prompt_version TEXT,
                method_version TEXT,
                code_version TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_fid ON {_TABLE}(forecast_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_mkt ON {_TABLE}(market_id)")
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── record ──────────────────────────────────────────────────────────────────--
def record_forecast_version(
    forecast_id,
    market_id,
    question,
    swarm_p,
    challenger_models,
    challenger_ps,
    blended_p,
    calibrated_p,
    weights,
    calibration_method,
    n_calib_history,
    prompt_version=None,
    method_version=None,
    code_version=None,
) -> bool:
    """INSERT one versioned forecast row. Returns True on success, else False.

    Best-effort: ANY error (un-serializable JSON field, missing DB, bad value)
    degrades to a False return — this never raises into the forecast path.

    Version stamps: ``prompt_version`` defaults to the module constant
    ``PROMPT_VERSION``; ``method_version`` / ``code_version`` default to the
    git_sha / code_version reported by obs.codeversion (imported defensively).
    Any explicitly-passed value wins over the default.
    """
    try:
        if prompt_version is None:
            prompt_version = PROMPT_VERSION
        if method_version is None or code_version is None:
            mv, cv = _version_stamps()
            if method_version is None:
                method_version = mv
            if code_version is None:
                code_version = cv

        # Serialize the JSON fields up front — a non-serializable value raises
        # here and is caught by the outer except (-> False), never a half-write.
        models_json = _json_dump(challenger_models)
        ps_json = _json_dump(challenger_ps)
        weights_json = _json_dump(weights)

        sp, bp, cp = _f(swarm_p), _f(blended_p), _f(calibrated_p)
        n_hist = _i(n_calib_history)

        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            conn.execute(
                f"INSERT INTO {_TABLE} (forecast_id, market_id, question, swarm_p, "
                f"challenger_models_json, challenger_ps_json, blended_p, calibrated_p, "
                f"weights_json, calibration_method, n_calib_history, prompt_version, "
                f"method_version, code_version, created_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    None if forecast_id is None else str(forecast_id),
                    None if market_id is None else str(market_id),
                    question,
                    sp,
                    models_json,
                    ps_json,
                    bp,
                    cp,
                    weights_json,
                    calibration_method,
                    n_hist,
                    prompt_version,
                    method_version,
                    code_version,
                    datetime.utcnow().isoformat(),
                ),
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


# ── read ──────────────────────────────────────────────────────────────────────
def get_forecast_version(forecast_id) -> dict | None:
    """Return the LATEST recorded version row for ``forecast_id`` as a dict.

    JSON fields are parsed back into Python objects under the keys
    ``challenger_models`` (list), ``challenger_ps`` (list) and ``weights``
    (dict). Returns None on miss or any error. Never raises.
    """
    if forecast_id is None:
        return None
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE forecast_id=? ORDER BY id DESC LIMIT 1",
                (str(forecast_id),),
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row is None:
            return None
        return _row_to_dict(row)
    except Exception:
        return None


def _row_to_dict(row) -> dict:
    """Map a forecast_versions sqlite.Row to the public dict (JSON fields parsed)."""
    return {
        "id": row["id"],
        "forecast_id": row["forecast_id"],
        "market_id": row["market_id"],
        "question": row["question"],
        "swarm_p": row["swarm_p"],
        "challenger_models": _json_load(row["challenger_models_json"]),
        "challenger_ps": _json_load(row["challenger_ps_json"]),
        "blended_p": row["blended_p"],
        "calibrated_p": row["calibrated_p"],
        "weights": _json_load(row["weights_json"]),
        "calibration_method": row["calibration_method"],
        "n_calib_history": row["n_calib_history"],
        "prompt_version": row["prompt_version"],
        "method_version": row["method_version"],
        "code_version": row["code_version"],
        "created_at": row["created_at"],
    }


def get_forecast_version_by_market(market_id) -> dict | None:
    """Return the LATEST recorded version row for ``market_id`` as a dict.

    Additive companion to :func:`get_forecast_version` for the settlement path,
    which is keyed by market_id (not forecast_id): it recovers the EXACT swarm and
    challenger per-forecaster probabilities that drove a market so the resolved
    Brier can be attributed to each forecaster. Same parsed-dict shape; None on
    miss or any error. Never raises.
    """
    if market_id is None:
        return None
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE market_id=? ORDER BY id DESC LIMIT 1",
                (str(market_id),),
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row is None:
            return None
        return _row_to_dict(row)
    except Exception:
        return None
