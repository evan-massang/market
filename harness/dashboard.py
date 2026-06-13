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
import sqlite3

# Load polyswarm/.env so the dashboard reflects MODEL_FAST and the CHALLENGER_* key.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from harness import wallet as paper
from harness import journal, scoreboard, challenger
from harness import mirofish_signal

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")
MARKET_BAR = 0.0627   # historical market-price Brier on resolved opinion markets (the bar)

app = FastAPI(title="Polymarket Harness Dashboard")


def _ab_rows(limit: int = 40) -> list[dict]:
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT b.question AS question, s.final_probability AS swarm_p, b.probability AS llm_p, "
            "b.market_odds AS market_p, s.outcome AS outcome "
            "FROM baseline_forecasts b JOIN swarm_forecasts s ON s.market_id=b.market_id "
            "ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
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
        "positions": paper.get_open_positions(),
        "decisions": journal.get_decisions(60),
        "ab": _ab_rows(40),
        "mirofish": mirofish_signal.get_signals(8),
    }
    return JSONResponse(data)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML, headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})


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
</style></head><body>
<h1><span class=dot></span>Polymarket Harness — Live Paper-Trading Monitor</h1>
<div class=sub id=sub>paper only · $0 local swarm (qwen2.5:7b) · loading…</div>

<div id=nextbet style="margin:8px 0 16px;padding:14px 18px;background:linear-gradient(90deg,#11224d,#0d1322);border:1px solid var(--cyan);border-radius:12px;box-shadow:0 0 24px #22d3ee22">
  <span style="color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.7px">⏱ Next bet resolves in</span>
  <span id=nextcd class=mono style="font-size:32px;font-weight:800;color:var(--cyan);margin:0 14px;text-shadow:0 0 14px #22d3ee88">—</span>
  <span id=nextq style="color:#aab6d6"></span>
</div>

<div class="grid cards" id=cards></div>

<div class="grid main">
  <div class=panel><div class=ttl>Equity &amp; realized P&amp;L</div><canvas id=pnl height=120></canvas></div>
  <div class=panel><div class=ttl>The two gates (vs market bar 0.0627)</div><div id=gauges></div></div>
</div>

<div class="grid" style="margin-top:14px">
  <div class=panel><div class=ttl>What it's betting on — open paper positions  <span class=legend>(rightmost column = live resolution countdown)</span></div>
    <table id=postbl><thead><tr><th>Market</th><th>Side</th><th>Model</th><th>Mkt</th><th>Edge</th><th>Stake</th><th>Fill</th><th style="color:var(--cyan)">⏱ Resolves in</th></tr></thead><tbody></tbody></table></div>
</div>

<div class="grid" style="margin-top:14px">
  <div class=panel><div class=ttl>Challenger A/B — swarm vs <span id=ablegend class=legend>single-LLM</span> vs market</div>
    <table id=abtbl><thead><tr><th>Market</th><th>Swarm</th><th>1-LLM</th><th>Market</th></tr></thead><tbody></tbody></table></div>
</div>

<div class="grid" style="margin-top:14px"><div class=panel><div class=ttl>🐟 MiroFish — crowd-simulation forecaster <span class=legend>(agents debate the market, then we read their verdict)</span></div><div id=mirofish></div></div></div>

<div class="grid" style="margin-top:14px"><div class=panel><div class=ttl>Decision transcript — why it's betting</div><div class=txn id=txn></div></div></div>

<script>
const $=s=>document.querySelector(s);
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const pct=v=>v==null?'–':(v*100).toFixed(1)+'%';
const b4=v=>v==null?'–':v.toFixed(4);
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
  if(ms<=0) return 'RESOLVED';
  const s=Math.floor(ms/1000),d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),ss=s%60;
  const pad=n=>n<10?'0'+n:n;
  return (d?d+'d ':'')+pad(h)+':'+pad(m)+':'+pad(ss);
}
function tickCountdowns(){
  if(window._soon){
    const ms=window._soon.t-Date.now();
    const el=document.getElementById('nextcd');
    if(el){ el.textContent = ms>0 ? fmtCountdown(new Date(window._soon.t).toISOString()) : 'RESOLVED';
            el.style.color = ms>0 && ms<3600000 ? 'var(--amber)' : 'var(--cyan)'; }
  }
  document.querySelectorAll('.cd').forEach(el=>{
    const e=el.getAttribute('data-end'); el.textContent=fmtCountdown(e);
    const ms=e?new Date(e).getTime()-Date.now():NaN;
    el.style.color = isNaN(ms)?'' : (ms<=0?'var(--red)' : ms<3600000?'var(--amber)' : 'var(--green)');
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
    <td class=q title="${(p.question||'').replace(/"/g,'')}">${p.question||''}</td>
    <td><span class="pill ${p.side=='YES'?'yes':'no'}">${p.side}</span></td>
    <td class=mono>${pct(p.model_p)}</td><td class=mono>${pct(p.market_p)}</td>
    <td class="mono ${p.edge>=0?'pos':'neg'}">${(p.edge*100>=0?'+':'')+(p.edge*100).toFixed(1)}%</td>
    <td class=mono>${money(p.stake)}</td><td class=mono>${(p.fill_price||0).toFixed(3)}</td>
    <td class="mono cd" data-end="${p.end_date||''}">${fmtCountdown(p.end_date)}</td></tr>`).join('')
    || '<tr><td colspan=8 style="color:var(--dim)">no open positions</td></tr>';

  // soonest-resolving open bet -> big banner at the top
  let soon=null;
  (d.positions||[]).forEach(p=>{ if(p.end_date){ const t=new Date(p.end_date).getTime(); if(t>Date.now() && (!soon||t<soon.t)) soon={t:t,q:p.question||'',side:p.side,stake:p.stake}; }});
  window._soon=soon;
  $('#nextq').textContent = soon ? `${soon.side} $${(soon.stake||0).toFixed(0)} · ${soon.q.substring(0,64)}` : '(no open bets)';

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
</script></body></html>"""


if __name__ == "__main__":
    main()
