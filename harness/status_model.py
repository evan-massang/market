"""harness/status_model.py — Plan 10: ONE canonical health/status truth model.

Every truth surface (dashboard cards, /api endpoints, supervisor status, heartbeat reader)
maps onto these states so the system can never show fake green. The cardinal rule: a process
existing is NOT "healthy"; only a service doing fresh verified work is. UNKNOWN is never green.

Service states:  disabled · not_configured · not_started · starting · healthy · degraded ·
                 stale · crashed · unknown
System states:   healthy · degraded · stale · unsafe · unknown

Canonical status dict (every surface returns this shape):
    {ok, state, reason, service, paper_only, generated_at, age_seconds, max_age_seconds,
     stale, version, git_branch, git_commit, details}

Pure / read-only (only os + git via obs.codeversion, all guarded). No network, no DB writes.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

# ── service states ──────────────────────────────────────────────────────────────
DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"
NOT_STARTED = "not_started"
STARTING = "starting"
HEALTHY = "healthy"
DEGRADED = "degraded"
STALE = "stale"
CRASHED = "crashed"
UNKNOWN = "unknown"

# ── system states ────────────────────────────────────────────────────────────────
SYS_HEALTHY = "healthy"
SYS_DEGRADED = "degraded"
SYS_STALE = "stale"
SYS_UNSAFE = "unsafe"
SYS_UNKNOWN = "unknown"

# ONLY these are "green". Everything else (incl. unknown) must NOT render green.
_GREEN = {HEALTHY, SYS_HEALTHY}
# benign service states — intentionally-down, NOT a fault, must NOT show red
_BENIGN = {DISABLED, NOT_CONFIGURED}

DEFAULT_MAX_AGE = 900.0

# system-severity ranking (worst wins)
_SYS_RANK = {SYS_HEALTHY: 0, SYS_UNKNOWN: 1, SYS_DEGRADED: 2, SYS_STALE: 3, SYS_UNSAFE: 4}


def now_iso(now: float | None = None) -> str:
    t = now if now is not None else time.time()
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def is_green(state) -> bool:
    """A state is green ONLY if it is the single HEALTHY state. unknown/stale/degraded/... are not."""
    return state in _GREEN


def status(state, *, reason: str = "", service=None, paper_only: bool = True,
           generated_at: str | None = None, age_seconds: float | None = None,
           max_age_seconds: float = DEFAULT_MAX_AGE, stale: bool | None = None,
           version=None, git_branch=None, git_commit=None, details: dict | None = None) -> dict:
    """Build the canonical status dict. ``ok`` is True ONLY for the green HEALTHY state."""
    if stale is None:
        stale = bool(age_seconds is not None and age_seconds > max_age_seconds)
    return {
        "ok": is_green(state),
        "state": state,
        "reason": reason or state,
        "service": service,
        "paper_only": bool(paper_only),
        "generated_at": generated_at or now_iso(),
        "age_seconds": (round(age_seconds, 3) if isinstance(age_seconds, (int, float)) else None),
        "max_age_seconds": float(max_age_seconds),
        "stale": bool(stale),
        "version": version,
        "git_branch": git_branch,
        "git_commit": git_commit,
        "details": details or {},
    }


# ── version / branch / commit (guarded; None on no-git, never raises) ────────────
_VERSION_CACHE = {}


def version_info(use_cache: bool = True) -> dict:
    """{git_branch, git_commit, git_dirty, code_version}. All None if git is unavailable."""
    if use_cache and _VERSION_CACHE:
        return dict(_VERSION_CACHE)
    out = {"git_branch": None, "git_commit": None, "git_dirty": None, "code_version": None}
    try:
        from harness.obs import codeversion as _cv
        rep = _cv.reproducibility()
        root = _cv._repo_root()
        branch = _cv._git(root, "rev-parse", "--abbrev-ref", "HEAD")
        out = {
            "git_branch": (branch.strip() if branch else None),
            "git_commit": rep.get("git_sha"),
            "git_dirty": rep.get("git_dirty"),
            "code_version": rep.get("code_version"),
        }
    except Exception:
        pass
    # cache ONLY a successful git read — never poison the cache with a transient None (which
    # would hide a later commit mismatch); retry on the next call until git is reachable.
    if use_cache and out.get("git_commit"):
        _VERSION_CACHE.clear()
        _VERSION_CACHE.update(out)
    return dict(out)


def _parse_ts(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def read_runtime_json(path: str, *, now: float | None = None, max_age_seconds: float = DEFAULT_MAX_AGE,
                      future_skew: float = 0.0) -> dict:
    """Read a generated runtime/cache JSON and classify FRESHNESS honestly. A cache without a
    verifiable ``generated_at`` (or older than max_age, or future-dated) is never green.

    Reasons: runtime_cache_ok · runtime_cache_missing · runtime_cache_malformed ·
    runtime_cache_stale · runtime_cache_future_timestamp."""
    import json
    import os
    t = now if now is not None else time.time()
    if not os.path.exists(path):
        return status(STALE, reason="runtime_cache_missing", stale=True,
                      max_age_seconds=max_age_seconds, generated_at=now_iso(t), details={"path": path})
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return status(DEGRADED, reason="runtime_cache_malformed", stale=True,
                      max_age_seconds=max_age_seconds, generated_at=now_iso(t), details={"path": path})
    gen = data.get("generated_at") if isinstance(data, dict) else None
    ts = _parse_ts(gen)
    if ts is None:                                  # no embedded generated_at -> cannot verify freshness
        return status(DEGRADED, reason="runtime_cache_malformed", stale=True,
                      max_age_seconds=max_age_seconds, generated_at=now_iso(t),
                      details={"path": path, "note": "no generated_at"})
    age = t - ts
    common = dict(max_age_seconds=max_age_seconds, generated_at=now_iso(t), age_seconds=age,
                  details={"path": path, "data_generated_at": gen})
    if age < -future_skew:
        return status(UNKNOWN, reason="runtime_cache_future_timestamp", stale=True, **common)
    if age > max_age_seconds:
        return status(STALE, reason="runtime_cache_stale", stale=True, **common)
    return status(HEALTHY, reason="runtime_cache_ok", stale=False, **common)


def commit_mismatch(recorded_commit, current_commit=None) -> bool:
    """True if a runtime/heartbeat record was written by a DIFFERENT commit than the current
    tree (i.e. the data may be from an old code version). Unknown commits -> False (cannot tell)."""
    cur = current_commit if current_commit is not None else version_info().get("git_commit")
    if not recorded_commit or not cur:
        return False
    return str(recorded_commit) != str(cur)


# ── service classification ───────────────────────────────────────────────────────
def classify_service(*, managed: bool, enabled: bool, exists: bool, alive: bool | None,
                     supervisor_status: str | None = None, heartbeat: dict | None = None) -> tuple[str, str]:
    """Map a supervisor service row (+ optional canonical heartbeat read) to (state, reason).

    Rules: a process existing is not healthy. Alive + fresh heartbeat -> healthy; alive + stale
    heartbeat -> stale; alive + missing heartbeat -> starting; not alive -> crashed; intentionally
    down -> disabled; not installed -> not_configured."""
    if not exists:
        return NOT_CONFIGURED, "service_not_installed"
    if not enabled:
        return DISABLED, "service_disabled"
    if not managed:                                   # external (e.g. ollama) — liveness only
        m = {"OK": HEALTHY, "WARN": DEGRADED, "FAIL": CRASHED}.get(supervisor_status, UNKNOWN)
        return m, f"external_{(supervisor_status or 'unknown').lower()}"
    if (supervisor_status or "") == "stopped":
        return DISABLED, "stopped_by_operator"
    if not alive:
        return CRASHED, "process_not_running"
    # alive: a service that declares a heartbeat must PROVE fresh work
    if heartbeat is not None:
        hbs = heartbeat.get("state")
        if hbs == HEALTHY:
            return HEALTHY, heartbeat.get("reason", "heartbeat_ok")
        if hbs == STALE:
            return STALE, heartbeat.get("reason", "heartbeat_stale")
        if hbs in (DEGRADED, UNKNOWN, CRASHED):
            return (hbs if hbs != CRASHED else DEGRADED), heartbeat.get("reason", "")
        # no heartbeat yet, but the process is up -> still starting/warming, not healthy
        return STARTING, heartbeat.get("reason", "heartbeat_missing")
    # alive, no heartbeat model declared -> fall back to the supervisor liveness verdict
    m = {"OK": HEALTHY, "WARN": DEGRADED, "FAIL": CRASHED}.get(supervisor_status, HEALTHY)
    return m, f"process_{(supervisor_status or 'ok').lower()}"


# ── system aggregation ───────────────────────────────────────────────────────────
def _component_severity(kind: str, state: str, critical: bool) -> str:
    """Map one component's (kind, state) to a SYSTEM-level severity."""
    kind = (kind or "service").lower()
    if kind == "db":
        return SYS_UNSAFE if state in ("error", "unavailable", CRASHED, "missing") else SYS_HEALTHY
    if kind == "accounting":
        if state in ("drift", "error", SYS_UNSAFE):
            return SYS_UNSAFE
        if state in ("degraded", "unknown"):
            return SYS_DEGRADED
        return SYS_HEALTHY
    if kind == "cache":
        if state in (STALE,):
            return SYS_STALE
        if state in (DEGRADED, UNKNOWN, "malformed", "missing"):
            return SYS_DEGRADED
        return SYS_HEALTHY
    if kind == "gate":
        # Gate 2 readiness is NOT a system-health fault (it gates real money, not liveness).
        # Reported, but never drags system health to red.
        return SYS_HEALTHY
    # service
    if state in _BENIGN:
        return SYS_HEALTHY
    if state == HEALTHY:
        return SYS_HEALTHY
    if state == STALE:
        return SYS_STALE
    if state == CRASHED:
        return SYS_UNSAFE if critical else SYS_DEGRADED
    if state in (DEGRADED,):
        return SYS_DEGRADED
    if state in (STARTING, NOT_STARTED, UNKNOWN):
        return SYS_UNKNOWN if not critical else SYS_DEGRADED
    return SYS_UNKNOWN


