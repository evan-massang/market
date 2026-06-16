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

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
GATE1_MIN_N = 50
GATE2_MIN_N = 30   # Gate 2 needs a real sample, not one lucky settled trade (audit)

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


def _resolved_opinion_rows(include_test=False, include_demo=False, environment=None) -> list[dict]:
    """Resolved swarm forecasts with a stored market price, restricted to OPINION
    markets (re-classified from the question). Excludes test/demo/benchmark records by
    default so they can't contaminate Gate 1 (audit Phase 3)."""
    from harness import environment as _env
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # ONE resolved row per market_id (latest) — a market re-forecast across cycles
        # must count ONCE toward the gate, not once per re-scout.
        rows = conn.execute(
            "SELECT question, market_id, final_probability, market_odds, outcome, brier_score "
            "FROM swarm_forecasts s WHERE outcome IS NOT NULL AND market_odds IS NOT NULL "
            "AND (s.market_id IS NULL OR s.id = (SELECT MAX(id) FROM swarm_forecasts s2 "
            "WHERE s2.market_id = s.market_id AND s2.outcome IS NOT NULL AND s2.market_odds IS NOT NULL))"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    out = []
    for r in rows:
        # exclude TEST / bench-test / demo legacy rows from the live gate
        if not _env.is_live(r["market_id"], include_test=include_test,
                            include_demo=include_demo, environment=environment):
            continue
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
            "SELECT question, brier_score FROM baseline_forecasts b WHERE brier_score IS NOT NULL "
            "AND (b.market_id IS NULL OR b.id = (SELECT MAX(id) FROM baseline_forecasts b2 "
            "WHERE b2.market_id = b.market_id AND b2.brier_score IS NOT NULL))"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [r["brier_score"] for r in rows if tag_market(r["question"]).label == "opinion"]


def compute(include_test=False, include_demo=False, environment=None) -> dict:
    rows = _resolved_opinion_rows(include_test=include_test, include_demo=include_demo,
                                  environment=environment)
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
    # require a real sample so one lucky +$0.01 settled trade can't flash Gate 2 PASS
    try:
        _c = sqlite3.connect(DB_PATH)
        n_settled = _c.execute("SELECT COUNT(*) FROM paper_positions WHERE status IN ('settled','closed')").fetchone()[0]
        _c.close()
    except Exception:
        n_settled = 0
    gate2_pass = bool(n_settled >= GATE2_MIN_N and st["realized_pnl"] > 0 and st["equity"] >= st["starting_bankroll"])

    return {
        "n": n, "n_required": GATE1_MIN_N,
        "model_brier": model_b, "market_brier": market_b,
        "baseline_brier": baseline_b, "baseline_n": len(base),
        "themes": {k: {"n": v.n, "model_brier": v.model_brier, "market_brier": v.market_brier}
                   for k, v in sorted(themes.items())},
        "gate1": {"pass": gate1_pass, "model_brier": model_b, "market_brier": market_b,
                  "n": n, "n_required": GATE1_MIN_N},
        "gate2": {"pass": gate2_pass, "starting_bankroll": st["starting_bankroll"],
                  "equity": st["equity"], "realized_pnl": st["realized_pnl"],
                  "n": n_settled, "n_required": GATE2_MIN_N},
        "both_pass": gate1_pass and gate2_pass,
    }


def profitability_report() -> dict:
    """P7 read-only surfacing: per-theme paper P&L, mean CLV (overall + per theme),
    the adaptive min_edge per theme, edge-decay buckets, and the experiment
    leaderboard.

    PURELY INFORMATIONAL — this reads analytics tables only and touches NO gate, NO
    threshold, and NO bet decision. Best-effort: any piece that errors or is too
    thin degrades to a safe default ({} / None) so a thin-data run simply shows
    "not yet meaningful". Lazy imports avoid an import cycle (adaptive imports
    scoreboard)."""
    out = {"theme_pnl": {}, "adaptive_min_edge": {}, "mean_clv": None,
           "clv_by_theme": {}, "edge_decay": {}, "experiment_leaderboard": []}
    try:
        from harness import adaptive as _adaptive
        out["theme_pnl"] = _adaptive.theme_pnl() or {}
        known = {"elections", "approval", "geopolitics", "culture", "other"}
        themes = sorted(set(out["theme_pnl"].keys()) | known)
        ame = {"_overall": _adaptive.adaptive_min_edge(None)}
        for t in themes:
            ame[t] = _adaptive.adaptive_min_edge(t)
        out["adaptive_min_edge"] = ame
    except Exception:
        pass
    try:
        from harness import clv as _clv
        out["mean_clv"] = _clv.mean_clv()
        out["clv_by_theme"] = _clv.clv_by_theme()
        out["edge_decay"] = _clv.edge_decay_report()
    except Exception:
        pass
    try:
        from harness import experiments as _experiments
        out["experiment_leaderboard"] = _experiments.experiment_leaderboard()
    except Exception:
        pass
    return out


def render_profitability() -> None:
    """Print the P7 read-only profitability/analytics panel. No gate is shown or
    changed here — it is clearly labelled as informational."""
    r = profitability_report()
    print()
    print(" P7 ANALYTICS (read-only — informational, changes NO gate/threshold)")
    print(" " + "-" * 58)
    # per-theme paper P&L
    tp = r.get("theme_pnl") or {}
    if tp:
        print(f" {'theme':14s} {'n':>4s} {'realized_pnl':>13s} {'win_rate':>9s} {'roi':>8s} {'min_edge':>9s}")
        ame = r.get("adaptive_min_edge") or {}
        for theme in sorted(tp):
            s = tp[theme]
            me = ame.get(theme)
            me_s = f"{me:.4f}" if isinstance(me, (int, float)) else "  n/a"
            print(f" {theme:14s} {s['n']:>4d} {s['realized_pnl']:>+13.4f} "
                  f"{s['win_rate']:>9.2f} {s['roi']:>+8.3f} {me_s:>9s}")
    else:
        print(" per-theme paper P&L: (no settled positions yet)")
    ame = r.get("adaptive_min_edge") or {}
    if "_overall" in ame:
        print(f" adaptive min_edge (overall): {ame['_overall']:.4f}  "
              f"(floor; only RAISES for a losing theme)")
    # CLV
    mc = r.get("mean_clv")
    if mc:
        print(f" mean CLV: {mc['mean_clv']:+.4f}  (n={mc['n']}, "
              f"{mc['pct_positive']*100:.0f}% beat the close)")
    else:
        print(" mean CLV: (not yet meaningful — below min_n)")
    cbt = r.get("clv_by_theme") or {}
    for theme in sorted(cbt):
        s = cbt[theme]
        print(f"   CLV[{theme}]: {s['mean_clv']:+.4f}  (n={s['n']}, {s['pct_positive']*100:.0f}% +)")
    # edge decay
    ed = r.get("edge_decay") or {}
    for bucket in ("<=1d", "1-7d", "7-30d", ">30d", "unknown"):
        s = ed.get(bucket)
        if not s:
            continue
        cap = s.get("edge_capture")
        cap_s = f"{cap:+.2f}" if isinstance(cap, (int, float)) else "n/a"
        print(f"   edge-decay[{bucket:>6s}]: n={s['n']}, realized_ret={s['mean_realized_return']:+.3f}, "
              f"capture={cap_s}, profitable={s['pct_profitable']*100:.0f}%")
    # experiment leaderboard
    lb = r.get("experiment_leaderboard") or []
    if lb:
        print(" experiment leaderboard (skill = market_Brier - model_Brier, higher better):")
        for e in lb:
            mb, mk = e.get("mean_model_brier"), e.get("mean_market_brier")
            skill = (mk - mb) if (mb is not None and mk is not None) else None
            skill_s = f"{skill:+.4f}" if skill is not None else "  n/a"
            print(f"   {e['exp_key']:16s} n={e['n']:>3d} skill={skill_s} total_pnl={e['total_pnl']:+.2f}")
    else:
        print(" experiment leaderboard: (no experiment has enough resolved outcomes yet)")
    print(" " + "-" * 58)


def _fmt(x):
    return f"{x:.4f}" if isinstance(x, (int, float)) else "  n/a "


def render(include_test=False, include_demo=False, environment=None) -> None:
    s = compute(include_test=include_test, include_demo=include_demo, environment=environment)
    if include_test or include_demo or environment == "all":
        print(" [including test/demo/benchmark records — NOT the default live gate]")
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
    # P7 read-only analytics panel (informational; changes NO gate). Best-effort.
    try:
        render_profitability()
    except Exception:
        pass
    print()
    verdict = "BOTH GATES PASS — real-money phase may be considered (legality first)" if s["both_pass"] \
        else "gates not both passed — stay on paper"
    print(f" >>> {verdict}")
    print("=" * 66)


if __name__ == "__main__":
    import sys as _sys
    _argv = _sys.argv[1:]
    render(include_test="--include-test" in _argv,
           include_demo="--include-demo" in _argv,
           environment="all" if "all" in _argv or "--environment" in _argv else None)
