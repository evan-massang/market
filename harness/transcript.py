"""
AI activity transcript — assembles a complete, READ-ONLY record of everything the AI
components actually did, from the real DB rows + the crowd-sim files:
  * the 12/5-persona swarm's forecast per market
  * the single-LLM challenger's forecast
  * the MiroFish crowd's verdict AND its actual generated posts
  * every betting decision with the reasoning the harness logged

No fabrication — every line comes from a real row in polyswarm.db or a real
reddit_simulation.db. Paper-only.

    python -m harness.transcript            # writes ai_transcript.md
    python -m harness.transcript --print    # also echo it to stdout
"""
from __future__ import annotations
import json, os, sqlite3, sys
from datetime import datetime, timezone

_DB = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")


def _conn():
    c = sqlite3.connect(_DB); c.row_factory = sqlite3.Row; return c


def _ts(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(s)


def _pct(v):
    return "—" if v is None else f"{float(v) * 100:.1f}%"


def _latest_by_market(rows):
    out = {}
    for r in rows:
        if r["market_id"]:
            out[r["market_id"]] = r        # rows come ordered by id asc -> last wins (latest)
    return out


def build() -> str:
    c = _conn()

    def q(sql):
        try:
            return c.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []

    swarm = _latest_by_market(q("SELECT * FROM swarm_forecasts ORDER BY id"))
    base = _latest_by_market(q("SELECT * FROM baseline_forecasts ORDER BY id"))
    crowd = _latest_by_market(q("SELECT * FROM mirofish_forecasts ORDER BY id"))
    decisions = q("SELECT * FROM decisions ORDER BY id DESC")

    try:
        from harness import wallet
        st = wallet.get_state()
    except Exception:
        st = {}
    try:
        from harness import mirofish_signal
    except Exception:
        mirofish_signal = None

    L = []
    L.append("# AI activity transcript — Polymarket paper-trading harness")
    L.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · paper-only · $0 local swarm (qwen2.5:7b)_\n")

    nbet = sum(1 for d in decisions if d["status"] == "bet")
    L.append("## Summary")
    L.append(f"- Swarm forecasts: **{len(swarm)} markets** · single-LLM challenger: **{len(base)}** · MiroFish crowd sims: **{len(crowd)}**")
    L.append(f"- Decisions logged: **{len(decisions)}** ({nbet} bets placed)")
    if st:
        L.append(f"- Wallet: cash ${st.get('cash', 0):.2f} · equity ${st.get('equity', 0):.2f} · "
                 f"realized P&L ${st.get('realized_pnl', 0):+.2f} · {st.get('n_open', 0)} open positions")
    L.append("")

    # ── per-market: what each AI concluded ────────────────────────────────────
    L.append("## What each AI concluded, per market")
    market_ids = set(swarm) | set(crowd) | set(base)

    def question_of(mid):
        for tbl in (swarm, crowd, base):
            if mid in tbl:
                return tbl[mid]["question"] or mid
        return mid

    def market_ts(mid):
        ts = [str((tbl[mid]["created_at"] if mid in tbl else "") or "") for tbl in (swarm, crowd, base)]
        return max(ts)

    for mid in sorted(market_ids, key=market_ts, reverse=True):
        sw, bs, cr = swarm.get(mid), base.get(mid), crowd.get(mid)
        L.append(f"### {question_of(mid)}")
        L.append(f"`{mid}`")
        if sw is not None:
            out = sw["outcome"]
            res = "resolved YES ✓" if out == 1.0 else ("resolved NO ✗" if out == 0.0 else "pending")
            L.append(f"- **Swarm**: {_pct(sw['final_probability'])} YES · consensus "
                     f"{('%.2f' % sw['consensus_score']) if sw['consensus_score'] is not None else '—'} · "
                     f"market {_pct(sw['market_odds'])} · {res}")
        if bs is not None:
            L.append(f"- **Single-LLM challenger**: {_pct(bs['probability'])} YES")
        if cr is not None and cr["crowd_probability"] is not None:
            L.append(f"- **MiroFish crowd**: {_pct(cr['crowd_probability'])} YES ({cr['n_posts'] or 0} posts)")
        for d in [d for d in decisions if d["market_id"] == mid][:3]:
            act = (f"{d['side']} ${d['stake']:.2f} @ {d['fill_price']:.3f}"
                   if d["status"] == "bet" and d["fill_price"] is not None else d["status"])
            L.append(f"- **Decision** [{_ts(d['ts'])}] {act} — {d['why'] or ''}")
        L.append("")

    # ── MiroFish crowd discussions: the actual generated posts ─────────────────
    L.append("## MiroFish crowd discussions — the actual AI-generated posts")
    any_posts = False
    for mid, cr in sorted(crowd.items(), key=lambda kv: str(kv[1]["created_at"] or ""), reverse=True):
        sid = cr["sim_id"]
        posts = []
        if mirofish_signal and sid:
            try:
                posts = mirofish_signal.read_crowd_posts(sid)
            except Exception:
                posts = []
        if not posts:
            try:
                posts = json.loads(cr["posts"] or "[]")
            except Exception:
                posts = []
        if not posts:
            continue
        any_posts = True
        cp = _pct(cr["crowd_probability"])
        L.append(f"### {cr['question']}")
        L.append(f"_sim `{sid}` · crowd {cp} YES · {len(posts)} posts_")
        for p in posts[:30]:
            L.append(f"- {str(p).strip()}")
        L.append("")
    if not any_posts:
        L.append("_No crowd posts recorded yet._\n")

    # ── chronological decision log ─────────────────────────────────────────────
    L.append("## Chronological decision log (newest first)")
    for d in decisions:
        head = f"`{_ts(d['ts'])}` · **{d['status']}**"
        if d["status"] == "bet" and d["fill_price"] is not None:
            head += f" {d['side']} ${d['stake']:.2f} @ {d['fill_price']:.3f}"
        head += f" · edge {((d['edge'] or 0) * 100):+.1f}%"
        L.append(f"- {head} — {d['question']}")
        if d["why"]:
            L.append(f"  - _{d['why']}_")

    c.close()
    return "\n".join(L)


if __name__ == "__main__":
    md = build()
    out = os.getenv("TRANSCRIPT_OUT", "ai_transcript.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"wrote {out} ({len(md):,} chars)")
    if "--print" in sys.argv:
        print("\n" + md)
