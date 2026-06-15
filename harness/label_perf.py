"""harness/label_perf.py — P4B: label backtest + auto observe-only.

Records the resolved outcome of every settled market keyed by its classifier
labels (a granular ``fine_label`` plus the coarse ``label`` —
'opinion'|'mechanical'|'unknown'), then lets the loop ask whether a given
``fine_label`` has historically LOST money or failed to beat the market. When it
has, that market is still forecast + logged (so the backtest keeps learning) but
is NEVER bet — OBSERVE ONLY.

Design contract
---------------
* Self-contained sqlite table ``classifier_backtest`` created idempotently in the
  SAME polyswarm.db as the rest of the harness. The DB path honors DATABASE_URL
  (exactly as core.calibration does, so the unit tests' temp DB is used) and
  falls back to ``obs.config.resolve_db_path()`` — the canonical polyswarm.db.
* Every public function is best-effort and import-safe: a missing table, a
  missing DB, or a malformed row degrades to a safe default. Nothing here may
  raise into settlement (loop.settle_resolved) or the bettor (predict_today).
* De-dupe is on READ — label_performance / should_observe_only keep the LATEST
  row per market_id — so a re-recorded settlement never double-counts. We also
  skip an exact duplicate insert (same market_id + outcome) as a cheap guard.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

_TABLE = "classifier_backtest"


# ── db path / connection ──────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path.

    Honors DATABASE_URL with the exact normalization core.calibration uses (so a
    test that points DATABASE_URL at a temp file hits the same file the rest of
    the harness wrote). Otherwise defers to obs.config.resolve_db_path(), which
    anchors to the canonical polyswarm/polyswarm.db regardless of cwd.
    """
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
    """Create classifier_backtest (+ indices) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fine_label TEXT,
                label TEXT,
                market_id TEXT,
                question TEXT,
                model_p REAL,
                market_p REAL,
                outcome REAL,
                brier REAL,
                pnl REAL,
                resolved_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_fine ON {_TABLE}(fine_label)")
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
def record_classification_outcome(market_id, question, fine_label, label,
                                  model_p, market_p, outcome, pnl) -> bool:
    """INSERT one resolved-market row into classifier_backtest.

    brier (the model Brier) = (model_p - outcome)**2 when both are available,
    else NULL. Returns True iff a row was inserted. Best-effort: returns False on
    any error (so a caller in settlement can ignore the result).

    Idempotent-ish: a second call for the same (market_id, outcome) within the
    table is skipped (returns False); de-dupe on read is the real guarantee.
    """
    try:
        mp, kp, oc, pl = _f(model_p), _f(market_p), _f(outcome), _f(pnl)
        brier = None if (mp is None or oc is None) else (mp - oc) ** 2
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            if market_id is not None and oc is not None:
                dup = conn.execute(
                    f"SELECT 1 FROM {_TABLE} WHERE market_id=? AND outcome=? LIMIT 1",
                    (market_id, oc),
                ).fetchone()
                if dup:
                    return False
            conn.execute(
                f"INSERT INTO {_TABLE} (fine_label, label, market_id, question, "
                f"model_p, market_p, outcome, brier, pnl, resolved_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?)",
                (fine_label, label, market_id, question, mp, kp, oc, brier, pl,
                 datetime.utcnow().isoformat()),
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
    """LATEST row per market_id (de-dupe re-recorded settlements). Rows with a
    NULL market_id are each kept (no key to de-dupe on)."""
    return conn.execute(
        f"SELECT fine_label, label, model_p, market_p, outcome, brier, pnl "
        f"FROM {_TABLE} t WHERE t.market_id IS NULL OR t.id = ("
        f"  SELECT MAX(id) FROM {_TABLE} t2 WHERE t2.market_id = t.market_id)"
    ).fetchall()


def _aggregate() -> dict:
    """Per-fine_label accumulators over the de-duped resolved rows. {} on error."""
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
        fl = r["fine_label"]
        a = agg.setdefault(fl, {"n": 0, "brier_sum": 0.0, "brier_n": 0,
                                "mkt_brier_sum": 0.0, "mkt_brier_n": 0,
                                "pnl_sum": 0.0, "wins": 0})
        a["n"] += 1
        oc = r["outcome"]
        # model Brier — prefer the stored value, recompute if absent
        b = r["brier"]
        if b is None and r["model_p"] is not None and oc is not None:
            b = (r["model_p"] - oc) ** 2
        if b is not None:
            a["brier_sum"] += b
            a["brier_n"] += 1
        # market Brier — what just betting the market price would have scored
        if r["market_p"] is not None and oc is not None:
            a["mkt_brier_sum"] += (r["market_p"] - oc) ** 2
            a["mkt_brier_n"] += 1
        # realized pnl / win rate
        pl = r["pnl"]
        if pl is not None:
            a["pnl_sum"] += pl
            if pl > 0:
                a["wins"] += 1
    return agg


def _summarize(a: dict) -> dict:
    n = a["n"]
    return {
        "n": n,
        "mean_brier": (a["brier_sum"] / a["brier_n"]) if a["brier_n"] else None,
        "mean_market_brier": (a["mkt_brier_sum"] / a["mkt_brier_n"]) if a["mkt_brier_n"] else None,
        "total_pnl": a["pnl_sum"],
        "win_rate": (a["wins"] / n) if n else None,
    }


def label_performance(min_n: int = 10) -> dict:
    """Per-fine_label performance, restricted to labels with n >= min_n.

    Returns ``{fine_label: {n, mean_brier, mean_market_brier, total_pnl, win_rate}}``.
    """
    out: dict = {}
    for fl, a in _aggregate().items():
        if a["n"] >= min_n:
            out[fl] = _summarize(a)
    return out


def should_observe_only(fine_label, min_n: int = 10) -> bool:
    """True when ``fine_label`` should be OBSERVE-ONLY (forecast + log, never bet).

    Triggers when the label has n >= min_n AND it either lost money
    (total_pnl < 0) OR failed to beat the market (mean_brier >= mean_market_brier).
    Below min_n, or on any error, returns False (i.e. betting stays allowed).
    """
    try:
        a = _aggregate().get(fine_label)
        if not a or a["n"] < min_n:
            return False
        s = _summarize(a)
        if (s["total_pnl"] or 0.0) < 0:
            return True
        mb, mk = s["mean_brier"], s["mean_market_brier"]
        if mb is not None and mk is not None and mb >= mk:
            return True
        return False
    except Exception:
        return False


# ── wiring helper ─────────────────────────────────────────────────────────────
def latest_forecast_pq(market_id):
    """(model_p, market_p) from the latest swarm_forecasts row for ``market_id``.

    Used by loop.settle_resolved to feed record_classification_outcome:
    final_probability is the model probability, market_odds is the market price.
    Returns (None, None) if unavailable. Never raises.
    """
    if not market_id:
        return (None, None)
    try:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT final_probability, market_odds FROM swarm_forecasts "
                "WHERE market_id=? ORDER BY id DESC LIMIT 1", (market_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return (_f(row[0]), _f(row[1]))
    except Exception:
        pass
    return (None, None)
