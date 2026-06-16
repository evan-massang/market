"""harness/services.py — the service registry (single source of truth for WHAT runs).

Declares every background service with its real command, cwd, ports, health check,
required/optional flag, startup order and dependencies. The enabled set is built
from the .env RUN_* flags (Phase 10). PAPER-ONLY — no service here trades real money.

The supervisor (harness.supervisor) consumes this; it owns process management.
"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field

# ── paths ─────────────────────────────────────────────────────────────────────
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/polyswarm
PROJECT_ROOT = os.path.dirname(PKG_ROOT)                                  # …/Polymarket
# Runtime state dir. Overridable (tests point it at a temp dir so they never touch the
# real .runtime). Read at import — set SUPERVISOR_RUNTIME_DIR before importing.
RUNTIME = os.getenv("SUPERVISOR_RUNTIME_DIR") or os.path.join(PKG_ROOT, ".runtime")
PIDS = os.path.join(RUNTIME, "pids")
LOGS = os.path.join(RUNTIME, "logs")
HEARTBEATS = os.path.join(RUNTIME, "heartbeats")
STATUS_JSON = os.path.join(RUNTIME, "status.json")
STOP_FLAG = os.path.join(RUNTIME, "stop.flag")

_WIN = os.name == "nt"


def _venv_py(root: str) -> str:
    """The venv interpreter under ``root`` if present, else the current interpreter."""
    import sys
    cand = os.path.join(root, ".venv", "Scripts" if _WIN else "bin", "python.exe" if _WIN else "python")
    return cand if os.path.exists(cand) else sys.executable


POLY_PY = _venv_py(PKG_ROOT)
MIROFISH_DIR = os.path.join(PROJECT_ROOT, "MiroFish")
MF_BACKEND_DIR = os.path.join(MIROFISH_DIR, "backend")
MF_PY = _venv_py(MF_BACKEND_DIR)
MF_FRONTEND_DIR = os.path.join(MIROFISH_DIR, "frontend")


# ── env config (Phase 10) ──────────────────────────────────────────────────────
def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def config() -> dict:
    return {
        "RUN_MIROFISH": _flag("RUN_MIROFISH", True),
        "RUN_MIROFISH_FRONTEND": _flag("RUN_MIROFISH_FRONTEND", False),
        "RUN_DASHBOARD": _flag("RUN_DASHBOARD", True),
        "RUN_SAMEDAY_DAEMON": _flag("RUN_SAMEDAY_DAEMON", True),
        "RUN_AI_PIPELINE": _flag("RUN_AI_PIPELINE", True),
        "DASHBOARD_PORT": int(os.getenv("DASHBOARD_PORT", os.getenv("DASH_PORT", "8800"))),
        "MIROFISH_BACKEND_PORT": int(os.getenv("MIROFISH_BACKEND_PORT", "5001")),
        "RESTART_CRASHED": _flag("SUPERVISOR_RESTART_CRASHED", True),
        "MAX_RESTARTS": int(os.getenv("SUPERVISOR_MAX_RESTARTS", "5")),
        "RESTART_WINDOW": int(os.getenv("SUPERVISOR_RESTART_WINDOW_SECONDS", "600")),
    }


# ── health-check primitives ─────────────────────────────────────────────────────
def http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout)
        return (r.status_code < 400, f"HTTP {r.status_code} {url}")
    except Exception as e:
        return (False, f"{url} unreachable ({type(e).__name__})")


def tcp_ok(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (True, f"tcp {host}:{port} open")
    except Exception as e:
        return (False, f"tcp {host}:{port} closed ({type(e).__name__})")


def heartbeat_fresh(path: str, max_age: float) -> tuple[bool, str]:
    try:
        if not os.path.exists(path):
            return (False, f"no heartbeat yet ({os.path.basename(path)})")
        age = time.time() - os.path.getmtime(path)
        return (age <= max_age, f"heartbeat {age:.0f}s ago")
    except Exception as e:
        return (False, f"heartbeat check error ({type(e).__name__})")


def ollama_check() -> tuple[bool, str]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        import httpx
        r = httpx.get(base + "/api/tags", timeout=4.0)
        if r.status_code >= 400:
            return (False, f"ollama HTTP {r.status_code}")
        models = [m.get("name", "") for m in (r.json().get("models") or [])]
        want = os.getenv("MODEL_FAST", "qwen2.5:3b")
        present = any(want.split(":")[0] in m for m in models)
        return (present, f"{len(models)} models, '{want}' {'present' if present else 'MISSING'}")
    except Exception as e:
        return (False, f"ollama unreachable ({type(e).__name__})")


# ── service model ───────────────────────────────────────────────────────────────
@dataclass
class Service:
    name: str
    cmd: list
    cwd: str
    enabled: bool
    required: bool          # required failing -> startup aborts; optional -> warn + continue
    manage: bool            # False = external, check-only (never started/stopped)
    order: int              # startup order (lower first)
    depends_on: list = field(default_factory=list)
    http_url: str | None = None      # startup waits for HTTP 2xx; live status uses it
    port: int | None = None          # else startup waits for TCP; live status uses it
    heartbeat_path: str | None = None
    heartbeat_max_age: float = 600.0
    start_timeout: float = 30.0      # seconds to become healthy after spawn
    restart: bool = True             # eligible for watcher auto-restart
    env: dict = field(default_factory=dict)
    exists: bool = True              # the command/executable exists on this machine
    note: str = ""

    def live_health(self, alive: bool) -> tuple[str, str]:
        """Ongoing status -> ('OK'|'WARN'|'FAIL', detail). Used by `status`."""
        if not self.manage:   # external (ollama)
            ok, detail = (ollama_check() if self.name == "ollama" else http_ok(self.http_url or ""))
            return ("OK" if ok else "FAIL", detail) if self.required else ("OK" if ok else "WARN", detail)
        if not alive:
            # It may be running but NOT under this supervisor (e.g. an old manual
            # launch on the same port). Report that truthfully rather than a flat FAIL.
            if self.http_url:
                ok, d = http_ok(self.http_url)
                if ok:
                    return ("WARN", "running but not supervisor-managed, " + d)
            elif self.port:
                ok, d = tcp_ok("127.0.0.1", self.port)
                if ok:
                    return ("WARN", "running but not supervisor-managed, " + d)
            return ("FAIL", "process not running")
        if self.http_url:
            ok, detail = http_ok(self.http_url)
            return ("OK", detail) if ok else ("WARN", "process up, " + detail)
        if self.heartbeat_path:
            ok, detail = heartbeat_fresh(self.heartbeat_path, self.heartbeat_max_age)
            return ("OK", "running, " + detail) if ok else ("WARN", "running (warming up), " + detail)
        if self.port:
            ok, detail = tcp_ok("127.0.0.1", self.port)
            return ("OK", detail) if ok else ("WARN", "process up, " + detail)
        return ("OK", "process running")


# ── registry ────────────────────────────────────────────────────────────────────
def registry(cfg: dict | None = None) -> list[Service]:
    cfg = cfg or config()
    dash_port = cfg["DASHBOARD_PORT"]
    mf_port = cfg["MIROFISH_BACKEND_PORT"]
    common_env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    svcs = [
        Service(
            name="ollama", cmd=[], cwd=PKG_ROOT, enabled=True, required=True, manage=False,
            order=0, http_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            note="external provider — checked, never started by the supervisor"),
        Service(
            name="mirofish_backend",
            cmd=[MF_PY, "run.py"], cwd=MF_BACKEND_DIR,
            enabled=cfg["RUN_MIROFISH"], required=False, manage=True, order=1,
            port=mf_port, http_url=f"http://localhost:{mf_port}/health",
            start_timeout=40.0, exists=os.path.isdir(MF_BACKEND_DIR),
            note="MiroFish crowd-sim Flask; ai_pipeline uses it but degrades if down"),
        Service(
            name="mirofish_frontend",
            cmd=(["npm.cmd"] if _WIN else ["npm"]) + ["run", "dev"], cwd=MF_FRONTEND_DIR,
            enabled=cfg["RUN_MIROFISH_FRONTEND"], required=False, manage=True, order=2,
            port=3000, start_timeout=40.0, exists=os.path.isdir(MF_FRONTEND_DIR),
            note="optional MiroFish web UI (Vite :3000)"),
        Service(
            name="dashboard",
            cmd=[POLY_PY, "-m", "harness.dashboard"], cwd=PKG_ROOT,
            enabled=cfg["RUN_DASHBOARD"], required=False, manage=True, order=3,
            port=dash_port, http_url=f"http://localhost:{dash_port}/health",
            start_timeout=30.0, env={**common_env, "DASH_STREAM_LOG": "ai_night.log"},
            note="live monitor (http://localhost:8800)"),
        Service(
            name="sameday_daemon",
            cmd=[POLY_PY, "-u", "-m", "harness.sameday", "daemon"], cwd=PKG_ROOT,
            enabled=cfg["RUN_SAMEDAY_DAEMON"], required=True, manage=True, order=4,
            heartbeat_path=os.path.join(PKG_ROOT, "sameday_live.log"), heartbeat_max_age=600.0,
            start_timeout=20.0, env=common_env,
            note="same-day favorite-longshot + AI scout daemon"),
        Service(
            name="ai_pipeline",
            cmd=[POLY_PY, "-u", "-m", "harness.predict_today", "daemon", "--with-mirofish",
                 "--size", "5", "--rounds", "1", "--min-edge", "0.03", "--interval", "30", "--mf-wait", "360"],
            cwd=PKG_ROOT, enabled=cfg["RUN_AI_PIPELINE"], required=True, manage=True, order=5,
            depends_on=["mirofish_backend"] if cfg["RUN_MIROFISH"] else [],
            heartbeat_path=os.path.join(PKG_ROOT, ".heartbeat.json"), heartbeat_max_age=900.0,
            start_timeout=20.0, env={**common_env, "DASH_STREAM_LOG": "ai_night.log"},
            note="precise FIND->GATHER->MiroFish->LLM->BET pipeline"),
    ]
    return svcs


def enabled_managed(cfg: dict | None = None) -> list[Service]:
    """Managed + enabled services in startup order (excludes external + disabled)."""
    return [s for s in registry(cfg) if s.enabled and s.manage and s.exists]


def by_name(name: str, cfg: dict | None = None):
    for s in registry(cfg):
        if s.name == name:
            return s
    return None
