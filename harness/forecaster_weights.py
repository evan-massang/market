"""harness/forecaster_weights.py — B4: skill-weighted forecaster blend.

Tracks per-FORECASTER (not per-agent) realized Brier so the SWARM and the
CHALLENGER ensemble can be weighted by their demonstrated skill, then blended
into the single decision probability. The two forecasters are the canonical
labels ``"swarm"`` and ``"challenger"``.

COLD-START INVARIANT (non-negotiable)
-------------------------------------
With no — or thin — resolved history (the situation TODAY: 0 resolved opinion
markets), the blend MUST reduce to swarm-only, i.e. the decision probability is
NUMERICALLY IDENTICAL to today's pre-B4 value:

* ``forecaster_weights`` returns the cold-start default
  ``{"swarm": 1.0, "challenger": 0.0}`` whenever EITHER forecaster has fewer than
  ``min_n`` resolved rows. The skill weighting ACTIVATES only once BOTH the swarm
  and the challenger have a real track record (>= min_n each).
* ``blend_forecasters`` returns ``swarm_p`` EXACTLY (same value, no float
  arithmetic) whenever the challenger weight is 0 or ``challenger_p`` is None.

Design contract (mirrors harness/label_perf.py)
-----------------------------------------------
* Self-contained sqlite table ``forecaster_scores`` created idempotently in the
  SAME polyswarm.db. The DB path honors DATABASE_URL (so the unit tests' temp DB
  is used) and falls back to ``obs.config.resolve_db_path()``.
* Every public function is best-effort and import-safe: a missing table, a
  missing DB, or a malformed row degrades to a safe default. Nothing here raises
  into settlement or the bettor.
* De-dupe is on READ — ``forecaster_brier`` / ``forecaster_weights`` keep the
  LATEST row per (forecaster, market_id) — so a re-recorded settlement never
  double-counts. We also skip an exact duplicate insert (same forecaster +
  market_id + outcome) as a cheap guard.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

_TABLE = "forecaster_scores"

# Smoothing added to a Brier before inversion (matches core.calibration's eps),
# so a forecaster with a perfect 0.0 Brier still yields a finite weight.
_EPS = 0.01

_COLD_START = {"swarm": 1.0, "challenger": 0.0}


# ── db path / connection ──────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path (DATABASE_URL-aware, same as label_perf)."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _f(x):
    """Coerce to float, mapping anything non-numeric (incl. None) to None."""
    try:
        return None if x is None else float(x)
    except Exception:
        return None


# ── schema ──────────────────────────────────────────────────────────────────--
def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create forecaster_scores (+ indices) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecaster TEXT,
                market_id TEXT,
                model_p REAL,
                outcome REAL,
                brier REAL,
                resolved_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_fc ON {_TABLE}(forecaster)")
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
def record_forecaster_outcome(forecaster, market_id, model_p, outcome) -> bool:
    """INSERT one resolved-forecaster row into forecaster_scores.

    ``brier = (model_p - outcome)**2`` when both are available, else NULL.
    Returns True iff a row was inserted. Best-effort: returns False on any error
    (so a caller in settlement can ignore the result).

    De-dupe: an exact re-record (same forecaster + market_id + outcome) is
    skipped (returns False); read-side de-dupe is the real guarantee.
    """
    try:
        mp, oc = _f(model_p), _f(outcome)
        brier = None if (mp is None or oc is None) else (mp - oc) ** 2
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            if forecaster is not None and market_id is not None and oc is not None:
                dup = conn.execute(
                    f"SELECT 1 FROM {_TABLE} "
                    f"WHERE forecaster=? AND market_id=? AND outcome=? LIMIT 1",
                    (forecaster, market_id, oc),
                ).fetchone()
                if dup:
                    return False
            conn.execute(
                f"INSERT INTO {_TABLE} "
                f"(forecaster, market_id, model_p, outcome, brier, resolved_at) "
                f"VALUES (?,?,?,?,?,?)",
                (forecaster, market_id, mp, oc, brier, datetime.utcnow().isoformat()),
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


# ── aggregation ───────────────────────────────────────────────────────────────
def _resolved_rows(conn: sqlite3.Connection):
    """LATEST row per (forecaster, market_id) — de-dupe re-recorded settlements.

    Rows with a NULL market_id are each kept (no key to de-dupe on)."""
    return conn.execute(
        f"SELECT forecaster, model_p, outcome, brier "
        f"FROM {_TABLE} t WHERE t.market_id IS NULL OR t.id = ("
        f"  SELECT MAX(id) FROM {_TABLE} t2 "
        f"  WHERE t2.forecaster = t.forecaster AND t2.market_id = t.market_id)"
    ).fetchall()


def _aggregate() -> dict:
    """Per-forecaster accumulators over the de-duped resolved rows. {} on error."""
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        init_db(conn)
        rows = _resolved_rows(conn)
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    agg: dict = {}
    for r in rows:
        fc = r["forecaster"]
        a = agg.setdefault(fc, {"n": 0, "brier_sum": 0.0, "brier_n": 0})
        a["n"] += 1
        b = r["brier"]
        if b is None and r["model_p"] is not None and r["outcome"] is not None:
            b = (r["model_p"] - r["outcome"]) ** 2
        if b is not None:
            a["brier_sum"] += b
            a["brier_n"] += 1
    return agg


def forecaster_brier(min_n: int = 10) -> dict:
    """Per-forecaster realized Brier, restricted to forecasters with n >= min_n.

    Returns ``{forecaster: {"n": int, "mean_brier": float | None}}`` (de-duped on
    read). A forecaster with fewer than ``min_n`` resolved rows is omitted.
    """
    out: dict = {}
    for fc, a in _aggregate().items():
        if a["n"] >= min_n:
            out[fc] = {
                "n": a["n"],
                "mean_brier": (a["brier_sum"] / a["brier_n"]) if a["brier_n"] else None,
            }
    return out


# ── weighting + blend ─────────────────────────────────────────────────────────
def forecaster_weights(min_n: int = 10) -> dict:
    """{"swarm": w, "challenger": w} from normalized inverse-Brier skill.

    Lower realized Brier -> higher weight (the more-accurate forecaster gets more
    say). Weights sum to 1.0.

    COLD-START: returns ``{"swarm": 1.0, "challenger": 0.0}`` whenever EITHER
    forecaster has fewer than ``min_n`` resolved rows (or a usable mean Brier).
    Until BOTH have a real track record the blend is swarm-only — IDENTICAL to
    today's decision probability. Never raises.
    """
    try:
        b = forecaster_brier(min_n=min_n)
        s, c = b.get("swarm"), b.get("challenger")
        # Either forecaster thin -> swarm-only (cold-start invariant).
        if not s or not c:
            return dict(_COLD_START)
        bs = s.get("mean_brier")
        bc = c.get("mean_brier")
        if bs is None or bc is None:
            return dict(_COLD_START)
        inv_s = 1.0 / (bs + _EPS)
        inv_c = 1.0 / (bc + _EPS)
        tot = inv_s + inv_c
        if tot <= 0:
            return dict(_COLD_START)
        return {"swarm": inv_s / tot, "challenger": inv_c / tot}
    except Exception:
        return dict(_COLD_START)


def blend_forecasters(swarm_p, challenger_p, weights) -> float:
    """Weighted mean of the swarm and challenger probabilities.

    Returns ``swarm_p`` EXACTLY (numerically identical, no float arithmetic)
    whenever the challenger weight is 0/absent or ``challenger_p`` is None — the
    cold-start path. Otherwise returns the weight-normalized weighted mean.
    Never raises (falls back to swarm_p on any error).
    """
    try:
        if challenger_p is None:
            return swarm_p
        w = weights or {}
        wc = w.get("challenger", 0.0)
        if not wc:  # 0, 0.0, None or missing -> swarm-only, exact identity
            return swarm_p
        ws = w.get("swarm", 0.0)
        tot = ws + wc
        if tot <= 0:
            return swarm_p
        return (ws * float(swarm_p) + wc * float(challenger_p)) / tot
    except Exception:
        return swarm_p
