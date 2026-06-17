"""
Live dashboard for the Polymarket paper-trading harness (dark, Splunk-style).

Reads ./polyswarm.db live and serves a single auto-refreshing page:
  * stat cards — cash, equity, realized P&L, open positions, forecasts, gate status
  * P&L / equity time-series chart (Chart.js)
  * gate gauges — model Brier vs the 0.0627 market bar, n vs 50, paper P&L
  * "what it's betting on" — open paper positions
  * Challenger A/B — swarm vs single-LLM (the MiroFish replacement) vs market
  * decision transcript — why it bet, which side, how much

Run:  ./.venv/Scripts/python.exe -m harness.dashboard   (then open http://localhost:8800)
"""
from __future__ import annotations

import os
import glob
import sqlite3
import json
import asyncio
import httpx

# Load polyswarm/.env so the dashboard reflects MODEL_FAST and the CHALLENGER_* key.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from harness import wallet as paper
from harness import journal, scoreboard, challenger
from harness import mirofish_signal
from harness import health

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")
MARKET_BAR = 0.0627   # historical market-price Brier on resolved opinion markets (the bar)
# Live agent feed: the daemon's console log, tailed to the browser over SSE.
STREAM_LOG = os.getenv("DASH_STREAM_LOG", "sameday_live.log")
# "Watch it think" widget: a small/fast local model narrates live reasoning token-by-token
# over a WebSocket. The heavy 7B swarm stays the real forecaster; this is just the live view.
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_LIVE_MODEL = os.getenv("DASH_LLM_MODEL", "qwen2.5:3b")

app = FastAPI(title="Polymarket Harness Dashboard")


