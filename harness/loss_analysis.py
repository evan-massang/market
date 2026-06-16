"""harness/loss_analysis.py — P17 dedicated losing-trade CAUSE analyzer (read-only).

The previous loss view (command_center.losing_trades) labelled EVERY loss generically
("-> wrong"). This module classifies each losing settled/closed paper position into a
PRIMARY likely cause from transparent, available signals:

  bad_forecast        — model backed the losing side at high confidence (a real miss)
  expected_variance   — a low-confidence / +EV bet that simply lost (normal noise)
  thin_edge_selection — bet on a razor-thin model-vs-market edge that didn't hold
  cashed_out_early    — cut at a loss before resolution (closed, price moved against us)
  oversized           — stake was a large fraction of bankroll
  bad_timing          — long-dated bet (capital tied up / more uncertainty)
  bad_theme           — the market's theme has a real losing track record
  unclassified        — none of the above signals fired

It is HONEST: it never claims a loss was avoidable, and `expected_variance` explicitly
flags losses that are consistent with a correctly-calibrated bet. Read-only,
best-effort; reuses scoreboard.theme_of + adaptive.theme_pnl.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime


def _db_path() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _parse_ts(s):
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _days_held(opened_at, end_date):
    a, b = _parse_ts(opened_at), _parse_ts(end_date)
    if a is None or b is None:
        return None
    try:
        return (b - a).total_seconds() / 86400.0
    except Exception:
        return None


# sizing / timing thresholds (relative to the $1000 paper bankroll)
OVERSIZED_STAKE = 60.0     # >$60 on a ~$1000 book == ~6%, above the documented caps
LONG_DATED_DAYS = 7.0
HIGH_CONF = 0.70
THIN_EDGE = 0.05


def classify_loss(pos: dict, losing_themes: set | None = None) -> dict:
    """Classify ONE losing position. Returns {primary_cause, signals, theme, detail}."""
    side = (pos.get("side") or "YES").upper()
    model_p = pos.get("model_p")
    market_p = pos.get("market_p")
    outcome = pos.get("outcome")
    status = pos.get("status")
    stake = float(pos.get("stake") or 0.0)
    question = pos.get("question") or ""

    try:
        from harness import scoreboard
        theme = scoreboard.theme_of(question)
    except Exception:
        theme = "other"

    conf = None
    if model_p is not None:
        conf = float(model_p) if side == "YES" else 1.0 - float(model_p)

    signals: list[tuple[str, float]] = []

    if status == "closed":
        # cashed out at a loss before resolution -> the price moved against us early
        signals.append(("cashed_out_early", 0.55))

    if outcome is not None and conf is not None:
        won = (side == "YES" and outcome >= 0.5) or (side == "NO" and outcome < 0.5)
        if not won and conf >= HIGH_CONF:
            signals.append(("bad_forecast", 0.5 + 0.5 * conf))      # confidently wrong
        elif not won:
            signals.append(("expected_variance", 0.35))            # low-conf bet that lost

    if model_p is not None and market_p is not None:
        if abs(float(model_p) - float(market_p)) < THIN_EDGE:
            signals.append(("thin_edge_selection", 0.45))

    if stake >= OVERSIZED_STAKE:
        signals.append(("oversized", 0.4 + min(0.3, stake / 1000.0)))

    days = _days_held(pos.get("opened_at"), pos.get("end_date"))
    if days is not None and days > LONG_DATED_DAYS:
        signals.append(("bad_timing", 0.4))

    if losing_themes and theme in losing_themes:
        signals.append(("bad_theme", 0.5))

    primary = max(signals, key=lambda s: s[1])[0] if signals else "unclassified"
    return {
        "primary_cause": primary,
        "signals": {k: round(v, 3) for k, v in signals},
        "theme": theme,
        "detail": _detail(primary, side, conf, model_p, market_p, days, stake),
    }


def _detail(cause, side, conf, model_p, market_p, days, stake):
    if cause == "bad_forecast":
        return f"backed {side} at {conf:.0%} confidence and lost — a real directional miss"
    if cause == "expected_variance":
        return f"low-confidence {side} ({(conf or 0):.0%}) that lost — consistent with normal variance"
    if cause == "thin_edge_selection":
        return f"razor-thin edge (model {model_p} vs market {market_p}) that didn't hold"
    if cause == "cashed_out_early":
        return "cashed out at a loss before resolution (price moved against us)"
    if cause == "oversized":
        return f"stake ${stake:.0f} was a large fraction of bankroll"
    if cause == "bad_timing":
        return f"long-dated bet ({days:.0f} days) — capital tied up / more uncertainty"
    if cause == "bad_theme":
        return "the market's theme has a real losing track record"
    return "no dominant cause signal"


def _losing_themes(min_n: int = 10) -> set:
    try:
        from harness import adaptive
        return {t for t, s in (adaptive.theme_pnl() or {}).items()
                if s.get("n", 0) >= min_n and s.get("realized_pnl", 0) < 0}
    except Exception:
        return set()


def analyze_losses(limit: int = 50) -> list[dict]:
    """Every losing settled/closed position with its classified cause, newest-first."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT market_id, question, side, model_p, market_p, edge, stake, realized_pnl, "
            "outcome, status, opened_at, end_date FROM paper_positions "
            "WHERE status IN ('settled','closed') AND realized_pnl < 0 "
            "ORDER BY settled_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    losing_themes = _losing_themes()
    out = []
    for r in rows:
        pos = dict(r)
        c = classify_loss(pos, losing_themes)
        out.append({"market_id": pos["market_id"], "question": pos["question"],
                    "side": pos["side"], "realized_pnl": pos["realized_pnl"],
                    "status": pos["status"], "cause": c["primary_cause"],
                    "detail": c["detail"], "theme": c["theme"]})
    return out


def cause_summary(limit: int = 500) -> dict:
    """Histogram of primary loss causes — the at-a-glance 'why are we losing'."""
    counts: dict = {}
    for row in analyze_losses(limit=limit):
        counts[row["cause"]] = counts.get(row["cause"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def recommendations() -> list[str]:
    """Honest, derived risk actions from the dominant loss causes — never a profit claim."""
    summ = cause_summary()
    recs: list[str] = []
    if summ.get("thin_edge_selection", 0) >= 3:
        recs.append("Many losses came from thin edges — raise min_edge / be more selective.")
    if summ.get("oversized", 0) >= 3:
        recs.append("Several losses were oversized — lower the per-bet cap.")
    if summ.get("bad_theme", 0) >= 3:
        recs.append("Losses concentrated in losing themes — consider observe-only for them.")
    if summ.get("bad_timing", 0) >= 3:
        recs.append("Long-dated bets are losing — tighten the time-to-resolution window.")
    if summ.get("cashed_out_early", 0) >= 5:
        recs.append("Many cashed-out losses — the scanner may be entering too late (check CLV).")
    if summ.get("expected_variance", 0) and not recs:
        recs.append("Losses look like normal variance on calibrated bets — keep sample size growing.")
    if not recs:
        recs.append("No dominant fixable loss pattern yet — keep accruing resolved trades.")
    return recs


def render() -> None:
    print("\n=== LOSS-CAUSE ANALYSIS (read-only) ===")
    summ = cause_summary()
    print(" causes:", summ or "(no losing trades yet)")
    print(" recommendations:")
    for r in recommendations():
        print("   -", r)
    print()


if __name__ == "__main__":
    render()
