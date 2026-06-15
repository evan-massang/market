"""harness/provenance.py — P12 full decision provenance.

Ties together every version stamp + tunable that produced a decision so any bet
(or skip) can be explained and reproduced:

  * versions()            — code_version / git_sha (obs.codeversion) + the static
                            component versions (classifier / guards / sizing /
                            prompt / strategy).
  * config_snapshot()     — every tunable threshold across the guard / sizing /
                            bankroll modules, plus a stable content hash.
  * record_config_snapshot() — append a row to config_history ONLY when the config
                            hash changes (a "config-change event"), returning the
                            changed keys (old -> new) — so a sweep of the timeline
                            shows exactly when and how the knobs moved.
  * provenance_for(forecast_id) — the full per-decision provenance: the P6 forecast
                            version (swarm/challenger/blend/calibration + version
                            stamps), the active P7 experiment, the component
                            versions, and the live config hash.
  * decision_diff(a, b)   — what differs between two forecasts (probabilities,
                            challenger roster, weights, versions).

DATABASE_URL-aware + best-effort: every function degrades to a safe default and
never raises. Read-only except record_config_snapshot (append-only history).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

try:
    from harness import obs
except Exception:  # pragma: no cover
    obs = None


# Static component versions — bump the string when that component's LOGIC changes
# (so a decision's provenance pins the exact behavior that produced it).
COMPONENT_VERSIONS = {
    "classifier": "p4-fine-labels-v1",
    "guards": "p8-adaptive-v1",
    "sizing": "p7-conviction-kelly-v1",
    "bankroll": "p9-killswitch-v1",
    "prompt": "swarm-v1",
    "strategy": "favorite-longshot+ai-opinion-v1",
    "evidence": "p5-pack-v1",
    "calibration": "p6-isotonic-v1",
}


def _db_path() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _err(where: str, exc: Exception) -> None:
    if obs:
        try:
            obs.hooks.on_error(where=where, exc=exc, action="skip")
        except Exception:
            pass


def _conn():
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    try:
        conn = _conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, config_hash TEXT, snapshot_json TEXT, "
            "changed_keys_json TEXT, recorded_at TEXT)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _err("provenance.init_db", e)


# ── versions ──────────────────────────────────────────────────────────────────
def versions() -> dict:
    out = dict(COMPONENT_VERSIONS)
    try:
        from harness.obs import codeversion
        repro = codeversion.reproducibility()
        out["code_version"] = repro.get("code_version")
        out["git_sha"] = repro.get("git_sha")
        out["git_dirty"] = repro.get("git_dirty")
    except Exception as e:
        _err("provenance.versions", e)
    return out


# ── config snapshot + change history ──────────────────────────────────────────
def config_snapshot() -> dict:
    """Every tunable threshold across the guard / sizing / bankroll modules + a
    stable content hash. Gathered defensively (a missing module/const is skipped)."""
    snap: dict = {}

    def grab(modname, names):
        try:
            mod = __import__(f"harness.{modname}", fromlist=[modname])
        except Exception:
            return
        for n in names:
            try:
                if hasattr(mod, n):
                    snap[f"{modname}.{n}"] = getattr(mod, n)
            except Exception:
                continue

    grab("sizing", ["DEFAULT_MIN_EDGE"])
    grab("market_quality", ["DEFAULT_MAX_SPREAD", "DEFAULT_MAX_EXIT_RISK"])
    grab("portfolio_guards", ["DEFAULT_MAX_SAME_THEME", "DEFAULT_MAX_SAME_EVENT",
                              "BAD_THEME_MIN_N", "BAD_THEME_LOSS", "DRAWDOWN_REF", "MAX_TIGHTEN"])
    grab("bankroll", ["MAX_DRAWDOWN_FRAC", "MAX_TOTAL_LOSS_FRAC", "COOLDOWN_STREAK",
                      "MAX_THEME_EXPOSURE_FRAC", "MAX_EVENT_EXPOSURE_FRAC"])
    grab("adaptive", ["MIN_N", "PENALTY", "MAX_MIN_EDGE"])
    grab("predict_today", ["MAX_SWARM_CHALLENGER_DIVERGENCE", "MIN_SWARM_CONSENSUS",
                           "MAX_GROUP_PROB_SUM", "MIN_EVIDENCE_QUALITY",
                           "CONVICTION_LAM_MIN", "CONVICTION_LAM_MAX",
                           "CONVICTION_CAP_MIN", "CONVICTION_CAP_MAX", "EDGE_FULL"])
    try:
        from harness.wallet import WalletConfig
        wc = WalletConfig()
        snap["wallet.slippage"] = wc.slippage
        snap["wallet.fee_frac"] = wc.fee_frac
    except Exception:
        pass

    blob = json.dumps(snap, sort_keys=True, default=str)
    snap["_hash"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return snap


def _latest_config_row():
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT config_hash, snapshot_json FROM config_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def record_config_snapshot(recorded_at: str | None = None) -> dict:
    """Append a config_history row ONLY when the config hash differs from the last.

    Returns ``{changed: bool, hash, diff}`` where diff maps each changed key to
    ``{"old":..., "new":...}``. Best-effort: any error -> {changed:False}.
    """
    try:
        init_db()
        snap = config_snapshot()
        h = snap.get("_hash")
        prev = _latest_config_row()
        if prev is not None and prev["config_hash"] == h:
            return {"changed": False, "hash": h, "diff": {}}
        diff = {}
        if prev is not None:
            try:
                old = json.loads(prev["snapshot_json"])
            except Exception:
                old = {}
            keys = set(old) | set(snap)
            for k in keys:
                if k == "_hash":
                    continue
                if old.get(k) != snap.get(k):
                    diff[k] = {"old": old.get(k), "new": snap.get(k)}
        conn = _conn()
        conn.execute(
            "INSERT INTO config_history (config_hash, snapshot_json, changed_keys_json, recorded_at) "
            "VALUES (?,?,?,?)",
            (h, json.dumps(snap, default=str), json.dumps(diff, default=str), recorded_at or ""),
        )
        conn.commit()
        conn.close()
        return {"changed": True, "hash": h, "diff": diff}
    except Exception as e:
        _err("provenance.record_config_snapshot", e)
        return {"changed": False, "hash": None, "diff": {}}


def config_history(limit: int = 50) -> list[dict]:
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT config_hash, changed_keys_json, recorded_at FROM config_history "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
    except Exception as e:
        _err("provenance.config_history", e)
        return []
    out = []
    for r in rows:
        try:
            out.append({"config_hash": r["config_hash"],
                        "changed_keys": json.loads(r["changed_keys_json"] or "{}"),
                        "recorded_at": r["recorded_at"]})
        except Exception:
            continue
    return out


# ── per-decision provenance + diff ────────────────────────────────────────────
def provenance_for(forecast_id) -> dict:
    """The complete provenance for one decision: its P6 forecast version, the active
    P7 experiment, the component versions, and the live config hash."""
    out = {"forecast_id": forecast_id, "versions": versions(),
           "config_hash": config_snapshot().get("_hash")}
    try:
        from harness import forecast_versions
        out["forecast_version"] = forecast_versions.get_forecast_version(forecast_id)
    except Exception as e:
        _err("provenance.provenance_for.fv", e)
        out["forecast_version"] = None
    try:
        from harness import experiments
        out["experiment"] = experiments.active_experiment()
    except Exception as e:
        _err("provenance.provenance_for.exp", e)
        out["experiment"] = None
    return out


def decision_diff(forecast_id_a, forecast_id_b) -> dict:
    """What differs between two decisions' forecast-version records."""
    try:
        from harness import forecast_versions
        a = forecast_versions.get_forecast_version(forecast_id_a) or {}
        b = forecast_versions.get_forecast_version(forecast_id_b) or {}
    except Exception as e:
        _err("provenance.decision_diff", e)
        return {}
    diff = {}
    for k in set(a) | set(b):
        if a.get(k) != b.get(k):
            diff[k] = {"a": a.get(k), "b": b.get(k)}
    return diff