def _ab_rows(limit: int = 40) -> list[dict]:
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try:
        # ONE row per market — the latest swarm + latest baseline. The old query
        # cross-joined every baseline against every swarm row for the same market_id,
        # so 3 re-scouts of one market showed as 9 duplicate-looking rows.
        rows = conn.execute(
            "SELECT s.question AS question, s.final_probability AS swarm_p, b.probability AS llm_p, "
            "b.market_odds AS market_p, s.outcome AS outcome "
            "FROM swarm_forecasts s JOIN baseline_forecasts b ON b.market_id = s.market_id "
            "WHERE s.id = (SELECT MAX(id) FROM swarm_forecasts s2 WHERE s2.market_id = s.market_id) "
            "AND b.id = (SELECT MAX(id) FROM baseline_forecasts b2 WHERE b2.market_id = b.market_id) "
            "ORDER BY s.id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


def _counts() -> dict:
    conn = sqlite3.connect(DB_PATH)
    def c(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    out = {
        "forecasts": c("SELECT COUNT(*) FROM swarm_forecasts"),
        "baselines": c("SELECT COUNT(*) FROM baseline_forecasts"),
        "resolved": c("SELECT COUNT(*) FROM swarm_forecasts WHERE outcome IS NOT NULL"),
        "decisions": c("SELECT COUNT(*) FROM decisions"),
        "bets": c("SELECT COUNT(*) FROM decisions WHERE status='bet'"),
    }
    conn.close()
    return out


@app.get("/api/state")
def api_state():
    try:
        paper.init_wallet()
        st = paper.get_state()
    except Exception:
        st = {"starting_bankroll": 0, "cash": 0, "equity": 0, "realized_pnl": 0, "open_exposure": 0, "n_open": 0}
    # Plan 10: a transient DB lock inside scoreboard.compute() must NOT 500 /api/state (which
    # would leave the HTML showing the LAST, stale cards — e.g. a stale gate2=green). Guard it,
    # and read every field with a safe default so a partial scoreboard never crashes the endpoint.
    sb = _safe(lambda: scoreboard.compute(), {}) or {}
    _g1 = sb.get("gate1") or {}
    _g2 = sb.get("gate2") or {}
    snaps = _safe(lambda: journal.get_snapshots(500), [])
    try:
        challenger_model = challenger.challenger_model_label()
        challenger_hosted = challenger._hosted_configured()
    except Exception:
        challenger_model, challenger_hosted = "local-llm", False
    data = {
        "wallet": st,
        "counts": _counts(),
        "bar": MARKET_BAR,
        "challenger_model": challenger_model,
        "challenger_hosted": challenger_hosted,
        "scoreboard": {
            "n": sb.get("n", 0), "n_required": sb.get("n_required"),
            "model_brier": sb.get("model_brier"), "market_brier": sb.get("market_brier"),
            "baseline_brier": sb.get("baseline_brier"), "baseline_n": sb.get("baseline_n", 0),
            "gate1": _g1.get("pass"), "gate2": _g2.get("pass"),
            "gate2_status": _g2.get("status"), "gate2_reasons": _g2.get("reasons"),
            "themes": sb.get("themes") or {},
        },
        # Plan 9: accounting honesty — the card shows VERIFIED status + realized/unrealized/total
        # split + equity, or "unverified" instead of a fake number. paper_only is always explicit.
        "accounting": sb.get("accounting"),
        "paper_only": True,
        "equity_verified": (sb.get("accounting") or {}).get("equity") is not None,
        "generated_at": sb.get("generated_at"),
        "pnl_series": [{"ts": s["ts"], "equity": s["equity"], "realized_pnl": s["realized_pnl"],
                        "cash": s["cash"], "n_open": s["n_open"]} for s in snaps],
        # guarded like every other read here — a transient 'database is locked' (a
        # daemon settling/opening) must not 500 the whole dashboard (audit #15).
        "positions": _safe(lambda: paper.get_open_positions(), []),
        "closed": _safe(lambda: paper.get_closed_positions(80), []),
        "decisions": journal.get_decisions(60),
        "ab": _ab_rows(40),
        "mirofish": mirofish_signal.get_signals(8),
    }
    return JSONResponse(data)


@app.get("/api/accounting")
def api_accounting():
    """Plan 9: the HONEST accounting card — read-only audit (cash / realized / unrealized /
    total / equity, drift, stale marks, status+reasons), the fail-closed Gate-2 readiness
    verdict, and journal consistency. Shows 'unverified'/'unknown' instead of fake numbers."""
    try:
        from harness import accounting_audit as _acct
        return JSONResponse({
            "audit": _safe(lambda: _acct.audit_accounting(), {"status": "error"}),
            "gate2": _safe(lambda: _acct.gate2_status(), {"status": "unknown"}),
            "journal": _safe(lambda: _acct.journal_consistency(), {"status": "unknown"}),
            "paper_only": True,
        })
    except Exception as e:
        return JSONResponse({"error": f"accounting unavailable: {e}", "paper_only": True})


@app.get("/api/command_center")
def api_command_center():
    """P11: read-only command-center panel — skipped markets + reasons, losing-trade
    diagnosis, theme/label performance, next-best-actions, replay handles, and the
    consolidated metrics/gate report. Best-effort: never 500s the dashboard."""
    try:
        from harness import command_center
        data = command_center.command_center()
    except Exception as e:
        data = {"error": f"command_center unavailable: {e}"}
    try:
        from harness import metrics
        data["metrics"] = metrics.full_report()
    except Exception as e:
        data["metrics"] = {"error": f"metrics unavailable: {e}"}
    return JSONResponse(data)


@app.get("/api/explain/{market_id}")
def api_explain(market_id: str):
    """P11: clickable obs replay — the full reconstructed decision trail for a market."""
    try:
        from harness.obs import explain as _explain
        return JSONResponse({"market_id": market_id, "trail": _explain.explain(market_id)})
    except Exception as e:
        return JSONResponse({"market_id": market_id, "error": f"explain unavailable: {e}"})


def _safe(fn, default):
    """Run a read and swallow any error (e.g. transient sqlite lock) -> default,
    so one contended read never 500s the whole dashboard response."""
    try:
        return fn()
    except Exception:
        return default


def _db_usable() -> bool:
    """Plan 10: the DB is 'ok' only if it actually OPENS and answers a query — a present-but-
    LOCKED/corrupt file is NOT ok (read-only handle, never creates the DB)."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1.0)
        # read the schema (not a constant SELECT 1) so a corrupt/locked file actually fails.
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        c.close()
        return True
    except Exception:
        return False


def _envelope(*, state, reason="", source="", stale=False, paper_only=True, **data):
    """Plan 10: the canonical truth envelope every dashboard card endpoint returns. ``ok`` is
    green ONLY for ok/healthy; unknown/stale/degraded/drift are never green."""
    from harness import status_model as _sm
    out = {"generated_at": _sm.now_iso(), "stale": bool(stale), "paper_only": bool(paper_only),
           "source": source, "state": state, "reason": reason or state,
           "ok": state in ("ok", "healthy")}
    out.update(data)
    return out


@app.get("/api/services")
def api_services():
    """Plan 10: per-service canonical truth (state/age/stale) + the SYSTEM state. A live process
    is not 'healthy'; supervisor-alive is not 'bot-healthy'. Never 500s."""
    try:
        from harness import supervisor as _sup
        d = _safe(lambda: _sup.status(as_dict=True),
                  {"services": {}, "system_state": "unknown", "supervisor_alive": None})
        return JSONResponse(_envelope(
            state=d.get("system_state", "unknown"), reason="supervisor_status",
            source="supervisor.status", stale=False, system_state=d.get("system_state"),
            supervisor_alive=d.get("supervisor_alive"), services=d.get("services", {})))
    except Exception as e:
        return JSONResponse(_envelope(state="unknown", reason=f"supervisor_unavailable:{e}",
                                      source="supervisor", services={}))


@app.get("/api/scoreboard")
def api_scoreboard():
    """Plan 10: scoreboard card — realized/unrealized/total/equity (Plan 9 accounting), gates,
    Brier, with accounting STATUS driving the card colour. Never 500s."""
    sb = _safe(lambda: scoreboard.compute(), {})
    acc = (sb.get("accounting") or {}) if isinstance(sb, dict) else {}
    return JSONResponse(_envelope(
        state=(acc.get("status") or "unknown"), reason="scoreboard", source="scoreboard.compute",
        stale=(acc.get("status") not in ("ok",)),
        data_generated_at=sb.get("generated_at"), n=sb.get("n"),
        model_brier=sb.get("model_brier"), market_brier=sb.get("market_brier"),
        gate1=sb.get("gate1"), gate2=sb.get("gate2"), accounting=acc, themes=sb.get("themes")))


@app.get("/api/mirofish")
def api_mirofish():
    """Plan 10: MiroFish CONTRIBUTION card (Plan 8 canonical). Backend alive is NOT a contribution
    — mirofish_used=true ONLY for a fresh, market-matched run actually fed to the swarm."""
    try:
        from harness import mirofish_validate as _mfv, mirofish_status as _mfs, health as _h
        runs = _safe(lambda: _mfv.get_runs(None, 25), [])
        for r in runs:
            r["state"] = _safe(lambda r=r: _mfs.state_from_row(r), "unknown")
            r["mirofish_used"] = (r["state"] == _mfs.FRESH_USED)
            r["stale_now"] = _safe(lambda r=r: _mfs.is_stale_now(r), True)
        used = sum(1 for r in runs if r.get("mirofish_used"))                       # historical fact
        fresh_used = sum(1 for r in runs if r.get("mirofish_used") and not r.get("stale_now"))
        backend = _safe(lambda: _h.mirofish_health(), {"up": False})
        # GREEN only when MiroFish is CURRENTLY contributing: a fresh (not stale_now) used run AND
        # the backend alive. A stale-but-historically-used run, or a dead backend, is not green.
        if fresh_used > 0 and backend.get("up"):
            state, reason = "ok", "mirofish_contributing"
        elif backend.get("up"):
            state, reason = "degraded", "backend_alive_not_fresh_contributing"
        else:
            state, reason = "unknown", "mirofish_unavailable"
        return JSONResponse(_envelope(
            state=state, reason=reason, source="mirofish_status",
            backend_alive=bool(backend.get("up")), used=used, fresh_used=fresh_used,
            n=len(runs), runs=runs,
            note="backend liveness is NOT a contribution; green needs a FRESH used run + live backend."))
    except Exception as e:
        return JSONResponse(_envelope(state="unknown", reason=f"mirofish_unavailable:{e}",
                                      source="mirofish", backend_alive=False, used=0, runs=[]))


@app.get("/api/decisions")
def api_decisions(limit: int = 60):
    """Plan 10: recent decisions — bets and no-bets counted SEPARATELY (no-bet is not a trade)."""
    rows = _safe(lambda: journal.get_decisions(limit), [])
    bets = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "bet")
    no_bets = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "no_bet")
    last = rows[0] if rows else None
    # no decisions yet is NOT "ok/green" — it's unknown (nothing to show).
    return JSONResponse(_envelope(
        state=("ok" if rows else "unknown"), reason=("decisions" if rows else "no_decisions"),
        source="journal.get_decisions", decisions=rows, n=len(rows), bets=bets, no_bets=no_bets,
        last_decision_at=(last.get("ts") if isinstance(last, dict) else None)))


@app.get("/api/gates")
def api_gates():
    """Plan 10: Gate 1 (calibration) + Gate 2 (Plan 9 FAIL-CLOSED readiness). Gate 2 shows PASS
    ONLY when gate2_status().pass is true; otherwise its status+reasons are surfaced."""
    try:
        from harness import accounting_audit as _acct
        sb = _safe(lambda: scoreboard.compute(), {})
        g1 = (sb.get("gate1") or {}) if isinstance(sb, dict) else {}
        g2 = _safe(lambda: _acct.gate2_status(), {"status": "unknown", "pass": False})
        both = bool(g1.get("pass") and g2.get("pass"))
        state = "ok" if both else (g2.get("status") or "unknown")
        return JSONResponse(_envelope(
            state=state, reason="gates", source="accounting_audit.gate2_status",
            gate1=g1, gate2=g2, both_pass=both))
    except Exception as e:
        return JSONResponse(_envelope(state="unknown", reason=f"gates_unavailable:{e}", source="gates"))


@app.get("/api/profit-intelligence")
def api_profit_intelligence():
    """Plan 11: PAPER-ONLY profit intelligence — candidate signals, no-bet learning, post-trade
    learning, attribution. NEVER reports 'profitable' unless Gate 2 (Plan 9) is pass; otherwise it
    shows learning / insufficient_sample / watching. Read-only; never trades."""
    try:
        from harness import profit_intel as _pi
        rep = _safe(lambda: _pi.profit_intelligence_report(), None)
        if not rep:
            return JSONResponse(_envelope(state="unknown", reason="profit_intel_unavailable",
                                          source="profit_intel", paper_only=True, report=None))
        gate2_pass = bool(rep.get("gate2_pass"))
        # green ('ok') ONLY on a Gate-2 pass; degraded if accounting unverified; else honest learning
        if gate2_pass:
            state = "ok"
        elif rep.get("accounting_status") not in ("ok", None):
            state = "degraded"
        else:
            state = "learning"
        return JSONResponse(_envelope(
            state=state, reason=rep.get("headline"), source="profit_intel", paper_only=True,
            accounting_status=rep.get("accounting_status"), gate2_status=rep.get("gate2_status"),
            gate2_pass=gate2_pass, needs_more_data=rep.get("needs_more_data"),
            profitable_claim_allowed=rep.get("profitable_claim_allowed"), report=rep))
    except Exception as e:
        return JSONResponse(_envelope(state="unknown", reason=f"profit_intel_error:{e}",
                                      source="profit_intel", paper_only=True))


@app.get("/api/version")
def api_version():
    """Plan 10: code version / git branch+commit / dirty tree. None (not crash) when git is absent."""
    from harness import status_model as _sm
    v = _safe(lambda: _sm.version_info(use_cache=False), {})
    return JSONResponse(_envelope(
        state=("ok" if v.get("git_commit") else "unknown"), reason="version",
        source="obs.codeversion", git_branch=v.get("git_branch"), git_commit=v.get("git_commit"),
        git_dirty=v.get("git_dirty"), code_version=v.get("code_version")))


@app.get("/api/truth")
def api_truth():
    """Plan 10: the UNIFIED 'is the system trustworthy?' signal the health badge consumes —
    accounting audit + Gate 2 + service states + DB. NEVER green unless every part is verified."""
    from harness import status_model as _sm
    comps, extra = [], {}
    try:
        from harness import accounting_audit as _acct
        acc = _safe(lambda: _acct.audit_accounting(), {"status": "unknown"})
        comps.append({"name": "accounting", "kind": "accounting", "state": acc.get("status", "unknown")})
        # Gate 2 is go-live READINESS, not operational health (it rarely passes for a paper bot),
        # so it is NOT a system-health component — it is reported as its own EXPLICIT, visible
        # field. The System badge reflects liveness + accounting; Gate 2 is surfaced separately.
        g2 = _safe(lambda: _acct.gate2_status(), {"status": "unknown", "pass": False})
        extra["accounting_status"] = acc.get("status")
        extra["gate2_pass"] = bool(g2.get("pass"))
        extra["gate2_status"] = g2.get("status")
        extra["gate2_reasons"] = g2.get("reasons")
    except Exception:
        comps.append({"name": "accounting", "kind": "accounting", "state": "unknown"})
    try:
        from harness import supervisor as _sup
        svc = _safe(lambda: _sup.status(as_dict=True), {"system_state": "unknown"})
        comps.append({"name": "services", "kind": "service", "state": svc.get("system_state", "unknown"),
                      "critical": True})
        extra["services_state"] = svc.get("system_state")
    except Exception:
        comps.append({"name": "services", "kind": "service", "state": "unknown", "critical": True})
    db_ok = bool(_safe(lambda: _db_usable(), False))
    db_present = bool(_safe(lambda: os.path.exists(DB_PATH), False))
    comps.append({"name": "db", "kind": "db",
                  "state": ("ok" if db_ok else ("error" if db_present else "missing"))})
    extra["db_ok"] = db_ok
    sysv = _sm.system_status(comps)
    sysv["source"] = "accounting+gate2+services+db"
    sysv.update(extra)
    return JSONResponse(sysv)


# ══════════════════════════════════════════════════════════════════════════════
# Live telemetry cockpit (UI upgrade) — SSE stream + paper-wallet/bets/pnl/ai-now.
# All read-only + best-effort; missing data shows honest unknown/stale/link_unavailable/etc.
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/events/live")
async def events_live(request: Request):
    """SSE stream of REAL live_events (cross-process ring buffer fed by the obs hooks). If no
    daemon is emitting, the client simply sees pings + a stale state — never faked activity."""
    from harness import live_events as _le

    async def gen():
        _le.register_client()
        last_id = 0
        try:
            backlog = list(reversed(_safe(lambda: _le.recent_events(60), [])))
            for e in backlog:
                try:
                    last_id = max(last_id, int(e["id"]))
                except Exception:
                    pass
                yield f"data: {json.dumps(e)}\n\n"
            tick = 0
            while tick < 7200:                          # ~2h cap; EventSource auto-reconnects
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    pass
                for e in _safe(lambda: _le.recent_events(200, since_id=last_id), []):
                    try:
                        last_id = max(last_id, int(e["id"]))
                    except Exception:
                        pass
                    yield f"data: {json.dumps(e)}\n\n"
                if tick % 5 == 0:
                    st = _safe(lambda: _le.event_status(), {"state": "unknown"})
                    yield f": ping {st.get('state')} {st.get('last_event_age_seconds')}\n\n"
                tick += 1
                await asyncio.sleep(1.0)
        finally:
            _le.unregister_client()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/live/recent")
def api_live_recent(limit: int = 200, type: str = ""):
    from harness import live_events as _le
    evs = _safe(lambda: _le.recent_events(limit, type=(type or None)), [])
    return JSONResponse({"paper_only": True, "generated_at": _now_iso_(),
                         "count": len(evs), "events": evs})


@app.get("/api/live/status")
def api_live_status():
    from harness import live_events as _le
    return JSONResponse(_safe(lambda: _le.event_status(),
                              {"paper_only": True, "state": "unknown", "transport": "sse",
                               "generated_at": _now_iso_()}))


@app.get("/api/paper-wallet")
def api_paper_wallet():
    """Plan 9 accounting truth → the cockpit wallet card. verified_equity is None (unverified)
    unless accounting says marks are fresh; never a fabricated number."""
    from harness import accounting_audit as _acct
    audit = _safe(lambda: _acct.audit_accounting(), {"status": "unknown", "reasons": []})
    st = _safe(lambda: paper.get_state(), {})
    open_n = _safe(lambda: len(paper.get_open_positions()), None)
    settled_n = _safe(lambda: _settled_count(), None)
    g2 = _safe(lambda: _acct.gate2_status(), {})
    equity_verified = audit.get("equity") is not None
    starting = audit.get("starting_bankroll")
    if starting is None:
        starting = st.get("starting_bankroll")
    cash = audit.get("cash")
    if cash is None:
        cash = st.get("cash")
    open_exposure = _safe(lambda: st.get("open_exposure"), None)
    avail = None
    if cash is not None:
        avail = round(float(cash), 6)
    return JSONResponse({
        "paper_only": True, "generated_at": _now_iso_(),
        "starting_bankroll": starting, "cash": cash,
        "verified_equity": (audit.get("equity") if equity_verified else None),
        "equity_verified": equity_verified,
        "realized_pnl": audit.get("realized_pnl"), "unrealized_pnl": audit.get("unrealized_pnl"),
        "total_pnl": audit.get("total_pnl"), "open_exposure": open_exposure,
        "available_balance": avail, "open_positions_count": open_n,
        "settled_positions_count": settled_n,
        "max_drawdown": (g2.get("max_drawdown") if isinstance(g2, dict) else None),
        "accounting_status": audit.get("status"), "accounting_reasons": audit.get("reasons", []),
        "gate2_status": (g2.get("status") if isinstance(g2, dict) else None),
        "gate2_pass": bool(g2.get("pass")) if isinstance(g2, dict) else False,
        "stale": (audit.get("status") not in ("ok",)),
    })


@app.get("/api/paper-bets/open")
def api_paper_bets_open():
    from harness import paper_bets as _pb
    return JSONResponse(_safe(lambda: _pb.open_positions(),
                              {"paper_only": True, "generated_at": _now_iso_(), "positions": []}))


@app.get("/api/paper-bets/settled")
def api_paper_bets_settled(limit: int = 100):
    from harness import paper_bets as _pb
    return JSONResponse(_safe(lambda: _pb.settled_positions(limit=limit),
                              {"paper_only": True, "generated_at": _now_iso_(), "positions": []}))


@app.get("/api/paper-bets/proof")
def api_paper_bets_proof(position_id: str = ""):
    from harness import paper_bets as _pb
    if not position_id:
        return JSONResponse({"paper_only": True, "position_id": None, "proof_status": "unknown",
                             "timeline": [], "warnings": ["missing_position_id"]})
    return JSONResponse(_safe(lambda: _pb.proof_timeline(position_id),
                              {"paper_only": True, "position_id": position_id,
                               "proof_status": "unknown", "timeline": [], "warnings": ["error"]}))


def _pnl_curve(db_path=None):
    """Equity/cash/realized/unrealized/total curve from the equity_snapshots history (Plan 9
    accounting truth). Marks the overall verification state; never fakes points."""
    from harness import accounting_audit as _acct
    snaps = _safe(lambda: journal.get_snapshots(1000), [])
    audit = _safe(lambda: _acct.audit_accounting(), {"status": "unknown"})
    verified = audit.get("status") == "ok"
    if not snaps:
        return {"paper_only": True, "generated_at": _now_iso_(), "state": "not_enough_data",
                "points": [], "warnings": ["not_enough_paper_wallet_history"]}
    peak = None
    points = []
    for s in snaps:
        eq = s.get("equity")
        rp = s.get("realized_pnl")
        cash = s.get("cash")
        if eq is not None:
            peak = eq if peak is None else max(peak, eq)
        dd = (round(peak - eq, 6) if (peak is not None and eq is not None) else None)
        points.append({"ts": s.get("ts"), "cash": cash, "equity": eq, "realized_pnl": rp,
                       "unrealized_pnl": (round(eq - cash - (rp or 0), 6) if (eq is not None and cash is not None) else None),
                       "total_pnl": (round((eq - (s.get("starting_bankroll") or 0)), 6) if eq is not None else None),
                       "drawdown": dd, "verified": verified,
                       "reason": ("accounting_ok" if verified else f"accounting_{audit.get('status')}")})
    state = "ok" if verified else "unverified"
    if len(points) < 2:
        state = "partial"
    return {"paper_only": True, "generated_at": _now_iso_(), "state": state, "points": points,
            "warnings": ([] if verified else [f"accounting_{audit.get('status')}"])}


@app.get("/api/pnl-curve")
def api_pnl_curve():
    return JSONResponse(_safe(lambda: _pnl_curve(), {"paper_only": True, "state": "error",
                                                     "points": [], "warnings": ["error"]}))


@app.get("/api/equity-curve")
def api_equity_curve():
    return JSONResponse(_safe(lambda: _pnl_curve(), {"paper_only": True, "state": "error",
                                                     "points": [], "warnings": ["error"]}))


@app.get("/api/pnl")
def api_pnl():
    from harness import accounting_audit as _acct
    audit = _safe(lambda: _acct.audit_accounting(), {"status": "unknown"})
    return JSONResponse({"paper_only": True, "generated_at": _now_iso_(),
                         "realized_pnl": audit.get("realized_pnl"),
                         "unrealized_pnl": audit.get("unrealized_pnl"),
                         "total_pnl": audit.get("total_pnl"), "equity": audit.get("equity"),
                         "accounting_status": audit.get("status")})


@app.get("/api/ai-now")
def api_ai_now():
    """What is the AI doing right now? From live events first; falls back to heartbeat/journal.
    Never fakes activity — idle/stale/unknown shown honestly."""
    from harness import live_events as _le
    evs = _safe(lambda: _le.recent_events(80), [])
    status = _safe(lambda: _le.event_status(), {"state": "unknown", "last_event_id": None})
    last = evs[0] if evs else None

    def _last_of(types):
        for e in evs:
            if e.get("type") in types:
                return e
        return None

    bet = _last_of({"decision.bet", "position.opened"})
    nobet = _last_of({"decision.no_bet"})
    settled = _last_of({"position.settled"})
    mf = _last_of({"mirofish.state", "mirofish.stage"})
    gate = _last_of({"gate.result"})
    agent = _last_of({"agent.started", "agent.finished"})
    swarm = _last_of({"swarm.started", "forecast.final"})
    cur = swarm or agent or last
    age = status.get("last_event_age_seconds")
    if not evs:
        state = "unknown"
    elif age is not None and age <= 20:
        state = "thinking" if (agent or swarm) else "evaluating"
    elif age is not None and age <= 300:
        state = "waiting"
    else:
        state = "stale"
    if nobet and last and last.get("type") == "decision.no_bet":
        state = "blocked"
    # heartbeat fallback
    hb_stage = None
    try:
        hsnap = health.snapshot()
        daemons = hsnap.get("daemons") or {}
        ai = daemons.get("ai_pipeline") or {}
        hb_stage = (ai.get("details") or {}).get("stage") if isinstance(ai.get("details"), dict) else None
    except Exception:
        hb_stage = None
    return JSONResponse({
        "paper_only": True, "state": state,
        "current_market_id": (cur or {}).get("market_id"),
        "current_question": (cur or {}).get("question"),
        "current_agent": ((agent or {}).get("data") or {}).get("persona"),
        "mirofish_state": ((mf or {}).get("data") or {}).get("state") or (mf or {}).get("status"),
        "last_gate_result": (gate or {}).get("message"),
        "last_no_bet_reason": ((nobet or {}).get("data") or {}).get("reason") or (nobet or {}).get("message"),
        "last_bet_position_id": ((bet or {}).get("data") or {}).get("trade_id"),
        "last_settled_position_id": ((settled or {}).get("data") or {}).get("trade_id"),
        "last_event_id": status.get("last_event_id"),
        "last_event_age_seconds": age,
        "last_heartbeat_stage": hb_stage,
        "generated_at": _now_iso_(),
    })


@app.get("/api/candidates/recent")
def api_candidates_recent(limit: int = 12):
    """Recent markets the AI evaluated, with the bet/no-bet reason. Reuses the journal + Plan 11
    profit-intel reason buckets. No-bets are shown as useful signals, not failures."""
    from harness import profit_intel as _pi
    rows = _safe(lambda: journal.get_decisions(limit), [])
    out = []
    for r in rows:
        status = r.get("status")
        out.append({
            "market_id": r.get("market_id"), "question": r.get("question"),
            "market_price": r.get("market_p"), "forecast_probability": r.get("model_p"),
            "raw_edge": r.get("edge"), "side": r.get("side"), "regime": r.get("regime"),
            "action": status, "reason": r.get("why"),
            "reason_bucket": (_safe(lambda r=r: _pi.classify_no_bet_reason(r.get("why")), "unknown")
                              if status == "no_bet" else "bet"),
        })
    return JSONResponse({"paper_only": True, "generated_at": _now_iso_(), "candidates": out})


def _settled_count(db_path=None):
    import sqlite3 as _sq
    try:
        conn = _sq.connect(DB_PATH)
        try:
            n = conn.execute("SELECT COUNT(*) FROM paper_positions WHERE status IN ('settled','closed')").fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        return None


def _now_iso_():
    from harness import status_model as _sm
    return _sm.now_iso()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML, headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})


@app.get("/api/health")
def api_health():
    """Live system-health snapshot — every value is a real HTTP probe, DB row ts, or file mtime."""
    return JSONResponse(_safe(lambda: health.snapshot(), {"error": "health unavailable"}))


@app.get("/health")
def health_alias():
    """FAST liveness probe (the supervisor's startup gate hits this). Must answer in
    well under a second — so NO external HTTP probes / DB reconciliation here (that
    detail lives in /api/health and /api/system/status). Just confirm the app is up
    and the DB opens."""
    db_ok = False
    try:
        import sqlite3
        from harness import wallet
        c = sqlite3.connect(wallet.DB_PATH, timeout=1.0)
        c.execute("SELECT 1")
        c.close()
        db_ok = True
    except Exception:
        pass
    import datetime as _dt
    # Plan 10: ok reflects the DB check this probe actually performs (its docstring promises the
    # DB opens) — never hardcoded green. HTTP stays 200 so the supervisor's liveness gate (which
    # only checks the status code) still sees the web server is up.
    return JSONResponse({
        "ok": bool(db_ok), "service": "dashboard",
        "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "db_ok": db_ok, "version": "polyswarm-harness", "paper_only": True,
    })


@app.get("/api/mirofish/runs")
def api_mirofish_runs(market_id: str = "", limit: int = 25):
    """MiroFish run history with HONEST status: fresh/stale/failed, usable, sim_id, report
    age, post count, question-match, warnings, and whether it was fed to the swarm. So you
    can see when MiroFish was stale/skipped vs actually contributing."""
    try:
        from harness import mirofish_validate as mfv
        from harness import mirofish_status as mfs
        runs = _safe(lambda: mfv.get_runs(market_id or None, limit), [])
        for r in runs:
            # Plan 8: two DISTINCT, honest signals.
            #  * mirofish_used = the IMMUTABLE historical fact — was this run fresh, market-
            #    matched, verifiable, terminal-stage, and actually fed to the swarm? Derived
            #    from the same state machine the decision used; never flips after the fact.
            #  * stale_now = is the report STILL fresh right now (re-aged vs current MAX_AGE)?
            #    A separate display signal so a once-used report that has since aged is shown
            #    honestly ("used, now stale") without rewriting the historical contribution.
            r["state"] = mfs.state_from_row(r)
            r["mirofish_used"] = (r["state"] == mfs.FRESH_USED)
            r["fed_to_swarm"] = r["mirofish_used"]
            r["stale_now"] = mfs.is_stale_now(r)
            r["label"] = ("FRESH" if (r["mirofish_used"] and not r["stale_now"])
                          else (r.get("freshness_status") or "?").upper())
        used = sum(1 for r in runs if r["mirofish_used"])
        usable = sum(1 for r in runs if r.get("usable"))
        return JSONResponse({"runs": runs, "n": len(runs), "used": used, "usable": usable,
                             "unusable": len(runs) - usable, "config": mfv.config(),
                             "note": "mirofish_used = was this run actually fed to the swarm "
                                     "(fresh, market-matched, verifiable, completed). It is a "
                                     "historical fact and never flips. stale_now = whether the "
                                     "report is still fresh right now. Backend liveness is NOT a "
                                     "contribution."})
    except Exception as e:
        return JSONResponse({"error": f"mirofish runs unavailable: {e}", "runs": []})


@app.get("/api/db/reconciliation")
def api_db_reconciliation():
    """Wallet↔ledger reconciliation (expected vs actual cash/realized + deltas) so you can
    see whether Gate 2's number is trustworthy. Read-only (never repairs)."""
    try:
        from harness import db_check
        return JSONResponse(db_check.ledger_reconciliation_report())
    except Exception as e:
        return JSONResponse({"error": f"reconciliation unavailable: {e}"})


@app.get("/api/clv/summary")
def api_clv_summary():
    """Closing-line-value: at-resolution CLV + timed 15m/1h/6h snapshots + per-theme.
    Positive = we found good entries (price drifted toward us). Read-only."""
    try:
        from harness import clv
        return JSONResponse({
            "at_resolution": _safe(lambda: clv.mean_clv(min_n=1), None),
            "by_theme": _safe(lambda: clv.clv_by_theme(min_n=1), {}),
            "timed_snapshots": _safe(lambda: clv.clv_snapshot_summary(min_n=1), {}),
        })
    except Exception as e:
        return JSONResponse({"error": f"clv unavailable: {e}"})


@app.get("/api/brain/status")
def api_brain_status():
    """Which reasoning brain is configured (swarm/mock/disabled/manus) + its health.
    The LLM is a replaceable provider; the system runs observe-only if it's unavailable."""
    try:
        from harness import brain
        return JSONResponse(brain.status())
    except Exception as e:
        return JSONResponse({"error": f"brain status unavailable: {e}"})


@app.get("/api/system/status")
def api_system_status():
    """Phase 15 — the supervisor/system status (same data as `supervisor status`):
    every service, running/stopped, pid, health, restart count, log path. Read-only."""
    try:
        from harness import supervisor
        return JSONResponse(supervisor.status(as_dict=True))
    except Exception as e:
        return JSONResponse({"error": f"supervisor status unavailable: {e}", "services": {}})


@app.get("/decisions/recent")
def decisions_recent(limit: int = 50):
    """Recent paper-trade decisions (bets + skips with reasons) — audit #14."""
    return JSONResponse({"decisions": _safe(lambda: journal.get_decisions(limit), [])})


@app.get("/errors")
def errors_recent(limit: int = 50):
    """Recent obs error events (market_id / stage / exception) for triage — audit #14."""
    rows = []
    try:
        from harness.obs import explain as _explain
        events = _safe(lambda: _explain._load_events()[0], [])
        for ev in events:
            if isinstance(ev, dict) and ev.get("event") == "error":
                ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
                rows.append({"ts": ev.get("ts"), "where": ev.get("where"),
                             "error": ev.get("error"), "action": ev.get("action"),
                             "market_id": ctx.get("market_id")})
    except Exception as e:
        return JSONResponse({"error": f"errors unavailable: {e}", "errors": []})
    return JSONResponse({"errors": rows[-limit:]})


@app.get("/debug")
def debug_state():
    """Effective config + guard tunables + daemon heartbeats + DB summary — audit #14.
    Read-only, secret-free (never prints API keys)."""
    out = {"trading": "PAPER (real-money execution disabled)"}
    out["config"] = {
        "provider": os.getenv("LLM_PROVIDER", "ollama"),
        "model_fast": os.getenv("MODEL_FAST", "(default)"),
        "dashboard_port": int(os.getenv("DASH_PORT", "8800")),
        "db": _safe(lambda: __import__("harness.wallet", fromlist=["DB_PATH"]).DB_PATH, "?"),
    }
    out["guards"] = _safe(lambda: __import__("harness.provenance", fromlist=["config_snapshot"]).config_snapshot(), {})
    out["health"] = _safe(lambda: health.snapshot(), {})
    out["db_check"] = _safe(lambda: __import__("harness.db_check", fromlist=["run"]).run(), {})
    out["version"] = _safe(lambda: __import__("harness.status_model", fromlist=["version_info"]).version_info(), {})
    out["paper_only"] = True
    return JSONResponse(out)


MF_BACKEND = os.getenv("MIROFISH_BASE", "http://localhost:5001")


@app.get("/api/mirofish_graph")
def api_mirofish_graph(graph_id: str = ""):
    """Proxy the REAL MiroFish knowledge graph from the :5001 backend (entities + relations).
    No fabrication — returns exactly what MiroFish extracted into Zep, normalized for the canvas.
    Default: the newest project graph that actually has nodes."""
    name = graph_id
    try:
        with httpx.Client(timeout=8) as cl:
            if not graph_id:
                projs = cl.get(f"{MF_BACKEND}/api/graph/project/list").json().get("data", [])
                if not projs:
                    return JSONResponse({"available": False, "reason": "no projects"})
                gd = None
                for p in projs:                                   # newest project with >0 nodes
                    gid = p.get("graph_id")
                    if not gid:
                        continue
                    try:
                        cand = cl.get(f"{MF_BACKEND}/api/graph/data/{gid}").json().get("data", {})
                    except Exception:
                        continue
                    if cand.get("node_count", 0) > 0:
                        graph_id, name, gd = gid, p.get("name", gid), cand
                        break
                if gd is None:
                    return JSONResponse({"available": False, "reason": "graphs are empty"})
            else:
                gd = cl.get(f"{MF_BACKEND}/api/graph/data/{graph_id}").json().get("data", {})
    except Exception as e:
        return JSONResponse({"available": False, "reason": str(e)[:140]})
    nodes = [{"id": n.get("uuid"), "name": n.get("name"),
              "type": ((n.get("labels") or ["Entity"])[0] or "Entity"),
              "summary": n.get("summary"), "attributes": n.get("attributes", {}),
              "created_at": n.get("created_at"), "labels": n.get("labels", [])}
             for n in gd.get("nodes", [])]
    edges = [{"source": e.get("source_node_uuid"), "target": e.get("target_node_uuid"),
              "type": e.get("name") or e.get("fact_type"), "fact": e.get("fact")}
             for e in gd.get("edges", []) if e.get("source_node_uuid") and e.get("target_node_uuid")]
    return JSONResponse({"available": True, "graph_id": gd.get("graph_id", graph_id), "name": name,
                         "node_count": gd.get("node_count", len(nodes)),
                         "edge_count": gd.get("edge_count", len(edges)),
                         "nodes": nodes, "edges": edges})


# MiroFish writes its report sections to <repo-parent>/MiroFish/backend/uploads/reports/<id>/section_*.md
MF_REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                              "MiroFish", "backend", "uploads", "reports")


@app.get("/api/mirofish_report")
def api_mirofish_report(report_id: str = ""):
    """Proxy MiroFish's REAL written report from the :5001 backend. Uses markdown_content when the
    report is complete; otherwise assembles the on-disk section_*.md files so a partial report
    (the agent stalled mid-way) is still shown. No fabrication."""
    try:
        with httpx.Client(timeout=8) as cl:
            if not report_id:
                lst = cl.get(f"{MF_BACKEND}/api/report/list").json().get("data", [])
                items = lst if isinstance(lst, list) else lst.get("reports", lst.get("items", []))
                if not items:
                    return JSONResponse({"available": False, "reason": "no reports"})
                items = sorted(items, key=lambda r: (1 if r.get("status") in ("done", "completed") else 0,
                                                      r.get("created_at", "")), reverse=True)
                report_id = items[0].get("report_id")
            rep = cl.get(f"{MF_BACKEND}/api/report/{report_id}").json().get("data", {})
            try:
                prog = cl.get(f"{MF_BACKEND}/api/report/{report_id}/progress").json().get("data", {})
            except Exception:
                prog = {}
    except Exception as e:
        return JSONResponse({"available": False, "reason": str(e)[:140]})
    md = (rep.get("markdown_content") or "").strip()
    if not md:                                            # incomplete -> assemble on-disk sections
        outline = rep.get("outline") or {}
        parts = []
        if outline.get("title"):
            parts.append(f"# {outline['title']}")
        for fp in sorted(glob.glob(os.path.join(MF_REPORTS_DIR, report_id, "section_*.md"))):
            try:
                with open(fp, encoding="utf-8") as fh:
                    parts.append(fh.read().strip())
            except Exception:
                pass
        md = "\n\n".join([p for p in parts if p])
    return JSONResponse({
        "available": True, "report_id": report_id, "simulation_id": rep.get("simulation_id"),
        "status": rep.get("status"), "progress": prog.get("progress"), "message": prog.get("message"),
        "sections_done": len(prog.get("completed_sections", []) or []),
        "sections_total": len((rep.get("outline") or {}).get("sections", []) or []),
        "requirement": rep.get("simulation_requirement"),
        "markdown": md or "_(report has no content yet — the crowd-sim is still gathering)_",
    })


@app.get("/api/stream")
async def stream():
    """Server-Sent-Events tail of the daemon's live console — watch the swarm + MiroFish
    gather data and the LLM work the numbers, line by line, in real time."""
    async def gen():
        waited = 0
        while not os.path.exists(STREAM_LOG) and waited < 600:
            yield "data: " + json.dumps("… waiting for the daemon to start writing — run: python -m harness.sameday daemon") + "\n\n"
            await asyncio.sleep(2); waited += 2
        try:
            f = open(STREAM_LOG, "r", encoding="utf-8", errors="replace")
        except Exception as e:  # pragma: no cover
            yield "data: " + json.dumps(f"(live stream unavailable: {e})") + "\n\n"
            return
        # Open ~6 KB from the end so the panel starts with recent context, not all history.
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 6000))
        f.readline()  # discard the partial first line
        idle = 0
        try:
            while True:
                line = f.readline()
                if line:
                    idle = 0
                    yield "data: " + json.dumps(line.rstrip("\n")) + "\n\n"
                else:
                    idle += 1
                    if idle % 30 == 0:
                        yield ": keepalive\n\n"   # SSE comment keeps the socket warm through proxies
                    await asyncio.sleep(0.4)
        finally:
            f.close()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


