"""harness/experiments.py — B4: parameter-experiment registry (A/B tagging).

A passive registry that lets the harness A/B different parameter sets WITHOUT
changing any default behavior. It only RECORDS which params a forecast/bet ran
under and COMPARES their resolved outcomes; it NEVER auto-switches the active
params. A human (or a later, separately-gated phase) promotes a winner.

Design contract
---------------
* Self-contained sqlite tables ``experiments`` and ``experiment_outcomes``
  created idempotently in the SAME polyswarm.db as the rest of the harness. The
  DB path honors DATABASE_URL with the exact normalization
  core.calibration / label_perf / forecast_versions use (so a test that points
  DATABASE_URL at a temp file hits that file) and otherwise defers to
  obs.config.resolve_db_path() — the canonical polyswarm.db.

* DEFAULT == CURRENT BEHAVIOR, NO AUTO-SWITCH. The lazily-created 'baseline'
  experiment's params ARE the current live defaults (quarter-Kelly lambda, the
  2% hard cap, the 0.02 min-edge floor, and the wallet's slippage / fee_frac /
  exposure caps), pulled straight from harness.sizing and harness.wallet. So
  tagging is always possible and running under the active (baseline) experiment
  is a numeric no-op. This module has NO write path back into sizing / gating /
  conviction — it cannot loosen a gate, lower a threshold, or change bet
  frequency. It is purely informational.

* Every public function is best-effort and import-safe. Writers
  (register_experiment / set_active / record_experiment_outcome) return False
  rather than raising on ANY error; readers (active_experiment /
  experiment_leaderboard / get_experiment) degrade to a safe default. Nothing
  here may raise into settlement, the forecast path, or the bettor.

* De-dupe is on BOTH sides for experiment_outcomes: an insert with an existing
  (exp_key, market_id) is skipped, AND the leaderboard keeps the LATEST row per
  (exp_key, market_id) on read — so a re-recorded settlement never
  double-counts (mirrors harness.label_perf).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

_EXP_TABLE = "experiments"
_OUT_TABLE = "experiment_outcomes"

# The default experiment key. Its params == the current live defaults, so the
# active experiment is always a no-op until a human promotes a different one.
BASELINE_KEY = "baseline"


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


def _json_dump(x):
    """json.dumps(x) (or '{}' for None). Raises on un-serializable input so the
    caller's best-effort try/except can degrade to False rather than half-write."""
    return json.dumps({} if x is None else x)


def _json_load(s):
    """json.loads(s) -> obj, else {} (never None / never raises)."""
    if s is None:
        return {}
    try:
        v = json.loads(s)
        return v if v is not None else {}
    except Exception:
        return {}


# ── current defaults (the baseline params) ──────────────────────────────────────
def _current_defaults() -> dict:
    """The CURRENT live defaults — the baseline experiment's params.

    Pulled defensively from harness.sizing (lambda / cap / min_edge) and the
    harness.wallet WalletConfig (slippage / fee_frac / exposure caps) so the
    baseline mirrors exactly how fills and sizing actually happen. Hard-coded
    literals match the in-tree defaults and are used only if an import fails, so
    this never raises and never returns something looser than the live code.
    """
    params = {
        "lambda": 0.25,
        "cap": 0.02,
        "min_edge": 0.02,
        "slippage": 0.01,
        "fee_frac": 0.0,
        "max_bet_frac": 0.02,
        "max_exposure_frac": 0.50,
    }
    try:
        from harness import sizing as _sz
        params["lambda"] = float(_sz.DEFAULT_LAMBDA)
        params["cap"] = float(_sz.DEFAULT_CAP)
        params["min_edge"] = float(_sz.DEFAULT_MIN_EDGE)
    except Exception:
        pass
    try:
        from harness.wallet import WalletConfig as _WC
        wc = _WC()
        params["slippage"] = float(wc.slippage)
        params["fee_frac"] = float(wc.fee_frac)
        params["max_bet_frac"] = float(wc.max_bet_frac)
        params["max_exposure_frac"] = float(wc.max_exposure_frac)
    except Exception:
        pass
    return params


