"""Plan 11 — decision feature snapshots (PAPER-ONLY profit intelligence).

At every bet / no-bet decision the bot can store a compact, honest feature snapshot so the
learning loop can later ask "what did the world look like when we decided?". The `decisions`
table (journal.py) is a flat transcript with no JSON column, so — rather than a risky migration
of that hot-path table — Plan 11 writes to a SEPARATE, additive `decision_features` table
(idempotent CREATE TABLE, the same pattern as Plan 9's clv_records / Plan 8's mirofish_runs).

GUARANTEES:
  * Recording is BEST-EFFORT and fully guarded: it never raises into the bettor and never
    changes a bet decision — it only observes.
  * Every snapshot carries paper_only=True.
  * No settlement/outcome field is required or stored as a *pre-trade* feature; this records
    the decision-time context only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

PAPER_ONLY_PROFIT_INTELLIGENCE = True

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")

# canonical actions
BET = "bet"
NO_BET = "no_bet"
OBSERVE_ONLY = "observe_only"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db(db_path=None) -> str:
    return db_path or DB_PATH


def init_features(db_path=None):
    conn = sqlite3.connect(_db(db_path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, market_id TEXT, question TEXT, source TEXT,
                action TEXT, reason TEXT, blocked_by_gate TEXT,
                price REAL, side TEXT, forecast_probability REAL,
                edge_raw REAL, edge_after_costs REAL,
                mirofish_state TEXT, mirofish_used INTEGER,
                accounting_status TEXT, gate2_status TEXT,
                features_json TEXT, paper_only INTEGER DEFAULT 1
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _num(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def build_snapshot(*, market_id=None, question=None, source=None, action=None, reason=None,
                   price=None, side=None, forecast_probability=None, edge_raw=None,
                   edge_after_costs=None, ev_reason=None, confidence=None, consensus=None,
                   n_agents_succeeded=None, challenger_probability=None, swarm_probability=None,
                   divergence=None, evidence_quality=None, evidence_source_count=None,
                   liquidity=None, spread=None, volume=None, stale=None, mirofish_state=None,
                   mirofish_used=None, mirofish_contribution=None, accounting_status=None,
                   gate2_status=None, blocked_by_gate=None, regime=None, ts=None,
                   **extra) -> dict:
    """Assemble a canonical decision feature snapshot. Always paper_only=True. Missing values
    stay None (never invented). `divergence` defaults to |swarm − challenger| when both given."""
    if divergence is None and swarm_probability is not None and challenger_probability is not None:
        try:
            divergence = abs(float(swarm_probability) - float(challenger_probability))
        except Exception:
            divergence = None
    snap = {
        "market_id": market_id, "question": question, "source": source,
        "timestamp": ts or _now(), "action": action, "reason": reason,
        "blocked_by_gate": blocked_by_gate, "price": _num(price), "side": side,
        "forecast_probability": _num(forecast_probability),
        "edge_raw": _num(edge_raw), "edge_after_costs": _num(edge_after_costs),
        "ev_reason": ev_reason, "confidence": _num(confidence), "consensus": _num(consensus),
        "n_agents_succeeded": n_agents_succeeded,
        "challenger_probability": _num(challenger_probability),
        "swarm_probability": _num(swarm_probability), "divergence": _num(divergence),
        "evidence_quality": _num(evidence_quality),
        "evidence_source_count": evidence_source_count,
        "liquidity": _num(liquidity), "spread": _num(spread), "volume": _num(volume),
        "stale": (None if stale is None else bool(stale)),
        "mirofish_state": mirofish_state, "mirofish_used": mirofish_used,
        "mirofish_contribution": mirofish_contribution,
        "accounting_status": accounting_status, "gate2_status": gate2_status,
        "regime": regime, "paper_only": True,
    }
    if extra:
        snap.update({k: v for k, v in extra.items() if k not in snap})
    return snap


def record(snapshot: dict, db_path=None) -> bool:
    """Persist a snapshot (core columns + the full snapshot as features_json). Best-effort:
    returns False on any error and NEVER raises (it must not perturb the decision path)."""
    try:
        s = dict(snapshot or {})
        s.setdefault("paper_only", True)
        init_features(db_path)
        conn = sqlite3.connect(_db(db_path))
        try:
            conn.execute(
                "INSERT INTO decision_features (ts, market_id, question, source, action, reason, "
                "blocked_by_gate, price, side, forecast_probability, edge_raw, edge_after_costs, "
                "mirofish_state, mirofish_used, accounting_status, gate2_status, features_json, "
                "paper_only) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.get("timestamp") or _now(), s.get("market_id"), s.get("question"),
                 s.get("source"), s.get("action"), s.get("reason"), s.get("blocked_by_gate"),
                 _num(s.get("price")), s.get("side"), _num(s.get("forecast_probability")),
                 _num(s.get("edge_raw")), _num(s.get("edge_after_costs")),
                 s.get("mirofish_state"),
                 (None if s.get("mirofish_used") is None else int(bool(s.get("mirofish_used")))),
                 s.get("accounting_status"), s.get("gate2_status"),
                 json.dumps(s, default=str), int(bool(s.get("paper_only", True)))))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def record_decision(m=None, *, action, reason=None, source=None, db_path=None, **fields) -> bool:
    """Build + record a snapshot from a candidate dict `m` (Plans 8/9 attach _mf_status etc.)
    plus explicit decision locals in `fields`. Fully guarded — best-effort, never raises."""
    try:
        m = m or {}
        mf = m.get("_mf_status") if isinstance(m, dict) else None
        defaults = dict(
            market_id=m.get("market_id"), question=m.get("question"),
            price=(m.get("_price") if m.get("_price") is not None else m.get("price")),
            liquidity=m.get("liquidity"), volume=m.get("volume"),
            stale=m.get("_stale"), regime=m.get("regime"),
            mirofish_state=(mf.get("state") if isinstance(mf, dict) else m.get("_mirofish_state")),
            mirofish_used=(mf.get("mirofish_used") if isinstance(mf, dict) else m.get("_mirofish_used")),
        )
        defaults.update({k: v for k, v in fields.items() if v is not None})
        snap = build_snapshot(action=action, reason=reason, source=source, **defaults)
        return record(snap, db_path)
    except Exception:
        return False


def get_features(limit: int = 200, db_path=None, *, action=None) -> list[dict]:
    """Read recent feature rows (newest first). Each row's full snapshot is parsed from
    features_json. Best-effort: returns [] on missing table / error."""
    try:
        conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
        try:
            if action:
                rows = conn.execute("SELECT * FROM decision_features WHERE action=? "
                                    "ORDER BY id DESC LIMIT ?", (action, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM decision_features ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            fj = json.loads(d.get("features_json") or "{}")
            if isinstance(fj, dict):
                d["features"] = fj
        except Exception:
            d["features"] = {}
        out.append(d)
    return out
