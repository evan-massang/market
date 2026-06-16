"""harness/adaptive.py — P7 (B3): per-theme paper-P&L + adaptive min_edge.

GUIDING PRINCIPLE — bet BETTER, not MORE. Everything here can only make the
bot MORE selective. ``adaptive_min_edge`` is a FLOOR-ONLY-UP knob: it returns
the floor (``sizing.DEFAULT_MIN_EDGE`` by default) UNCHANGED unless a theme
(or the book overall) has a real, losing track record, in which case it RAISES
the demanded edge. It can NEVER return a value below the floor, and it NEVER
loosens anything for a winning theme.

Design contract
---------------
* Read-only. We only SELECT from ``paper_positions`` (the immutable paper-trade
  audit table written by wallet.py). Nothing here writes, alters, or deletes.
* DATABASE_URL-aware: the DB path is resolved at CALL time (not import time)
  with the exact normalization label_perf / core.calibration use, so a test that
  points DATABASE_URL at a temp file hits the same file the rest of the harness
  wrote, and the prod polyswarm.db is never touched by a test.
* Best-effort + import-safe: a missing table / DB / malformed row degrades to a
  safe default (``{}`` for theme_pnl, the floor for adaptive_min_edge). Nothing
  here may raise into the bettor (predict_today / sameday) or settlement (loop).
* ``status IN ('settled','closed')`` count — on-chain-resolved AND cashed-out
  Each such row's realized_pnl already nets wallet costs (slippage + fee), so a
  losing theme is losing AFTER costs by construction.
"""
from __future__ import annotations

import os
import sqlite3

from harness import sizing
from harness import scoreboard

try:
    from harness import obs
except Exception:  # pragma: no cover - obs is optional
    obs = None


# ── tunables (all conservative; only ever TIGHTEN) ────────────────────────────
MIN_N = 15            # need >= this many settled bets before we trust a verdict
PENALTY = 0.5         # losing theme -> demand floor * (1 + PENALTY) edge
MAX_MIN_EDGE = 0.10   # sane ceiling so the knob can never wedge betting fully shut


# ── db path / connection (mirrors harness.label_perf) ─────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path, honoring DATABASE_URL exactly as the rest of
    the harness does, else deferring to obs.config.resolve_db_path()."""
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


# ── opinion-market scoping (so the AI's knob reflects the AI's own bets) ───────
_OPINION_CACHE: dict = {}


def _is_opinion(question: str) -> bool:
    """True iff ``question`` classifies as an OPINION market (the population the
    AI swarm actually forecasts + the population GATE1 scores). Deterministic,
    no-network; memoized per distinct question. Classification failure -> False
    (exclude), so an unattributable bet never tightens the AI's edge demand."""
    try:
        if question in _OPINION_CACHE:
            return _OPINION_CACHE[question]
        from harness import classifier
        val = classifier.tag_market({"question": question}).label == "opinion"
        _OPINION_CACHE[question] = val
        return val
    except Exception:
        return False


