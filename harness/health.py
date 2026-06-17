"""System health probes. Every value is a live HTTP call, a real DB row timestamp, or a
file mtime — never a hardcoded status."""
from __future__ import annotations
import os, sqlite3, time
from datetime import datetime, timezone
import httpx

_DB        = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")
OLLAMA     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL      = os.getenv("MODEL_FAST", "qwen2.5:7b")
MF_BACKEND = os.getenv("MIROFISH_BASE", "http://localhost:5001")
STREAM_LOG = os.getenv("DASH_STREAM_LOG", "sameday_live.log")
HEARTBEAT  = os.getenv("HARNESS_HEARTBEAT", ".heartbeat.json")

def _age(ts):  # seconds since an epoch ts, or None
    return None if ts is None else max(0.0, time.time() - ts)

def _last_row_ts(table, col="created_at"):
    try:
        conn = sqlite3.connect(_DB)
        row = conn.execute(f"SELECT {col} FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if not row or not row[0]: return None
        dt = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except Exception:
        return None

def ollama_health():
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=4)
        models = [m.get("name","") for m in r.json().get("models", [])]
        return {"up": True, "model_present": any(MODEL.split(':')[0] in m for m in models),
                "model": MODEL, "n_models": len(models)}
    except Exception as e:
        return {"up": False, "model_present": False, "model": MODEL, "error": str(e)[:120]}

def mirofish_health():
    try:
        httpx.get(MF_BACKEND, timeout=3); return {"up": True, "mode": "external"}
    except Exception:
        return {"up": False, "mode": "local-crowd-fallback"}   # not an error: A2 is the fallback

def heartbeat_health():
    # Daemon liveness = whichever signal it last touched: loop/predict_today write
    # .heartbeat.json; the sameday daemon writes the stream log every cycle. Trust the
    # FRESHEST of the two — first-exists wrongly pinned the badge to a stale .heartbeat.json
    # while the sameday daemon was actively running (false "daemon down").
    cands = [(p, os.path.getmtime(p)) for p in (HEARTBEAT, STREAM_LOG) if os.path.exists(p)]
    if not cands:
        return {"source": None, "age_sec": None}
    p, mt = max(cands, key=lambda x: x[1])
    return {"source": p, "age_sec": _age(mt)}

def snapshot():
    # Table names confirmed against `sqlite3 polyswarm.db '.tables'`; timestamp COLUMNS corrected
    # to the real schema (paper_positions has no created_at -> opened_at; decisions -> ts). The
    # swarm's market-level forecasts live in swarm_forecasts (one row per swarm forecast).
    snap = {
        "ts": time.time(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ollama": ollama_health(),
        "mirofish_backend": mirofish_health(),
        "daemon": heartbeat_health(),
        "freshness_sec": {
            "swarm_forecast": _age(_last_row_ts("swarm_forecasts")),
            "challenger":     _age(_last_row_ts("baseline_forecasts")),
            "mirofish":       _age(_last_row_ts("mirofish_forecasts")),
            "paper_position": _age(_last_row_ts("paper_positions", "opened_at")),
            "decision":       _age(_last_row_ts("decisions", "ts")),
        },
        "paper_only": True,
    }
    # Plan 9: ACCOUNTING TRUTH. Service-liveness alone must not paint the badge green — the
    # health card distinguishes "service alive" + "data fresh" from "accounting VERIFIED".
    # Read-only, best-effort (an audit fault is reported as unverified, never as healthy).
    try:
        from harness import accounting_audit as _acct
        a = _acct.audit_accounting()
        snap["accounting"] = {"status": a["status"], "verified": bool(a["ok"]),
                              "drift": a["drift"], "mark_stale_count": a["mark_stale_count"],
                              "reasons": a["reasons"]}
    except Exception as e:
        snap["accounting"] = {"status": "unknown", "verified": False, "reasons": ["accounting_unavailable"],
                              "error": str(e)}
    # Plan 10: per-daemon HONEST heartbeat — the OLD `daemon` field uses MAX(mtime) which masks
    # one stale daemon when the other is fresh. The structured per-daemon read can never mask a
    # dead/stale daemon (a heartbeat whose PID is gone -> crashed, not fresh).
    try:
        from harness import heartbeat as _hb
        snap["daemons"] = {
            "ai_pipeline": _hb.read(path=HEARTBEAT, max_age_seconds=900.0),
            "sameday_daemon": _hb.read(path=os.getenv("SAMEDAY_HEARTBEAT", ".heartbeat.sameday.json"),
                                       max_age_seconds=600.0),
        }
    except Exception:
        snap["daemons"] = {}
    # Plan 10: code version / branch / commit (None when git is unavailable; never raises).
    try:
        from harness import status_model as _sm
        snap["version"] = _sm.version_info()
    except Exception:
        snap["version"] = {}
    return snap

if __name__ == "__main__":
    import json; print(json.dumps(snapshot(), indent=2, default=str))