# ── schema ──────────────────────────────────────────────────────────────────--
def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create experiments + experiment_outcomes (+ indices) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_EXP_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exp_key TEXT UNIQUE,
                params_json TEXT,
                active INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_OUT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exp_key TEXT,
                market_id TEXT,
                model_brier REAL,
                market_brier REAL,
                realized_pnl REAL,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_EXP_TABLE}_key ON {_EXP_TABLE}(exp_key)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_EXP_TABLE}_active ON {_EXP_TABLE}(active)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_OUT_TABLE}_key ON {_OUT_TABLE}(exp_key)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_OUT_TABLE}_mkt ON {_OUT_TABLE}(market_id)")
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── active-flag helper ──────────────────────────────────────────────────────--
def _activate(conn: sqlite3.Connection, exp_key: str) -> None:
    """Make ``exp_key`` the single active experiment (deactivate all others).

    Enforces the one-active invariant. Caller commits. Assumes the row exists.
    """
    conn.execute(f"UPDATE {_EXP_TABLE} SET active=0")
    conn.execute(f"UPDATE {_EXP_TABLE} SET active=1 WHERE exp_key=?", (exp_key,))


# ── register / activate ───────────────────────────────────────────────────────
def register_experiment(exp_key, params, active: bool = False) -> bool:
    """Register (or update) a parameter set under ``exp_key``.

    Inserts a new experiment row; if ``exp_key`` already exists its params are
    updated (so re-registering refreshes the params). ``active=False`` (the
    default) NEVER changes which experiment is active — registering a candidate
    is decoupled from promoting it. ``active=True`` promotes it (deactivating all
    others) — a deliberate human action, not an auto-switch.

    Returns True on success, else False. Best-effort: any error -> False.
    """
    try:
        if not exp_key:
            return False
        pj = _json_dump(params)  # raises here on un-serializable params -> caught -> False
        key = str(exp_key)
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            existing = conn.execute(
                f"SELECT id FROM {_EXP_TABLE} WHERE exp_key=?", (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    f"UPDATE {_EXP_TABLE} SET params_json=? WHERE exp_key=?", (pj, key)
                )
            else:
                conn.execute(
                    f"INSERT INTO {_EXP_TABLE} (exp_key, params_json, active, created_at) "
                    f"VALUES (?,?,0,?)",
                    (key, pj, datetime.utcnow().isoformat()),
                )
            if active:
                _activate(conn, key)
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


def set_active(exp_key) -> bool:
    """Promote an EXISTING experiment to active (deactivating all others).

    Returns True iff ``exp_key`` exists and was activated; False on a missing key
    or any error. This is the only switch — and it is manual; nothing in this
    module calls it automatically.
    """
    try:
        if not exp_key:
            return False
        key = str(exp_key)
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT id FROM {_EXP_TABLE} WHERE exp_key=?", (key,)
            ).fetchone()
            if not row:
                return False
            _activate(conn, key)
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


# ── active read (lazy baseline) ─────────────────────────────────────────────--
def active_experiment() -> dict:
    """Return the active experiment as ``{"exp_key": str, "params": dict}``.

    If no active experiment exists, lazily create (or reactivate) the 'baseline'
    experiment whose params ARE the current live defaults and return it — so
    tagging is always possible and the default is a numeric no-op. Never raises;
    on any DB error it still returns the baseline defaults so the caller always
    has a usable, conservative tag.
    """
    try:
        conn = _connect()
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT exp_key, params_json FROM {_EXP_TABLE} "
                f"WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                return {"exp_key": row["exp_key"], "params": _json_load(row["params_json"])}
            # No active experiment — lazily ensure baseline exists and activate it.
            params = _current_defaults()
            ex = conn.execute(
                f"SELECT id FROM {_EXP_TABLE} WHERE exp_key=?", (BASELINE_KEY,)
            ).fetchone()
            if ex is None:
                conn.execute(
                    f"INSERT INTO {_EXP_TABLE} (exp_key, params_json, active, created_at) "
                    f"VALUES (?,?,1,?)",
                    (BASELINE_KEY, _json_dump(params), datetime.utcnow().isoformat()),
                )
            else:
                conn.execute(
                    f"UPDATE {_EXP_TABLE} SET active=1 WHERE exp_key=?", (BASELINE_KEY,)
                )
                # Prefer the stored baseline params if present (preserve any prior
                # registration); fall back to the freshly computed defaults.
                stored = conn.execute(
                    f"SELECT params_json FROM {_EXP_TABLE} WHERE exp_key=?", (BASELINE_KEY,)
                ).fetchone()
                if stored is not None:
                    params = _json_load(stored["params_json"]) or params
            conn.commit()
            return {"exp_key": BASELINE_KEY, "params": params}
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return {"exp_key": BASELINE_KEY, "params": _current_defaults()}


