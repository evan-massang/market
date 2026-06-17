"""Plan 10 — dashboard + supervisor TRUTH.

Proves the dashboard, health endpoints, supervisor, and runtime status can never show fake
green when services, data, accounting, MiroFish, Gate 2, or runtime cache are stale / unknown /
degraded. Temp dirs + temp DB only. NO real service starts, NO live daemons, NO live APIs
(network probes mocked), NO live DB, NO DB repair.
"""
import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_dst_")

# isolate the supervisor runtime + the heartbeat files BEFORE importing services/dashboard
_RT = tempfile.mkdtemp(prefix="ps_dst_rt_")
os.environ["SUPERVISOR_RUNTIME_DIR"] = os.path.join(_RT, ".runtime")
os.environ["HARNESS_HEARTBEAT"] = os.path.join(_RT, ".heartbeat.json")
os.environ["SAMEDAY_HEARTBEAT"] = os.path.join(_RT, ".heartbeat.sameday.json")
import atexit, shutil  # noqa: E402
atexit.register(lambda: shutil.rmtree(_RT, ignore_errors=True))

from harness import status_model as SM       # noqa: E402
from harness import heartbeat as HB           # noqa: E402
from harness import wallet                    # noqa: E402

# ── mock ALL network probes so tests are fast + offline ──────────────────────────
import harness.health as _H                   # noqa: E402
import harness.services as _SV                # noqa: E402
_H.ollama_health = lambda: {"up": False, "model_present": False, "model": "x"}
_H.mirofish_health = lambda: {"up": False, "mode": "local-fallback"}
_SV.ollama_check = lambda: (False, "mocked down")
_SV.http_ok = lambda url, timeout=3.0: (False, "mocked")
_SV.tcp_ok = lambda host, port, timeout=2.0: (False, "mocked")


def _db():
    return os.environ["DATABASE_URL"]


def _hb_path(name="svc"):
    return os.path.join(_RT, f"{name}.hb.json")


def _write_hb(path, *, now=None, pid=None, last_error=None, **over):
    t = time.time() if now is None else now
    base = {"service": "svc", "pid": (os.getpid() if pid is None else pid),
            "last_tick_at": SM.now_iso(t), "generated_at": SM.now_iso(t),
            "paper_only": True, "branch": "b", "commit": "abc", "loop_count": 1,
            "last_error": last_error}
    base.update(over)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(base, f)


# ─────────────────────── A. heartbeat / status (1-8) ──────────────────────────────

def test_fresh_heartbeat_healthy():
    p = _hb_path()
    HB.write("svc", path=p, stage="forecasting", loop_count=3)
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.HEALTHY and r["ok"] is True and r["reason"] == "heartbeat_ok"
    assert r["paper_only"] is True and r["details"]["stage"] == "forecasting"


def test_missing_heartbeat_not_started():
    r = HB.read(path=os.path.join(_RT, "nope.json"))
    assert r["state"] in (SM.NOT_STARTED, SM.UNKNOWN) and r["ok"] is False
    assert r["reason"] == "heartbeat_missing" and r["stale"] is True


def test_malformed_heartbeat_degraded():
    p = _hb_path("bad")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json at all")
    r = HB.read(path=p)
    assert r["state"] in (SM.DEGRADED, SM.UNKNOWN) and r["ok"] is False
    assert r["reason"] == "heartbeat_malformed"


def test_stale_heartbeat_stale():
    p = _hb_path("stale")
    _write_hb(p, now=time.time() - 100000)          # last tick long ago, live pid
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.STALE and r["ok"] is False and r["reason"] == "heartbeat_stale"


def test_future_heartbeat_unknown():
    p = _hb_path("future")
    _write_hb(p, now=time.time() + 100000)
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["ok"] is False and r["reason"] == "heartbeat_future_timestamp"


def test_heartbeat_last_error_degraded():
    p = _hb_path("err")
    _write_hb(p, last_error="boom in cycle")
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.DEGRADED and r["ok"] is False and r["reason"] == "heartbeat_service_error"


def test_dead_pid_not_healthy():
    p = _hb_path("dead")
    _write_hb(p, pid=999999)                          # fresh tick but the process is gone
    r = HB.read(path=p, max_age_seconds=900, check_pid=True)
    assert r["state"] == SM.CRASHED and r["ok"] is False and r["reason"] == "heartbeat_pid_not_running"


