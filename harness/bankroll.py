"""harness/bankroll.py — P9 sizing / bankroll risk controls.

A pre-bet KILL SWITCH + stake-based exposure caps + a mark-to-market equity view.
Every control is a TIGHTENING — it can only PAUSE betting or SHRINK/SKIP a bet,
never approve a new bet or increase frequency. Cold start / healthy book -> every
control passes, so today's behavior is unchanged; the controls bite only once the
book is actually losing or over-concentrated.

Read-only over the paper wallet (paper_wallet + paper_positions). DATABASE_URL-
aware (resolved at call time, like harness.label_perf) and best-effort: any error
degrades to the PERMISSIVE default for caps (a bug never vetoes a bet) EXCEPT the
kill switch, which on a read error stays permissive too (fail-open, never crash
the bettor) and logs via obs.

Scope notes:
  * drawdown_pause + loss_limit are BOOK-WIDE (capital preservation protects the
    shared bankroll regardless of which strategy drew it down).
  * the losing-streak cooldown is OPINION-SCOPED (the AI swarm's own settled bets)
    so the favorite-longshot price strategy's cash-out losses don't pause the AI.
"""
from __future__ import annotations

import os
import sqlite3

from harness import portfolio_guards

try:
    from harness import obs
except Exception:  # pragma: no cover - obs optional
    obs = None
try:
    from harness import adaptive  # for _is_opinion (opinion-scoped cooldown)
except Exception:  # pragma: no cover
    adaptive = None


# ── tunables (all conservative; only ever PAUSE / SHRINK) ──────────────────────
MAX_DRAWDOWN_FRAC = 0.25        # pause new bets when equity is >= 25% below start
MAX_TOTAL_LOSS_FRAC = 0.30      # hard loss limit: realized <= -30% of starting
COOLDOWN_STREAK = 5             # this many consecutive OPINION losses -> pause
MAX_THEME_EXPOSURE_FRAC = 0.25  # <= 25% of bankroll open in any one theme
MAX_EVENT_EXPOSURE_FRAC = 0.15  # <= 15% of bankroll open in any one event


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


# ── losing-streak (opinion-scoped) ────────────────────────────────────────────
def opinion_loss_streak() -> int:
    """Count the most-recent CONSECUTIVE losing settled OPINION bets (resets on a
    win). 0 when there is no history. Best-effort -> 0 on any error."""
    try:
        conn = _conn()
    except Exception as e:
        _err("bankroll.opinion_loss_streak.connect", e)
        return 0
    try:
        rows = conn.execute(
            "SELECT question, realized_pnl FROM paper_positions "
            "WHERE status='settled' AND settled_at IS NOT NULL "
            "ORDER BY settled_at DESC, id DESC"
        ).fetchall()
    except Exception as e:
        _err("bankroll.opinion_loss_streak.select", e)
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    streak = 0
    for r in rows:
        q = r["question"]
        if adaptive is not None:
            try:
                if not adaptive._is_opinion(q):
                    continue  # ignore the price strategy's bets
            except Exception:
                pass
        pnl = r["realized_pnl"]
        if pnl is None:
            continue
        if float(pnl) < 0:
            streak += 1
        else:
            break  # a win (or break-even) ends the streak
    return streak


# ── the kill switch ───────────────────────────────────────────────────────────
def can_trade() -> tuple[bool, str]:
    """Pre-bet KILL SWITCH. Returns ``(ok, reason)``.

    Pauses NEW bets (the forecast is still computed + logged + frozen for scoring;
    only the bet is withheld — i.e. observe-only) when:
      * drawdown_pause: equity is >= MAX_DRAWDOWN_FRAC below the starting bankroll
      * loss_limit:     realized P&L <= -(MAX_TOTAL_LOSS_FRAC * starting)
      * cooldown:       >= COOLDOWN_STREAK consecutive losing OPINION bets

    FAIL-OPEN: any error -> (True, ...) so the bettor never crashes or wrongly halts.
    """
    try:
        st = portfolio_guards.drawdown_state()
        starting = st.get("starting_bankroll") or 0.0
        if starting > 0:
            if st.get("drawdown_frac", 0.0) >= MAX_DRAWDOWN_FRAC:
                return False, f"drawdown_pause ({st['drawdown_frac']:.0%} >= {MAX_DRAWDOWN_FRAC:.0%})"
            if (st.get("realized_pnl") or 0.0) <= -(MAX_TOTAL_LOSS_FRAC * starting):
                return False, f"loss_limit (realized ${st['realized_pnl']:.2f} <= -${MAX_TOTAL_LOSS_FRAC*starting:.2f})"
        streak = opinion_loss_streak()
        if streak >= COOLDOWN_STREAK:
            return False, f"cooldown (losing streak {streak} >= {COOLDOWN_STREAK})"
        return True, "ok"
    except Exception as e:
        _err("bankroll.can_trade", e)
        return True, "can_trade_error"


