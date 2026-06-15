"""
harness.doctor — read-only health check for the Polymarket harness.

Run from polyswarm/ with:  python -m harness.doctor   (PYTHONUTF8=1)
Optional:                   python -m harness.doctor --json

Runs ~12 cheap, READ-ONLY probes and prints an aligned PASS/WARN/FAIL table
with a final summary. Exits non-zero iff any check FAILs (WARN never fails the
run). This is a diagnostic only: it NEVER writes, NEVER touches real money, and
NEVER prints the value of any *_API_KEY.

Design notes
------------
* Every check is wrapped so a single broken probe can never crash doctor.
* All heavy imports (httpx, obs, health, gamma/gdelt/wiki) are LAZY — inside the
  individual checks — so the module imports even if a dependency is missing
  (the deps check then reports it).
* Paths are anchored to the polyswarm/ package via harness.obs.config (the DB is
  opened mode=ro; the events dir is read by path, never created) so doctor gives
  the same answer regardless of the current working directory.
* Network checks use short timeouts and DEGRADE TO WARN on timeout (a transient
  blip is not a broken harness); a hard connection-refused or a bad/non-200
  response for a required service is a FAIL.
* Local-only services (MiroFish :5001, dashboard :8800) and daemon heartbeat
  freshness are WARN-on-miss, never FAIL — the trading pipeline tolerates them.
* GDELT is probed LAST because its public API is sticky-rate-limited (~1 req/5s).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

# Load polyswarm/.env exactly like the rest of the harness (best-effort; guarded
# so a missing python-dotenv never stops doctor — the deps/env checks report it).
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

# ── status constants ──────────────────────────────────────────────────────────
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# Required python (the venv ships 3.11+; union/`str | None` annotations need 3.10+).
MIN_PY = (3, 11)
# Importable deps that the running daemons rely on (sqlite3 is stdlib, always present).
REQUIRED_DEPS = ("httpx", "fastapi", "uvicorn", "dotenv", "pydantic", "sqlalchemy", "sqlite3")
# Tables that must exist in polyswarm.db for the harness + obs layer to function.
REQUIRED_TABLES = ("forecasts", "swarm_forecasts", "paper_positions", "paper_wallet", "obs_forecasts")
# Heartbeat is "fresh" if touched within this many seconds (one daemon interval + margin).
HEARTBEAT_MAX_AGE = 900.0


# ── small helpers ─────────────────────────────────────────────────────────────
def _trunc(s, n=90):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _net_status(exc, httpx):
    """Map an httpx exception to (status, detail). Timeout -> WARN; else FAIL."""
    try:
        if isinstance(exc, httpx.TimeoutException):
            return WARN, "timeout (transient)"
        if isinstance(exc, httpx.ConnectError):
            return FAIL, "connection refused / unreachable"
    except Exception:
        pass
    return FAIL, _trunc(type(exc).__name__ + ": " + str(exc), 80)


def _bad_http(r, who):
    """(status, detail) for a non-200 response. 429/503 (throttle/overload) are
    transient -> WARN; any other non-200 is a real problem -> FAIL."""
    if r.status_code in (429, 503):
        return WARN, f"HTTP {r.status_code} from {who} (rate-limited / throttled)"
    return FAIL, f"HTTP {r.status_code} from {who}"


# ── checks (each returns (status, detail)) ──────────────────────────────────────
def check_python():
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PY:
        return PASS, f"{ver} (>= {MIN_PY[0]}.{MIN_PY[1]})"
    return FAIL, f"{ver} is below required {MIN_PY[0]}.{MIN_PY[1]}"


def check_deps():
    import importlib.util

    missing = []
    for m in REQUIRED_DEPS:
        try:
            if importlib.util.find_spec(m) is None:
                missing.append(m)
        except Exception:
            missing.append(m)
    if missing:
        return FAIL, "missing: " + ", ".join(missing)
    return PASS, f"all {len(REQUIRED_DEPS)} importable ({', '.join(REQUIRED_DEPS)})"


def check_env():
    from harness import obs

    env_path = obs.config.PKG_ROOT / ".env"
    if not env_path.exists():
        return FAIL, f".env not found at {env_path}"
    model = os.getenv("MODEL_FAST")
    base = os.getenv("OLLAMA_BASE_URL")
    if not model:
        return WARN, f".env present but MODEL_FAST unset ({env_path.name})"
    base_note = "OLLAMA_BASE_URL set" if base else "OLLAMA_BASE_URL unset (default localhost:11434)"
    # NB: never echo *_API_KEY values — only the (non-secret) model name + base flag.
    return PASS, f"MODEL_FAST={model}, {base_note}"


def check_db():
    from harness import obs

    db = obs.config.resolve_db_path()
    if not db.exists():
        return FAIL, f"db not found: {db}"
    try:
        uri = db.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except Exception as e:
        return FAIL, f"cannot open {db} read-only: {_trunc(e, 60)}"
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    have = {r[0] for r in rows}
    missing = [t for t in REQUIRED_TABLES if t not in have]
    if missing:
        return FAIL, f"{len(have)} tables, missing required: {', '.join(missing)}"
    return PASS, f"{len(have)} tables, all required present ({db.name})"


def check_ollama():
    try:
        import httpx
    except Exception:
        return FAIL, "httpx not importable"
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("MODEL_FAST", "qwen2.5:3b")
    family = model.split(":")[0]
    try:
        r = httpx.get(f"{base}/api/tags", timeout=4)
        if r.status_code != 200:
            return _bad_http(r, f"{base}/api/tags")
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if any(family in n for n in names):
            return PASS, f"up, {len(names)} models, '{model}' present"
        return FAIL, f"up but model family '{family}' not pulled ({len(names)} models)"
    except Exception as e:
        return _net_status(e, httpx)


def check_gamma():
    try:
        import httpx

        from harness import gamma
    except Exception as e:
        return FAIL, f"import failed: {_trunc(e, 60)}"
    try:
        r = httpx.get(
            gamma.MARKETS_URL,
            params={"limit": 1, "closed": "false"},
            headers=gamma._HEADERS,
            timeout=6,
        )
        if r.status_code != 200:
            return _bad_http(r, "Gamma /markets")
        data = r.json()
        if isinstance(data, list) and data:
            return PASS, f"reachable, {len(data)} market returned"
        return WARN, "reachable but returned no markets"
    except Exception as e:
        return _net_status(e, httpx)


def check_wikipedia():
    try:
        import httpx

        from harness import wiki
    except Exception as e:
        return FAIL, f"import failed: {_trunc(e, 60)}"
    try:
        r = httpx.get(
            wiki._SUMMARY_URL + "Polymarket",
            headers=wiki._HEADERS,
            timeout=6,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return _bad_http(r, "Wikipedia REST summary")
        if (r.json() or {}).get("extract"):
            return PASS, "reachable, summary 'extract' present"
        return WARN, "reachable but no 'extract' in body"
    except Exception as e:
        return _net_status(e, httpx)


def check_mirofish():
    # WARN-on-miss: the local-crowd fallback covers a missing MiroFish backend.
    try:
        from harness import health

        h = health.mirofish_health()
        if h.get("up"):
            return PASS, "backend up (:5001, external mode)"
        return WARN, "down (:5001) — local-crowd fallback in use"
    except Exception as e:
        return WARN, f"probe error: {_trunc(e, 60)}"


def check_dashboard():
    # WARN-on-miss: the dashboard is a viewer, not required for trading.
    try:
        import httpx
    except Exception:
        return WARN, "httpx not importable"
    port = os.getenv("DASH_PORT", "8800")
    url = f"http://localhost:{port}"
    try:
        r = httpx.get(url, timeout=3)
        if r.status_code == 200:
            return PASS, f"serving ({url})"
        return WARN, f"HTTP {r.status_code} from {url}"
    except Exception:
        return WARN, f"down ({url}) — viewer only"


def check_heartbeat():
    # WARN-on-miss. Anchor to PKG_ROOT (cwd-independent) and trust the FRESHEST of
    # .heartbeat.json + the daemons' stream logs (loop/predict_today write the json;
    # the sameday daemon writes its stream log every cycle).
    try:
        from harness import obs

        root = obs.config.PKG_ROOT
        cands = []
        for name in (".heartbeat.json", "sameday_live.log", "ai_night.log", "predict_today.log"):
            p = root / name
            try:
                if p.exists():
                    cands.append((name, p.stat().st_mtime))
            except Exception:
                pass
        if not cands:
            return WARN, "no heartbeat / stream log found"
        name, mt = max(cands, key=lambda x: x[1])
        age = max(0.0, time.time() - mt)
        detail = f"{name} touched {age:.0f}s ago"
        if age <= HEARTBEAT_MAX_AGE:
            return PASS, detail
        return WARN, detail + f" (> {HEARTBEAT_MAX_AGE:.0f}s — daemon may be idle/stopped)"
    except Exception as e:
        return WARN, f"probe error: {_trunc(e, 60)}"


def check_obs_chain():
    from harness import obs

    # Read the events dir by PATH — do NOT call events_dir() (it mkdir's). Read-only.
    ev = obs.config.LOGS_DIR() / "events"
    if not ev.is_dir():
        return WARN, f"no events dir yet ({ev})"
    try:
        files = sorted(ev.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:
        return WARN, f"cannot list events: {_trunc(e, 60)}"
    if not files:
        return WARN, "no event logs yet"
    run_id = files[0].stem
    res = obs.verify_chain(run_id)
    if res.get("ok"):
        return PASS, f"chain intact: {run_id} ({res.get('n', 0)} lines)"
    return FAIL, (
        f"chain BROKEN: {run_id} reason={res.get('reason')} "
        f"first_bad={res.get('first_bad_index')} n={res.get('n')}"
    )


def check_gdelt():
    # Probed LAST: GDELT throttles to ~1 req/5s and replies HTTP 200 + PLAIN TEXT
    # when throttled, so a 200 alone is NOT enough — the body must JSON-decode.
    try:
        import httpx

        from harness import gdelt
    except Exception as e:
        return FAIL, f"import failed: {_trunc(e, 60)}"
    try:
        r = httpx.get(
            gdelt.BASE_URL,
            params={"query": "test", "mode": "artlist", "format": "json", "maxrecords": 1},
            headers=gdelt.HEADERS,
            timeout=8,
        )
        if r.status_code != 200:
            return _bad_http(r, "GDELT doc API")
        try:
            r.json()
        except Exception:
            return WARN, "HTTP 200 but non-JSON body (throttled)"
        return PASS, "reachable, JSON body parsed"
    except Exception as e:
        return _net_status(e, httpx)


# ── runner ──────────────────────────────────────────────────────────────────--
# (label, callable, soft) — soft=True => an UNEXPECTED exception in the check maps
# to WARN instead of FAIL. Execution order == print order; GDELT runs last.
CHECKS = [
    ("python", check_python, False),
    ("deps", check_deps, False),
    ("env", check_env, False),
    ("db", check_db, False),
    ("ollama", check_ollama, False),
    ("gamma", check_gamma, False),
    ("wikipedia", check_wikipedia, False),
    ("mirofish", check_mirofish, True),
    ("dashboard", check_dashboard, True),
    ("heartbeat", check_heartbeat, True),
    ("obs_chain", check_obs_chain, False),
    ("gdelt", check_gdelt, False),
]


def run_checks():
    """Run every check, catching any unexpected exception. Returns list of dicts."""
    results = []
    for label, fn, soft in CHECKS:
        t0 = time.perf_counter()
        try:
            status, detail = fn()
        except Exception as e:  # a buggy check must never crash doctor
            status = WARN if soft else FAIL
            detail = f"check raised {type(e).__name__}: {_trunc(e, 60)}"
        ms = (time.perf_counter() - t0) * 1000.0
        results.append({"name": label, "status": status, "detail": detail, "ms": round(ms, 1)})
    return results


def _summary_counts(results):
    n_pass = sum(1 for r in results if r["status"] == PASS)
    n_warn = sum(1 for r in results if r["status"] == WARN)
    n_fail = sum(1 for r in results if r["status"] == FAIL)
    return n_pass, n_warn, n_fail


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv

    results = run_checks()
    n_pass, n_warn, n_fail = _summary_counts(results)

    if as_json:
        import json

        print(
            json.dumps(
                {
                    "ok": n_fail == 0,
                    "summary": {"pass": n_pass, "warn": n_warn, "fail": n_fail, "total": len(results)},
                    "checks": results,
                },
                indent=2,
            )
        )
        return 1 if n_fail else 0

    width = max((len(r["name"]) for r in results), default=8)
    print("harness.doctor — read-only health check")
    print("-" * 60)
    for r in results:
        print(f"[{r['status']}] {r['name']:<{width}}  {r['detail']}")
    print("-" * 60)
    verdict = "OK" if n_fail == 0 else "PROBLEMS"
    print(f"{verdict}: {n_pass} pass, {n_warn} warn, {n_fail} fail  (of {len(results)} checks)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
