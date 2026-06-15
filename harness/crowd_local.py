"""
Local crowd-simulation — a $0, no-external-app producer that writes the SAME artifact
the real MiroFish backend would (a reddit_simulation.db full of crowd posts), using only
the local Ollama model. Lets the existing mirofish_signal reader + dashboard panel light
up on a CPU box with no Zep and no :5001 service. Clearly labelled 'local-*'.
"""
from __future__ import annotations
import os, sqlite3, time, uuid

# Load the repo .env so LLM_PROVIDER/OLLAMA_BASE_URL/MODEL_FAST are set — same as every other
# harness entrypoint. Without it, core.agent._get_llm_client() defaults to anthropic and crashes.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass

from harness.mirofish_signal import MF_SIM_DIR
from core.agent import _get_llm_client, _call_llm

REDDITORS = [
    ("eli_normie",     "casual retail trader who follows headlines and vibes"),
    ("quant_kate",     "data-driven skeptic who quotes base rates"),
    ("doomer_dan",     "contrarian who expects the longshot to fail"),
    ("hopium_hank",    "optimist who argues the YES case"),
    ("wonk_wendy",     "domain expert who cites specifics"),
    ("lurker_liu",     "cautious moderate weighing both sides"),
    ("degen_dora",     "momentum chaser reacting to recent moves"),
    ("greybeard_gus",  "veteran who has watched many of these resolve"),
]

def _say(provider, client, handle, bio, question, thread=""):
    system = (f"You are r/predictions user {handle}: a {bio}. Write ONE short reddit comment "
              f"(1-3 sentences) with your honest in-character take on whether this market resolves "
              f"YES. No preamble, no markdown.")
    user = f"Market: {question}\n" + (f"\nThread so far:\n{thread}\n\nYour reply:" if thread else "\nYour comment:")
    try:
        return _call_llm(provider, client, system, user, max_tokens=120).strip()
    except Exception:
        return ""

def run_local_crowd(question: str, rounds: int = 1, sim_id: str | None = None) -> str:
    """Generate a crowd discussion -> MF_SIM_DIR/<sim_id>/reddit_simulation.db. Returns sim_id."""
    sim_id = sim_id or f"local-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    sim_dir = os.path.join(MF_SIM_DIR, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(sim_dir, "reddit_simulation.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS post (id INTEGER PRIMARY KEY, handle TEXT, content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS comment (id INTEGER PRIMARY KEY, handle TEXT, content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    provider, client = _get_llm_client()
    posts = []
    for handle, bio in REDDITORS:
        c = _say(provider, client, handle, bio, question)
        if c:
            conn.execute("INSERT INTO post (handle, content) VALUES (?,?)", (handle, c)); conn.commit()
            posts.append(f"{handle}: {c}")
    for _ in range(max(0, rounds - 1)):                       # light debate rounds
        thread = "\n".join(posts[-8:])
        for handle, bio in REDDITORS[:4]:
            c = _say(provider, client, handle, bio, question, thread=thread)
            if c:
                conn.execute("INSERT INTO comment (handle, content) VALUES (?,?)", (handle, c)); conn.commit()
                posts.append(f"{handle} (reply): {c}")
    conn.close()
    return sim_id

if __name__ == "__main__":
    import sys, json
    from harness import mirofish_signal as ms
    q = sys.argv[1] if len(sys.argv) > 1 else "Will the event resolve YES?"
    sid = run_local_crowd(q, rounds=int(os.getenv("CROWD_ROUNDS", "1")))
    sig = ms.crowd_signal(sid, q)                             # 1 LLM call distils the crowd's YES prob
    ms.save_signal(market_id=sid, res=sig, market_odds=None, sim_id=sid)
    print(json.dumps({"sim_id": sid, "n_posts": sig.get("n_posts"), "probability": sig.get("probability")}, indent=2))
