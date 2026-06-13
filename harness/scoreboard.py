"""
P4 — dual-Brier + paper-P&L scoreboard. The TWO GATES.

GATE 1 (calibration): swarm model Brier  <  market-price Brier,
        on >= 50 resolved OPINION markets, out-of-sample.   -> is the swarm
        actually better-calibrated than the price it's betting against?
GATE 2 (profitability): the paper bankroll grew over the same sample after costs.

Both must pass before any real money. They can diverge (better-calibrated yet
losing thin/illiquid paper money), so we report BOTH, per theme, with n.

Reads the same ./polyswarm.db. Only OPINION markets count toward the gate (we
re-classify each row's question so a stray mechanical row can't dilute it).
No LLM, no network.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from harness.classifier import tag_market
from harness import wallet as paper

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "")
GATE1_MIN_N = 50

# ── coarse theme tagger (opinion markets) ─────────────────────────────────────
_THEMES = [
    ("elections", ("election", "nomination", "nominee", "primary", "caucus", "senate",
                   "president", "presidential", "governor", "gubernatorial", "vote",
                   "democrat", "republican", "gop", "ballot", "reelection", "mayor")),
    ("approval",  ("approval", "rating", "poll", "favorability")),
    ("geopolitics", ("war", "ceasefire", "invasion", "sanctions", "treaty", "nuclear",
                     "missile", "troops", "ukraine", "russia", "israel", "gaza", "china",
                     "taiwan", "iran", "coup", "summit")),
    ("culture", ("oscar", "grammy", "emmy", "award", "viral", "person of the year",
                 "box office", "movie", "song", "chart", "tiktok", "celebrity", "number one")),
]


def theme_of(question: str) -> str:
    q = (question or "").lower()
    for name, kws in _THEMES:
        if any(k in q for k in kws):
            return name
    return "other"


@dataclass
class ThemeStat:
    theme: str
    n: int = 0
    model_brier_sum: float = 0.0
    market_brier_sum: float = 0.0

    @property
    def model_brier(self) -> float | None:
        return self.model_brier_sum / self.n if self.n else None

    @property
    def market_brier(self) -> float | None:
        return self.market_brier_sum / self.n if self.n else None


def _resolved_opinion_rows() -> list[dict]:
    """Resolved swarm forecasts with a stored market price, restricted to OPINION
    markets (re-classified from the question)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT question, market_id, final_probability, market_odds, outcome, brier_score "
            "FROM swarm_forecasts WHERE outcome IS NOT NULL AND market_odds IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    out = []
    for r in rows:
        if tag_market(r["question"]).label != "opinion":
            continue
        model_b = r["brier_score"]
        if model_b is None:
            model_b = (r["final_probability"] - r["outcome"]) ** 2
        market_b = (r["market_odds"] - r["outcome"]) ** 2
        out.append({"question": r["question"], "market_id": r["market_id"],
                    "model_brier": model_b, "market_brier": market_b,
                    "theme": theme_of(r["question"])})
    return out


