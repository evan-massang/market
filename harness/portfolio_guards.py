"""harness/portfolio_guards.py — P8 (B2): portfolio-concentration + drawdown guards.

GUIDING PRINCIPLE — bet BETTER, not MORE. Every function here can only make the
bot MORE selective: it blocks a bet that is over-correlated with the open book,
that lives in a theme with a SUBSTANTIAL losing record, or it hands the Wire
layer a >= 1.0 multiplier that makes the OTHER quality/theme bars HARDER under
drawdown. Nothing here ever loosens a guard, raises bet frequency, or lets a bet
through that the existing P4/P5/P7 guards would have stopped.

What this ADDS (the gaps P4/P5/P7 do not cover):
  * check_correlation  — concentration risk: too many open positions in the SAME
    theme (scoreboard.theme_of) or the SAME event_slug. The existing guards size
    and gate each bet in isolation; none of them looks at how correlated the new
    bet is with what is ALREADY open.
  * check_bad_theme    — a HARD block for a theme with a substantial, settled
    losing record. This is the complement to P7 ``adaptive.adaptive_min_edge``
    (which only RAISES the edge bar for a mildly-losing theme): once a theme has
    lost real money over enough settled bets, stop betting it outright. The loss
    bar is LOWERED by a ``tighten`` multiplier so it bites sooner under drawdown.
  * drawdown_state / stricter_tighten — read the paper book's realized state and
    derive a single >= 1.0 knob the Wire layer passes to the market-quality and
    bad-theme guards so EVERYTHING gets stricter when we are losing (and exactly
    nothing changes when the book is healthy).

Design contract (identical to harness.adaptive / harness.label_perf):
  * Read-only. We only SELECT from the immutable paper tables (paper_wallet via
    wallet.get_state, paper_positions via wallet.get_open_positions /
    adaptive.theme_pnl). Nothing here writes, alters, or deletes a row.
  * DATABASE_URL-aware: every DB read goes through wallet/adaptive, which resolve
    the path the same way the rest of the harness does, so a test pointing
    DATABASE_URL at a temp file hits that file and the prod polyswarm.db is never
    touched by a test.
  * Best-effort + import-safe: a missing table / DB / malformed row degrades to a
    SAFE default. "Safe" here means DO-NOT-BLOCK (return ok) and DO-NOT-TIGHTEN
    (return 1.0 / severity 0) — an analytics failure must never silently veto a
    clean bet, and it must never raise into the bettor (predict_today / sameday).
  * Drawdown is measured off REALIZED state (settled positions only, via
    wallet.get_state's marked-at-cost equity + realized_pnl), so transient
    open-position swings never flap the verdict.
"""
from __future__ import annotations

from harness import scoreboard
from harness import wallet as paper
from harness import adaptive

try:
    from harness import obs
except Exception:  # pragma: no cover - obs is optional
    obs = None


# ── tunables (all conservative; only ever TIGHTEN / BLOCK, never loosen) ───────
# Drawdown fraction at which severity saturates to 1.0 (a 20% equity drawdown is
# treated as a full-severity drawdown).
DRAWDOWN_REF = 0.20
# Ceiling for the stricter-when-losing multiplier handed to the Wire layer.
MAX_TIGHTEN = 2.0

# Concentration caps: the new bet is blocked when the OPEN book already holds at
# least this many positions sharing the same theme / event (adding one more would
# exceed the cap). Picked permissively so a normal diversified book never trips.
DEFAULT_MAX_SAME_THEME = 4
DEFAULT_MAX_SAME_EVENT = 3

# Bad-theme HARD block: only a SUBSTANTIAL, well-evidenced loss qualifies. A
# mildly-losing theme is deliberately NOT hard-blocked here — that is
# adaptive.adaptive_min_edge's job (it just raises the edge bar). BAD_THEME_LOSS
# is the realized-$ loss bar at tighten=1.0; ``tighten`` LOWERS it (bites sooner).
BAD_THEME_MIN_N = 20
BAD_THEME_LOSS = 50.0


def _err(where: str, exc: Exception) -> None:
    if obs:
        try:
            obs.hooks.on_error(where=where, exc=exc, action="skip")
        except Exception:
            pass


def _f(x, default: float = 0.0) -> float:
    try:
        return default if x is None else float(x)
    except Exception:
        return default


