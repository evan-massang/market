"""harness.supervisor — one-command background orchestrator for the Polymarket AI.

    python -m harness.supervisor start            start all enabled services (background)
    python -m harness.supervisor stop             stop everything cleanly
    python -m harness.supervisor restart          stop + start
    python -m harness.supervisor status           show every service + health
    python -m harness.supervisor logs             recent logs from all services
    python -m harness.supervisor tail <service>   follow one service's log
    python -m harness.supervisor doctor           readiness check before starting
    python -m harness.supervisor start|stop|restart <service>

Design: the Python supervisor is the SINGLE source of truth. Services are launched
DETACHED (no window, survive the terminal) via harness.procman; PIDs are recorded in
.runtime/pids/ and ONLY those PIDs are ever stopped (never an unrelated process). A
detached watcher (`_watch`) restarts crashed REQUIRED services within a capped window.
PAPER-ONLY; no secrets are printed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from harness import procman, services
from harness import status_model as _sm
from harness.services import (RUNTIME, PIDS, LOGS, HEARTBEATS, STATUS_JSON, STOP_FLAG)

DISABLED_DIR = os.path.join(RUNTIME, "disabled")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_KEEP = 5


# ── paths / fs ──────────────────────────────────────────────────────────────────
def _ensure_dirs():
    for d in (RUNTIME, PIDS, LOGS, HEARTBEATS, DISABLED_DIR):
        os.makedirs(d, exist_ok=True)


def _pid_path(name): return os.path.join(PIDS, name + ".pid")
def _log_path(name): return os.path.join(LOGS, name + ".log")
def _hb_path(name): return os.path.join(HEARTBEATS, name + ".json")
def _disabled_path(name): return os.path.join(DISABLED_DIR, name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _service_alive(name):
    pid = procman.read_pid(_pid_path(name))
    if pid and procman.is_alive(pid):
        return True, pid
    if pid:                       # stale pid file -> clean it (Phase 11)
        procman.clear_pid(_pid_path(name))
    return False, None


def _rotate_log(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            for i in range(LOG_KEEP - 1, 0, -1):
                a, b = f"{path}.{i}", f"{path}.{i + 1}"
                if os.path.exists(a):
                    os.replace(a, b)
            os.replace(path, path + ".1")
    except Exception:
        pass


def _log_tail(name, n=40) -> list[str]:
    try:
        with open(_log_path(name), "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def _log_has_fatal(name):
    markers = ("Traceback (most recent", "Address already in use", "ModuleNotFoundError",
               "ImportError", "could not be resolved", "Errno 98", "Errno 10048")
    for line in _log_tail(name, 60):
        for m in markers:
            if m in line:
                return line.strip()[:160]
    return None


# ── heartbeat + status ──────────────────────────────────────────────────────────
def _write_heartbeat(name, pid, started_at, restart_count, status, detail):
    try:
        with open(_hb_path(name), "w", encoding="utf-8") as f:
            json.dump({"service": name, "pid": pid, "started_at": started_at,
                       "last_seen": _now(), "restart_count": restart_count,
                       "status": status, "detail": detail}, f)
    except Exception:
        pass


def _read_heartbeat(name):
    try:
        with open(_hb_path(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _restart_count(name) -> int:
    return int(_read_heartbeat(name).get("restart_count", 0) or 0)


def _started_at(name) -> str:
    return _read_heartbeat(name).get("started_at") or _now()


def _write_status(svcs, supervisor_pid=None):
    data = {"updated_at": _now(), "supervisor_pid": supervisor_pid, "services": {}}
    for svc, row in _collect_status(svcs):
        data["services"][svc.name] = row
    try:
        _ensure_dirs()
        with open(STATUS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    return data


def _service_hb_canon(svc, alive):
    """Plan 10: a canonical heartbeat read for a managed, alive service. Prefers the STRUCTURED
    hb_json (stage/last_error/pid) and falls back to the supervisor's liveness file mtime.
    Returns (heartbeat_dict_or_None, age_seconds_or_None)."""
    if not (svc.manage and alive):
        return None, None
    # 1) structured heartbeat the SERVICE writes itself (richest signal)
    if getattr(svc, "hb_json", None):
        try:
            from harness import heartbeat as _hbmod
            # check_pid=True: a heartbeat whose recorded process is gone is CRASHED immediately
            # (don't wait out the staleness window — narrows the PID-reuse/stale-file gap).
            hbr = _hbmod.read(path=svc.hb_json, max_age_seconds=svc.heartbeat_max_age, check_pid=True)
            return hbr, hbr.get("age_seconds")
        except Exception:
            # a service that DECLARES a structured heartbeat but whose heartbeat we cannot read
            # must NOT silently fall back to an mtime-only "healthy" — that would trust a file
            # timestamp without verifying fresh work. Unverifiable -> unknown.
            return {"state": _sm.UNKNOWN, "reason": "heartbeat_read_error", "stale": True}, None
    # 2) plain liveness file (mtime only) -> fresh/stale (ONLY for services with no hb_json)
    if svc.heartbeat_path:
        try:
            fresh, _d = services.heartbeat_fresh(svc.heartbeat_path, svc.heartbeat_max_age)
            age = None
            if os.path.exists(svc.heartbeat_path):
                age = time.time() - os.path.getmtime(svc.heartbeat_path)
            return ({"state": _sm.HEALTHY if fresh else _sm.STALE,
                     "reason": "heartbeat_ok" if fresh else "heartbeat_stale",
                     "stale": (not fresh)}, age)
        except Exception:
            pass
    return None, None


def _canonical(svc, row):
    """Map a status row to the canonical (state, reason, age_seconds, stale). ADDITIVE — never
    changes the existing OK/WARN/FAIL `status` or `alive` fields."""
    hb_canon, age = _service_hb_canon(svc, bool(row.get("alive")))
    state, reason = _sm.classify_service(
        managed=bool(row.get("managed")), enabled=bool(row.get("enabled")), exists=svc.exists,
        alive=row.get("alive"), supervisor_status=row.get("status"), heartbeat=hb_canon)
    stale = bool(hb_canon.get("stale")) if (hb_canon and "stale" in hb_canon) else (state == _sm.STALE)
    return state, reason, age, stale


def _collect_status(svcs):
    out = []
    for svc in svcs:
        if not svc.manage:                              # external (ollama)
            st, detail = svc.live_health(False)
            out.append((svc, {"managed": False, "enabled": svc.enabled, "pid": None,
                              "alive": None, "status": st, "detail": detail,
                              "required": svc.required}))
            continue
        if not svc.enabled or not svc.exists:
            out.append((svc, {"managed": True, "enabled": svc.enabled, "pid": None, "alive": False,
                              "status": "disabled", "detail": svc.note if svc.exists else "not installed",
                              "required": svc.required}))
            continue
        alive, pid = _service_alive(svc.name)
        hb = _read_heartbeat(svc.name)
        if not alive and (os.path.exists(_disabled_path(svc.name)) or hb.get("status") == "stopped"):
            st, detail = "stopped", "stopped by operator"      # intentionally down, not a crash
        else:
            st, detail = svc.live_health(alive)
        out.append((svc, {"managed": True, "enabled": True, "pid": pid, "alive": alive,
                          "status": st, "detail": detail, "required": svc.required,
                          "restart_count": int(hb.get("restart_count", 0) or 0),
                          "started_at": hb.get("started_at"), "last_error": hb.get("last_error")}))
    # Plan 10: ADD the canonical truth layer (state/reason/age/stale/paper_only) to every row,
    # without touching the existing status/alive fields that callers/tests depend on.
    for svc, row in out:
        state, reason, age, stale = _canonical(svc, row)
        row["state"] = state
        row["reason"] = reason
        row["age_seconds"] = (round(age, 1) if isinstance(age, (int, float)) else None)
        row["stale"] = stale
        row["paper_only"] = True
    return out


def system_status(svcs=None) -> dict:
    """Plan 10: ONE canonical system status from the supervisor's view. Supervisor-being-alive is
    NOT counted as bot-healthy — a stale/crashed REQUIRED service drags the system off green."""
    all_svcs = svcs if svcs is not None else services.registry()
    rows = _collect_status(all_svcs)
    components = [{"name": svc.name, "kind": "service", "state": row["state"],
                  "critical": bool(svc.required and svc.manage)} for svc, row in rows]
    sys_st = _sm.system_status(components)
    sys_st["details"]["services"] = {svc.name: {"state": row["state"], "reason": row["reason"],
                                                 "alive": row.get("alive"), "stale": row.get("stale"),
                                                 "age_seconds": row.get("age_seconds"),
                                                 "required": row.get("required")}
                                     for svc, row in rows}
    spid = procman.read_pid(_pid_path("supervisor"))
    sys_st["details"]["supervisor_alive"] = bool(spid and procman.is_alive(spid))
    return sys_st


# ── spawn / startup ───────────────────────────────────────────────────────────--
def _port_busy_by_other(svc) -> bool:
    """A configured port is open but WE don't have a live pid for it -> someone else holds it."""
    if not svc.port:
        return False
    alive, _ = _service_alive(svc.name)
    if alive:
        return False
    ok, _ = services.tcp_ok("127.0.0.1", svc.port, timeout=1.0)
    return ok