def _bet_candidates(limit: int = 40) -> list[dict]:
    """The pool the AI scouts for NEW bets: live same-day markets we DON'T already hold.
    Each carries the current price, hours-to-resolve, and the favorite-longshot rule's
    suggested side/edge — so the live panel shows real bet-HUNTING, not held positions."""
    try:
        from datetime import datetime, timezone
        from harness import gamma, wallet, strategy
        held = {p["market_id"] for p in wallet.get_open_positions()}
        ms = gamma.fetch_markets_ending_within(36, limit=150)
    except Exception:
        return []

    def _hours(ed):
        if not ed:
            return None
        try:
            dt = datetime.fromisoformat(str(ed).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except Exception:
            return None

    out = []
    for m in ms:
        mid = m.get("market_id")
        if not mid or mid in held:
            continue
        price = gamma.yes_price(m)
        if price is None:
            continue
        hrs = _hours(m.get("end_date"))
        if hrs is not None and hrs < 0.3:
            continue
        d = strategy.decide_bet(price)
        out.append({"market_id": mid, "question": m.get("question") or "",
                    "price": round(price, 4), "side": d.side,
                    "edge": round(abs(d.edge), 4) if d.side else 0.0, "hours": hrs})
    # real bet candidates (the rule fires) first, then soonest-resolving
    out.sort(key=lambda c: (c["side"] is None, c["hours"] if c["hours"] is not None else 1e9))
    return out[:limit]


def _held_ids() -> set[str]:
    """market_ids we currently hold an open paper position on — checked live so the LLM
    scout never shows a market the daemon has bet since the candidate pool was built."""
    conn = sqlite3.connect(DB_PATH)
    try:
        ids = {r[0] for r in conn.execute("SELECT market_id FROM paper_positions WHERE status='open'")}
    except sqlite3.OperationalError:
        ids = set()
    conn.close()
    return ids


@app.websocket("/ws/llm")
async def ws_llm(ws: WebSocket):
    """Real WebSocket: the local LLM HUNTS for new bets — it scans live same-day markets we
    don't already hold and streams a bet/skip decision for each, token by token."""
    await ws.accept()
    seen: set[str] = set()     # markets already analyzed this session — NEVER re-asked
    try:
        while True:
            # only GENUINELY NEW markets: not yet analyzed, and not already in the book
            held = await asyncio.to_thread(_held_ids)
            fresh = [c for c in (await asyncio.to_thread(_bet_candidates, 40))
                     if c["market_id"] not in seen and c["market_id"] not in held]
            if not fresh:
                # nothing new -> STAY PUT (keep the last analysis on screen), idle, re-check
                # every 20s. We do NOT re-ask markets we've already analyzed.
                await ws.send_json({"t": "idle", "msg": "all current same-day markets analyzed — watching for new ones"})
                await asyncio.sleep(20)
                continue
            c = fresh[0]; seen.add(c["market_id"])
            await ws.send_json({"t": "start", "market": c["question"], "model": LLM_LIVE_MODEL,
                                "price": c["price"], "side": c["side"], "edge": c["edge"], "hours": c["hours"]})
            rule = (f"The favorite-longshot price rule flags {c['side']} (edge ~{c['edge'] * 100:.1f}%)."
                    if c["side"] else "The price rule sees no favorite-longshot edge here.")
            hrs = f"{c['hours']:.0f}" if c["hours"] is not None else "?"
            prompt = (
                "You are a prediction-market trader hunting for a NEW bet. Decide whether the market below "
                "is worth betting and which side. Be brief — 2-3 reasons — then end with a line "
                "'VERDICT: BET YES' or 'BET NO' or 'SKIP', plus your probability.\n\n"
                f"MARKET: {c['question']}\n"
                f"Current YES price: {c['price']:.0%}  |  resolves in ~{hrs}h\n"
                f"{rule}\n\nDECISION:")
            payload = {"model": LLM_LIVE_MODEL, "prompt": prompt, "stream": True,
                       "options": {"temperature": 0.7, "num_predict": 240}}
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    async with client.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as resp:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except Exception:
                                continue
                            tok = obj.get("response", "")
                            if tok:
                                await ws.send_json({"t": "tok", "v": tok})
                            if obj.get("done"):
                                break
                await ws.send_json({"t": "done"})
            except Exception as e:  # ollama hiccup — report and keep the socket alive
                await ws.send_json({"t": "meta", "msg": f"(model stream paused: {e})"})
            await asyncio.sleep(7)  # breathe, then analyze the next market
    except WebSocketDisconnect:
        return
    except Exception:
        return


def main():
    import uvicorn
    port = int(os.getenv("DASH_PORT", "8800"))
    print(f"\n  Polymarket harness dashboard -> http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>POLYMARKET AI · COMMAND CENTER</title>
<style>
:root{--bg:#06060c;--bg2:#0b0b16;--panel:rgba(14,14,26,.72);--ink:#e8e8ff;--dim:#6a6a85;
--mag:#ff2bd1;--grn:#00ffa3;--amb:#ffd23f;--red:#ff4d5e;--cyan:#39e1ff;--line:rgba(255,43,209,.18)}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 60% -10%,#150a22 0,#06060c 60%),var(--bg);
color:var(--ink);font:13px/1.45 'Segoe UI',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.mono{font-family:'Cascadia Code',Consolas,'Courier New',monospace}
a{color:var(--cyan);text-decoration:none}a:hover{text-decoration:underline}
.wrap{padding:10px 14px;max-width:1700px;margin:0 auto}
.glow{border:1px solid var(--line);border-radius:10px;background:var(--panel);
box-shadow:0 0 0 1px rgba(0,0,0,.4),0 0 22px -8px rgba(255,43,209,.35);backdrop-filter:blur(4px)}
.lab{font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.big{font-size:30px;font-weight:700;letter-spacing:.5px}
.huge{font-size:40px;font-weight:800;letter-spacing:.5px}
.grn{color:var(--grn)}.mag{color:var(--mag)}.amb{color:var(--amb)}.red{color:var(--red)}.cyan{color:var(--cyan)}.dim{color:var(--dim)}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;letter-spacing:.08em;
border:1px solid currentColor;font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap}
.topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 14px;margin-bottom:10px}
.topbar .sp{flex:1}
.kpi{padding:14px 16px;min-width:230px;flex:1}
.kpi .r{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px dashed rgba(255,255,255,.05)}
.kpi .r b{font-variant-numeric:tabular-nums}
.col{display:flex;flex-direction:column;gap:10px}
.sec{padding:12px 14px}
h2.t{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--mag);margin:0 0 8px}
.grid3{display:grid;grid-template-columns:1.15fr 1.7fr 1.15fr;gap:10px}
.grid2{display:grid;grid-template-columns:1.6fr 1fr;gap:10px}
@media(max-width:1100px){.grid3,.grid2{grid-template-columns:1fr}}
canvas{display:block;width:100%}
.stream{height:330px;overflow:auto;font-size:11.5px}
.ev{padding:3px 6px;border-left:2px solid var(--dim);margin:2px 0;background:rgba(255,255,255,.02)}
.ev .ts{color:var(--dim);font-size:10px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px}
.tab{padding:2px 9px;border-radius:6px;border:1px solid var(--line);cursor:pointer;font-size:10.5px;color:var(--dim)}
.tab.on{color:var(--mag);border-color:var(--mag);box-shadow:0 0 10px -4px var(--mag)}
.bet{padding:11px 13px;margin-bottom:9px}
.bet .q{font-weight:600;font-size:13px}
.bet .g{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}
.gp{font-size:9px;padding:1px 6px;border-radius:5px;border:1px solid var(--grn);color:var(--grn)}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:7px}
.metrics div{background:rgba(255,255,255,.03);border-radius:6px;padding:5px 7px}
.metrics .lab{font-size:8.5px}.metrics b{font-size:14px;font-variant-numeric:tabular-nums}
.timer{font-family:'Cascadia Code',monospace;font-size:18px;font-weight:700;letter-spacing:1px}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th,td{text-align:left;padding:4px 7px;border-bottom:1px solid rgba(255,255,255,.05)}
th{color:var(--dim);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase}
.term{height:200px;overflow:auto;background:#04040a;border-radius:8px;padding:8px;font-size:11px}
.term .l{white-space:pre-wrap;word-break:break-word}
.proof{font-size:11px;margin-top:6px;display:none}
.proof .p{padding:2px 6px;border-left:2px solid var(--dim);margin:1px 0}
.dotpulse{animation:pulse 1.4s infinite}@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
.empty{color:var(--dim);font-style:italic;padding:8px}
.smallbtn{font-size:9.5px;padding:1px 7px;border:1px solid var(--line);border-radius:5px;background:none;color:var(--cyan);cursor:pointer}
</style></head><body><div class=wrap>

<div class="topbar glow">
  <span class="big mag">◆ POLYMARKET&nbsp;<span class=ink>AI</span></span>
  <span class="lab">COMMAND CENTER</span>
  <span id=ver class="lab mono">branch · commit</span>
  <span class="badge grn" id=paperbadge>PAPER-ONLY</span>
  <span class="badge" id=sysbadge>SYSTEM unknown</span>
  <span class="sp"></span>
  <span class="lab">DB</span><span id=dbstat class=mono>unknown</span>
  <span class="lab">STREAM</span><span id=sse class=mono>connecting…</span>
  <span class="lab">LAST EVT</span><span id=evage class=mono>—</span>
  <span class="huge mono" id=clock style="font-size:20px">--:--:--</span>
</div>

<!-- HERO -->
<div class="row" style="margin-bottom:10px">
  <div class="kpi glow"><div class=lab>◢ PAPER WALLET</div>
    <div class="huge" id=equity>—</div>
    <div class="lab" id=eqver>equity unknown</div>
    <div style="margin-top:8px" class=mono>
      <div class=r><span class=dim>Starting</span><b id=w_start>—</b></div>
      <div class=r><span class=dim>Cash</span><b id=w_cash>—</b></div>
      <div class=r><span class=dim>Realized PnL</span><b id=w_real>—</b></div>
      <div class=r><span class=dim>Unrealized PnL</span><b id=w_unreal>—</b></div>
      <div class=r><span class=dim>Total PnL</span><b id=w_total>—</b></div>
      <div class=r><span class=dim>Open exposure</span><b id=w_exp>—</b></div>
      <div class=r><span class=dim>Open / Settled</span><b id=w_counts>—</b></div>
      <div class=r><span class=dim>Accounting</span><b id=w_acct>—</b></div>
    </div>
  </div>
  <div class="kpi glow"><div class=lab>◢ P&amp;L / READINESS</div>
    <div class="huge" id=totalpnl>—</div>
    <div class="lab">total paper PnL</div>
    <div style="margin-top:8px" class=mono>
      <div class=r><span class=dim>Realized</span><b id=p_real>—</b></div>
      <div class=r><span class=dim>Unrealized</span><b id=p_unreal>—</b></div>
      <div class=r><span class=dim>Max drawdown</span><b id=p_dd>—</b></div>
      <div class=r><span class=dim>CLV avg</span><b id=p_clv>—</b></div>
      <div class=r><span class=dim>Gate 2 readiness</span><b id=p_gate2>—</b></div>
      <div class=r><span class=dim>Sample</span><b id=p_sample>—</b></div>
    </div>
  </div>
  <div class="kpi glow"><div class=lab>◢ SAFETY GATES</div>
    <div class=mono style="margin-top:4px">
      <div class=r><span class=dim>System truth</span><b id=s_sys>—</b></div>
      <div class=r><span class=dim>Accounting</span><b id=s_acct>—</b></div>
      <div class=r><span class=dim>Gate 1 / Gate 2</span><b id=s_gates>—</b></div>
      <div class=r><span class=dim>MiroFish</span><b id=s_mf>—</b></div>
      <div class=r><span class=dim>Swarm/daemons</span><b id=s_daemon>—</b></div>
      <div class=r><span class=dim>Parser</span><b id=s_parser>strict (Plan 7)</b></div>
      <div class=r><span class=dim>Event safety</span><b id=s_event>multi-leg off</b></div>
    </div>
    <div class=lab style="margin-top:6px">verified, never faked · unknown ≠ green</div>
  </div>
</div>

<!-- AI NOW + CHARTS -->
<div class="grid2" style="margin-bottom:10px">
  <div class="sec glow"><h2 class=t>◢ PnL / Equity Curve</h2>
    <canvas id=curve height=170></canvas>
    <div class=lab id=curvestate>—</div>
  </div>
  <div class="sec glow"><h2 class=t>◢ What is the AI doing now?</h2>
    <div class="big" id=ai_state>—</div>
    <div class=mono style="margin-top:6px;font-size:11.5px">
      <div class=r><span class=dim>Market</span><b id=ai_mkt>—</b></div>
      <div class=r><span class=dim>Agent</span><b id=ai_agent>—</b></div>
      <div class=r><span class=dim>MiroFish</span><b id=ai_mf>—</b></div>
      <div class=r><span class=dim>Last gate</span><b id=ai_gate>—</b></div>
      <div class=r><span class=dim>Last no-bet</span><b id=ai_nobet>—</b></div>
      <div class=r><span class=dim>Heartbeat</span><b id=ai_hb>—</b></div>
      <div class=r><span class=dim>Last event age</span><b id=ai_age>—</b></div>
    </div>
  </div>
</div>

<!-- SWARM MAP + STREAM -->
<div class="grid2" style="margin-bottom:10px">
  <div class="sec glow"><h2 class=t>◢ AI Swarm Map <span class=lab>(nodes glow on REAL events)</span></h2>
    <canvas id=swarm height=300></canvas>
  </div>
  <div class="sec glow"><h2 class=t>◢ Live AI Stream</h2>
    <div class=tabs id=tabs></div>
    <div class="stream mono" id=stream><div class=empty>waiting for live events…</div></div>
  </div>
</div>

<!-- ACTIVE BETS -->
<div class="sec glow" style="margin-bottom:10px"><h2 class=t>◢ Active Paper Bets <span class=lab id=openct></span></h2>
  <div id=openbets><div class=empty>no open paper bets</div></div>
</div>

<!-- SETTLED + CANDIDATES -->
<div class="grid2" style="margin-bottom:10px">
  <div class="sec glow"><h2 class=t>◢ Recent Settled Bets</h2>
    <table><thead><tr><th>Market</th><th>Side</th><th>Entry</th><th>Out</th><th>Stake</th><th>Payout</th><th>PnL</th></tr></thead>
    <tbody id=settled><tr><td colspan=7 class=empty>none</td></tr></tbody></table>
  </div>
  <div class="sec glow"><h2 class=t>◢ Markets Evaluated (bet / no-bet)</h2>
    <div id=cands><div class=empty>none</div></div>
  </div>
</div>

<!-- PROOF PANEL -->
<div class="sec glow" style="margin-bottom:10px"><h2 class=t>◢ Proof This Is Working</h2>
  <div class="row mono" style="font-size:11px">
    <div class=col style="flex:1;min-width:240px">
      <div class=r><span class=dim>Stream state</span><b id=pr_sse>—</b></div>
      <div class=r><span class=dim>Last event id</span><b id=pr_eid>—</b></div>
      <div class=r><span class=dim>Last event age</span><b id=pr_eage>—</b></div>
      <div class=r><span class=dim>Clients</span><b id=pr_clients>—</b></div>
    </div>
    <div class=col style="flex:1;min-width:240px">
      <div class=r><span class=dim>ai_pipeline HB</span><b id=pr_hb1>—</b></div>
      <div class=r><span class=dim>sameday HB</span><b id=pr_hb2>—</b></div>
      <div class=r><span class=dim>MiroFish state</span><b id=pr_mf>—</b></div>
      <div class=r><span class=dim>Last decision</span><b id=pr_dec>—</b></div>
    </div>
    <div class=col style="flex:1;min-width:240px">
      <div class=r><span class=dim>Branch / commit</span><b id=pr_ver>—</b></div>
      <div class=r><span class=dim>Dirty tree</span><b id=pr_dirty>—</b></div>
      <div class=r><span class=dim>Paper-only</span><b class=grn>true</b></div>
      <div class=r><span class=dim>DB usable</span><b id=pr_db>—</b></div>
    </div>
  </div>
</div>

<!-- TERMINAL -->
<div class="sec glow"><h2 class=t>◢ Execution Log · Live <span class=lab id=replaytag></span></h2>
  <div class=tabs><span class=lab>filter:</span>
    <span class="tab on" data-f="">all</span><span class=tab data-f=decision>decisions</span>
    <span class=tab data-f=gate>gates</span><span class=tab data-f=agent>agents</span>
    <span class=tab data-f=error>errors</span></div>
  <div class="term mono" id=term><div class=empty>no events yet — start the AI daemons to see live activity</div></div>
</div>
</div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const ND='—';
function money(v){if(v===null||v===undefined||v==='')return ND;const n=+v;if(!isFinite(n))return ND;
  return (n<0?'-$':'$')+Math.abs(n).toFixed(2);}
function num(v,d=2){if(v===null||v===undefined||v==='')return ND;const n=+v;return isFinite(n)?n.toFixed(d):ND;}
function pct(v){if(v===null||v===undefined)return ND;const n=+v;return isFinite(n)?(n*100).toFixed(1)+'%':ND;}
function sgnColor(v){if(v===null||v===undefined||!isFinite(+v))return 'var(--dim)';return +v>0?'var(--grn)':(+v<0?'var(--red)':'var(--ink)');}
// honesty: ONLY explicit ok/healthy/pass map to green. unknown/stale/etc never green.
function stateColor(s){s=(s||'').toLowerCase();
  if(['ok','healthy','pass','passed','connected','won','running','finished'].includes(s))return 'var(--grn)';
  if(['degraded','stale','idle','partial','awaiting_settlement','warning','learning'].includes(s))return 'var(--amb)';
  if(['unsafe','fail','failed','blocked','error','crashed','lost','disconnected','invalid_timer'].includes(s))return 'var(--red)';
  return 'var(--dim)';}
function setBadge(el,txt,st){el.textContent=txt;el.style.color=stateColor(st);el.style.borderColor=stateColor(st);}
async function getj(u){try{const r=await fetch(u,{cache:'no-store'});if(!r.ok)return null;return await r.json();}catch(e){return null;}}

setInterval(()=>{$('#clock').textContent=new Date().toLocaleTimeString();},1000);

/* ---------- swarm map (data-backed: nodes glow from real event types) ---------- */
const SW=$('#swarm'),sx=SW.getContext('2d');
const NODES=[['LLM·1','agent',.12,.2],['LLM·2','agent',.12,.5],['LLM·3','agent',.12,.8],
 ['SWARM','swarm',.34,.5],['CHALLENGER','challenger',.34,.18],['EVIDENCE','evidence',.34,.82],
 ['MIROFISH','mirofish',.56,.3],['GATES','gate',.56,.7],['WALLET','wallet',.78,.4],
 ['ACCOUNTING','accounting',.78,.7],['JOURNAL','journal',.93,.55]];
const EDGES=[[0,3],[1,3],[2,3],[4,3],[5,3],[3,6],[3,7],[6,7],[7,8],[8,9],[8,10],[6,8]];
const heat={}; // source/type -> last-active ts
function nodeColor(k){const t=heat[k];if(!t)return 'var(--dim)';const a=(Date.now()-t)/1000;
  if(a<3)return 'var(--mag)';if(a<20)return 'var(--grn)';if(a<120)return 'var(--amb)';return 'var(--dim)';}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()||v;}