# ── per-theme realized P&L (read-only) ────────────────────────────────────────
def theme_pnl(opinion_only: bool = False) -> dict:
    """Aggregate realized paper-P&L per theme over SETTLED positions.

    Returns ``{theme: {n, realized_pnl, n_win, win_rate, roi}}`` where:
      * n            = settled positions in the theme
      * realized_pnl = sum of realized_pnl (already net of slippage + fee)
      * n_win        = positions with realized_pnl > 0
      * win_rate     = n_win / n
      * roi          = realized_pnl / total_staked  (0.0 if nothing staked)

    ``opinion_only=True`` restricts to OPINION-classified markets only — the bets
    the AI swarm path actually places — so ``adaptive_min_edge`` is not tightened
    by a different bet source (e.g. the favorite-longshot price strategy's
    cash-out losses, which land in the ``other`` theme). Default ``False`` reports
    the whole book for the dashboard.

    Read-only and best-effort: any DB error yields ``{}``.
    """
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    except Exception as e:  # pragma: no cover - sqlite connect rarely fails
        _err("adaptive.theme_pnl.connect", e)
        return {}
    try:
        rows = conn.execute(
            "SELECT question, stake, realized_pnl FROM paper_positions "
            # settled (on-chain resolved) AND closed (cashed-out) so per-theme P&L
            # reconciles with the wallet realized_pnl Gate 2 reads (audit #8/#19).
            "WHERE status IN ('settled','closed')"
        ).fetchall()
    except Exception as e:
        # missing table / older schema -> no track record yet
        _err("adaptive.theme_pnl.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    acc: dict = {}
    for r in rows:
        try:
            q = r["question"]
            if opinion_only and not _is_opinion(q):
                continue  # the AI knob only counts the AI's own (opinion) bets
            theme = scoreboard.theme_of(q)
            stake = float(r["stake"]) if r["stake"] is not None else 0.0
            pnl = float(r["realized_pnl"]) if r["realized_pnl"] is not None else 0.0
        except Exception:
            # a single malformed row never breaks the aggregate
            continue
        a = acc.setdefault(theme, {"n": 0, "realized_pnl": 0.0, "n_win": 0, "stake_sum": 0.0})
        a["n"] += 1
        a["realized_pnl"] += pnl
        a["stake_sum"] += stake
        if pnl > 0:
            a["n_win"] += 1

    out: dict = {}
    for theme, a in acc.items():
        n = a["n"]
        out[theme] = {
            "n": n,
            "realized_pnl": round(a["realized_pnl"], 6),
            "n_win": a["n_win"],
            "win_rate": (a["n_win"] / n) if n else 0.0,
            "roi": (a["realized_pnl"] / a["stake_sum"]) if a["stake_sum"] > 0 else 0.0,
        }
    return out


# ── adaptive min-edge (FLOOR ONLY UP) ─────────────────────────────────────────
def adaptive_min_edge(theme: str | None = None, floor: float | None = None) -> float:
    """Return the min_edge to demand, RAISED above the floor only where we lose.

    ``floor`` defaults to ``sizing.DEFAULT_MIN_EDGE``. Contract (be MORE selective
    where you lose, never less):

      * No resolved track record (cold start)            -> floor EXACTLY.
      * theme/overall with n >= MIN_N AND realized_pnl<0  -> floor*(1+PENALTY),
        capped at MAX_MIN_EDGE, but never below the floor.
      * winning / break-even theme, or n < MIN_N          -> floor EXACTLY.

    NEVER returns below the floor. NEVER lowers the threshold for a winning theme.
    Best-effort: any error returns the floor (the conservative default).
    """
    base = sizing.DEFAULT_MIN_EDGE if floor is None else float(floor)
    try:
        # Scope to OPINION markets — the population this knob actually gates (the
        # AI swarm bettor). Excludes the favorite-longshot price strategy's bets so
        # the AI's edge demand is driven by the AI's own track record, not another
        # source's losses. (theme_pnl() without the flag still reports the book.)
        stats = theme_pnl(opinion_only=True)
        if not stats:
            return base  # cold start == floor EXACTLY

        if theme is None:
            # overall book = aggregate across every theme
            n = sum(s["n"] for s in stats.values())
            realized = sum(s["realized_pnl"] for s in stats.values())
        else:
            s = stats.get(theme)
            if not s:
                return base  # no track record for this theme == floor EXACTLY
            n = s["n"]
            realized = s["realized_pnl"]

        # Only TIGHTEN, and only with enough evidence that we are losing.
        # realized_pnl already nets costs, so realized < 0 == losing after costs.
        if n >= MIN_N and realized < 0:
            raised = min(base * (1.0 + PENALTY), MAX_MIN_EDGE)
            return max(raised, base)  # guard: never below the floor even if base > cap

        return base  # winning / break-even / too-thin == floor EXACTLY
    except Exception as e:
        _err("adaptive.adaptive_min_edge", e)
        return base
