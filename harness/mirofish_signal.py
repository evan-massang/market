"""
MiroFish crowd signal — distill a probability from the OASIS simulation's generated
crowd posts.

The full MiroFish report pipeline (all rounds + the reflective ReportAgent) is too
slow to COMPLETE on a CPU box, but the crowd DOES generate real debate posts early
in the simulation. So we read those posts straight from the sim's reddit_simulation.db
and make ONE LLM call to extract the crowd's collective YES probability. This makes
MiroFish's crowd-reaction signal actually usable locally, today, without a faster model.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3

MF_SIM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "MiroFish", "backend", "uploads", "simulations")


def read_crowd_posts(sim_id: str) -> list[str]:
    """All generated posts + comments from a MiroFish reddit simulation."""
    db = os.path.join(MF_SIM_DIR, sim_id, "reddit_simulation.db")
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    out: list[str] = []
    try:
        for table in ("post", "comment"):
            try:
                for r in conn.execute(f"SELECT content FROM {table}"):
                    if r["content"] and str(r["content"]).strip():
                        out.append(str(r["content"]).strip())
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()
    return out


def crowd_signal(sim_id: str, question: str) -> dict:
    """Read a sim's crowd posts and distill a YES probability via one LLM call."""
    posts = read_crowd_posts(sim_id)
    if not posts:
        return {"probability": None, "n_posts": 0, "posts": [], "question": question,
                "error": "no crowd posts generated yet"}
    try:
        from core.agent import _get_llm_client, _call_llm  # type: ignore
    except Exception as e:
        return {"probability": None, "n_posts": len(posts), "posts": posts[:8],
                "question": question, "error": f"llm import: {e}"}

    crowd = "\n".join(f"- {p}" for p in posts[:30])
    system = "You analyze a simulated crowd's discussion and reply with ONLY the requested JSON."
    user = (
        "A simulated crowd of independent agents discussed this prediction market:\n"
        f"QUESTION: {question}\n\nTheir posts:\n{crowd}\n\n"
        "Weighing ONLY the crowd's collective sentiment and reasoning above, what probability "
        "does the crowd implicitly assign to this market resolving YES?\n"
        'Reply with ONLY JSON: {"probability": <number between 0 and 1>}')
    prob = None
    try:
        provider, client = _get_llm_client()
        raw = _call_llm(provider, client, system, user)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            prob = float(json.loads(m.group(0))["probability"])
            prob = min(max(prob, 0.01), 0.99)
    except Exception as e:
        return {"probability": None, "n_posts": len(posts), "posts": posts[:8],
                "question": question, "error": f"extract: {e}"}
    return {"probability": prob, "n_posts": len(posts), "posts": posts[:10], "question": question}


_DB = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")


def init_mf_table():
    conn = sqlite3.connect(_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS mirofish_forecasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT, question TEXT,
        crowd_probability REAL, market_odds REAL, n_posts INTEGER, posts TEXT,
        sim_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit(); conn.close()


def save_signal(market_id: str, res: dict, market_odds: float | None = None, sim_id: str = ""):
    init_mf_table()
    conn = sqlite3.connect(_DB)
    conn.execute("INSERT INTO mirofish_forecasts (market_id, question, crowd_probability, market_odds, n_posts, posts, sim_id) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (market_id, res.get("question"), res.get("probability"), market_odds,
                  res.get("n_posts"), json.dumps(res.get("posts", [])[:6], ensure_ascii=False), sim_id))
    conn.commit(); conn.close()


def get_signals(limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(_DB); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM mirofish_forecasts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try: d["posts"] = json.loads(d.get("posts") or "[]")
        except Exception: d["posts"] = []
        out.append(d)
    return out


def latest_sim_id() -> str | None:
    """Most-recently-modified simulation dir that has a posts DB."""
    if not os.path.isdir(MF_SIM_DIR):
        return None
    sims = []
    for d in os.listdir(MF_SIM_DIR):
        db = os.path.join(MF_SIM_DIR, d, "reddit_simulation.db")
        if os.path.exists(db):
            sims.append((os.path.getmtime(db), d))
    return max(sims)[1] if sims else None


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else (latest_sim_id() or "")
    q = sys.argv[2] if len(sys.argv) > 2 else "Will the event resolve YES?"
    res = crowd_signal(sid, q)
    print(json.dumps({k: v for k, v in res.items() if k != "posts"}, ensure_ascii=False, indent=2))
    for p in res.get("posts", []):
        print("  crowd>", p[:160])