function drawSwarm(){const w=SW.width=SW.clientWidth,h=SW.height=300;sx.clearRect(0,0,w,h);
  EDGES.forEach(([a,b])=>{const A=NODES[a],B=NODES[b];const hot=Math.max(heatAge(A[1]),heatAge(B[1]));
    sx.strokeStyle=hot<2?css('--mag'):'rgba(255,43,209,.10)';sx.lineWidth=hot<2?1.6:.7;
    sx.beginPath();sx.moveTo(A[2]*w,A[3]*h);sx.lineTo(B[2]*w,B[3]*h);sx.stroke();});
  NODES.forEach(n=>{const x=n[2]*w,y=n[3]*h,c=mapColor(nodeColor(n[1]));
    const age=heatAge(n[1]);const r=age<3?11:7;
    sx.beginPath();sx.arc(x,y,r,0,7);sx.fillStyle=c;sx.shadowColor=c;sx.shadowBlur=age<20?16:0;sx.fill();sx.shadowBlur=0;
    sx.fillStyle=css('--dim');sx.font='9px monospace';sx.fillText(n[0],x+13,y+3);});}
function heatAge(k){const t=heat[k];return t?(Date.now()-t)/1000:9999;}
function mapColor(v){return v.startsWith('var(')?css(v.slice(4,-1)):v;}
function touch(k){heat[k]=Date.now();}
setInterval(drawSwarm,700);window.addEventListener('resize',drawSwarm);