def get_experiment(exp_key) -> dict | None:
    """Return ``{"exp_key", "params", "active"}`` for ``exp_key``, else None.

    A read-only helper (used by tests / dashboards) to round-trip stored params
    for ANY experiment, active or not. Never raises.
    """
    if not exp_key:
        return None
    try:
        conn = _connect()
        try:
            init_db(conn)
            row = conn.execute(
                f"SELECT exp_key, params_json, active FROM {_EXP_TABLE} WHERE exp_key=?",
                (str(exp_key),),
            ).fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if row is None:
            return None
        return {
            "exp_key": row["exp_key"],
            "params": _json_load(row["params_json"]),
            "active": bool(row["active"]),
        }
    except Exception:
        return None


# ── record outcome ──────────────────────────────────────────────────────────--
def record_experiment_outcome(exp_key, market_id, model_brier, market_brier,
                              realized_pnl) -> bool:
    """Record one resolved-market outcome under ``exp_key``.

    De-dupe on (exp_key, market_id): a second call for the same pair is skipped
    (returns False). Returns True iff a row was inserted. Best-effort: any error
    -> False (never raises into settlement).
    """
    try:
        if not exp_key:
            return False
        key = str(exp_key)
        mb, kb, pl = _f(model_brier), _f(market_brier), _f(realized_pnl)
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            if market_id is not None:
                dup = conn.execute(
                    f"SELECT 1 FROM {_OUT_TABLE} WHERE exp_key=? AND market_id=? LIMIT 1",
                    (key, str(market_id)),
                ).fetchone()
                if dup:
                    return False
            conn.execute(
                f"INSERT INTO {_OUT_TABLE} (exp_key, market_id, model_brier, "
                f"market_brier, realized_pnl, recorded_at) VALUES (?,?,?,?,?,?)",
                (key, None if market_id is None else str(market_id), mb, kb, pl,
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


# ── leaderboard ───────────────────────────────────────────────────────────────
def _leaderboard_rows(conn: sqlite3.Connection):
    """LATEST outcome row per (exp_key, market_id) — read-side de-dupe. Rows with
    a NULL market_id are each kept (no key to de-dupe on)."""
    return conn.execute(
        f"SELECT exp_key, model_brier, market_brier, realized_pnl "
        f"FROM {_OUT_TABLE} t WHERE t.market_id IS NULL OR t.id = ("
        f"  SELECT MAX(id) FROM {_OUT_TABLE} t2 "
        f"  WHERE t2.exp_key = t.exp_key AND t2.market_id = t.market_id)"
    ).fetchall()


def experiment_leaderboard(min_n: int = 10) -> list:
    """Per-experiment outcome summary, restricted to experiments with n >= min_n.

    Returns a list of
    ``{exp_key, n, mean_model_brier, mean_market_brier, total_pnl}`` sorted by
    SKILL descending, where skill = mean_market_brier - mean_model_brier (how
    much the model's Brier beats just betting the market price; higher is
    better). An experiment missing either mean Brier sorts last. Experiments
    below ``min_n`` resolved outcomes are excluded. [] on error.
    """
    try:
        conn = _connect()
    except Exception:
        return []
    try:
        init_db(conn)
        rows = _leaderboard_rows(conn)
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    agg: dict = {}
    for r in rows:
        k = r["exp_key"]
        a = agg.setdefault(k, {"n": 0, "mb_sum": 0.0, "mb_n": 0,
                               "kb_sum": 0.0, "kb_n": 0, "pnl_sum": 0.0})
        a["n"] += 1
        if r["model_brier"] is not None:
            a["mb_sum"] += r["model_brier"]
            a["mb_n"] += 1
        if r["market_brier"] is not None:
            a["kb_sum"] += r["market_brier"]
            a["kb_n"] += 1
        if r["realized_pnl"] is not None:
            a["pnl_sum"] += r["realized_pnl"]

    out: list = []
    for k, a in agg.items():
        if a["n"] < min_n:
            continue
        out.append({
            "exp_key": k,
            "n": a["n"],
            "mean_model_brier": (a["mb_sum"] / a["mb_n"]) if a["mb_n"] else None,
            "mean_market_brier": (a["kb_sum"] / a["kb_n"]) if a["kb_n"] else None,
            "total_pnl": a["pnl_sum"],
        })

    def _skill(d):
        mb, mk = d["mean_model_brier"], d["mean_market_brier"]
        if mb is None or mk is None:
            return float("-inf")
        return mk - mb

    out.sort(key=_skill, reverse=True)
    return out