def _spawn(svc, restart_count=0):
    _ensure_dirs()
    procman.clear_pid(_pid_path(svc.name))
    try:
        os.remove(_disabled_path(svc.name))
    except OSError:
        pass
    log = _log_path(svc.name)
    _rotate_log(log)
    pid = procman.spawn(svc.cmd, cwd=svc.cwd, log_path=log, env=svc.env)
    procman.write_pid(_pid_path(svc.name), pid)
    _write_heartbeat(svc.name, pid, _now(), restart_count, "starting", "spawned")
    return pid


def _wait_startup(svc, pid):
    """Return (ok, detail). HTTP/port services must answer; daemons need only stay alive
    a few seconds with no fatal error in their log (their real heartbeat takes minutes)."""
    deadline = time.monotonic() + svc.start_timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if not procman.is_alive(pid):
            fatal = _log_has_fatal(svc.name)
            return False, ("exited during startup" + (f": {fatal}" if fatal else " (see log)"))
        if svc.http_url:
            ok, d = services.http_ok(svc.http_url)
            if ok:
                return True, d
        elif svc.port:
            ok, d = services.tcp_ok("127.0.0.1", svc.port)
            if ok:
                return True, d
        else:                                    # heartbeat / process-only daemon
            if time.monotonic() - started >= 3.0:
                fatal = _log_has_fatal(svc.name)
                if fatal:
                    return False, f"fatal in log: {fatal}"
                return True, "process running"
        time.sleep(1.0)
    if procman.is_alive(pid):
        if not (svc.http_url or svc.port):
            return True, "process running (warming up)"
        return False, f"did not become healthy in {svc.start_timeout:.0f}s"
    return False, "exited during startup (see log)"