/* ---------- equity / pnl curve ---------- */
async function drawCurve(){const d=await getj('/api/pnl-curve');const c=$('#curve'),g=c.getContext('2d');
  const w=c.width=c.clientWidth,h=c.height=170;g.clearRect(0,0,w,h);
  if(!d||!d.points||d.points.length<2){$('#curvestate').textContent=(d&&d.warnings&&d.warnings[0])||'not_enough_paper_wallet_history';return;}
  $('#curvestate').textContent='state: '+d.state+(d.state==='unverified'?' · marks unverified (Plan 9)':'');
  const pts=d.points,eq=pts.map(p=>p.equity).filter(v=>v!=null);
  if(eq.length<2){$('#curvestate').textContent='not_enough_data';return;}
  const mn=Math.min(...eq),mx=Math.max(...eq),sp=(mx-mn)||1;
  function line(key,col){g.beginPath();let started=false;pts.forEach((p,i)=>{const v=p[key];if(v==null)return;
    const x=i/(pts.length-1)*(w-8)+4,y=h-6-((v-mn)/sp)*(h-16);if(!started){g.moveTo(x,y);started=true;}else g.lineTo(x,y);});
    g.strokeStyle=col;g.lineWidth=1.8;g.shadowColor=col;g.shadowBlur=8;g.stroke();g.shadowBlur=0;}
  line('cash',css('--cyan'));line('equity',d.state==='ok'?css('--grn'):css('--amb'));}

