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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
    sb = scoreboard.compute()
    snaps = journal.get_snapshots(500)
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
            "n": sb["n"], "n_required": sb["n_required"],
            "model_brier": sb["model_brier"], "market_brier": sb["market_brier"],
            "baseline_brier": sb.get("baseline_brier"), "baseline_n": sb.get("baseline_n", 0),
            "gate1": sb["gate1"]["pass"], "gate2": sb["gate2"]["pass"],
            "themes": sb["themes"],
        },
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
    return JSONResponse({
        "ok": True, "service": "dashboard",
        "time": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "db_ok": db_ok, "version": "polyswarm-harness",
    })


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
<title>Polymarket Harness — Live Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#070b16;--panel:#0d1322;--panel2:#0a0f1c;--bd:#1b2540;--tx:#e6ebf5;--dim:#7c89a8;
--purple:#8b5cf6;--cyan:#22d3ee;--green:#34d399;--amber:#f59e0b;--pink:#f472b6;--red:#f4475e;--blue:#3b82f6}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#10183020,transparent),var(--bg);
color:var(--tx);font:14px/1.45 'Segoe UI',Inter,system-ui,sans-serif;padding:18px 22px 40px}
h1{font-size:19px;font-weight:700;letter-spacing:.3px;margin:0 0 2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;
box-shadow:0 0 8px var(--green);animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.grid{display:grid;gap:14px}
.cards{grid-template-columns:repeat(6,1fr)}
.main{grid-template-columns:2.2fr 1fr;margin-top:14px}
.row2{grid-template-columns:1.3fr 1fr;margin-top:14px}
.panel{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--bd);
border-radius:10px;padding:14px 16px}
.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--bd);
border-radius:10px;padding:12px 14px;position:relative;overflow:hidden}
.card .lab{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
.card .big{font-size:26px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
.card .delta{font-size:12px;margin-top:3px;color:var(--dim)}
.card .accent{position:absolute;left:0;top:0;height:3px;width:100%}
.ttl{font-size:12px;color:#aab6d6;text-transform:uppercase;letter-spacing:.7px;margin-bottom:10px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid var(--bd);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
td{padding:7px 8px;border-bottom:1px solid #131a2e;font-variant-numeric:tabular-nums}
tr:hover td{background:#0f1830}
.q{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.yes{background:#0d2e22;color:var(--green)}.no{background:#2e1418;color:var(--red)}
.pos{color:var(--green)}.neg{color:var(--red)}
.gauge{margin-bottom:14px}
.gauge .gl{display:flex;justify-content:space-between;font-size:12px;margin-bottom:5px}
.gbar{height:12px;border-radius:7px;background:#121a30;position:relative;overflow:hidden}
.gfill{height:100%;border-radius:7px}
.gmark{position:absolute;top:-3px;width:2px;height:18px;background:#fff;box-shadow:0 0 6px #fff}
.txn{max-height:340px;overflow:auto}
.tx{border-left:3px solid var(--bd);padding:8px 12px;margin-bottom:8px;background:#0b1120;border-radius:0 8px 8px 0}
.tx.bet{border-color:var(--cyan)}.tx.nobet{border-color:#33405e;opacity:.8}.tx.rejected{border-color:var(--amber)}
.tx .h{display:flex;justify-content:space-between;gap:10px;font-size:12.5px}
.tx .why{color:#c6cfe6;font-size:12px;margin-top:4px}
.tx .meta{color:var(--dim);font-size:11px;margin-top:3px}
.mono{font-variant-numeric:tabular-nums}
.bartrack{display:inline-block;width:90px;height:8px;border-radius:5px;background:#121a30;vertical-align:middle;position:relative;margin:0 6px}
.barf{position:absolute;left:0;top:0;height:100%;border-radius:5px}
.legend{font-size:11px;color:var(--dim);margin-left:6px}
a{color:var(--cyan)}
.stream{height:320px;overflow:auto;background:#04070e;border:1px solid var(--bd);border-radius:8px;
padding:10px 12px;margin-top:4px;font:12px/1.5 'Cascadia Code',Consolas,ui-monospace,monospace;white-space:pre}
.stream .ln{display:block;color:#9fb0d8}
.stream .ln.head{color:#e6ebf5;font-weight:700}
.stream .ln.swarm{color:#c4b5fd}
.stream .ln.data{color:#fbbf24}
.stream .ln.fish{color:#67e8f9}
.stream .ln.bet{color:#34d399;font-weight:600}
.stream .ln.dim{color:#566184}
.livedot{float:right;font-size:11px;color:var(--dim);font-weight:600}
.health{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 14px}
.hbadge{display:inline-flex;align-items:center;gap:6px;background:linear-gradient(180deg,var(--panel),var(--panel2));
border:1px solid var(--bd);border-radius:20px;padding:5px 11px;font-size:11.5px;color:var(--tx);font-weight:600}
.hbadge .hdot{width:8px;height:8px;border-radius:50%}
.hbadge .hsub{color:var(--dim);font-size:10.5px;font-weight:500;margin-left:1px}
.netwrap{position:relative;overflow:hidden}
#net{width:100%;height:340px;display:block;border-radius:8px;border:1px solid var(--bd);
background:radial-gradient(700px 280px at 50% 42%,#0a1330,transparent),#05080f}
.llm{height:340px;overflow:auto;background:#05080f;border:1px solid var(--bd);border-radius:8px;
padding:10px 12px;font:12.5px/1.55 'Cascadia Code',Consolas,ui-monospace,monospace}
.llm .llmq{color:var(--cyan);font-weight:700;margin-bottom:8px;border-bottom:1px solid var(--bd);
padding-bottom:6px;white-space:normal;word-break:break-word}
.llm .llmbody{color:#cdd6ee;white-space:pre-wrap;word-break:break-word}
.cursor{display:inline-block;width:7px;height:14px;background:var(--green);margin-left:1px;
vertical-align:-2px;box-shadow:0 0 8px var(--green);animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.mfgwrap{position:relative;height:380px;border:1px solid var(--bd);border-radius:8px;overflow:hidden;
background:radial-gradient(700px 320px at 40% 35%,#0a1330,transparent),#05080f}
#mfgraph{width:100%;height:100%;display:block;cursor:grab}
.mfgbtn{float:right;background:var(--panel);color:var(--cyan);border:1px solid var(--bd);border-radius:6px;
font-size:11px;padding:3px 9px;cursor:pointer;margin-left:8px}
.mfgbtn:hover{border-color:var(--cyan)}
.mfglegend{position:absolute;left:10px;bottom:10px;background:#0b1120cc;border:1px solid var(--bd);
border-radius:8px;padding:8px 10px;font-size:10.5px;max-width:48%;backdrop-filter:blur(4px)}
.mfglegend .lghdr{color:#aab6d6;font-weight:700;text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:4px;font-size:10px}
.mfglegend .lgrow{display:inline-flex;align-items:center;gap:5px;margin:2px 8px 2px 0;color:var(--dim)}
.mfglegend .lgdot{width:8px;height:8px;border-radius:50%}
.mfgdetails{position:absolute;right:10px;top:10px;width:300px;max-height:92%;overflow:auto;display:none;
background:#0b1120f2;border:1px solid var(--cyan);border-radius:10px;padding:12px 14px;box-shadow:0 8px 30px #000a;backdrop-filter:blur(4px)}
.mfgdetails.show{display:block}
.mfgdetails .dh{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px}
.mfgdetails .dh b{font-size:13px}
.mfgdetails .dx{cursor:pointer;color:var(--dim);font-size:14px}
.mfgdetails .drow{font-size:11.5px;margin:5px 0;color:#cdd6ee}
.mfgdetails .dk{color:var(--dim);display:inline-block;min-width:78px}
.mfgdetails .dmono{font-family:Consolas,monospace;font-size:10.5px;color:#9fb0d8;word-break:break-all}
.mfgdetails .dsum{font-size:11.5px;color:#c6cfe6;margin-top:4px;line-height:1.5}
.mfgtag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10.5px;font-weight:600;margin:2px 4px 0 0}
.mfrprog{height:6px;background:#121a30;border-radius:5px;overflow:hidden;margin:4px 0 10px}
.mfrprog .bar{height:100%;background:linear-gradient(90deg,#22d3ee,#34d399);border-radius:5px;transition:width .4s}
.mfrbody{max-height:420px;overflow:auto;font-size:13px;line-height:1.6;color:#cdd6ee;padding:2px 6px}
.mfrbody h1{font-size:17px;color:#e6ebf5;margin:6px 0}
.mfrbody h2{font-size:14px;color:var(--cyan);margin:14px 0 6px;border-bottom:1px solid var(--bd);padding-bottom:4px}
.mfrbody h3{font-size:13px;color:#aab6d6;margin:10px 0 4px}
.mfrbody blockquote{border-left:3px solid var(--purple);margin:8px 0;padding:4px 12px;color:#9fb0d8;background:#0b1120;border-radius:0 6px 6px 0;font-style:italic}
.mfrbody strong{color:#e6ebf5} .mfrbody li{margin:3px 0} .mfrbody p{margin:7px 0}
</style></head><body>
<h1><span class=dot></span>Polymarket Harness — Live Paper-Trading Monitor</h1>
<div class=sub id=sub>paper only · $0 local swarm (qwen2.5:7b) · loading…</div>

<div class=ttl style="margin-bottom:6px">System health <span class=legend>(every badge is a live probe · green &lt;15m · amber 15–60m · red down/&gt;60m)</span></div>
<div class="health" id=health><span class=hbadge><span class=hdot style="background:var(--dim)"></span>probing…</span></div>

<div id=nextbet style="margin:8px 0 16px;padding:14px 18px;background:linear-gradient(90deg,#11224d,#0d1322);border:1px solid var(--cyan);border-radius:12px;box-shadow:0 0 24px #22d3ee22">
  <span style="color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.7px">⏱ Next bet resolves in</span>
  <span id=nextcd class=mono style="font-size:32px;font-weight:800;color:var(--cyan);margin:0 14px;text-shadow:0 0 14px #22d3ee88">—</span>
  <span id=nextq style="color:#aab6d6"></span>
</div>

<div class="grid cards" id=cards></div>

<div class="grid" style="grid-template-columns:1.5fr 1fr;gap:14px;margin-top:14px">
  <div class="panel netwrap">
    <div class=ttl>🧠 Agent network — live
      <span class=legend>nodes flare as the swarm gathers data &amp; the agents reason</span>
      <span id=netstate class=livedot>● idle</span></div>
    <canvas id=net></canvas>
  </div>
  <div class=panel>
    <div class=ttl>🤖 LLM live — watch it think
      <span class=legend>real token stream over WebSocket</span>
      <span id=llmdot class=livedot>● connecting…</span></div>
    <div id=llmlive class=llm><div class=llmq></div><div class=llmbody></div></div>
  </div>
</div>

<div class="grid main">
  <div class=panel><div class=ttl>Equity &amp; realized P&amp;L</div><canvas id=pnl height=120></canvas></div>
  <div class=panel><div class=ttl>The two gates (vs market bar 0.0627)</div><div id=gauges></div></div>
</div>

<div class="grid" style="margin-top:14px"><div class=panel>
  <div class=ttl>🛰 Live agent feed — watch the swarm &amp; MiroFish gather data and reason
    <span class=legend>(streaming the daemon's console in real time)</span>
    <span id=streamdot class=livedot>● connecting…</span></div>
  <div id=stream class=stream></div>
</div></div>

<div class="grid" style="margin-top:14px">
  <div class=panel><div class=ttl>What it's betting on — open paper positions  <span class=legend>(click a market to open it on Polymarket ↗ · "⏳ awaiting result" = game/market in progress, NOT a loss)</span></div>
    <table id=postbl><thead><tr><th>Market</th><th>Side</th><th>Model</th><th>Mkt</th><th>Edge</th><th>Stake</th><th>Fill</th><th style="color:var(--green)">If win</th><th style="color:var(--cyan)">⏱ Resolves in</th></tr></thead><tbody></tbody></table></div>
</div>

<div class="grid" style="margin-top:14px">
  <div class=panel><div class=ttl>Settled bets — what we won / lost  <span class=legend id=closedsum></span></div>
    <table id=closedtbl><thead><tr><th>Market</th><th>Side</th><th>Stake</th><th>Result</th><th>Won / Lost</th><th>When</th></tr></thead><tbody></tbody></table></div>
</div>

<div class="grid" style="margin-top:14px">
  <div class=panel><div class=ttl>Challenger A/B — swarm vs <span id=ablegend class=legend>single-LLM</span> vs market</div>
    <table id=abtbl><thead><tr><th>Market</th><th>Swarm</th><th>1-LLM</th><th>Market</th></tr></thead><tbody></tbody></table></div>
</div>

<div class="grid" style="margin-top:14px"><div class=panel>
  <div class=ttl>🐟 MiroFish — knowledge graph
    <span class=legend>entities &amp; relations the crowd-sim extracted · real data from :5001 (click a node)</span>
    <button id=mfgrefresh class=mfgbtn>⟳ refresh</button>
    <span id=mfgmeta class=livedot>—</span></div>
  <div class=mfgwrap>
    <canvas id=mfgraph></canvas>
    <div id=mfglegend class=mfglegend></div>
    <div id=mfgdetails class=mfgdetails></div>
  </div>
  <div id=mirofish style="margin-top:10px"></div>
</div></div>

<div class="grid" style="margin-top:14px"><div class=panel>
  <div class=ttl>🐟 MiroFish report — the crowd-sim's written analysis
    <span class=legend>generated by MiroFish's report agent (:5001)</span>
    <button id=mfrrefresh class=mfgbtn>⟳ refresh</button>
    <span id=mfrmeta class=livedot>—</span></div>
  <div id=mfrprog class=mfrprog></div>
  <div id=mfrbody class=mfrbody></div>
</div></div>

<div class="grid" style="margin-top:14px"><div class=panel><div class=ttl>Decision transcript — why it's betting</div><div class=txn id=txn></div></div></div>

<script>
const $=s=>document.querySelector(s);
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const pct=v=>v==null?'–':(v*100).toFixed(1)+'%';
const b4=v=>v==null?'–':v.toFixed(4);
// profit if an open bet resolves in our favor: each share pays $1, minus stake + fee
function winIfHits(p){
  const profit=(p.shares||0)-(p.stake||0)-(p.fee||0), pay=(p.shares||0);
  const c=profit>=0?'pos':'neg';
  return `<span class="${c}" style="font-weight:700">${profit>=0?'+':''}${money(profit)}</span>`
       + `<span style="color:var(--dim);font-size:11px"> → ${money(pay)}</span>`;
}
let chart;
function card(lab,big,delta,color){return `<div class=card><div class=accent style="background:${color}"></div>
<div class=lab>${lab}</div><div class=big>${big}</div><div class=delta>${delta||''}</div></div>`}
function gauge(label,val,disp,max,bar,goodLow){
  const p=Math.max(0,Math.min(1,val/max));
  const col = goodLow ? (val<=bar?'var(--green)':'var(--red)') : (val>=bar?'var(--green)':'var(--amber)');
  const markP=Math.max(0,Math.min(1,bar/max));
  return `<div class=gauge><div class=gl><span>${label}</span><span class=mono>${disp}</span></div>
  <div class=gbar><div class=gfill style="width:${p*100}%;background:${col}"></div>
  <div class=gmark style="left:${markP*100}%"></div></div></div>`;
}
function abbar(p,col){const w=Math.max(2,(p||0)*100);return `<span class=bartrack><span class=barf style="width:${w}%;background:${col}"></span></span>`}
function fmtCountdown(end){
  if(!end) return '—';
  const ms=new Date(end).getTime()-Date.now();
  if(isNaN(ms)) return '—';
  if(ms<=0) return '⏳ awaiting result';
  const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),ss=s%60;
  const pad=n=>n<10?'0'+n:n;
  return (d?d+'d ':'')+pad(h)+':'+pad(m)+':'+pad(ss);
}
function tickCountdowns(){
  if(window._soon){
    const ms=window._soon.t-Date.now();
    const el=document.getElementById('nextcd');
    if(el){ el.textContent = ms>0 ? fmtCountdown(new Date(window._soon.t).toISOString()) : '⏳ awaiting result';
            el.style.color = ms>0 && ms<3600000 ? 'var(--amber)' : 'var(--cyan)'; }
  }
  document.querySelectorAll('.cd').forEach(el=>{
    const e=el.getAttribute('data-end'); el.textContent=fmtCountdown(e);
    const ms=e?new Date(e).getTime()-Date.now():NaN;
    // past end-date but still open = game/market in progress, not a loss -> amber, not red
    el.style.color = isNaN(ms)?'' : (ms<=0?'var(--amber)' : ms<3600000?'var(--amber)' : 'var(--green)');
    el.style.fontWeight = (!isNaN(ms)&&ms>0&&ms<3600000)?'700':'';
  });
}
setInterval(tickCountdowns, 1000);

async function tick(){
  let d; try{ d=await (await fetch('/api/state')).json(); }catch(e){ return; }
  const w=d.wallet, sb=d.scoreboard, c=d.counts;
  const pnlCol = w.realized_pnl>=0?'var(--green)':'var(--red)';
  $('#sub').innerHTML=`paper only · $0 local swarm (qwen2.5:7b) · ${c.bets} bets placed · ${c.forecasts} forecasts · updated ${new Date().toLocaleTimeString()}`;
  $('#ablegend').textContent = (d.challenger_hosted?'🟢 ':'')+(d.challenger_model||'single-LLM')+(d.challenger_hosted?' (live)':' (local)');
  $('#cards').innerHTML=[
    card('Cash', money(w.cash), 'available to bet','var(--cyan)'),
    card('Equity', money(w.equity), 'cash + open (at cost)','var(--purple)'),
    card('Realized P&amp;L', money(w.realized_pnl), `start ${money(w.starting_bankroll)}`, pnlCol),
    card('Open positions', w.n_open, money(w.open_exposure)+' exposure','var(--amber)'),
    card('Forecasts', c.forecasts, `${c.resolved} resolved`,'var(--blue)'),
    card('Gates', (sb.gate1?'1✓':'1✗')+' '+(sb.gate2?'2✓':'2✗'), `n=${sb.n}/${sb.n_required}`, (sb.gate1&&sb.gate2)?'var(--green)':'var(--pink)'),
  ].join('');

  $('#gauges').innerHTML =
    gauge('GATE 1 · swarm Brier (lower=better)', sb.model_brier==null?0.25:sb.model_brier,
          sb.model_brier==null?'no data':b4(sb.model_brier)+' vs bar '+d.bar, 0.25, d.bar, true)
   + gauge('Resolved opinion markets', sb.n, sb.n+' / '+sb.n_required, sb.n_required, sb.n_required, false)
   + gauge('GATE 2 · paper realized P&L', Math.max(0,w.realized_pnl), money(w.realized_pnl), Math.max(50,Math.abs(w.realized_pnl)*1.2||50), 0.0001, false)
   + `<div class=legend style="margin-top:8px">single-LLM A/B Brier: <b>${b4(sb.baseline_brier)}</b> (n=${sb.baseline_n}) · white tick = market bar to beat</div>`;

  const pb=$('#postbl tbody'); pb.innerHTML = (d.positions||[]).map(p=>`<tr>
    <td class=q title="${(p.question||'').replace(/"/g,'')}">${p.event_slug?`<a href="https://polymarket.com/event/${p.event_slug}" target="_blank" rel="noopener">${p.question||''} ↗</a>`:(p.question||'')}</td>
    <td><span class="pill ${p.side=='YES'?'yes':'no'}">${p.side}</span></td>
    <td class=mono>${pct(p.model_p)}</td><td class=mono>${pct(p.market_p)}</td>
    <td class="mono ${p.edge>=0?'pos':'neg'}">${(p.edge*100>=0?'+':'')+(p.edge*100).toFixed(1)}%</td>
    <td class=mono>${money(p.stake)}</td><td class=mono>${(p.fill_price||0).toFixed(3)}</td>
    <td class=mono>${winIfHits(p)}</td>
    <td class="mono cd" data-end="${p.end_date||''}">${fmtCountdown(p.end_date)}</td></tr>`).join('')
    || '<tr><td colspan=9 style="color:var(--dim)">no open positions</td></tr>';

  // settled / closed bets — explicit win/loss per bet
  const cl=d.closed||[];
  let nWon=0,nLost=0,net=0;
  cl.forEach(p=>{ const r=p.realized_pnl||0; net+=r; if(r>=0)nWon++; else nLost++; });
  $('#closedsum').innerHTML = cl.length
    ? `<span class=pos>${nWon} won</span> · <span class=neg>${nLost} lost</span> · net <b style="color:${net>=0?'var(--green)':'var(--red)'}">${money(net)}</b>`
    : '';
  $('#closedtbl tbody').innerHTML = cl.map(p=>{
    const r=p.realized_pnl||0, win=r>=0;
    const when = p.settled_at ? new Date(p.settled_at.replace(' ','T')+(/[zZ]|[+\-]\d\d:?\d\d$/.test(p.settled_at)?'':'Z')).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
    return `<tr>
      <td class=q title="${(p.question||'').replace(/"/g,'')}">${p.event_slug?`<a href="https://polymarket.com/event/${p.event_slug}" target="_blank" rel="noopener">${p.question||''} ↗</a>`:(p.question||'')}</td>
      <td><span class="pill ${p.side=='YES'?'yes':'no'}">${p.side}</span></td>
      <td class=mono>${money(p.stake)}</td>
      <td><span class="pill ${win?'yes':'no'}">${win?'WON':'LOST'}${p.status=='closed'?' (cashed out)':''}</span></td>
      <td class="mono ${win?'pos':'neg'}" style="font-weight:700">${(r>=0?'+':'')+money(r)}</td>
      <td class=mono style="color:var(--dim)">${when}</td></tr>`;
  }).join('') || '<tr><td colspan=6 style="color:var(--dim)">no settled bets yet — each bet shows here with its win/loss once it resolves or is cashed out</td></tr>';

  // soonest-resolving open bet -> big banner at the top
  let soon=null;
  (d.positions||[]).forEach(p=>{ if(p.end_date){ const t=new Date(p.end_date).getTime(); if(t>Date.now() && (!soon||t<soon.t)) soon={t:t,q:p.question||'',side:p.side,stake:p.stake,win:(p.shares||0)-(p.stake||0)-(p.fee||0)}; }});
  window._soon=soon;
  $('#nextq').innerHTML = soon ? `${soon.side} $${(soon.stake||0).toFixed(0)} · wins <b style="color:var(--green)">+${money(soon.win)}</b> if it hits · ${soon.q.substring(0,60)}` : '(no open bets)';

  const ab=$('#abtbl tbody'); ab.innerHTML=(d.ab||[]).map(r=>`<tr>
    <td class=q title="${(r.question||'').replace(/"/g,'')}">${r.question||''}</td>
    <td class=mono>${abbar(r.swarm_p,'var(--purple)')}${pct(r.swarm_p)}</td>
    <td class=mono>${abbar(r.llm_p,'var(--cyan)')}${pct(r.llm_p)}</td>
    <td class=mono>${abbar(r.market_p,'var(--green)')}${pct(r.market_p)}</td></tr>`).join('')
    || '<tr><td colspan=4 style="color:var(--dim)">no A/B data yet</td></tr>';

  $('#txn').innerHTML=(d.decisions||[]).map(x=>`<div class="tx ${x.status=='bet'?'bet':x.status=='rejected'?'rejected':'nobet'}">
    <div class=h><span class=q title="${(x.question||'').replace(/"/g,'')}">${x.question||''}</span>
    <span>${x.status=='bet'?`<span class="pill ${x.side=='YES'?'yes':'no'}">${x.side} ${money(x.stake)}</span>`:`<span style="color:var(--dim)">${x.status}</span>`}</span></div>
    <div class=why>${x.why||''}</div>
    <div class=meta>${x.ts?new Date(x.ts).toLocaleString():''} ${x.regime?'· regime '+x.regime:''} ${x.signal?'· '+x.signal:''}</div></div>`).join('')
    || '<div style="color:var(--dim)">no decisions logged yet — they appear as the loop forecasts</div>';

  $('#mirofish').innerHTML=(d.mirofish||[]).map(mf=>`<div class=tx style="border-color:#22d3ee">
    <div class=h><span class=q title="${(mf.question||'').replace(/"/g,'')}">${mf.question||''}</span>
    <span class=mono>crowd ${pct(mf.crowd_probability)}${mf.market_odds!=null?' · mkt '+pct(mf.market_odds):''} · ${mf.n_posts||0} posts</span></div>
    ${(mf.posts||[]).slice(0,3).map(p=>`<div class=meta style="color:#9fb0d8">🗣 ${(p||'').substring(0,150)}</div>`).join('')}</div>`).join('')
    || '<div style="color:var(--dim)">no MiroFish crowd simulations yet — they appear after a sim generates posts</div>';

  const s=d.pnl_series||[];
  const labels=s.map(p=>new Date(p.ts).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
  const eq=s.map(p=>p.equity), rp=s.map(p=>p.realized_pnl), cash=s.map(p=>p.cash);
  if(!chart){
    chart=new Chart($('#pnl'),{type:'line',data:{labels,datasets:[
      {label:'Equity',data:eq,borderColor:'#8b5cf6',backgroundColor:'#8b5cf622',tension:.25,fill:true,pointRadius:0,borderWidth:2},
      {label:'Cash',data:cash,borderColor:'#22d3ee',tension:.25,pointRadius:0,borderWidth:1.5},
      {label:'Realized P&L',data:rp,borderColor:'#34d399',tension:.25,pointRadius:0,borderWidth:1.5,yAxisID:'y1'},
    ]},options:{responsive:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:'#aab6d6',boxWidth:12}}},
      scales:{x:{ticks:{color:'#5e6b8a',maxTicksLimit:8},grid:{color:'#131a2e'}},
        y:{ticks:{color:'#5e6b8a'},grid:{color:'#131a2e'},title:{display:true,text:'$ equity/cash',color:'#5e6b8a'}},
        y1:{position:'right',ticks:{color:'#34d399'},grid:{display:false},title:{display:true,text:'$ realized',color:'#34d399'}}}}});
  } else {
    chart.data.labels=labels; chart.data.datasets[0].data=eq; chart.data.datasets[1].data=cash; chart.data.datasets[2].data=rp; chart.update('none');
  }
}
tick(); setInterval(tick,5000);

// ── system health strip (every badge is a live probe / real DB ts / real mtime) ──
function freshColor(sec){ if(sec==null) return 'var(--red)'; if(sec<900) return 'var(--green)'; if(sec<3600) return 'var(--amber)'; return 'var(--red)'; }
function ago(sec){ if(sec==null) return 'never'; if(sec<60) return Math.round(sec)+'s ago'; if(sec<3600) return Math.round(sec/60)+'m ago'; if(sec<86400) return Math.round(sec/3600)+'h ago'; return Math.round(sec/86400)+'d ago'; }
function hbadge(label,color,sub){ return `<span class=hbadge><span class=hdot style="background:${color};box-shadow:0 0 6px ${color}"></span>${label}<span class=hsub>${sub}</span></span>`; }
async function healthTick(){
  let h; try{ h=await(await fetch('/api/health')).json(); }catch(e){ return; }
  const o=h.ollama||{}, mf=h.mirofish_backend||{}, d=h.daemon||{}, f=h.freshness_sec||{};
  const out=[];
  out.push(hbadge('Ollama', (o.up&&o.model_present)?'var(--green)':'var(--red)',
     o.up ? (o.model_present ? (o.model||'model')+' ✓' : 'model missing') : 'down'));
  out.push(hbadge('MiroFish', mf.up?'var(--green)':'var(--amber)', mf.up?'external :5001':(mf.mode||'local fallback')));
  out.push(hbadge('Daemon', freshColor(d.age_sec), d.age_sec==null?'no heartbeat':ago(d.age_sec)));
  [['swarm',f.swarm_forecast],['1-LLM',f.challenger],['crowd',f.mirofish],['bet',f.paper_position],['decision',f.decision]]
    .forEach(([lab,sec])=> out.push(hbadge(lab, freshColor(sec), ago(sec))));
  const el=document.getElementById('health'); if(el) el.innerHTML=out.join('');
}
healthTick(); setInterval(healthTick, 5000);

// ── live agent feed (SSE tail of the daemon console) ──────────────────────────
(function(){
  const box=document.getElementById('stream'), dot=document.getElementById('streamdot');
  if(!box) return;
  function cls(t){
    if(/\bBET\b|SHORT —|LONG \(|\bWON\b|\bLOST\b|placed|cashed out/i.test(t)) return 'bet';
    if(/Fetching|sources|Context ready|gather|scouting/i.test(t)) return 'data';
    if(/MiroFish|🐟|crowd|🗣|posts/i.test(t)) return 'fish';
    if(/^[\s]*[┌└│├┤─━┃┏┗┣]/.test(t)||/POLYSWARM|RESULT|FORECAST|═══/.test(t)) return 'head';
    if(/ROUND|Regime|Bayesian|Weighted|Shapley|Consensus|swarm|agent|persona/i.test(t)) return 'swarm';
    if(/not resolved yet|keepalive|waiting for/i.test(t)) return 'dim';
    return '';
  }
  function add(t){
    try{ window.netFeed && window.netFeed(t); }catch(_){}
    const atBottom = box.scrollHeight-box.scrollTop-box.clientHeight < 50;
    const el=document.createElement('span'); el.className='ln '+cls(t); el.textContent=t;
    box.appendChild(el);
    while(box.childNodes.length>600) box.removeChild(box.firstChild);
    if(atBottom) box.scrollTop=box.scrollHeight;
  }
  function connect(){
    const es=new EventSource('/api/stream');
    es.onopen=()=>{ dot.textContent='● live'; dot.style.color='var(--green)'; };
    es.onmessage=e=>{ let t; try{ t=JSON.parse(e.data); }catch(_){ t=e.data; } add(t); };
    es.onerror=()=>{ dot.textContent='● reconnecting…'; dot.style.color='var(--amber)'; }; // EventSource auto-reconnects
  }
  connect();
})();

// ── agent network bubble graph (driven by the live feed) ──────────────────────
(function(){
  const cv=document.getElementById('net'); if(!cv) return;
  const ctx=cv.getContext('2d'); const state=document.getElementById('netstate');
  let W=0,H=0; const DPR=Math.min(window.devicePixelRatio||1,2);
  const NODES=[
    {id:'market',label:'MARKET',col:'#22d3ee',r:16},
    {id:'macro', label:'Macro Analyst',     col:'#8b5cf6',r:9},
    {id:'contra',label:'Contrarian Skeptic',col:'#f472b6',r:9},
    {id:'crypto',label:'Crypto Native',     col:'#34d399',r:9},
    {id:'quant', label:'Quant Trader',      col:'#f59e0b',r:9},
    {id:'retail',label:'Retail',            col:'#60a5fa',r:9},
    {id:'fish',  label:'MiroFish crowd',    col:'#22d3ee',r:11},
    {id:'data',  label:'Data · 23 src',     col:'#fbbf24',r:11},
    {id:'llm',   label:'1-LLM',             col:'#a78bfa',r:8},
  ];
  const byId={}; NODES.forEach(n=>byId[n.id]=n);
  const EDGES=[]; NODES.forEach(n=>{ if(n.id!=='market') EDGES.push(['market',n.id]); });
  ['macro','contra','crypto','quant','retail'].forEach((a,i,A)=>EDGES.push([a,A[(i+1)%A.length]]));
  EDGES.push(['fish','retail'],['data','quant'],['data','macro']);
  const pulses=[];
  function layout(){
    const cx=W/2,cy=H/2,R=Math.min(W,H)*0.34; byId.market.hx=cx; byId.market.hy=cy;
    const ring=NODES.filter(n=>n.id!=='market');
    ring.forEach((n,i)=>{ const a=-Math.PI/2+i/ring.length*6.2832; n.hx=cx+Math.cos(a)*R; n.hy=cy+Math.sin(a)*R; });
    NODES.forEach(n=>{ if(n.x==null){ n.x=n.hx; n.y=n.hy; n.ph=Math.random()*6.28; n.e=0; }});
  }
  function resize(){ const r=cv.getBoundingClientRect(); W=r.width; H=r.height||340;
    cv.width=W*DPR; cv.height=H*DPR; ctx.setTransform(DPR,0,0,DPR,0,0); layout(); }
  function activate(id,power){ const n=byId[id]; if(!n) return;
    n.e=Math.min(1.5,(n.e||0)+(power||0.9)); byId.market.e=Math.min(1.3,(byId.market.e||0)+0.35);
    pulses.push({a:'market',b:id,t:0,col:n.col});
    if(state){ state.textContent='● working'; state.style.color='var(--green)'; window._netBusy=Date.now(); } }
  window.netFeed=function(line){ const t=line||'';
    if(/Macro Analyst/i.test(t)) activate('macro');
    else if(/Contrarian/i.test(t)) activate('contra');
    else if(/Crypto Native/i.test(t)) activate('crypto');
    else if(/Quantitative|Quant/i.test(t)) activate('quant');
    else if(/Retail/i.test(t)) activate('retail');
    if(/MiroFish|🐟|crowd|🗣/i.test(t)) activate('fish');
    if(/Fetching|sources|Context ready|gather|scouting/i.test(t)) activate('data',1.1);
    if(/single-LLM|1-LLM|baseline|challenger/i.test(t)) activate('llm');
    if(/FORECAST|RESULT|ROUND|Bayesian|Weighted|swarm/i.test(t)){ activate('market',0.5);
      ['macro','contra','crypto','quant','retail'].forEach(a=>activate(a,0.45)); }
    if(/\bBET\b|SHORT|LONG \(|placed/i.test(t)) activate('market',0.8);
  };
  let tt=0;
  function frame(){ tt+=0.016; ctx.clearRect(0,0,W,H);
    if(state && window._netBusy && Date.now()-window._netBusy>4000){ state.textContent='● idle'; state.style.color='var(--dim)'; window._netBusy=0; }
    NODES.forEach(n=>{ const tx=n.hx+Math.cos(tt*0.6+n.ph)*6, ty=n.hy+Math.sin(tt*0.5+n.ph)*6;
      n.x+=(tx-n.x)*0.04; n.y+=(ty-n.y)*0.04; n.e=(n.e||0)*0.97; });
    EDGES.forEach(([a,b])=>{ const na=byId[a],nb=byId[b],en=Math.max(na.e||0,nb.e||0);
      ctx.beginPath(); ctx.moveTo(na.x,na.y); ctx.lineTo(nb.x,nb.y);
      ctx.strokeStyle='rgba(120,140,205,'+(0.05+en*0.4)+')'; ctx.lineWidth=0.6+en*1.6; ctx.stroke(); });
    for(let i=pulses.length-1;i>=0;i--){ const p=pulses[i]; p.t+=0.03; const na=byId[p.a],nb=byId[p.b];
      const x=na.x+(nb.x-na.x)*p.t, y=na.y+(nb.y-na.y)*p.t;
      ctx.beginPath(); ctx.arc(x,y,2.6,0,6.28); ctx.fillStyle=p.col; ctx.shadowColor=p.col; ctx.shadowBlur=10; ctx.fill(); ctx.shadowBlur=0;
      if(p.t>=1) pulses.splice(i,1); }
    NODES.forEach(n=>{ const e=n.e||0, rr=n.r*(1+e*0.6);
      const g=ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,rr*3.2);
      g.addColorStop(0,n.col+'cc'); g.addColorStop(0.4,n.col+(e>0.2?'55':'22')); g.addColorStop(1,'transparent');
      ctx.beginPath(); ctx.arc(n.x,n.y,rr*3.2,0,6.28); ctx.fillStyle=g; ctx.fill();
      ctx.beginPath(); ctx.arc(n.x,n.y,rr,0,6.28); ctx.fillStyle=n.col; ctx.shadowColor=n.col; ctx.shadowBlur=8+e*18; ctx.fill(); ctx.shadowBlur=0;
      ctx.font='10px Segoe UI,system-ui'; ctx.textAlign='center'; ctx.fillStyle='rgba(220,228,245,'+(0.42+e*0.55)+')';
      ctx.fillText(n.label,n.x,n.y-rr-5); });
    requestAnimationFrame(frame);
  }
  window.addEventListener('resize',resize); resize(); requestAnimationFrame(frame);
  setInterval(()=>{ if(!window._netBusy){ const ids=['macro','contra','crypto','quant','retail','data','fish']; activate(ids[Math.floor(tt*7)%ids.length],0.22);} },2600);
})();

// ── MiroFish knowledge graph (REAL entities/relations from :5001, force-directed) ──
(function(){
  const cv=document.getElementById('mfgraph'); if(!cv) return;
  const ctx=cv.getContext('2d'); const DPR=Math.min(window.devicePixelRatio||1,2);
  const meta=document.getElementById('mfgmeta'), legend=document.getElementById('mfglegend'), details=document.getElementById('mfgdetails');
  const TYPECOL={ Person:'#f472b6', PersonEntity:'#f472b6', Company:'#34d399', Organization:'#fb923c',
    MediaOutlet:'#f59e0b', GovernmentAgency:'#9ca3af', InvestorInstitution:'#60a5fa', PolicyMaker:'#a78bfa',
    DeveloperCommunity:'#22d3ee', TechExecutive:'#f59e0b', Entity:'#3b82f6', EventEntity:'#eab308' };
  function colorFor(t){ if(TYPECOL[t]) return TYPECOL[t];
    let h=0; for(let i=0;i<(t||'').length;i++) h=(h*31+t.charCodeAt(i))%360; return 'hsl('+h+',65%,60%)'; }
  let W=0,H=0, nodes=[], edges=[], byId={}, sel=null, drag=null;
  function resize(){ const r=cv.getBoundingClientRect(); W=r.width;H=r.height||380;
    cv.width=W*DPR;cv.height=H*DPR;ctx.setTransform(DPR,0,0,DPR,0,0); }
  function layout(){ const cx=W/2,cy=H/2,R=Math.min(W,H)*0.28;
    nodes.forEach((n,i)=>{ const a=i/Math.max(1,nodes.length)*6.2832;
      n.x=cx+Math.cos(a)*R+(i%2?12:-12); n.y=cy+Math.sin(a)*R; n.vx=0;n.vy=0; n.deg=0; });
    edges.forEach(e=>{ if(byId[e.source]) byId[e.source].deg++; if(byId[e.target]) byId[e.target].deg++; }); }
  async function load(){
    let d; try{ d=await(await fetch('/api/mirofish_graph')).json(); }
    catch(e){ if(meta){meta.textContent='backend offline';meta.style.color='var(--amber)';} return; }
    if(!d.available){ if(meta){meta.textContent=(d.reason||'unavailable');meta.style.color='var(--amber)';}
      nodes=[];edges=[];byId={}; if(legend) legend.innerHTML=''; return; }
    if(meta){ meta.textContent='● '+d.name+' · '+d.node_count+' entities · '+d.edge_count+' relations'; meta.style.color='var(--green)'; }
    nodes=(d.nodes||[]).map(n=>({...n})); byId={}; nodes.forEach(n=>byId[n.id]=n);
    edges=(d.edges||[]).filter(e=>byId[e.source]&&byId[e.target]);
    layout();
    const types=[...new Set(nodes.map(n=>n.type))];
    if(legend) legend.innerHTML='<span class=lghdr>Entity types</span>'+types.map(t=>'<span class=lgrow><span class=lgdot style="background:'+colorFor(t)+'"></span>'+t+'</span>').join('');
  }
  function step(){
    for(let i=0;i<nodes.length;i++){ for(let j=i+1;j<nodes.length;j++){ const a=nodes[i],b=nodes[j];
      let dx=a.x-b.x,dy=a.y-b.y,d2=(dx*dx+dy*dy)||1,d=Math.sqrt(d2),f=2600/d2;
      a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f; }}
    edges.forEach(e=>{ const a=byId[e.source],b=byId[e.target]; if(!a||!b)return;
      let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||1,f=(d-130)*0.012;
      a.vx+=dx/d*f;a.vy+=dy/d*f;b.vx-=dx/d*f;b.vy-=dy/d*f; });
    const cx=W/2,cy=H/2;
    nodes.forEach(n=>{ if(n===drag) return; n.vx+=(cx-n.x)*0.002;n.vy+=(cy-n.y)*0.002;
      n.vx*=0.85;n.vy*=0.85; n.x+=n.vx;n.y+=n.vy;
      n.x=Math.max(24,Math.min(W-24,n.x)); n.y=Math.max(22,Math.min(H-22,n.y)); });
    draw(); requestAnimationFrame(step);
  }
  function draw(){ ctx.clearRect(0,0,W,H);
    edges.forEach(e=>{ const a=byId[e.source],b=byId[e.target]; if(!a||!b)return; const hot=sel&&(sel.id===a.id||sel.id===b.id);
      ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);
      ctx.strokeStyle=hot?'#f472b6cc':'#f472b63a'; ctx.lineWidth=hot?1.8:0.9; ctx.stroke();
      if(hot){ const mx=(a.x+b.x)/2,my=(a.y+b.y)/2; ctx.font='9px Segoe UI';ctx.fillStyle='#f472b6';ctx.textAlign='center';
        ctx.fillText((e.type||'').slice(0,26),mx,my-3); } });
    nodes.forEach(n=>{ const col=colorFor(n.type), r=7+Math.min(8,(n.deg||0)*2)+(n===sel?3:0);
      ctx.beginPath();ctx.arc(n.x,n.y,r,0,6.28);ctx.fillStyle=col;
      ctx.shadowColor=col;ctx.shadowBlur=n===sel?16:7;ctx.fill();ctx.shadowBlur=0;
      if(n===sel){ctx.lineWidth=2;ctx.strokeStyle='#fff';ctx.stroke();}
      ctx.font='10px Segoe UI';ctx.fillStyle='rgba(220,228,245,.85)';ctx.textAlign='center';
      ctx.fillText((n.name||'').slice(0,24),n.x,n.y-r-4); });
  }
  function at(mx,my){ let hit=null,best=520; nodes.forEach(n=>{const d=(n.x-mx)**2+(n.y-my)**2; if(d<best){best=d;hit=n;}}); return hit; }
  function pos(ev){ const r=cv.getBoundingClientRect(); return [ev.clientX-r.left, ev.clientY-r.top]; }
  cv.addEventListener('mousedown',ev=>{ const p=pos(ev); drag=at(p[0],p[1]); if(drag) selectNode(drag); });
  cv.addEventListener('mousemove',ev=>{ if(drag){ const p=pos(ev); drag.x=p[0];drag.y=p[1];drag.vx=0;drag.vy=0; } });
  window.addEventListener('mouseup',()=>{ drag=null; });
  function selectNode(n){ sel=n; if(!details) return;
    const attrs=Object.entries(n.attributes||{}).filter(([k])=>k!=='name').map(([k,v])=>'<div class=drow><span class=dk>'+k+'</span>'+v+'</div>').join('');
    const labs=((n.labels&&n.labels.length?n.labels:[n.type])).map(l=>'<span class=mfgtag style="background:'+colorFor(l)+'22;color:'+colorFor(l)+'">'+l+'</span>').join('');
    details.innerHTML='<div class=dh><b>'+(n.name||'')+'</b><span class=dx onclick="document.getElementById(\'mfgdetails\').classList.remove(\'show\')">✕</span></div>'
      +'<div class=drow><span class=dk>type</span>'+n.type+'</div>'
      +'<div class=drow><span class=dk>uuid</span><span class=dmono>'+(n.id||'')+'</span></div>'
      +'<div class=drow><span class=dk>created</span>'+(n.created_at?new Date(n.created_at).toLocaleString():'—')+'</div>'
      +(attrs?'<div class=drow style="margin-top:6px;color:var(--dim);font-weight:600">Properties</div>'+attrs:'')
      +(n.summary?'<div class=drow style="margin-top:6px;color:var(--dim);font-weight:600">Summary</div><div class=dsum>'+n.summary+'</div>':'')
      +'<div class=drow style="margin-top:8px">'+labs+'</div>';
    details.classList.add('show');
  }
  const rb=document.getElementById('mfgrefresh'); if(rb) rb.onclick=load;
  window.addEventListener('resize',resize);
  resize(); load(); requestAnimationFrame(step); setInterval(load,30000);
})();

// ── MiroFish report (the crowd-sim's written analysis, real markdown from :5001) ──
(function(){
  const body=document.getElementById('mfrbody'), meta=document.getElementById('mfrmeta'), prog=document.getElementById('mfrprog');
  if(!body) return;
  function md2html(md){
    const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const inline=s=>esc(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/\*(.+?)\*/g,'<em>$1</em>');
    let html='',inList=false; const close=()=>{ if(inList){html+='</ul>';inList=false;} };
    (md||'').split('\n').forEach(raw=>{ const l=raw.trim();
      if(!l){ close(); return; }
      if(l.startsWith('### ')){ close(); html+='<h3>'+inline(l.slice(4))+'</h3>'; }
      else if(l.startsWith('## ')){ close(); html+='<h2>'+inline(l.slice(3))+'</h2>'; }
      else if(l.startsWith('# ')){ close(); html+='<h1>'+inline(l.slice(2))+'</h1>'; }
      else if(l.startsWith('> ')){ close(); html+='<blockquote>'+inline(l.slice(2))+'</blockquote>'; }
      else if(/^(-|\*|\d+\.)\s/.test(l)){ if(!inList){html+='<ul>';inList=true;} html+='<li>'+inline(l.replace(/^(-|\*|\d+\.)\s/,''))+'</li>'; }
      else { close(); html+='<p>'+inline(l)+'</p>'; }
    });
    close(); return html;
  }
  async function load(){
    let d; try{ d=await(await fetch('/api/mirofish_report')).json(); }
    catch(e){ if(meta){meta.textContent='offline';meta.style.color='var(--amber)';} return; }
    if(!d.available){ if(meta){meta.textContent=d.reason||'no report';meta.style.color='var(--amber)';}
      body.innerHTML='<p style="color:var(--dim)">No MiroFish report yet — none have completed.</p>'; if(prog)prog.innerHTML=''; return; }
    const done=d.status==='done'||d.status==='completed';
    if(meta){ meta.textContent='● '+(d.status||'')+(d.sections_total?(' · '+d.sections_done+'/'+d.sections_total+' sections'):''); meta.style.color=done?'var(--green)':'var(--amber)'; }
    if(prog){ const p=d.progress!=null?d.progress:(done?100:0); prog.innerHTML='<div class=bar style="width:'+p+'%"></div>'; }
    body.innerHTML=md2html(d.markdown);
  }
  const rb=document.getElementById('mfrrefresh'); if(rb) rb.onclick=load;
  load(); setInterval(load,30000);
})();

// ── LLM live: real WebSocket token stream ─────────────────────────────────────
(function(){
  const wrap=document.getElementById('llmlive'), dot=document.getElementById('llmdot'); if(!wrap) return;
  const qEl=wrap.querySelector('.llmq'), body=wrap.querySelector('.llmbody'); let cursor=null;
  const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  function setCursor(on){ if(cursor){cursor.remove();cursor=null;} if(on){ cursor=document.createElement('span'); cursor.className='cursor'; body.appendChild(cursor);} }
  function connect(){
    const proto=location.protocol==='https:'?'wss':'ws'; let ws;
    try{ ws=new WebSocket(proto+'://'+location.host+'/ws/llm'); }catch(_){ dot.textContent='● ws unavailable'; dot.style.color='var(--red)'; return; }
    ws.onopen=()=>{ dot.textContent='● streaming'; dot.style.color='var(--green)'; };
    ws.onmessage=e=>{ let m; try{m=JSON.parse(e.data);}catch(_){return;}
      if(m.t==='start'){
        var meta=(m.price!=null?' · mkt '+Math.round(m.price*100)+'%':'')
               +(m.hours!=null?' · '+(m.hours<1?'<1':Math.round(m.hours))+'h':'')
               +(m.side?' · rule '+m.side+(m.edge?' +'+(m.edge*100).toFixed(1)+'%':''):' · no edge');
        qEl.innerHTML='🔎 <b>scouting</b> ▸ '+esc(m.market)+'<span style="color:var(--dim)">'+esc(meta)+(m.model?'  ['+m.model+']':'')+'</span>';
        body.textContent=''; setCursor(true);
        try{ window.netFeed && window.netFeed('Fetching sources single-LLM scouting'); }catch(_){}}
      else if(m.t==='tok'){ setCursor(false); body.appendChild(document.createTextNode(m.v)); setCursor(true); wrap.scrollTop=wrap.scrollHeight; }
      else if(m.t==='meta'){ qEl.textContent='▸ live model'; body.textContent=m.msg||''; setCursor(false); }
      else if(m.t==='done'){ setCursor(false); body.appendChild(document.createTextNode('\n\n— analysis complete —')); wrap.scrollTop=wrap.scrollHeight; }
      else if(m.t==='idle'){ setCursor(false); if(dot){ dot.textContent='● idle · caught up'; dot.style.color='var(--dim)'; } }
    };
    ws.onclose=()=>{ dot.textContent='● reconnecting…'; dot.style.color='var(--amber)'; setCursor(false); setTimeout(connect,2500); };
    ws.onerror=()=>{ try{ws.close();}catch(_){} };
  }
  connect();
})();
</script></body></html>"""


if __name__ == "__main__":
    main()