def test_disabled_service_not_red_not_green():
    state, reason = SM.classify_service(managed=True, enabled=False, exists=True, alive=False)
    assert state == SM.DISABLED and SM.is_green(state) is False
    # disabled is BENIGN — it does not drag the system to unsafe/red
    sysv = SM.system_status([{"name": "x", "kind": "service", "state": SM.DISABLED, "critical": True}])
    assert sysv["state"] == SM.SYS_HEALTHY


# ─────────────────────── B. supervisor (9-12) ─────────────────────────────────────

def test_alive_but_stale_heartbeat_is_stale():
    state, reason = SM.classify_service(managed=True, enabled=True, exists=True, alive=True,
                                        supervisor_status="WARN", heartbeat={"state": SM.STALE})
    assert state == SM.STALE and SM.is_green(state) is False


def test_process_missing_is_crashed():
    state, _ = SM.classify_service(managed=True, enabled=True, exists=True, alive=False,
                                   supervisor_status="FAIL")
    assert state == SM.CRASHED and SM.is_green(state) is False


def test_supervisor_alive_alone_not_system_healthy():
    # a crashed REQUIRED service must drag the system off green even if the supervisor is alive
    comps = [{"name": "ai_pipeline", "kind": "service", "state": SM.CRASHED, "critical": True}]
    sysv = SM.system_status(comps)
    assert sysv["ok"] is False and sysv["state"] in (SM.SYS_UNSAFE, SM.SYS_DEGRADED)


def test_paper_only_always_present():
    assert SM.status(SM.HEALTHY)["paper_only"] is True
    assert SM.system_status([])["paper_only"] is True
    assert HB.read(path=os.path.join(_RT, "x.json"))["paper_only"] is True


# ─────────────────────── C. dashboard API (13-20) ─────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    import harness.dashboard as D
    return TestClient(D.app)


def test_api_truth_has_canonical_fields():
    c = _client()
    r = c.get("/api/truth")
    assert r.status_code == 200
    b = r.json()
    assert set(("state", "generated_at", "paper_only", "ok")) <= set(b) and b["paper_only"] is True
    h = c.get("/api/health").json()
    assert h["paper_only"] is True and "generated_at" in h


def test_api_services_returns_states():
    c = _client()
    b = c.get("/api/services").json()
    assert "services" in b and "state" in b and b["paper_only"] is True
    # the real registry services should each carry a canonical state
    for name, row in (b["services"] or {}).items():
        assert "state" in row


def test_api_accounting_degrades_on_drift():
    _ready_drift()
    c = _client()
    b = c.get("/api/accounting").json()
    assert b["audit"]["status"] == "drift" and b["paper_only"] is True
    t = c.get("/api/truth").json()
    assert t["ok"] is False and t["accounting_status"] == "drift"


def test_api_mirofish_backend_alive_not_used():
    # backend "alive" (probe) must NOT make mirofish_used true / GREEN with no fresh_used runs
    _H.mirofish_health = lambda: {"up": True, "mode": "external"}
    try:
        c = _client()
        b = c.get("/api/mirofish").json()
        assert b["used"] == 0 and b.get("backend_alive") is True
        # backend alive + 0 used -> degraded, NOT green (the adversarial fake-green fix)
        assert b["state"] == "degraded" and b["ok"] is False
    finally:
        _H.mirofish_health = lambda: {"up": False, "mode": "local-fallback"}


def test_heartbeat_pid_check_unavailable_not_healthy():
    # ADVERSARIAL: if the PID liveness check itself cannot run, the heartbeat must FAIL CLOSED
    # (never fall through to HEALTHY on an unverifiable heartbeat).
    p = _hb_path("pidfail")
    _write_hb(p)                                          # fresh, no error, live pid
    import harness.procman as _pm
    def _boom(pid):
        raise RuntimeError("procman exploded")
    with patched(_pm, "is_alive", _boom):
        r = HB.read(path=p, max_age_seconds=900, check_pid=True)
    assert r["state"] == SM.UNKNOWN and r["ok"] is False and r["reason"] == "heartbeat_pid_check_unavailable"


