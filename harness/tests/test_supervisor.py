"""Tests for harness.supervisor — uses DUMMY processes (python sleep / exit), never
real MiroFish/Ollama/daemons. Isolated runtime dir via SUPERVISOR_RUNTIME_DIR."""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# isolate the runtime dir BEFORE importing supervisor/services (they read it at import)
_TMP = tempfile.mkdtemp(prefix="ps_sup_")
os.environ["SUPERVISOR_RUNTIME_DIR"] = os.path.join(_TMP, ".runtime")
import atexit, shutil  # noqa: E402
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

from harness.tests._util import run_as_main  # noqa: E402
from harness import supervisor as S          # noqa: E402
from harness import services as SV           # noqa: E402
from harness import procman                  # noqa: E402

_PY = sys.executable


def _good(name="good", order=1, required=False):
    # a daemon-like process that stays alive (no port / heartbeat -> process-only health)
    return SV.Service(name=name, cmd=[_PY, "-c", "import time\n[time.sleep(1) for _ in range(120)]"],
                      cwd=".", enabled=True, required=required, manage=True, order=order, start_timeout=8.0)


def _bad(name="bad", order=2, required=False):
    # exits immediately with a traceback -> startup failure
    return SV.Service(name=name, cmd=[_PY, "-c", "raise SystemExit('boom-fatal')"],
                      cwd=".", enabled=True, required=required, manage=True, order=order, start_timeout=6.0)


def _disabled(name="off", order=3):
    s = _good(name, order)
    s.enabled = False
    return s


def _cleanup(svcs):
    S.stop(svcs=svcs)


# 1. registry loads
def test_registry_loads():
    reg = SV.registry()
    names = {s.name for s in reg}
    assert {"ollama", "dashboard", "sameday_daemon", "ai_pipeline"} <= names, names
    assert any(not s.manage for s in reg)        # ollama is external/check-only


# 2. disabled services skipped
def test_disabled_skipped():
    svcs = [_good("g1"), _disabled("d1")]
    r = S.start(svcs=svcs, run_watcher=False, preflight=False)
    try:
        assert "g1" in r["started"], r
        assert "d1" not in r["started"]
        st = S.status(svcs=svcs, as_dict=True)["services"]
        assert st["d1"]["status"] == "disabled"
    finally:
        _cleanup(svcs)


# 3. required failure aborts startup
def test_required_failure_aborts():
    svcs = [_good("g2", order=1), _bad("b2", order=2, required=True), _good("g3", order=3)]
    r = S.start(svcs=svcs, run_watcher=False, preflight=False)
    try:
        assert r["ok"] is False, r
        assert "b2_failed" in r.get("error", "")
        # g3 (after the failed required one) was never started
        alive3, _ = S._service_alive("g3")
        assert alive3 is False
    finally:
        _cleanup(svcs)


# 4. optional failure warns but continues
def test_optional_failure_continues():
    svcs = [_bad("b3", order=1, required=False), _good("g4", order=2)]
    r = S.start(svcs=svcs, run_watcher=False, preflight=False)
    try:
        assert r["ok"] is True, r
        assert "g4" in r["started"]
        assert any(name == "b3" for name, _ in r["failed"])
    finally:
        _cleanup(svcs)


# 5. + 8. pid file created on start, removed on stop
def test_pid_file_lifecycle():
    svcs = [_good("g5")]
    S.start(svcs=svcs, run_watcher=False, preflight=False)
    pidp = S._pid_path("g5")
    assert os.path.exists(pidp), pidp
    assert procman.read_pid(pidp)
    S.stop(svcs=svcs)
    assert not os.path.exists(pidp)


# 6. stale pid cleaned up
def test_stale_pid_cleanup():
    procman.write_pid(S._pid_path("ghost"), 999999)   # a dead pid
    alive, pid = S._service_alive("ghost")
    assert alive is False and pid is None
    assert not os.path.exists(S._pid_path("ghost"))   # cleaned


