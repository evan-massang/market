"""harness/command_center.py — P11 dashboard command-center data (read-only).

Assembles the operator's "command center" view from the analytics built in
P4-P10: recently SKIPPED markets + their reasons, LOSING-trade diagnosis,
calibration, per-theme / per-label performance, NEXT-BEST-ACTIONS, and the
correlation IDs needed for a clickable obs replay.

Pure read-only + best-effort: every section degrades to [] / {} on error and
never raises, so a dashboard request can never crash the daemon. No decision is
made here and nothing is written.
"""
from __future__ import annotations

import os
import sqlite3

try:
    from harness import obs
except Exception:  # pragma: no cover
    obs = None


def _db_path() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _err(where: str, exc: Exception) -> None:
    if obs:
        try:
            obs.hooks.on_error(where=where, exc=exc, action="skip")
        except Exception:
            pass


def _conn():
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    return c


# ── skipped markets + reasons ─────────────────────────────────────────────────
def skipped_markets(limit: int = 50) -> list[dict]:
    """Recent decisions that did NOT bet, with the guard reason (from the journal's
    decisions table; status='no_bet'). Returns newest-first."""
    try:
        from harness import journal
        rows = journal.get_decisions(limit=limit * 3)  # over-fetch, then filter
    except Exception as e:
        _err("command_center.skipped_markets", e)
        return []
    out = []
    for r in rows:
        try:
            if (r.get("status") or "") == "no_bet":
                out.append({
                    "ts": r.get("ts"),
                    "market_id": r.get("market_id"),
                    "question": r.get("question"),
                    "model_p": r.get("model_p"),
                    "market_p": r.get("market_p"),
                    "reason": r.get("why") or "(no reason recorded)",
                })
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def skip_reason_counts() -> dict:
    """Histogram of skip reasons (coarse first token of the why) for at-a-glance
    'why is the bot not betting' — e.g. {'neg_ev_after_costs': 12, 'high_spread': 4}."""
    counts: dict = {}
    for s in skipped_markets(limit=500):
        why = (s.get("reason") or "").replace("Guard skip:", "").strip()
        key = why.split("(")[0].split(":")[0].strip() or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ── losing-trade diagnosis ────────────────────────────────────────────────────