def test_heartbeat_commit_mismatch_degraded():
    # ADVERSARIAL: a heartbeat written by a DIFFERENT commit must not read fully green.
    p = _hb_path("oldcommit")
    with patched(SM, "version_info", lambda use_cache=True: {"git_commit": "CURRENT", "git_branch": "b",
                                                              "git_dirty": False, "code_version": "v"}):
        _write_hb(p, commit="OLD-DIFFERENT-COMMIT")      # fresh, live pid, but stale code
        r = HB.read(path=p, max_age_seconds=900, check_pid=True)
    assert r["state"] == SM.DEGRADED and r["ok"] is False and r["reason"] == "heartbeat_commit_mismatch"


def test_heartbeat_moderate_future_skew_caught():
    # ADVERSARIAL: a tick seconds in the future (well beyond same-clock jitter) is anomalous and
    # must NOT read healthy (tolerance tightened from 60s to 5s).
    p = _hb_path("skew")
    _write_hb(p, now=time.time() + 30)
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["reason"] == "heartbeat_future_timestamp"


def test_heartbeat_stale_generated_at_caught():
    # ADVERSARIAL r5 (critical): a fresh last_tick_at must NOT mask a stale generated_at — a
    # heartbeat is fresh only if ALL its timestamps are fresh.
    p = _hb_path("split")
    base = time.time()
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"service": "svc", "pid": os.getpid(),
                   "last_tick_at": SM.now_iso(base),               # fresh
                   "generated_at": SM.now_iso(base - 100000),      # stale
                   "commit": None, "paper_only": True}, f)
    r = HB.read(path=p, now=base, max_age_seconds=900)
    assert r["state"] == SM.STALE and r["reason"] == "heartbeat_stale" and r["ok"] is False


def test_api_mirofish_stale_used_not_green():
    # ADVERSARIAL r5 (medium): a run that was historically used but is STALE now (or a dead
    # backend) must not show the card green — green needs a FRESH used run + live backend.
    from datetime import datetime, timezone, timedelta
    import harness.mirofish_validate as _MFV
    old = (datetime.now(timezone.utc) - timedelta(seconds=100000)).isoformat()
    row = {"market_id": "m", "usable": 1, "freshness_status": "fresh", "stage_reached": "report_done",
           "question_match_score": 0.9, "report_age_seconds": 5.0, "n_posts": 4,
           "min_sims_used": 3.0, "match_threshold_used": 0.3, "report_generated_at": old}
    _H.mirofish_health = lambda: {"up": True, "mode": "external"}
    try:
        with patched(_MFV, "get_runs", lambda *a, **k: [dict(row)]):
            c = _client()
            b = c.get("/api/mirofish").json()
        assert b["used"] == 1 and b["fresh_used"] == 0
        assert b["state"] == "degraded" and b["ok"] is False
    finally:
        _H.mirofish_health = lambda: {"up": False, "mode": "local-fallback"}


def test_api_decisions_empty_is_unknown_not_green():
    c = _client()
    b = c.get("/api/decisions").json()
    assert b["n"] == 0 and b["state"] == "unknown" and b["ok"] is False


def test_heartbeat_missing_pid_not_healthy():
    # ADVERSARIAL r2 (critical): a heartbeat with NO pid field must not read healthy when
    # check_pid is on — absence of verification defaults to untrusted (unknown), not green.
    p = _hb_path("nopid")
    _now = SM.now_iso(time.time())
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"service": "svc", "last_tick_at": _now, "generated_at": _now,
                   "commit": None, "paper_only": True}, f)   # NO pid
    r = HB.read(path=p, max_age_seconds=900, check_pid=True)
    assert r["state"] == SM.UNKNOWN and r["ok"] is False and r["reason"] == "heartbeat_pid_missing"


def test_heartbeat_missing_generated_at_malformed():
    # ADVERSARIAL r6 (medium): the structured contract requires BOTH timestamps; a heartbeat
    # omitting generated_at must read malformed (not healthy), even with a fresh last_tick_at.
    p = _hb_path("onestamp")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"service": "svc", "pid": os.getpid(), "last_tick_at": SM.now_iso(time.time()),
                   "commit": None, "paper_only": True}, f)   # NO generated_at
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.DEGRADED and r["ok"] is False and r["reason"] == "heartbeat_malformed"