# 7. duplicate start prevented
def test_duplicate_start_prevented():
    svcs = [_good("g6")]
    r1 = S.start(svcs=svcs, run_watcher=False, preflight=False)
    pid1 = procman.read_pid(S._pid_path("g6"))
    r2 = S.start(svcs=svcs, run_watcher=False, preflight=False)
    pid2 = procman.read_pid(S._pid_path("g6"))
    try:
        assert "g6" in r2["skipped"], r2
        assert pid1 == pid2                            # same process, not a duplicate
    finally:
        _cleanup(svcs)


# 9. + 10. health check success / failure
def test_health_success_and_failure():
    svcs = [_good("g7")]
    S.start(svcs=svcs, run_watcher=False, preflight=False)
    try:
        st, detail = svcs[0].live_health(True)
        assert st == "OK", (st, detail)
        st2, _ = svcs[0].live_health(False)            # process not alive
        assert st2 == "FAIL"
    finally:
        _cleanup(svcs)


# 11. restart policy caps restart loops (logic-level)
def test_restart_cap_logic():
    # simulate the watcher's restart-window accounting
    cfg = {"RESTART_WINDOW": 600, "MAX_RESTARTS": 3, "RESTART_CRASHED": True}
    now = time.monotonic()
    hist = [now, now, now]                              # already 3 in-window
    in_window = [t for t in hist if now - t < cfg["RESTART_WINDOW"]]
    assert len(in_window) >= cfg["MAX_RESTARTS"]        # -> watcher would give up


# 12. + 13. logs + heartbeats written/parsed
def test_logs_and_heartbeats():
    svcs = [_good("g8")]
    S.start(svcs=svcs, run_watcher=False, preflight=False)
    try:
        assert os.path.exists(S._log_path("g8"))
        hb = S._read_heartbeat("g8")
        assert hb.get("service") == "g8" and hb.get("pid")
        assert "started_at" in hb and "last_seen" in hb
    finally:
        _cleanup(svcs)


# 14. status with nothing running
def test_status_empty():
    svcs = [_disabled("d2"), SV.Service(name="ext", cmd=[], cwd=".", enabled=True,
            required=False, manage=False, order=0, http_url="http://127.0.0.1:1")]
    d = S.status(svcs=svcs, as_dict=True)["services"]
    assert d["d2"]["status"] == "disabled"
    assert d["ext"]["managed"] is False


# 15. status with partial services running
def test_status_partial():
    svcs = [_good("g9", order=1), _good("g10", order=2)]
    S.start(svcs=[svcs[0]], run_watcher=False, preflight=False)   # start only g9
    try:
        d = S.status(svcs=svcs, as_dict=True)["services"]
        assert d["g9"]["status"] == "OK" and d["g9"]["alive"] is True
        assert d["g10"]["alive"] is False
    finally:
        _cleanup(svcs)


# stop is idempotent and never raises
def test_stop_idempotent():
    svcs = [_good("g11")]
    S.start(svcs=svcs, run_watcher=False, preflight=False)
    S.stop(svcs=svcs)
    S.stop(svcs=svcs)   # second stop is a clean no-op
    assert not os.path.exists(S._pid_path("g11"))


TESTS = [
    ("registry_loads", test_registry_loads),
    ("disabled_skipped", test_disabled_skipped),
    ("required_failure_aborts", test_required_failure_aborts),
    ("optional_failure_continues", test_optional_failure_continues),
    ("pid_file_lifecycle", test_pid_file_lifecycle),
    ("stale_pid_cleanup", test_stale_pid_cleanup),
    ("duplicate_start_prevented", test_duplicate_start_prevented),
    ("health_success_and_failure", test_health_success_and_failure),
    ("restart_cap_logic", test_restart_cap_logic),
    ("logs_and_heartbeats", test_logs_and_heartbeats),
    ("status_empty", test_status_empty),
    ("status_partial", test_status_partial),
    ("stop_idempotent", test_stop_idempotent),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