# ── public ops ──────────────────────────────────────────────────────────────────
def doctor(svcs=None) -> dict:
    """Readiness check before starting. Never starts anything."""
    svcs = svcs if svcs is not None else services.registry()
    rows = []
    # provider
    ok, d = services.ollama_check()
    rows.append(("ollama", "OK" if ok else "FAIL", d))
    # DB writable
    try:
        from harness import db_check
        res = db_check.run()
        rows.append(("database", "OK" if res["fail"] == 0 else "FAIL",
                     f"{res['ok']} ok / {res['warn']} warn / {res['fail']} fail"))
    except Exception as e:
        rows.append(("database", "WARN", f"db_check unavailable ({type(e).__name__})"))
    # each managed+enabled service: command present
    for svc in svcs:
        if not svc.manage or not svc.enabled:
            continue
        if not svc.exists:
            rows.append((svc.name, "WARN", "not installed — will be skipped"))
        elif svc.cmd and not (os.path.isabs(svc.cmd[0]) and not os.path.exists(svc.cmd[0])):
            rows.append((svc.name, "OK", "command resolved"))
        else:
            rows.append((svc.name, "WARN", f"command not found: {svc.cmd[:1]}"))
        if _port_busy_by_other(svc):
            rows.append((svc.name + ":port", "WARN", f"port {svc.port} already in use by another process"))
    n_fail = sum(1 for _, s, _ in rows if s == "FAIL")
    return {"rows": rows, "fail": n_fail}