# ── stake-based exposure caps (per theme / event) ─────────────────────────────
def _open_stake_by(theme: str | None, event: str | None) -> tuple[float, float]:
    """(open_stake_in_theme, open_stake_in_event) over OPEN positions."""
    try:
        positions = portfolio_guards.open_positions()
    except Exception as e:
        _err("bankroll._open_stake_by", e)
        return 0.0, 0.0
    t_sum = e_sum = 0.0
    for p in positions:
        try:
            stake = float(p.get("stake") or 0.0)
        except Exception:
            stake = 0.0
        if theme and p.get("theme") == theme:
            t_sum += stake
        if event and p.get("event_slug") == event:
            e_sum += stake
    return t_sum, e_sum


def exposure_ok(theme, event, new_stake, bankroll=None,
                max_theme_frac: float = MAX_THEME_EXPOSURE_FRAC,
                max_event_frac: float = MAX_EVENT_EXPOSURE_FRAC) -> tuple[bool, str, dict]:
    """Block a new bet that would push open STAKE in its theme/event past the cap.

    Returns ``(ok, reason, detail)``. reason in {'theme_exposure_cap',
    'event_exposure_cap'} on a block. Healthy / under-cap -> (True, None, detail).
    The unclassified 'other' theme is EXEMPT from the theme cap (it is a catch-all
    of unrelated markets, not a real concentrated cluster). Best-effort -> allow.
    """
    try:
        if bankroll is None:
            bankroll = portfolio_guards.drawdown_state().get("equity") or 0.0
        bankroll = float(bankroll or 0.0)
        new_stake = float(new_stake or 0.0)
        t_sum, e_sum = _open_stake_by(theme, event)
        detail = {"theme": theme, "event": event, "bankroll": round(bankroll, 2),
                  "open_theme_stake": round(t_sum, 2), "open_event_stake": round(e_sum, 2),
                  "new_stake": round(new_stake, 2)}
        if bankroll <= 0:
            return True, None, detail  # no bankroll signal -> don't veto
        if event and (e_sum + new_stake) > max_event_frac * bankroll:
            detail["cap"] = round(max_event_frac * bankroll, 2)
            return False, "event_exposure_cap", detail
        if theme and theme != "other" and (t_sum + new_stake) > max_theme_frac * bankroll:
            detail["cap"] = round(max_theme_frac * bankroll, 2)
            return False, "theme_exposure_cap", detail
        return True, None, detail
    except Exception as e:
        _err("bankroll.exposure_ok", e)
        return True, None, {}


# ── mark-to-market equity ─────────────────────────────────────────────────────
def mark_to_market_equity(price_map: dict | None = None) -> dict:
    """Equity valuing OPEN positions at the CURRENT market price instead of at cost.

    ``price_map`` maps market_id -> current YES price (0..1). For an open position
    of ``shares`` on ``side``, the mark value is ``shares * current_side_price``
    (YES uses the current price, NO uses 1 - current price). A market_id absent
    from ``price_map`` falls back to its entry fill (== at-cost), so without a
    price_map this equals the wallet's at-cost equity.

    Returns ``{cash, mtm_open_value, mtm_equity, n_open, n_marked}``. Read-only,
    best-effort.
    """
    price_map = price_map or {}
    try:
        conn = _conn()
    except Exception as e:
        _err("bankroll.mark_to_market_equity.connect", e)
        return {"cash": 0.0, "mtm_open_value": 0.0, "mtm_equity": 0.0, "n_open": 0, "n_marked": 0}
    cash = 0.0
    open_val = 0.0
    n_open = n_marked = 0
    try:
        w = conn.execute("SELECT cash FROM paper_wallet WHERE id=1").fetchone()
        cash = float(w["cash"]) if w and w["cash"] is not None else 0.0
        rows = conn.execute(
            "SELECT market_id, side, shares, stake, fill_price FROM paper_positions WHERE status='open'"
        ).fetchall()
        for r in rows:
            n_open += 1
            try:
                shares = float(r["shares"] or 0.0)
                mid = r["market_id"]
                if mid in price_map:
                    cur = float(price_map[mid])
                    side_price = cur if (r["side"] or "YES").upper() == "YES" else (1.0 - cur)
                    side_price = min(max(side_price, 0.0), 1.0)
                    open_val += shares * side_price
                    n_marked += 1
                else:
                    # fall back to entry cost (== at-cost equity contribution)
                    open_val += float(r["stake"] or (shares * float(r["fill_price"] or 0.0)))
            except Exception:
                continue
    except Exception as e:
        _err("bankroll.mark_to_market_equity.read", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"cash": round(cash, 4), "mtm_open_value": round(open_val, 4),
            "mtm_equity": round(cash + open_val, 4), "n_open": n_open, "n_marked": n_marked}