/* ---------- countdown timers (client-side, per second) ---------- */
const timers={}; // position_id -> {endMs, status}
function fmtCountdown(secs){if(secs===null||secs===undefined)return '—';if(secs<=0)return 'ENDED';
  const d=Math.floor(secs/86400),h=Math.floor(secs%86400/3600),m=Math.floor(secs%3600/60),s=Math.floor(secs%60);
  return (d?d+'d ':'')+String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');}
setInterval(()=>{Object.entries(timers).forEach(([id,t])=>{const el=document.getElementById('tm_'+id);if(!el)return;
  if(t.status==='timer_unknown'){el.textContent='timer_unknown';el.style.color='var(--dim)';return;}
  if(t.status==='invalid_timer'){el.textContent='invalid_timer';el.style.color='var(--red)';return;}
  if(t.endMs===null){el.textContent='timer_unknown';el.style.color='var(--dim)';return;}
  const secs=Math.floor((t.endMs-Date.now())/1000);
  if(secs<=0){el.textContent='AWAITING SETTLEMENT';el.style.color='var(--red)';return;}
  el.textContent=fmtCountdown(secs);el.style.color=secs<86400?'var(--amb)':'var(--ink)';});},1000);

/* ---------- active bets ---------- */
async function renderBets(){const d=await getj('/api/paper-bets/open');const box=$('#openbets');
  const ps=(d&&d.positions)||[];$('#openct').textContent='· '+ps.length+' open';
  if(!ps.length){box.innerHTML='<div class=empty>no open paper bets</div>';return;}
  box.innerHTML=ps.map(p=>{const pid=p.position_id;
    timers[pid]={endMs:p.end_time?Date.parse(p.end_time):(p.seconds_until_end!=null?Date.now()+p.seconds_until_end*1000:null),status:p.timer_status};
    const link=p.url_status==='ok'&&p.url?`<a href="${p.url}" target=_blank rel=noopener>open on Polymarket ↗</a>`:`<span class=red>link_unavailable</span>`;
    const payout=p.payout_reason==='payout_ok';
    const gates=(p.gates_passed||[]).map(g=>`<span class=gp>${g}</span>`).join('');
    return `<div class="bet glow"><div style="display:flex;justify-content:space-between;gap:10px">
      <div style="flex:1"><div class=q>${(p.question||'?')}</div>
        <div class=lab>${p.market_id||''} · ${link}</div></div>
      <div style="text-align:right"><span class="badge" style="color:${p.side==='YES'?'var(--grn)':'var(--mag)'}">${p.side||'?'}</span>
        <div class=timer id="tm_${pid}">—</div></div></div>
      <div class=metrics>
        <div><div class=lab>Entry</div><b>${num(p.entry_price,3)}</b></div>
        <div><div class=lab>Stake</div><b>${money(p.stake)}</b></div>
        <div><div class=lab>Payout if win</div><b class=grn>${payout?money(p.possible_payout_if_win):'payout_unknown'}</b></div>
        <div><div class=lab>Profit if win</div><b class=grn>${payout?money(p.possible_profit_if_win):ND}</b></div>
        <div><div class=lab>Max loss</div><b class=red>${payout?money(p.max_loss):ND}</b></div>
        <div><div class=lab>Unrealized</div><b style="color:${sgnColor(p.unrealized_pnl)}">${p.unrealized_pnl==null?'unknown':money(p.unrealized_pnl)}</b></div>
        <div><div class=lab>Forecast</div><b>${pct(p.forecast_probability)}</b></div>
        <div><div class=lab>MiroFish</div><b style="color:${stateColor(p.mirofish_state)}">${p.mirofish_state||'unknown'}</b></div>
      </div>
      <div class=g>${gates}</div>
      <div class=lab style="margin-top:5px">AI reason: <span class=ink>${(p.ai_reason||'unknown')}</span></div>
      <button class=smallbtn onclick="proof('${pid}')">▸ proof timeline</button>
      <div class=proof id="proof_${pid}"></div></div>`;}).join('');}

async function proof(pid){const el=$('#proof_'+pid);if(el.style.display==='block'){el.style.display='none';return;}
  el.style.display='block';el.innerHTML='loading…';const d=await getj('/api/paper-bets/proof?position_id='+pid);
  if(!d){el.innerHTML='<span class=red>proof unavailable</span>';return;}
  el.innerHTML='<div class=lab>proof: '+d.proof_status+'</div>'+(d.timeline||[]).map(t=>
    `<div class=p style="border-color:${stateColor(t.status)}"><span style="color:${stateColor(t.status)}">${t.status}</span> · ${t.step} <span class=dim>(${t.source})</span> — ${t.message||''}</div>`).join('');}

/* ---------- settled + candidates ---------- */
async function renderSettled(){const d=await getj('/api/paper-bets/settled');const tb=$('#settled');
  const ps=(d&&d.positions)||[];if(!ps.length){tb.innerHTML='<tr><td colspan=7 class=empty>none</td></tr>';return;}
  tb.innerHTML=ps.map(p=>{const link=p.url_status==='ok'&&p.url?`<a href="${p.url}" target=_blank rel=noopener>${(p.question||'?').substring(0,42)} ↗</a>`:(p.question||'?').substring(0,42)+' <span class=red>(link_unavailable)</span>';
    return `<tr><td>${link}</td><td>${p.side||'?'}</td><td>${num(p.entry_price,3)}</td>
    <td style="color:${stateColor(p.outcome)}">${p.outcome||'unknown'}</td><td>${money(p.stake)}</td>
    <td>${money(p.payout)}</td><td style="color:${sgnColor(p.realized_pnl)}">${money(p.realized_pnl)}</td></tr>`;}).join('');}

async function renderCands(){const d=await getj('/api/candidates/recent');const box=$('#cands');
  const cs=(d&&d.candidates)||[];if(!cs.length){box.innerHTML='<div class=empty>none</div>';return;}
  box.innerHTML=cs.map(c=>{const bet=c.action==='bet';
    return `<div style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)">
      <span class="badge" style="color:${bet?'var(--grn)':'var(--amb)'};font-size:9px">${bet?'BET':'NO-BET · '+(c.reason_bucket||'')}</span>
      <span style="font-size:11.5px"> ${(c.question||'?').substring(0,58)}</span>
      <div class=lab>fc ${pct(c.forecast_probability)} · mkt ${pct(c.market_price)} · ${(c.reason||'').substring(0,80)}</div></div>`;}).join('');}

/* ---------- wallet / pnl / safety / proof / ai-now ---------- */
async function tick(){
  const [w,pnl,truth,ver,live,ai,gates,mf,hl]=await Promise.all([
    getj('/api/paper-wallet'),getj('/api/pnl'),getj('/api/truth'),getj('/api/version'),
    getj('/api/live/status'),getj('/api/ai-now'),getj('/api/gates'),getj('/api/mirofish'),getj('/api/health')]);
  if(w){$('#equity').textContent=w.equity_verified?money(w.verified_equity):'unverified';
    $('#equity').style.color=w.equity_verified?'var(--grn)':'var(--amb)';
    $('#eqver').textContent=w.equity_verified?'equity VERIFIED (Plan 9)':'equity unverified · '+(w.accounting_status||'unknown');
    $('#w_start').textContent=money(w.starting_bankroll);$('#w_cash').textContent=money(w.cash);
    $('#w_real').textContent=money(w.realized_pnl);$('#w_real').style.color=sgnColor(w.realized_pnl);
    $('#w_unreal').textContent=w.unrealized_pnl==null?'unverified':money(w.unrealized_pnl);
    $('#w_total').textContent=money(w.total_pnl);$('#w_total').style.color=sgnColor(w.total_pnl);
    $('#w_exp').textContent=money(w.open_exposure);
    $('#w_counts').textContent=(w.open_positions_count??'?')+' / '+(w.settled_positions_count??'?');
    $('#w_acct').textContent=w.accounting_status||'unknown';$('#w_acct').style.color=stateColor(w.accounting_status);}
  if(pnl){$('#totalpnl').textContent=money(pnl.total_pnl);$('#totalpnl').style.color=sgnColor(pnl.total_pnl);
    $('#p_real').textContent=money(pnl.realized_pnl);$('#p_unreal').textContent=pnl.unrealized_pnl==null?'unverified':money(pnl.unrealized_pnl);}
  if(w){$('#p_dd').textContent=w.max_drawdown==null?ND:money(w.max_drawdown);
    $('#p_gate2').textContent=(w.gate2_status||'unknown');$('#p_gate2').style.color=stateColor(w.gate2_pass?'pass':w.gate2_status);
    $('#p_sample').textContent=(w.settled_positions_count??'?')+' settled';}
  const clv=await getj('/api/clv/summary');if(clv){const c=clv.mean_clv||clv.overall||(clv.summary&&clv.summary.mean_clv);
    $('#p_clv').textContent=(c&&c.mean_clv!=null)?num(c.mean_clv,3):(typeof c==='number'?num(c,3):'insufficient');}
  if(truth){setBadge($('#sysbadge'),'SYSTEM '+(truth.state||'unknown'),truth.state);
    $('#s_sys').textContent=truth.state||'unknown';$('#s_sys').style.color=stateColor(truth.state);
    $('#s_acct').textContent=truth.accounting_status||'unknown';$('#s_acct').style.color=stateColor(truth.accounting_status);}
  if(gates){const g2=gates.gate2||{};$('#s_gates').textContent=((gates.gate1&&gates.gate1.pass)?'G1✓':'G1✗')+' / '+(g2.pass?'G2 PASS':'G2 '+(g2.status||'unknown'));
    $('#s_gates').style.color=stateColor(g2.pass?'pass':(g2.status||'unknown'));}
  if(mf){$('#s_mf').textContent=(mf.used>0?('used×'+mf.used):'not used')+(mf.backend_alive?' · alive':'');$('#s_mf').style.color=stateColor(mf.used>0?'ok':'idle');
    $('#pr_mf').textContent=mf.used>0?('used×'+mf.used):'not used';}
  if(hl){const dm=hl.daemons||{};const a=dm.ai_pipeline||{},s=dm.sameday_daemon||{};
    $('#s_daemon').textContent=(a.state||'unknown')+' / '+(s.state||'unknown');$('#s_daemon').style.color=stateColor(a.state);
    $('#pr_hb1').textContent=a.state||'unknown';$('#pr_hb1').style.color=stateColor(a.state);
    $('#pr_hb2').textContent=s.state||'unknown';$('#pr_hb2').style.color=stateColor(s.state);
    $('#dbstat').textContent=hl.db_ok===false?'unusable':(hl.db_ok===true?'ok':'unknown');$('#dbstat').style.color=hl.db_ok?'var(--grn)':'var(--red)';
    $('#pr_db').textContent=$('#dbstat').textContent;}
  if(ver){const v=ver.git_branch?(ver.git_branch+' · '+(ver.git_commit||'').substring(0,7)):'unknown';
    $('#ver').textContent=v;$('#pr_ver').textContent=v;$('#pr_dirty').textContent=ver.git_dirty==null?'unknown':(ver.git_dirty?'DIRTY':'clean');
    $('#pr_dirty').style.color=ver.git_dirty?'var(--amb)':'var(--grn)';}
  if(ai){$('#ai_state').textContent=(ai.state||'unknown').toUpperCase();$('#ai_state').style.color=stateColor(ai.state==='thinking'||ai.state==='evaluating'?'running':ai.state);
    $('#ai_mkt').textContent=(ai.current_question||ai.current_market_id||'—');$('#ai_agent').textContent=ai.current_agent||'—';
    $('#ai_mf').textContent=ai.mirofish_state||'unknown';$('#ai_gate').textContent=ai.last_gate_result||'—';
    $('#ai_nobet').textContent=ai.last_no_bet_reason||'—';$('#ai_hb').textContent=ai.last_heartbeat_stage||'unknown';
    $('#ai_age').textContent=ai.last_event_age_seconds==null?'no events':(ai.last_event_age_seconds.toFixed(0)+'s ago');
    $('#pr_dec').textContent=ai.last_no_bet_reason||ai.last_bet_position_id||'—';}
  if(live){setBadge($('#sysbadge'),$('#sysbadge').textContent,truth?truth.state:'unknown');
    $('#pr_eid').textContent=live.last_event_id||'none';$('#pr_clients').textContent=live.client_count??'?';
    $('#pr_sse').textContent=live.state||'unknown';$('#pr_sse').style.color=stateColor(live.state);
    $('#pr_eage').textContent=live.last_event_age_seconds==null?'—':live.last_event_age_seconds.toFixed(0)+'s';
    $('#evage').textContent=live.last_event_age_seconds==null?'no events':live.last_event_age_seconds.toFixed(0)+'s';
    $('#evage').style.color=stateColor(live.state);}
  drawCurve();renderBets();renderSettled();renderCands();
}

/* ---------- SSE live stream ---------- */
let curTab='', curFilter='';
const TABS=['ALL','LLM','MIROFISH','SWARM','GATES','DECISIONS','WALLET','ERRORS'];
const TABMAP={ALL:null,LLM:['agent.started','agent.finished','agent.token','agent.parse_failed'],
 MIROFISH:['mirofish.stage','mirofish.state'],SWARM:['swarm.started','swarm.vote','swarm.degraded','forecast.final'],
 GATES:['gate.result'],DECISIONS:['decision.bet','decision.no_bet','candidate.ranked','position.opened','position.settled'],
 WALLET:['wallet.update','position.opened','position.settled','pnl.tick'],ERRORS:['error','agent.parse_failed']};
$('#tabs').innerHTML=TABS.map((t,i)=>`<span class="tab ${i===0?'on':''}" data-t="${t}">${t}</span>`).join('');
$$('#tabs .tab').forEach(el=>el.onclick=()=>{$$('#tabs .tab').forEach(x=>x.classList.remove('on'));el.classList.add('on');curTab=el.dataset.t==='ALL'?'':el.dataset.t;renderStream();});
$$('.tabs .tab[data-f]').forEach(el=>el.onclick=()=>{$$('.tabs .tab[data-f]').forEach(x=>x.classList.remove('on'));el.classList.add('on');curFilter=el.dataset.f;renderTerm();});
let EVTS=[];
const NODEOF={agent:'agent',swarm:'swarm',challenger:'challenger',mirofish:'mirofish',gate:'gate',
 wallet:'wallet',accounting:'accounting',predict_today:'journal',profit_intel:'journal',system:'journal'};
function ingest(e){EVTS.unshift(e);if(EVTS.length>600)EVTS.pop();
  const n=NODEOF[e.source]||(e.type&&e.type.split('.')[0]);if(n)touch(n);
  if(e.type==='evidence.pack')touch('evidence');if(e.type&&e.type.startsWith('position'))touch('wallet');
  renderStream();renderTerm();}
function evRow(e){const c=stateColor(e.status);const rp=e.replay?'<span class=amb>[replay]</span> ':'';
  return `<div class=ev style="border-color:${c}"><span class=ts>${(e.ts||'').substring(11,19)}</span>
   <span style="color:${c}">${e.type}</span> <span class=dim>${e.source}</span> ${rp}${(e.message||'')}
   ${e.question?'<span class=dim>· '+e.question.substring(0,40)+'</span>':''}</div>`;}
function renderStream(){const f=curTab?TABMAP[curTab]:null;const evs=EVTS.filter(e=>!f||f.includes(e.type)).slice(0,120);
  $('#stream').innerHTML=evs.length?evs.map(evRow).join(''):'<div class=empty>no events for this tab</div>';}
function renderTerm(){const evs=EVTS.filter(e=>!curFilter||(e.type||'').includes(curFilter)||(e.source||'')===curFilter).slice(0,200);
  $('#term').innerHTML=evs.length?evs.map(e=>`<div class=l><span class=dim>${(e.ts||'').substring(11,23)}</span> <span style="color:${stateColor(e.status)}">${(e.type||'').padEnd(16)}</span> ${e.source} · ${e.message||''} ${e.replay?'[replay]':''}</div>`).join(''):'<div class=empty>no events yet — start the AI daemons to see live activity</div>';
  $('#replaytag').textContent=EVTS.some(e=>e.replay)?'· includes replay':'· live';}
function connectSSE(){let es;try{es=new EventSource('/events/live');}catch(e){$('#sse').textContent='unsupported';return;}
  es.onopen=()=>{$('#sse').textContent='● connected';$('#sse').style.color='var(--grn)';};
  es.onmessage=ev=>{try{const e=JSON.parse(ev.data);if(e&&e.type)ingest(e);}catch(_){}};
  es.onerror=()=>{$('#sse').textContent='● reconnecting…';$('#sse').style.color='var(--amb)';};}

connectSSE();tick();setInterval(tick,3000);drawSwarm();
</script></body></html>"""


if __name__ == "__main__":
    main()