def start(names=None, svcs=None, run_watcher=True, preflight=True) -> dict:
    _ensure_dirs()
    cfg = services.config()
    all_svcs = svcs if svcs is not None else services.registry(cfg)
    if preflight:
        ok, d = services.ollama_check()
        if not ok:
            print(f"[supervisor] FAIL ollama — {d}")
            print("[supervisor] aborting: the LLM provider must be reachable. Start Ollama, then retry.")
            return {"ok": False, "error": "ollama_unreachable"}
        print(f"[supervisor] ok    ollama — {d}")

    targets = [s for s in all_svcs if s.manage and s.enabled and s.exists]
    if names:
        targets = [s for s in targets if s.name in names]
    targets.sort(key=lambda s: s.order)

    started, skipped, failed = [], [], []
    for svc in targets:
        alive, pid = _service_alive(svc.name)
        if alive:
            print(f"[supervisor] ok    {svc.name} — already running (pid {pid})")
            skipped.append(svc.name)
            continue
        if _port_busy_by_other(svc):
            print(f"[supervisor] WARN  {svc.name} — port {svc.port} held by an unknown process; "
                  f"NOT starting (won't kill it). Free the port or change the port and retry.")
            failed.append((svc.name, "port busy"))
            if svc.required:
                _write_status(all_svcs)
                return {"ok": False, "error": f"{svc.name}_port_busy"}
            continue
        pid = _spawn(svc, restart_count=_restart_count(svc.name))
        ok, detail = _wait_startup(svc, pid)
        if ok:
            print(f"[supervisor] ok    {svc.name} — started (pid {pid}) {detail}")
            _write_heartbeat(svc.name, pid, _started_at(svc.name), _restart_count(svc.name), "OK", detail)
            started.append(svc.name)
        else:
            procman.terminate(pid)
            procman.clear_pid(_pid_path(svc.name))
            _write_heartbeat(svc.name, None, _started_at(svc.name), _restart_count(svc.name), "FAIL", detail)
            if svc.required:
                print(f"[supervisor] FAIL  {svc.name} — {detail}")
                print(f"[supervisor] aborting: required service '{svc.name}' did not start. "
                      f"See .runtime/logs/{svc.name}.log")
                _write_status(all_svcs)
                return {"ok": False, "error": f"{svc.name}_failed", "detail": detail}
            print(f"[supervisor] WARN  {svc.name} (optional) — {detail}; continuing")
            failed.append((svc.name, detail))

    if run_watcher and cfg["RESTART_CRASHED"]:
        _ensure_watcher()
    data = _write_status(all_svcs, supervisor_pid=procman.read_pid(_pid_path("supervisor")))
    print("\n[supervisor] READY — system started in the background. "
          "`python -m harness.supervisor status` to inspect.")
    return {"ok": True, "started": started, "skipped": skipped, "failed": failed, "status": data}