def _baseline_opinion_briers() -> list[float]:
    """Resolved single-LLM challenger Briers, restricted to OPINION markets."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT question, brier_score FROM baseline_forecasts WHERE brier_score IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [r["brier_score"] for r in rows if tag_market(r["question"]).label == "opinion"]


def compute() -> dict:
    rows = _resolved_opinion_rows()
    n = len(rows)
    model_b = sum(r["model_brier"] for r in rows) / n if n else None
    market_b = sum(r["market_brier"] for r in rows) / n if n else None

    base = _baseline_opinion_briers()
    baseline_b = sum(base) / len(base) if base else None

    themes: dict[str, ThemeStat] = {}
    for r in rows:
        ts = themes.setdefault(r["theme"], ThemeStat(r["theme"]))
        ts.n += 1
        ts.model_brier_sum += r["model_brier"]
        ts.market_brier_sum += r["market_brier"]

    # GATE 1 — calibration
    gate1_pass = bool(n >= GATE1_MIN_N and model_b is not None and market_b is not None and model_b < market_b)

    # GATE 2 — profitability (paper bankroll grew after costs)
    try:
        st = paper.get_state()
    except Exception:
        st = {"starting_bankroll": 0.0, "equity": 0.0, "realized_pnl": 0.0, "cash": 0.0, "open_exposure": 0.0}
    gate2_pass = bool(st["realized_pnl"] > 0 and st["equity"] >= st["starting_bankroll"])

    return {
        "n": n, "n_required": GATE1_MIN_N,
        "model_brier": model_b, "market_brier": market_b,
        "baseline_brier": baseline_b, "baseline_n": len(base),
        "themes": {k: {"n": v.n, "model_brier": v.model_brier, "market_brier": v.market_brier}
                   for k, v in sorted(themes.items())},
        "gate1": {"pass": gate1_pass, "model_brier": model_b, "market_brier": market_b,
                  "n": n, "n_required": GATE1_MIN_N},
        "gate2": {"pass": gate2_pass, "starting_bankroll": st["starting_bankroll"],
                  "equity": st["equity"], "realized_pnl": st["realized_pnl"]},
        "both_pass": gate1_pass and gate2_pass,
    }


def _fmt(x):
    return f"{x:.4f}" if isinstance(x, (int, float)) else "  n/a "


def render() -> None:
    s = compute()
    print("=" * 66)
    print(" POLYMARKET HARNESS — DUAL-GATE SCOREBOARD  (paper, out-of-sample)")
    print("=" * 66)
    print(f" Resolved opinion markets: n = {s['n']}  (gate needs >= {s['n_required']})")
    print()
    print(f" {'theme':14s} {'n':>4s}  {'model_Brier':>12s}  {'market_Brier':>12s}  {'edge':>8s}")
    print(" " + "-" * 58)
    for theme, t in s["themes"].items():
        mb, kb = t["model_brier"], t["market_brier"]
        edge = (kb - mb) if (mb is not None and kb is not None) else None  # +ve = model better
        edge_s = f"{edge:+.4f}" if edge is not None else "   n/a"
        print(f" {theme:14s} {t['n']:>4d}  {_fmt(mb):>12s}  {_fmt(kb):>12s}  {edge_s:>8s}")
    print(" " + "-" * 58)
    overall_edge = (s["market_brier"] - s["model_brier"]) if (s["model_brier"] is not None and s["market_brier"] is not None) else None
    print(f" {'OVERALL':14s} {s['n']:>4d}  {_fmt(s['model_brier']):>12s}  {_fmt(s['market_brier']):>12s}  "
          f"{(f'{overall_edge:+.4f}' if overall_edge is not None else '   n/a'):>8s}")
    print()
    if s.get("baseline_n"):
        bb = s["baseline_brier"]
        vs_model = "swarm better" if (s["model_brier"] is not None and bb is not None and s["model_brier"] < bb) \
            else ("single-LLM better" if bb is not None else "n/a")
        print(f" A/B challenger — single-LLM Brier: {_fmt(bb)}  (n={s['baseline_n']})  -> "
              f"swarm {_fmt(s['model_brier'])} vs single-LLM {_fmt(bb)}: {vs_model}")
        print()
    g1, g2 = s["gate1"], s["gate2"]
    print(f" GATE 1  (model Brier < market Brier, n>={s['n_required']}):  "
          f"{'PASS' if g1['pass'] else 'FAIL'}"
          + (f"   (lower is better: {_fmt(g1['model_brier'])} vs {_fmt(g1['market_brier'])})" if s['n'] else "   (no resolved markets yet)"))
    print(f" GATE 2  (paper bankroll grew after costs):                 "
          f"{'PASS' if g2['pass'] else 'FAIL'}"
          f"   (start ${g2['starting_bankroll']:.2f} -> equity ${g2['equity']:.2f}, realized ${g2['realized_pnl']:+.2f})")
    print()
    verdict = "BOTH GATES PASS — real-money phase may be considered (legality first)" if s["both_pass"] \
        else "gates not both passed — stay on paper"
    print(f" >>> {verdict}")
    print("=" * 66)


if __name__ == "__main__":
    render()
