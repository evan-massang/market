"""
P5 — bigger-model benchmark (free). Disambiguates METHOD vs WEAK-MODEL.

Re-runs the SAME logged, resolved OPINION markets through a configurable (bigger)
model and compares its Brier against our Qwen swarm and the market price:
  - bigger model STILL can't beat the price  -> the approach is likely dead; stop.
  - bigger beats price but our 7B doesn't     -> method works, model is the bottleneck.
  - our 7B already beats the price             -> run free forever.

The bigger model is whatever LLM env you point at (all $0 options):
  * a larger LOCAL model:  MODEL_FAST=qwen2.5:14b  (if it fits)
  * a free HOSTED model via an OpenAI-compatible endpoint:
      LLM_PROVIDER=openai  OPENAI_BASE_URL=https://api.groq.com/openai/v1
      OPENAI_API_KEY=<free key>  and  --model llama-3.3-70b-versatile
This re-forecast uses a SINGLE bigger-model call per market (cheap, isolates the
model factor); it is a baseline, not the full swarm.

CLI:  python -m harness.benchmark --model <name> [--limit N]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime

from harness.classifier import tag_market
from harness import challenger

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")


def _init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS benchmark_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, model TEXT, question TEXT,
            probability REAL, market_odds REAL, outcome REAL, brier_score REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench ON benchmark_forecasts(model, market_id)")
    conn.commit(); conn.close()


def _resolved_opinion():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT question, market_id, market_odds, outcome, brier_score "
        "FROM swarm_forecasts WHERE outcome IS NOT NULL AND market_odds IS NOT NULL").fetchall()
    conn.close()
    return [r for r in rows if tag_market(r["question"]).label == "opinion"]


def run_benchmark(model: str | None = None, limit: int | None = None) -> dict:
    _init()
    rows = _resolved_opinion()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("[benchmark] no resolved opinion markets logged yet — run the loop and let "
              "markets resolve first."); return {"n": 0}

    label = model or os.getenv("MODEL_FAST") or "default-model"
    print(f"[benchmark] re-forecasting {len(rows)} resolved opinion markets with '{label}'…")
    big_briers, swarm_briers, market_briers, done = [], [], [], 0
    conn = sqlite3.connect(DB_PATH)
    for r in rows:
        p = challenger.single_llm_forecast(r["question"], r["market_odds"], model=model)
        if p is None:
            print(f"  skip (no forecast): {r['question'][:50]}"); continue
        bb = (p - r["outcome"]) ** 2
        big_briers.append(bb)
        swarm_briers.append(r["brier_score"] if r["brier_score"] is not None else None)
        market_briers.append((r["market_odds"] - r["outcome"]) ** 2)
        conn.execute(
            "INSERT INTO benchmark_forecasts (market_id, model, question, probability, market_odds, outcome, brier_score) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["market_id"], label, r["question"], p, r["market_odds"], r["outcome"], bb))
        done += 1
        print(f"  [{done}/{len(rows)}] big_p={p:.3f} mkt={r['market_odds']:.3f} "
              f"out={r['outcome']:.0f} big_brier={bb:.3f}  {r['question'][:45]}")
    conn.commit(); conn.close()

    n = len(big_briers)
    big = sum(big_briers) / n if n else None
    sw = [b for b in swarm_briers if b is not None]
    swarm = sum(sw) / len(sw) if sw else None
    market = sum(market_briers) / n if n else None
    res = {"n": n, "model": label, "bigger_model_brier": big, "swarm_brier": swarm, "market_brier": market}

    print("\n" + "=" * 60)
    print(f" P5 BENCHMARK — bigger model '{label}'  (n={n} opinion markets)")
    print("=" * 60)
    print(f"   bigger-model Brier : {big:.4f}" if big is not None else "   bigger-model Brier : n/a")
    print(f"   our-swarm  Brier   : {swarm:.4f}" if swarm is not None else "   our-swarm  Brier   : n/a")
    print(f"   market     Brier   : {market:.4f}" if market is not None else "   market     Brier   : n/a")
    print("-" * 60)
    if big is not None and market is not None:
        if big >= market and (swarm is None or swarm >= market):
            verdict = "bigger model ALSO can't beat the price -> approach likely dead"
        elif big < market and swarm is not None and swarm >= market:
            verdict = "bigger model beats price but our 7B doesn't -> METHOD works, model is the bottleneck (scale up)"
        elif swarm is not None and swarm < market:
            verdict = "our 7B already beats the price -> run free forever"
        else:
            verdict = "bigger model beats the price"
        print(f"   VERDICT: {verdict}")
    print("=" * 60)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness.benchmark")
    ap.add_argument("--model", default=None, help="model name override (e.g. qwen2.5:14b or llama-3.3-70b-versatile)")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    run_benchmark(model=a.model, limit=a.limit)


if __name__ == "__main__":
    main()