# ── open book snapshot (read-only) ────────────────────────────────────────────
def _open_positions_impl() -> list[dict]:
    """The real reader behind :func:`open_positions` (kept private so the public
    name can also be used as a parameter in :func:`check_correlation`)."""
    try:
        rows = paper.get_open_positions()
    except Exception as e:
        _err("portfolio_guards.open_positions", e)
        return []
    out: list[dict] = []
    for r in rows or []:
        try:
            q = r.get("question")
            out.append({
                "market_id": r.get("market_id"),
                "question": q,
                "side": r.get("side"),
                "stake": _f(r.get("stake")),
                "theme": scoreboard.theme_of(q or ""),
                "event_slug": r.get("event_slug"),
            })
        except Exception:
            # a single malformed row never breaks the snapshot
            continue
    return out


def open_positions() -> list[dict]:
    """Open paper positions as ``[{market_id, question, side, stake, theme,
    event_slug}]`` (theme via scoreboard.theme_of). Read-only; ``[]`` on error."""
    return _open_positions_impl()


# ── drawdown state (realized, marked-at-cost) ─────────────────────────────────
def drawdown_state() -> dict:
    """The paper book's drawdown state, derived from REALIZED equity.

    Returns ``{equity, starting_bankroll, realized_pnl, drawdown_frac,
    in_drawdown, severity}`` where:
      * drawdown_frac = max(0, (starting - equity) / starting)   [0.0 if no start]
      * in_drawdown   = realized_pnl < 0  OR  equity < starting_bankroll
      * severity      = min(1, drawdown_frac / DRAWDOWN_REF) in [0, 1]

    Equity is wallet.get_state()'s marked-at-cost equity (open positions valued at
    stake), so an open bet that is temporarily under water does NOT move the
    drawdown verdict — only settled (realized) P&L can. A healthy or empty book
    returns ``in_drawdown=False, severity=0.0``. Best-effort: any DB error
    degrades to a healthy (non-tightening) state.
    """
    try:
        st = paper.get_state()
    except Exception as e:
        _err("portfolio_guards.drawdown_state", e)
        st = {"starting_bankroll": 0.0, "equity": 0.0, "realized_pnl": 0.0}

    starting = _f(st.get("starting_bankroll"))
    equity = _f(st.get("equity"))
    realized = _f(st.get("realized_pnl"))

    drawdown_frac = max(0.0, (starting - equity) / starting) if starting > 0 else 0.0
    in_drawdown = bool(realized < 0 or (starting > 0 and equity < starting))
    severity = min(1.0, drawdown_frac / DRAWDOWN_REF) if DRAWDOWN_REF > 0 else 0.0
    severity = max(0.0, severity)

    return {
        "equity": round(equity, 6),
        "starting_bankroll": round(starting, 6),
        "realized_pnl": round(realized, 6),
        "drawdown_frac": round(drawdown_frac, 6),
        "in_drawdown": in_drawdown,
        "severity": round(severity, 6),
    }


def stricter_tighten() -> float:
    """The SINGLE >= 1.0 knob the Wire layer passes to the market-quality and
    bad-theme guards so everything gets stricter when we are losing.

    1.0 when the book is healthy (severity 0); scales linearly with drawdown
    severity up to MAX_TIGHTEN at a full-severity drawdown. NEVER below 1.0, so it
    can only ever make the other guards HARDER, never softer. Best-effort: any
    error returns 1.0 (no tightening).
    """
    try:
        sev = _f(drawdown_state().get("severity"))
    except Exception as e:
        _err("portfolio_guards.stricter_tighten", e)
        sev = 0.0
    sev = max(0.0, min(1.0, sev))
    return 1.0 + sev * (MAX_TIGHTEN - 1.0)


# ── candidate field extraction ────────────────────────────────────────────────
def _market_theme(market) -> str:
    """Theme of a candidate market via scoreboard.theme_of — the SAME tagger
    open_positions() uses, so the candidate and the held book are comparable."""
    try:
        if isinstance(market, dict):
            return scoreboard.theme_of(market.get("question") or "")
        return scoreboard.theme_of(str(market or ""))
    except Exception:
        return "other"


def _market_event(market):
    """event_slug of a candidate market, or None when absent (no event grouping)."""
    try:
        if isinstance(market, dict):
            ev = market.get("event_slug")
            return ev or None
    except Exception:
        pass
    return None