def test_heartbeat_2s_future_caught():
    # ADVERSARIAL r2 (high): even a 2s-future tick (within the OLD 5s tolerance) must be caught.
    p = _hb_path("skew2")
    _write_hb(p, now=time.time() + 2)
    r = HB.read(path=p, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["reason"] == "heartbeat_future_timestamp"


def test_heartbeat_subsecond_future_caught():
    # ADVERSARIAL r4 (high): even a sub-second future tick (full-precision, not floored) must be
    # caught — floored genuine ticks have age >= 0, so any age < 0 is anomalous.
    from datetime import datetime, timezone
    p = _hb_path("subsec")
    base = time.time()
    fut = datetime.fromtimestamp(base + 0.7, timezone.utc).isoformat()   # 0.7s future, full precision
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"service": "svc", "pid": os.getpid(), "last_tick_at": fut, "generated_at": fut,
                   "commit": None, "paper_only": True}, f)
    r = HB.read(path=p, now=base, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["reason"] == "heartbeat_future_timestamp"


def test_api_state_survives_scoreboard_error():
    # ADVERSARIAL r6 (high): a DB error in scoreboard.compute() must NOT 500 /api/state (which
    # would leave the HTML showing the LAST, stale gate cards). It degrades to safe defaults.
    import harness.scoreboard as _SB
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    c = _client()
    with patched(_SB, "compute", _boom):
        r = c.get("/api/state")
    assert r.status_code == 200
    b = r.json()
    assert b["paper_only"] is True and b["scoreboard"]["gate2"] in (None, False)
    assert b["equity_verified"] is False


def test_health_ok_reflects_db():
    # ADVERSARIAL r4 (high): /health 'ok' mirrors the actual DB check it performs — never hardcoded.
    c = _client()
    h = c.get("/health").json()
    assert h["ok"] == h["db_ok"] and h["paper_only"] is True       # on the temp DB both are True
    import harness.wallet as W
    with patched(W, "DB_PATH", os.path.join(_RT, "missing_dir", "x.db")):   # unopenable -> db_ok False
        h2 = c.get("/health").json()
    assert h2["db_ok"] is False and h2["ok"] is False


def test_truth_gate2_reported_separately():
    # Gate 2 is surfaced as an EXPLICIT field, NOT hidden inside the system-health aggregation.
    c = _client()
    t = c.get("/api/truth").json()
    assert "gate2_pass" in t and "gate2_status" in t
    names = [comp.get("name") for comp in t["details"]["components"]]
    assert "gate2" not in names                                     # not a system-health component


def test_db_locked_or_corrupt_not_green():
    # ADVERSARIAL r2 (critical): a present-but-UNUSABLE DB (locked/corrupt) is not 'ok' — the
    # System truth must not be green just because the DB FILE exists.
    import harness.dashboard as D
    bad = os.path.join(_RT, "corrupt.db")
    with open(bad, "wb") as f:
        f.write(b"this is definitely not a sqlite database")
    with patched(D, "DB_PATH", bad):
        assert D._db_usable() is False
        t = D.api_truth().body
    import json as _json
    body = _json.loads(t)
    db_comp = [c for c in body["details"]["components"] if c["name"] == "db"]
    assert db_comp and db_comp[0]["state"] == "error"
    assert SM.is_green(body["state"]) is False and body["db_ok"] is False


def test_api_gates_uses_gate2_status():
    c = _client()
    b = c.get("/api/gates").json()
    assert "gate2" in b and "status" in b["gate2"]      # Plan 9 gate2_status surfaced
    assert (b["gate2"].get("pass") is True) == (b["state"] == "ok" and b["gate1"].get("pass"))


def test_endpoints_no_500_on_empty_db():
    c = _client()
    for path in ("/api/services", "/api/scoreboard", "/api/mirofish", "/api/decisions",
                 "/api/gates", "/api/version", "/api/truth", "/api/health", "/api/accounting"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)


def test_expected_cards_not_404():
    c = _client()
    for path in ("/api/health", "/api/services", "/api/accounting", "/api/scoreboard",
                 "/api/mirofish", "/api/decisions", "/api/gates", "/api/version"):
        assert c.get(path).status_code != 404, path


# ─────────────────────── D. dashboard honesty (21-27) ─────────────────────────────

def _ready_drift():
    """A wallet whose running total is drifted from the ledger -> accounting status 'drift'."""
    import sqlite3
    conn = sqlite3.connect(_db())
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)
    conn = sqlite3.connect(_db())
    conn.execute("UPDATE paper_wallet SET cash=cash+500 WHERE id=1")   # break the ledger
    conn.commit(); conn.close()