def system_status(components: list[dict], *, generated_at: str | None = None,
                  paper_only: bool = True, version: dict | None = None) -> dict:
    """Aggregate component statuses into ONE system status. Worst severity wins; the system is
    green ONLY when every component is healthy/benign. Each component is a dict:
        {name, kind, state, critical(bool, default False)}.
    """
    worst = SYS_HEALTHY
    reasons: list[str] = []
    for c in components:
        sev = _component_severity(c.get("kind", "service"), c.get("state", UNKNOWN),
                                  bool(c.get("critical", False)))
        if _SYS_RANK[sev] > _SYS_RANK[worst]:
            worst = sev
        if sev != SYS_HEALTHY:
            reasons.append(f"{c.get('name', '?')}={c.get('state', '?')}")
    v = version or version_info()
    return status(worst, reason=("; ".join(reasons) if reasons else "all_components_healthy"),
                  paper_only=paper_only, generated_at=generated_at,
                  git_branch=v.get("git_branch"), git_commit=v.get("git_commit"),
                  version=v.get("code_version"),
                  details={"components": [{"name": c.get("name"), "kind": c.get("kind", "service"),
                                           "state": c.get("state"), "critical": bool(c.get("critical", False))}
                                          for c in components],
                           "git_dirty": v.get("git_dirty")})
