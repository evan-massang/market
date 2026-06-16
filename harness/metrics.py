"""harness/metrics.py — P10 consolidated scoreboard metrics + gate report.

READ-ONLY analytics. Computes the metrics the raw scoreboard did not — log loss
(cross-entropy), ROI, hit rate, profit factor, max drawdown, calibration buckets,
CLV — and folds them, together with the AUTHORITATIVE gate verdict from
``scoreboard.compute()``, into one report for the dashboard and the operator.

It does NOT re-implement or override the two gates (that logic stays in
scoreboard.py / obs.gate so it can't be faked here); it only SURFACES them with
their supporting evidence. Nothing here writes to the DB or touches a decision.

DATABASE_URL-aware (resolved at call time) and best-effort: any error degrades to
None / {} for that metric, never raises.
"""
from __future__ import annotations

import math
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


# ── forecast-quality metrics (model probability vs outcome) ───────────────────
def _resolved_pairs() -> list[tuple[float, float]]:
    """(model_p, outcome) over RESOLVED swarm_forecasts (outcome in {0,1})."""
    try:
        conn = _conn()
    except Exception as e:
        _err("metrics._resolved_pairs.connect", e)
        return []
    try:
        rows = conn.execute(
            "SELECT final_probability AS p, outcome AS y FROM swarm_forecasts "
            "WHERE outcome IS NOT NULL AND final_probability IS NOT NULL"
        ).fetchall()
    except Exception as e:
        _err("metrics._resolved_pairs.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    for r in rows:
        try:
            out.append((float(r["p"]), float(r["y"])))
        except Exception:
            continue
    return out


def log_loss(min_n: int = 1) -> dict | None:
    """Mean binary cross-entropy of the model's probability vs the outcome (lower
    is better; a fair coin scores ln(2)=0.693). None below min_n. Probabilities
    are clamped to [1e-15, 1-1e-15] so a confident miss never yields inf."""
    pairs = _resolved_pairs()
    if len(pairs) < max(1, min_n):
        return None
    eps = 1e-15
    s = 0.0
    for p, y in pairs:
        p = min(max(p, eps), 1.0 - eps)
        s += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
    return {"n": len(pairs), "log_loss": round(s / len(pairs), 6),
            "coin_flip_ref": round(math.log(2), 6)}


# ── paper P&L metrics (settled positions) ─────────────────────────────────────
def _settled_rows() -> list[dict]:
    try:
        conn = _conn()
    except Exception as e:
        _err("metrics._settled_rows.connect", e)
        return []
    try:
        rows = conn.execute(
            "SELECT stake, realized_pnl, settled_at FROM paper_positions "
            # include cashed-out ('closed') trades so ROI/hit/profit-factor/drawdown
            # reconcile with the wallet realized_pnl that Gate 2 reads (audit #8/#19).
            "WHERE status IN ('settled','closed') ORDER BY settled_at, id"
        ).fetchall()
    except Exception as e:
        _err("metrics._settled_rows.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    for r in rows:
        try:
            out.append({"stake": float(r["stake"] or 0.0),
                        "pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else None})
        except Exception:
            continue
    return [r for r in out if r["pnl"] is not None]


def paper_metrics() -> dict:
    """ROI, hit rate, profit factor, max drawdown over SETTLED paper positions.

    * roi           = total realized_pnl / total staked
    * hit_rate      = fraction of positions with realized_pnl > 0
    * profit_factor = gross profit / gross loss (None when no losses yet)
    * max_drawdown  = largest peak-to-trough drop of the cumulative realized P&L
    Returns zeros / None on an empty book.
    """
    rows = _settled_rows()
    n = len(rows)
    if n == 0:
        return {"n": 0, "roi": 0.0, "hit_rate": 0.0, "profit_factor": None,
                "max_drawdown": 0.0, "realized_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0}
    total_stake = sum(r["stake"] for r in rows)
    realized = sum(r["pnl"] for r in rows)
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # positive magnitude
    # max drawdown of the cumulative realized P&L curve
    cum = peak = 0.0
    max_dd = 0.0
    for r in rows:
        cum += r["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "n": n,
        "roi": round(realized / total_stake, 6) if total_stake > 0 else 0.0,
        "hit_rate": round(len(wins) / n, 6),
        "profit_factor": (round(gross_profit / gross_loss, 6) if gross_loss > 0 else None),
        "max_drawdown": round(max_dd, 6),
        "realized_pnl": round(realized, 6),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
    }


# ── calibration + CLV (reuse the dedicated modules) ───────────────────────────
def calibration_buckets(min_n: int = 30) -> dict:
    """Reliability diagram + ECE from the P6 calibration layer (read-only)."""
    try:
        from harness import calibration_apply
        return calibration_apply.calibration_report(min_n=min_n)
    except Exception as e:
        _err("metrics.calibration_buckets", e)
        return {}


def clv_summary(min_n: int = 5) -> dict:
    """Mean CLV + per-theme CLV from the P7 clv module (read-only)."""
    try:
        from harness import clv
        return {"overall": clv.mean_clv(min_n=min_n), "by_theme": clv.clv_by_theme(min_n=min_n)}
    except Exception as e:
        _err("metrics.clv_summary", e)
        return {}


# ── consolidated gate report ──────────────────────────────────────────────────
def gate_report() -> dict:
    """The AUTHORITATIVE two-gate verdict (from scoreboard.compute) PLUS all the
    supporting metrics, in one dict. The gate booleans come straight from
    scoreboard — this never re-decides or fakes them."""
    try:
        from harness import scoreboard
        sb = scoreboard.compute()
    except Exception as e:
        _err("metrics.gate_report.scoreboard", e)
        sb = {}
    return {
        "gate1": sb.get("gate1"),
        "gate2": sb.get("gate2"),
        "both_pass": sb.get("both_pass", False),
        "n_resolved_opinion": sb.get("n"),
        "n_required": sb.get("n_required"),
        "model_brier": sb.get("model_brier"),
        "market_brier": sb.get("market_brier"),
        "log_loss": log_loss(),
        "paper": paper_metrics(),
        "clv": clv_summary(),
    }


def full_report() -> dict:
    """Everything, for the dashboard. Read-only, best-effort."""
    rep = gate_report()
    rep["calibration"] = calibration_buckets()
    try:
        from harness import adaptive
        rep["theme_pnl"] = adaptive.theme_pnl()
    except Exception as e:
        _err("metrics.full_report.theme_pnl", e)
        rep["theme_pnl"] = {}
    try:
        from harness import bankroll
        rep["drawdown"] = bankroll.mark_to_market_equity()
    except Exception as e:
        _err("metrics.full_report.drawdown", e)
        rep["drawdown"] = {}
    return rep


def _fmt(x, pct=False):
    if x is None:
        return "n/a"
    return f"{x*100:.1f}%" if pct else f"{x}"


def render() -> None:
    """`python -m harness.metrics` — human-readable read-only report."""
    r = gate_report()
    ll = r.get("log_loss") or {}
    pm = r.get("paper") or {}
    print("\n=== P10 METRICS (read-only) ===")
    g1, g2 = r.get("gate1") or {}, r.get("gate2") or {}
    print(f" GATE 1 (model Brier < market Brier, n>={r.get('n_required')}): "
          f"{'PASS' if g1.get('pass') else 'FAIL'}  "
          f"(n={r.get('n_resolved_opinion')}, model={_fmt(r.get('model_brier'))}, market={_fmt(r.get('market_brier'))})")
    print(f" GATE 2 (paper bankroll grew after costs):                  "
          f"{'PASS' if g2.get('pass') else 'FAIL'}  "
          f"(start ${g2.get('starting_bankroll', 0):.2f} -> equity ${g2.get('equity', 0):.2f}, "
          f"realized ${g2.get('realized_pnl', 0):+.2f})")
    print(f" log loss : {ll.get('log_loss', 'n/a')}  (coin-flip {ll.get('coin_flip_ref', '0.693')}, n={ll.get('n', 0)})")
    print(f" paper    : n={pm.get('n', 0)} roi={_fmt(pm.get('roi'), pct=True)} "
          f"hit={_fmt(pm.get('hit_rate'), pct=True)} profit_factor={pm.get('profit_factor')} "
          f"max_dd=${pm.get('max_drawdown', 0):.2f}")
    clv = (r.get("clv") or {}).get("overall")
    print(f" CLV      : {clv if clv else 'n/a (need more resolved bets)'}")
    print(f" >>> both gates {'PASS — eligible to consider real money' if r.get('both_pass') else 'NOT passed — stay on paper'}\n")


if __name__ == "__main__":
    render()