def test_accounting_failure_prevents_green():
    _ready_drift()
    c = _client()
    t = c.get("/api/truth").json()
    assert SM.is_green(t["state"]) is False and t["accounting_status"] == "drift"
    h = c.get("/api/health").json()
    assert h["accounting"]["verified"] is False


def test_stale_service_prevents_green():
    comps = [{"name": "sameday_daemon", "kind": "service", "state": SM.STALE, "critical": True}]
    sysv = SM.system_status(comps)
    assert SM.is_green(sysv["state"]) is False and sysv["state"] == SM.SYS_STALE


def test_stale_cache_prevents_green():
    p = os.path.join(_RT, "cache.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"generated_at": SM.now_iso(time.time() - 100000), "x": 1}, f)
    r = SM.read_runtime_json(p, max_age_seconds=900)
    assert SM.is_green(r["state"]) is False and r["reason"] == "runtime_cache_stale"


def test_mirofish_unused_not_green_used():
    c = _client()
    b = c.get("/api/mirofish").json()
    assert b["used"] == 0           # no fresh_used runs on an empty DB


def test_gate2_unknown_not_pass():
    # empty DB -> gate2 cannot pass
    c = _client()
    b = c.get("/api/gates").json()
    assert b["gate2"].get("pass") is False and b["state"] != "ok"


def test_unknown_is_not_green():
    assert SM.is_green(SM.UNKNOWN) is False
    assert SM.status(SM.UNKNOWN)["ok"] is False


def test_paper_only_labels_present():
    c = _client()
    for path in ("/api/services", "/api/scoreboard", "/api/mirofish", "/api/decisions",
                 "/api/gates", "/api/version", "/api/truth", "/api/accounting"):
        assert c.get(path).json().get("paper_only") is True, path


# ─────────────────────── E. runtime cache (28-31) ─────────────────────────────────

def test_cache_requires_generated_at():
    p = os.path.join(_RT, "nogen.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"x": 1}, f)
    r = SM.read_runtime_json(p)
    assert r["state"] == SM.DEGRADED and r["reason"] == "runtime_cache_malformed"


def test_cache_stale_degraded():
    p = os.path.join(_RT, "old.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"generated_at": SM.now_iso(time.time() - 5000)}, f)
    r = SM.read_runtime_json(p, max_age_seconds=900)
    assert r["state"] == SM.STALE and r["stale"] is True


def test_cache_future_unknown():
    p = os.path.join(_RT, "fut.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"generated_at": SM.now_iso(time.time() + 5000)}, f)
    r = SM.read_runtime_json(p, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["reason"] == "runtime_cache_future_timestamp"


def test_cache_malformed_degraded():
    p = os.path.join(_RT, "mal.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("<<<not json>>>")
    r = SM.read_runtime_json(p)
    assert r["state"] == SM.DEGRADED and r["reason"] == "runtime_cache_malformed"


def test_cache_missing_stale():
    r = SM.read_runtime_json(os.path.join(_RT, "ghost.json"))
    assert r["reason"] == "runtime_cache_missing" and r["stale"] is True


def test_cache_moderate_future_caught():
    # ADVERSARIAL r3 (high): runtime-cache future tolerance tightened from 60s to 1s.
    p = os.path.join(_RT, "fut2.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"generated_at": SM.now_iso(time.time() + 3)}, f)
    r = SM.read_runtime_json(p, max_age_seconds=900)
    assert r["state"] == SM.UNKNOWN and r["reason"] == "runtime_cache_future_timestamp"


def test_supervisor_unreadable_heartbeat_not_green():
    # ADVERSARIAL r3 (high): a service that DECLARES a structured heartbeat but whose heartbeat
    # cannot be read must NOT fall back to an mtime-only "healthy" — unverifiable -> unknown.
    from harness import supervisor as SUP
    from harness import services as SVm
    import harness.heartbeat as _HBmod
    svc = SVm.Service(name="x", cmd=[], cwd=".", enabled=True, required=True, manage=True, order=1,
                      hb_json=os.path.join(_RT, "whatever.json"), heartbeat_max_age=600.0,
                      heartbeat_path=os.path.join(_RT, "x.log"))
    row = {"managed": True, "enabled": True, "alive": True, "status": "OK"}
    def _boom(*a, **k):
        raise RuntimeError("hb read exploded")
    with patched(_HBmod, "read", _boom):
        state, reason, age, stale = SUP._canonical(svc, row)
    assert state == SM.UNKNOWN and SM.is_green(state) is False


def test_version_info_does_not_cache_none():
    # ADVERSARIAL r3 (high): a transient git-unavailable must not POISON the version cache with
    # None (which would hide a later commit mismatch). None is never cached.
    import harness.obs.codeversion as _cv
    SM._VERSION_CACHE.clear()
    with patched(_cv, "reproducibility", lambda: {"git_sha": None, "git_dirty": None, "code_version": None}), \
         patched(_cv, "_git", lambda *a, **k: None):
        v = SM.version_info(use_cache=True)
    assert v["git_commit"] is None and not SM._VERSION_CACHE     # not poisoned -> retries later


# ─────────────────────── F. version freshness (32-34) ─────────────────────────────

def test_commit_mismatch():
    assert SM.commit_mismatch("aaa", "bbb") is True
    assert SM.commit_mismatch("aaa", "aaa") is False
    assert SM.commit_mismatch(None, "bbb") is False        # unknown -> cannot tell -> no false alarm


def test_version_info_shape():
    v = SM.version_info(use_cache=False)
    assert set(("git_branch", "git_commit", "git_dirty", "code_version")) <= set(v)


def test_version_unavailable_returns_unknown_not_crash():
    # if codeversion explodes, version_info returns the None-filled dict (never raises)
    import harness.obs.codeversion as _cv
    def _boom():
        raise RuntimeError("no git")
    with patched(_cv, "reproducibility", _boom):
        v = SM.version_info(use_cache=False)
    assert v["git_commit"] is None and v["code_version"] is None


# ─────────────────────── G. static scans (35-38) ──────────────────────────────────

def _src(rel):
    return open(os.path.join(ROOT, "harness", rel), encoding="utf-8").read()


def test_static_dashboard_health_uses_accounting_audit():
    assert "audit_accounting" in _src("health.py")           # health snapshot consumes the audit
    d = _src("dashboard.py")
    assert "audit_accounting" in d and "/api/truth" in d and "accounting" in d


def test_static_mirofish_card_uses_canonical_state():
    d = _src("dashboard.py")
    assert "mirofish_status" in d and "state_from_row" in d and "mirofish_used" in d


def test_static_gates_card_uses_gate2_status():
    d = _src("dashboard.py")
    assert "gate2_status" in d                                # /api/gates + scoreboard use Plan 9 gate2
    assert "gate2_reasons" in d                               # the gates card renders the reason


def test_static_no_unknown_mapped_to_green():
    # the status model never makes unknown green, and the dashboard JS colour maps don't either
    assert SM.is_green(SM.UNKNOWN) is False and SM.is_green(SM.STALE) is False
    d = _src("dashboard.py")
    assert not re.search(r"unknown\s*:\s*'var\(--green\)'", d)
    assert not re.search(r"crashed\s*:\s*'var\(--green\)'", d)


TESTS = [
    ("fresh_heartbeat_healthy", test_fresh_heartbeat_healthy),
    ("missing_heartbeat_not_started", test_missing_heartbeat_not_started),
    ("malformed_heartbeat_degraded", test_malformed_heartbeat_degraded),
    ("stale_heartbeat_stale", test_stale_heartbeat_stale),
    ("future_heartbeat_unknown", test_future_heartbeat_unknown),
    ("heartbeat_last_error_degraded", test_heartbeat_last_error_degraded),
    ("dead_pid_not_healthy", test_dead_pid_not_healthy),
    ("disabled_service_not_red_not_green", test_disabled_service_not_red_not_green),
    ("alive_but_stale_heartbeat_is_stale", test_alive_but_stale_heartbeat_is_stale),
    ("process_missing_is_crashed", test_process_missing_is_crashed),
    ("supervisor_alive_alone_not_system_healthy", test_supervisor_alive_alone_not_system_healthy),
    ("paper_only_always_present", test_paper_only_always_present),
    ("api_truth_has_canonical_fields", test_api_truth_has_canonical_fields),
    ("api_services_returns_states", test_api_services_returns_states),
    ("api_accounting_degrades_on_drift", test_api_accounting_degrades_on_drift),
    ("api_mirofish_backend_alive_not_used", test_api_mirofish_backend_alive_not_used),
    ("heartbeat_stale_generated_at_caught", test_heartbeat_stale_generated_at_caught),
    ("api_mirofish_stale_used_not_green", test_api_mirofish_stale_used_not_green),
    ("heartbeat_pid_check_unavailable_not_healthy", test_heartbeat_pid_check_unavailable_not_healthy),
    ("heartbeat_commit_mismatch_degraded", test_heartbeat_commit_mismatch_degraded),
    ("heartbeat_moderate_future_skew_caught", test_heartbeat_moderate_future_skew_caught),
    ("api_decisions_empty_is_unknown_not_green", test_api_decisions_empty_is_unknown_not_green),
    ("heartbeat_missing_pid_not_healthy", test_heartbeat_missing_pid_not_healthy),
    ("heartbeat_missing_generated_at_malformed", test_heartbeat_missing_generated_at_malformed),
    ("heartbeat_2s_future_caught", test_heartbeat_2s_future_caught),
    ("api_state_survives_scoreboard_error", test_api_state_survives_scoreboard_error),
    ("heartbeat_subsecond_future_caught", test_heartbeat_subsecond_future_caught),
    ("health_ok_reflects_db", test_health_ok_reflects_db),
    ("truth_gate2_reported_separately", test_truth_gate2_reported_separately),
    ("db_locked_or_corrupt_not_green", test_db_locked_or_corrupt_not_green),
    ("api_gates_uses_gate2_status", test_api_gates_uses_gate2_status),
    ("endpoints_no_500_on_empty_db", test_endpoints_no_500_on_empty_db),
    ("expected_cards_not_404", test_expected_cards_not_404),
    ("accounting_failure_prevents_green", test_accounting_failure_prevents_green),
    ("stale_service_prevents_green", test_stale_service_prevents_green),
    ("stale_cache_prevents_green", test_stale_cache_prevents_green),
    ("mirofish_unused_not_green_used", test_mirofish_unused_not_green_used),
    ("gate2_unknown_not_pass", test_gate2_unknown_not_pass),
    ("unknown_is_not_green", test_unknown_is_not_green),
    ("paper_only_labels_present", test_paper_only_labels_present),
    ("cache_requires_generated_at", test_cache_requires_generated_at),
    ("cache_stale_degraded", test_cache_stale_degraded),
    ("cache_future_unknown", test_cache_future_unknown),
    ("cache_malformed_degraded", test_cache_malformed_degraded),
    ("cache_missing_stale", test_cache_missing_stale),
    ("cache_moderate_future_caught", test_cache_moderate_future_caught),
    ("supervisor_unreadable_heartbeat_not_green", test_supervisor_unreadable_heartbeat_not_green),
    ("version_info_does_not_cache_none", test_version_info_does_not_cache_none),
    ("commit_mismatch", test_commit_mismatch),
    ("version_info_shape", test_version_info_shape),
    ("version_unavailable_returns_unknown_not_crash", test_version_unavailable_returns_unknown_not_crash),
    ("static_dashboard_health_uses_accounting_audit", test_static_dashboard_health_uses_accounting_audit),
    ("static_mirofish_card_uses_canonical_state", test_static_mirofish_card_uses_canonical_state),
    ("static_gates_card_uses_gate2_status", test_static_gates_card_uses_gate2_status),
    ("static_no_unknown_mapped_to_green", test_static_no_unknown_mapped_to_green),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