def stop(names=None, svcs=None) -> dict:
    _ensure_dirs()
    all_svcs = svcs if svcs is not None else services.registry()
    managed = [s for s in all_svcs if s.manage]
    if names:                                     # single-service stop -> don't touch the watcher
        for svc in [s for s in managed if s.name in names]:
            _stop_one(svc, mark_disabled=True)
        _write_status(all_svcs)
        return {"ok": True, "stopped": list(names)}

    # full stop: tell the watcher to exit, then stop every managed service in reverse order
    try:
        open(STOP_FLAG, "w").close()
    except Exception:
        pass
    spid = procman.read_pid(_pid_path("supervisor"))
    if spid and procman.is_alive(spid):
        procman.terminate(spid)
    procman.clear_pid(_pid_path("supervisor"))

    stopped = []
    for svc in sorted([s for s in managed if s.enabled], key=lambda s: s.order, reverse=True):
        if _stop_one(svc, mark_disabled=False):
            stopped.append(svc.name)
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass
    _write_status(all_svcs)
    print(f"[supervisor] stopped {len(stopped)} service(s): {', '.join(stopped) or '(none running)'}")
    return {"ok": True, "stopped": stopped}


def _stop_one(svc, mark_disabled) -> bool:
    alive, pid = _service_alive(svc.name)
    if alive:
        procman.terminate(pid)
        print(f"[supervisor] stopped {svc.name} (pid {pid})")
    procman.clear_pid(_pid_path(svc.name))
    if mark_disabled:
        try:
            _ensure_dirs()
            open(_disabled_path(svc.name), "w").close()
        except Exception:
            pass
    hb = _read_heartbeat(svc.name)
    _write_heartbeat(svc.name, None, hb.get("started_at"), int(hb.get("restart_count", 0) or 0),
                     "stopped", "stopped by operator")
    return alive


def restart(names=None, svcs=None):
    stop(names=names, svcs=svcs)
    time.sleep(1.0)
    return start(names=names, svcs=svcs)


def status(svcs=None, as_dict=False):
    all_svcs = svcs if svcs is not None else services.registry()
    rows = _collect_status(all_svcs)
    if as_dict:
        sysv = _sm.system_status([{"name": s.name, "kind": "service", "state": r["state"],
                                   "critical": bool(s.required and s.manage)} for s, r in rows])
        spid = procman.read_pid(_pid_path("supervisor"))
        return {"updated_at": _now(), "paper_only": True,
                "system_state": sysv["state"], "system_ok": sysv["ok"],
                "supervisor_alive": bool(spid and procman.is_alive(spid)),
                "services": {s.name: r for s, r in rows}}
    print("\nSYSTEM STATUS")
    print(f"{'STATE':<11}{'SERVICE':<20}{'PID':<10}EXPECTED  AGE      DETAIL / REASON")
    print("-" * 92)
    for svc, r in rows:
        pid = f"pid {r['pid']}" if r.get("pid") else "-"
        expected = ("required" if svc.required else "optional") if svc.manage else "external"
        age = (f"{r['age_seconds']:.0f}s" if r.get("age_seconds") is not None else "-")
        rc = f" · restarts {r['restart_count']}" if r.get("restart_count") else ""
        err = f"  !{r['last_error']}" if r.get("last_error") else ""
        print(f"{r['state']:<11}{svc.name:<20}{pid:<10}{expected:<10}{age:<9}{r['detail']}{rc}{err}")
    spid = procman.read_pid(_pid_path("supervisor"))
    watch = f"watcher pid {spid} (auto-restart on)" if (spid and procman.is_alive(spid)) else "watcher not running"
    sysv = _sm.system_status([{"name": s.name, "kind": "service", "state": r["state"],
                               "critical": bool(s.required and s.manage)} for s, r in rows])
    print("-" * 92)
    print(f"supervisor: {watch}  ·  SYSTEM = {sysv['state'].upper()} "
          f"(supervisor-alive != bot-healthy)  ·  PAPER-ONLY")
    return None


def logs(n=25, svcs=None):
    all_svcs = svcs if svcs is not None else services.registry()
    for svc in [s for s in all_svcs if s.manage]:
        tail = _log_tail(svc.name, n)
        if not tail:
            continue
        print(f"\n===== {svc.name}  (.runtime/logs/{svc.name}.log) =====")
        print("\n".join(tail))


def tail(name, follow=True):
    path = _log_path(name)
    if not os.path.exists(path):
        print(f"[supervisor] no log for '{name}' at {path}")
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        print("\n".join(f.read().splitlines()[-40:]))
        if not follow:
            return
        try:
            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


