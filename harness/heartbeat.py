"""harness/heartbeat.py — Plan 10: structured service heartbeats + an honest reader.

A long-running service WRITES a JSON heartbeat each loop tick (atomically). Any consumer
(supervisor, dashboard) READS it through :func:`read`, which returns a canonical
:mod:`harness.status_model` status that can never be fooled into green by a stale, malformed,
future-dated, or PID-dead heartbeat. A process existing is not "healthy" — fresh work is.

Heartbeat JSON fields: service · pid · started_at · last_tick_at · stage · market_id ·
market_question · last_decision_id · last_decision_at · last_error · paper_only · branch ·
commit · config_flags · version · loop_count · generated_at.

Best-effort: a heartbeat WRITE never crashes the service; a READ never crashes the dashboard.
PAPER-ONLY. No network, no DB.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from harness import status_model as _sm

# last_tick_at is written on the SAME machine/clock and FLOORED to the second, so a genuine tick
# read AFTER it was written always has age >= 0. Therefore ANY future-dated tick (age < 0) is
# anomalous (clock-backward step or a spoofed timestamp) and must never read healthy. Tolerance 0.
FUTURE_SKEW_SECONDS = 0.0
DEFAULT_MAX_AGE = 900.0


# ── paths ─────────────────────────────────────────────────────────────────────--
def _runtime_dir() -> str:
    base = os.getenv("SUPERVISOR_RUNTIME_DIR")
    if base:
        return base
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_root, ".runtime")


def default_path(service: str) -> str:
    return os.path.join(_runtime_dir(), "heartbeats", f"{service}.json")


def _resolve(service: str | None, path: str | None) -> str:
    if path:
        return path
    if service:
        return default_path(service)
    raise ValueError("heartbeat: need a service name or an explicit path")


def _now_iso(t: float) -> str:
    return _sm.now_iso(t)


def _read_raw(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (json.JSONDecodeError, ValueError):
        return None, "malformed"
    except Exception as e:
        return None, f"error:{type(e).__name__}"


# ── write (atomic, best-effort) ──────────────────────────────────────────────────
def write(service: str, *, path: str | None = None, stage: str | None = None,
          market_id=None, market_question=None, last_decision_id=None, last_decision_at=None,
          last_error=None, config_flags: dict | None = None, loop_count: int | None = None,
          paper_only: bool = True, extra: dict | None = None, now: float | None = None) -> bool:
    """Atomically write a structured heartbeat. Preserves ``started_at`` and auto-increments
    ``loop_count`` from any existing file. NEVER raises (returns False on failure)."""
    t = time.time() if now is None else now
    p = _resolve(service, path)
    prev, _ = _read_raw(p)
    prev = prev if isinstance(prev, dict) else {}
    started_at = prev.get("started_at") or _now_iso(t)
    if loop_count is None:
        try:
            loop_count = int(prev.get("loop_count", 0) or 0) + 1
        except (TypeError, ValueError):
            loop_count = 1
    ver = _sm.version_info()
    hb = {
        "service": service,
        "pid": os.getpid(),
        "started_at": started_at,
        "last_tick_at": _now_iso(t),
        "stage": stage,
        "market_id": market_id,
        "market_question": (market_question[:160] if isinstance(market_question, str) else market_question),
        "last_decision_id": last_decision_id,
        "last_decision_at": last_decision_at,
        "last_error": (str(last_error)[:300] if last_error else None),
        "paper_only": bool(paper_only),
        "branch": ver.get("git_branch"),
        "commit": ver.get("git_commit"),
        "config_flags": config_flags or {},
        "version": ver.get("code_version"),
        "loop_count": loop_count,
        "generated_at": _now_iso(t),
    }
    if extra:
        hb.update(extra)
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p) or ".", prefix=".hb_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(hb, f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, p)            # atomic
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True
    except Exception:
        return False


# ── read -> canonical status (honest) ────────────────────────────────────────────
def read(service: str | None = None, *, path: str | None = None, now: float | None = None,
         max_age_seconds: float = DEFAULT_MAX_AGE, check_pid: bool = True) -> dict:
    """Read a heartbeat and return a canonical status. Handles missing / malformed / stale /
    future-dated / PID-dead heartbeats — and a recorded ``last_error`` — never crashing and
    never returning green for any of them.

    Reasons: heartbeat_ok · heartbeat_missing · heartbeat_malformed · heartbeat_stale ·
    heartbeat_future_timestamp · heartbeat_pid_not_running · heartbeat_service_error."""
    t = time.time() if now is None else now
    p = _resolve(service, path)
    raw, err = _read_raw(p)

    if err == "missing":
        return _sm.status(_sm.NOT_STARTED, reason="heartbeat_missing", service=service,
                          max_age_seconds=max_age_seconds, generated_at=_now_iso(t), stale=True,
                          details={"path": p})
    if err == "malformed":
        return _sm.status(_sm.DEGRADED, reason="heartbeat_malformed", service=service,
                          max_age_seconds=max_age_seconds, generated_at=_now_iso(t), stale=True,
                          details={"path": p})
    if raw is None or not isinstance(raw, dict):
        return _sm.status(_sm.UNKNOWN, reason="heartbeat_service_error", service=service,
                          max_age_seconds=max_age_seconds, generated_at=_now_iso(t), stale=True,
                          details={"path": p, "error": err})

    svc = raw.get("service") or service
    # A heartbeat is fresh only if ALL its timestamps are fresh. Use the OLDEST stamp for the age
    # (so a fresh last_tick_at can't mask a stale generated_at) and the NEWEST for the future
    # check (so a future-dated stamp on either field is caught).
    _stamps = [x for x in (_parse_ts(raw.get("last_tick_at")), _parse_ts(raw.get("generated_at")))
               if x is not None]
    age = (t - min(_stamps)) if _stamps else None
    future_age = (t - max(_stamps)) if _stamps else None
    common = dict(service=svc, max_age_seconds=max_age_seconds, generated_at=_now_iso(t),
                  age_seconds=age, git_branch=raw.get("branch"), git_commit=raw.get("commit"),
                  version=raw.get("version"), paper_only=bool(raw.get("paper_only", True)),
                  details={"path": p, "pid": raw.get("pid"), "stage": raw.get("stage"),
                           "market_id": raw.get("market_id"), "loop_count": raw.get("loop_count"),
                           "started_at": raw.get("started_at"),
                           "last_decision_id": raw.get("last_decision_id"),
                           "last_decision_at": raw.get("last_decision_at"),
                           "last_error": raw.get("last_error"),
                           "config_flags": raw.get("config_flags")})

    # the structured contract requires BOTH last_tick_at AND generated_at; a heartbeat missing
    # either is malformed and must not read healthy (closes the "omit generated_at" spoof).
    if len(_stamps) < 2:
        return _sm.status(_sm.DEGRADED, reason="heartbeat_malformed", stale=True, **common)
    if future_age < -FUTURE_SKEW_SECONDS:
        return _sm.status(_sm.UNKNOWN, reason="heartbeat_future_timestamp", stale=True, **common)
    # PID liveness: a heartbeat whose process is gone (or whose PID we cannot verify) is NOT
    # fresh. FAIL CLOSED — a MISSING pid means we cannot prove the daemon is alive, so it must
    # never read healthy (a stripped-down heartbeat JSON cannot fake green).
    pid = raw.get("pid")
    if check_pid:
        if pid is None:
            return _sm.status(_sm.UNKNOWN, reason="heartbeat_pid_missing", stale=True, **common)
        try:
            from harness import procman
            if not procman.is_alive(pid):
                return _sm.status(_sm.CRASHED, reason="heartbeat_pid_not_running", stale=True, **common)
        except Exception:
            return _sm.status(_sm.UNKNOWN, reason="heartbeat_pid_check_unavailable", stale=True, **common)
    if age > max_age_seconds:
        return _sm.status(_sm.STALE, reason="heartbeat_stale", stale=True, **common)
    if raw.get("last_error"):
        return _sm.status(_sm.DEGRADED, reason="heartbeat_service_error", stale=False, **common)
    # Plan 10: a heartbeat written by a DIFFERENT code commit than the current tree means the
    # daemon is running stale code — do not show fully green (PHASE 9 runtime_commit_mismatch).
    if _sm.commit_mismatch(raw.get("commit")):
        return _sm.status(_sm.DEGRADED, reason="heartbeat_commit_mismatch", stale=False, **common)
    return _sm.status(_sm.HEALTHY, reason="heartbeat_ok", stale=False, **common)


def _parse_ts(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        from datetime import datetime, timezone
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None