def losing_trades(limit: int = 25) -> list[dict]:
    """Settled positions that LOST money, with a short diagnosis (what the model
    said vs how it resolved). Newest-first."""
    try:
        conn = _conn()
    except Exception as e:
        _err("command_center.losing_trades.connect", e)
        return []
    try:
        rows = conn.execute(
            "SELECT market_id, question, side, model_p, market_p, edge, stake, "
            "realized_pnl, outcome, status, settled_at FROM paper_positions "
            # include cashed-out ('closed') losers so they are not hidden (audit #8/#19)
            "WHERE status IN ('settled','closed') AND realized_pnl < 0 "
            "ORDER BY settled_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as e:
        _err("command_center.losing_trades.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    for r in rows:
        try:
            mp = r["model_p"]
            outc = r["outcome"]
            diag = ""
            if mp is not None and outc is not None:
                conf = mp if (r["side"] or "YES").upper() == "YES" else (1 - mp)
                diag = (f"model backed {r['side']} at {conf:.0%} confidence; "
                        f"resolved {'YES' if outc >= 0.5 else 'NO'} -> wrong")
            out.append({
                "market_id": r["market_id"], "question": r["question"], "side": r["side"],
                "model_p": mp, "market_p": r["market_p"], "edge": r["edge"],
                "stake": r["stake"], "realized_pnl": r["realized_pnl"],
                "outcome": outc, "diagnosis": diag,
            })
        except Exception:
            continue
    return out


# ── theme / label performance ─────────────────────────────────────────────────
def theme_label_performance() -> dict:
    out = {"by_theme": {}, "by_label": {}}
    try:
        # Plan 11: DISPLAY-safe theme P&L — small-sample win-rate/ROI withheld (never a fake
        # '100% win rate' from n=1). Does not touch adaptive.theme_pnl (sizing) itself.
        from harness import profit_intel as _pi
        out["by_theme"] = _pi.guarded_theme_pnl()
    except Exception as e:
        _err("command_center.theme_perf", e)
    try:
        from harness import label_perf
        out["by_label"] = label_perf.label_performance(min_n=1)
    except Exception as e:
        _err("command_center.label_perf", e)
    return out


# ── next-best-actions (derived, honest) ───────────────────────────────────────
def next_best_actions() -> list[str]:
    """Operator suggestions derived from the live metrics — never a profit claim."""
    actions: list[str] = []
    try:
        from harness import metrics
        rep = metrics.gate_report()
        n = rep.get("n_resolved_opinion") or 0
        need = rep.get("n_required") or 50
        if n < need:
            actions.append(f"Keep running: {n}/{need} resolved opinion markets toward GATE 1.")
        ll = rep.get("log_loss") or {}
        if ll.get("log_loss") is not None and ll.get("log_loss") > ll.get("coin_flip_ref", 0.693):
            actions.append(f"Model log loss {ll['log_loss']:.3f} is WORSE than a coin flip "
                           f"({ll.get('coin_flip_ref'):.3f}) over {ll.get('n')} forecasts — observe-only is correct; "
                           f"improve the model before sizing up.")
        pm = rep.get("paper") or {}
        if pm.get("n") and (pm.get("roi") or 0) < 0:
            actions.append(f"Paper ROI {pm['roi']*100:.1f}% over {pm['n']} settled bets — tighten guards, do not bet more.")
        g2 = rep.get("gate2") or {}
        if g2 and not g2.get("pass"):
            actions.append("GATE 2 not passed (bankroll has not grown after costs) — stay on paper.")
    except Exception as e:
        _err("command_center.next_best_actions", e)
    try:
        from harness import adaptive
        for theme, s in (adaptive.theme_pnl(opinion_only=True) or {}).items():
            if s.get("n", 0) >= 10 and s.get("realized_pnl", 0) < 0:
                actions.append(f"Theme '{theme}' is losing (${s['realized_pnl']:.2f} over {s['n']} bets) — "
                               f"adaptive min_edge is already raising its bar; consider observe-only if it persists.")
    except Exception as e:
        _err("command_center.next_best_actions.themes", e)
    if not actions:
        actions.append("No action flags — system nominal; keep accruing resolved markets.")
    return actions


# ── replay handle ──────────────────────────────────────────────────────────────
def replay_handles(limit: int = 25) -> list[dict]:
    """market_id / forecast handles for a clickable obs replay (the dashboard links
    these to obs.explain(market_id) / obs.replay(forecast_id))."""
    try:
        conn = _conn()
    except Exception as e:
        _err("command_center.replay_handles.connect", e)
        return []
    try:
        rows = conn.execute(
            "SELECT market_id, question, status, settled_at FROM paper_positions "
            "ORDER BY COALESCE(settled_at, opened_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception as e:
        _err("command_center.replay_handles.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [{"market_id": r["market_id"], "question": r["question"],
             "status": r["status"], "explain_path": f"/api/explain/{r['market_id']}"}
            for r in rows]


# ── loss-cause analysis (P17 dedicated analyzer) ───────────────────────────────
def loss_analysis() -> dict:
    """Per-loss CAUSE classification + histogram + honest recommendations (audit #20)."""
    try:
        from harness import loss_analysis as la
        return {"by_trade": la.analyze_losses(), "cause_counts": la.cause_summary(),
                "recommendations": la.recommendations()}
    except Exception as e:
        _err("command_center.loss_analysis", e)
        return {"by_trade": [], "cause_counts": {}, "recommendations": []}


# ── the assembled command center ──────────────────────────────────────────────
def command_center() -> dict:
    """One read-only payload for the dashboard command-center panel."""
    return {
        "skipped_markets": skipped_markets(),
        "skip_reason_counts": skip_reason_counts(),
        "losing_trades": losing_trades(),
        "loss_analysis": loss_analysis(),
        "theme_label_performance": theme_label_performance(),
        "next_best_actions": next_best_actions(),
        "replay_handles": replay_handles(),
    }