# ── watcher daemon (auto-restart) ─────────────────────────────────────────────--
def _ensure_watcher():
    spid = procman.read_pid(_pid_path("supervisor"))
    if spid and procman.is_alive(spid):
        return spid
    try:
        os.remove(STOP_FLAG)
    except OSError:
        pass
    pid = procman.spawn([services.POLY_PY, "-m", "harness.supervisor", "_watch"],
                        cwd=services.PKG_ROOT, log_path=_log_path("supervisor"),
                        env={"PYTHONUTF8": "1"})
    procman.write_pid(_pid_path("supervisor"), pid)
    return pid


def _watch():
    """Detached watcher loop: restart crashed REQUIRED services within a capped window."""
    cfg = services.config()
    restarts: dict[str, list] = {}
    while True:
        if os.path.exists(STOP_FLAG):
            break
        all_svcs = services.registry(cfg)
        for svc in sorted([s for s in all_svcs if s.manage and s.enabled and s.exists], key=lambda s: s.order):
            if os.path.exists(_disabled_path(svc.name)):
                continue
            alive, pid = _service_alive(svc.name)
            if alive:
                st, detail = svc.live_health(True)
                _write_heartbeat(svc.name, pid, _started_at(svc.name), _restart_count(svc.name), st, detail)
                continue
            if not (cfg["RESTART_CRASHED"] and svc.restart and svc.required):
                continue
            now = time.monotonic()
            hist = [t for t in restarts.get(svc.name, []) if now - t < cfg["RESTART_WINDOW"]]
            if len(hist) >= cfg["MAX_RESTARTS"]:
                _write_heartbeat(svc.name, None, _started_at(svc.name), len(hist),
                                 "FAIL", f"crash-looping: {len(hist)} restarts in {cfg['RESTART_WINDOW']}s — giving up")
                continue
            hist.append(now)
            restarts[svc.name] = hist
            npid = _spawn(svc, restart_count=len(hist))
            _write_heartbeat(svc.name, npid, _now(), len(hist), "restarted",
                             f"auto-restarted (crash #{len(hist)})")
        _write_status(services.registry(cfg), supervisor_pid=os.getpid())
        time.sleep(5.0)
    # stop everything on the way out
    for svc in sorted([s for s in services.registry(cfg) if s.manage and s.enabled], key=lambda s: s.order, reverse=True):
        _stop_one(svc, mark_disabled=False)


# ── CLI ──────────────────────────────────────────────────────────────────────--
def main(argv=None):
    import argparse
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="harness.supervisor",
                                description="One-command background orchestrator (PAPER-ONLY).")
    p.add_argument("command", choices=["start", "stop", "restart", "status", "logs",
                                        "tail", "doctor", "_watch"])
    p.add_argument("service", nargs="?", default=None, help="optional single service name")
    p.add_argument("-n", type=int, default=25, help="lines for logs")
    args = p.parse_args(argv)

    if args.command == "_watch":
        return _watch()
    if args.command == "doctor":
        d = doctor()
        print("\nharness.supervisor doctor — readiness")
        print("-" * 60)
        for name, st, detail in d["rows"]:
            print(f"[{st:<4}] {name:<18} {detail}")
        print("-" * 60)
        print(f"{'READY' if d['fail'] == 0 else 'NOT READY'} — {d['fail']} blocking failure(s)")
        return 1 if d["fail"] else 0
    if args.command == "start":
        r = start(names=[args.service] if args.service else None)
        return 0 if r.get("ok") else 1
    if args.command == "stop":
        stop(names=[args.service] if args.service else None)
        return 0
    if args.command == "restart":
        r = restart(names=[args.service] if args.service else None)
        return 0 if r.get("ok") else 1
    if args.command == "status":
        status()
        return 0
    if args.command == "logs":
        logs(n=args.n)
        return 0
    if args.command == "tail":
        if not args.service:
            print("usage: supervisor tail <service>")
            return 2
        tail(args.service)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