# ── correlation / concentration guard ─────────────────────────────────────────
def check_correlation(market, side, open_positions=None,
                      max_same_theme: int = DEFAULT_MAX_SAME_THEME,
                      max_same_event: int = DEFAULT_MAX_SAME_EVENT):
    """Block over-correlated exposure (concentration risk).

    Returns ``(ok, reason, detail)``. ``reason`` is ``'correlated_exposure'`` and
    ``ok`` is False when adding this bet would EXCEED the cap of open positions
    sharing the SAME event_slug, or the SAME (real) theme. Otherwise ``(True,
    None, detail)``.

    Notes:
      * "would exceed the cap" == the book already holds >= the cap, so adding one
        more crosses it.
      * The unclassified ``"other"`` theme is the catch-all bucket (unrelated
        markets), NOT a real correlated cluster, so it is exempt from the theme
        cap — this is the explicit guard against OVER-blocking a clean market.
      * Event grouping only applies when the candidate carries an event_slug;
        without one there is no event to be concentrated in.
      * Healthy / empty book -> ``(True, None, detail)``. Best-effort: any error
        returns ``(True, None, {})`` (an analytics failure never vetoes a bet).
    """
    try:
        positions = open_positions if open_positions is not None else _open_positions_impl()
        theme = _market_theme(market)
        event = _market_event(market)

        n_theme = sum(1 for p in positions if (p.get("theme") if isinstance(p, dict) else None) == theme)
        n_event = sum(1 for p in positions
                      if event and (p.get("event_slug") if isinstance(p, dict) else None) == event)

        detail = {
            "theme": theme,
            "event_slug": event,
            "side": side,
            "n_same_theme": n_theme,
            "n_same_event": n_event,
            "max_same_theme": max_same_theme,
            "max_same_event": max_same_event,
        }

        # Event concentration (only meaningful when we know the event).
        if event and n_event >= max_same_event:
            detail["trigger"] = "event"
            return (False, "correlated_exposure", detail)

        # Theme concentration (skip the unclassified 'other' bucket).
        if theme and theme != "other" and n_theme >= max_same_theme:
            detail["trigger"] = "theme"
            return (False, "correlated_exposure", detail)

        return (True, None, detail)
    except Exception as e:
        _err("portfolio_guards.check_correlation", e)
        return (True, None, {})


# ── bad-theme HARD block (complement to adaptive_min_edge) ─────────────────────
def check_bad_theme(question, tighten: float = 1.0):
    """HARD-block a theme with a SUBSTANTIAL, well-evidenced losing record.

    Returns ``(ok, reason, detail)``. ``reason`` is ``'bad_theme'`` and ``ok`` is
    False only when the question's theme has, over OPINION-classified settled bets
    (adaptive.theme_pnl(opinion_only=True)):
        n >= BAD_THEME_MIN_N  AND  realized_pnl <= -(BAD_THEME_LOSS / tighten)

    ``tighten`` (>= 1.0, from :func:`stricter_tighten`) LOWERS the loss bar so the
    block bites sooner under drawdown. A MILDLY-losing theme is intentionally NOT
    hard-blocked here — that is adaptive.adaptive_min_edge's job (raise the edge
    bar). Cold start (no settled history for the theme) -> ``(True, None, ...)``.
    Best-effort: any error returns ``(True, None, {})``.
    """
    try:
        theme = scoreboard.theme_of(question or "")
        try:
            stats = adaptive.theme_pnl(opinion_only=True)
        except Exception as e:
            _err("portfolio_guards.check_bad_theme.theme_pnl", e)
            stats = {}

        t = max(1.0, _f(tighten, 1.0))
        loss_bar = BAD_THEME_LOSS / t
        detail = {"theme": theme, "tighten": round(t, 4),
                  "min_n": BAD_THEME_MIN_N, "loss_bar": round(loss_bar, 4)}

        s = stats.get(theme)
        if not s:
            detail["reason"] = "cold_start"
            return (True, None, detail)

        n = int(s.get("n") or 0)
        realized = _f(s.get("realized_pnl"))
        detail.update({"n": n, "realized_pnl": round(realized, 6)})

        if n >= BAD_THEME_MIN_N and realized <= -loss_bar:
            detail["trigger"] = "substantial_loss"
            return (False, "bad_theme", detail)

        return (True, None, detail)
    except Exception as e:
        _err("portfolio_guards.check_bad_theme", e)
        return (True, None, {})
