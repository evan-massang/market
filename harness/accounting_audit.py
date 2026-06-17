"""harness/accounting_audit.py — Plan 9: read-only accounting truth model + audit.

The ONE canonical, READ-ONLY view of the paper book. It NEVER writes, NEVER repairs,
and NEVER assumes health on a fault. Everything that reports performance/health (db_check,
scoreboard, the Gate-2 readiness gate, the dashboard) consumes this so the bot can never show
fake equity / fake PnL / fake Gate-2 / green-when-stale.

Accounting truth model
----------------------
  starting_bankroll        paper_wallet.starting_bankroll
  cash                     paper_wallet.cash
  open_cost_basis          sum(stake) over OPEN positions
  open_mark_value          sum(shares * current_side_price) over OPEN positions   (needs marks)
  realized_pnl             sum(realized_pnl) over SETTLED/CLOSED positions   (the LEDGER truth)
  unrealized_pnl           open_mark_value - open_cost_basis                  (needs marks)
  equity                   cash + open_mark_value                             (MTM; needs marks)
  equity_from_ledger       starting_bankroll + realized_pnl + unrealized_pnl
  equity_from_wallet_state cash + open_cost_basis        (the at-cost number the old code used)
  drift                    max(|wallet.cash - ledger_cash|, |wallet.realized - ledger_realized|)

Rules enforced (as detections, never mutations):
  * cash must never be negative
  * open stake must be positive; entry price in (0,1); shares > 0; side in {YES,NO}
  * realized PnL comes ONLY from settled/closed positions
  * unrealized PnL comes ONLY from open positions marked to a CURRENT price
  * equity must include cash + open MARK value; if marks are missing/stale -> equity UNVERIFIED
  * if the invariant cannot be computed -> status "unknown" (never "ok")
  * if drift exceeds tolerance -> status "drift" (blocking)

No network, no LLM. PAPER-ONLY.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

# tolerance for wallet<->ledger reconciliation (dollars)
DRIFT_TOLERANCE = 0.01
# default freshness window for a mark price (seconds)
DEFAULT_MAX_MARK_AGE = 900

# ── Gate-2 readiness thresholds (fail-closed) ──────────────────────────────────
GATE2_MIN_SAMPLE = 30          # min settled/closed trades
GATE2_MIN_DAYS = 3.0           # min calendar span of settled trades
GATE2_MAX_DRAWDOWN_FRAC = 0.25 # peak-to-trough realized drawdown cap (of starting bankroll)
GATE2_OUTLIER_FRAC = 0.50      # no single trade may be > this share of total |PnL|


# ── db path / read-only connection ─────────────────────────────────────────────
def _db_path(db_path: str | None = None) -> str:
    if db_path:
        return db_path
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _ro_conn(path: str) -> sqlite3.Connection:
    """Open STRICTLY read-only so an audit can never mutate the live DB."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except Exception:
        c = sqlite3.connect(path, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _f(x, default=None):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _parse_ts(s):
    if not s:
        return None
    try:
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


# ── mark resolution ─────────────────────────────────────────────────────────---
def _resolve_mark(mark_source, market_id, now: float, max_age: int):
    """Return (yes_price | None, is_stale: bool).

    ``mark_source`` may be:
      * None                              -> no marks at all
      * a dict {market_id: price}         -> price, age UNKNOWN -> treated as STALE (unverifiable)
      * a dict {market_id: {price, time}} -> price + freshness checked against max_age
      * a callable market_id -> (None | price | {price, time} | (price, time))
    A mark is FRESH only if a price is present AND its time is within ``max_age``. A mark with
    no verifiable timestamp is treated as STALE (we never trust an unverifiable mark)."""
    if mark_source is None:
        return None, True
    raw = None
    try:
        if callable(mark_source):
            raw = mark_source(market_id)
        elif isinstance(mark_source, dict):
            raw = mark_source.get(market_id)
    except Exception:
        return None, True
    if raw is None:
        return None, True
    price = mtime = None
    if isinstance(raw, dict):
        price = _f(raw.get("price"))
        mtime = _parse_ts(raw.get("time")) if not isinstance(raw.get("time"), (int, float)) else _f(raw.get("time"))
    elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
        price = _f(raw[0])
        mtime = _f(raw[1]) if isinstance(raw[1], (int, float)) else _parse_ts(raw[1])
    else:
        price = _f(raw)            # a bare price -> no timestamp -> unverifiable freshness
    if price is None:
        return None, True
    if mtime is None:
        return price, True          # have a price but cannot verify freshness -> stale
    return price, bool((now - mtime) > max_age)


# ── the audit ───────────────────────────────────────────────────────────────---
def _blank(status, reasons, **over):
    out = {
        "ok": status == "ok",
        "status": status,
        "reasons": list(reasons),
        "starting_bankroll": None, "cash": None,
        "open_cost_basis": 0.0, "open_mark_value": None,
        "realized_pnl": None, "unrealized_pnl": None,
        "equity": None, "total_pnl": None, "drift": None,
        "mark_stale_count": 0, "open_position_count": 0, "closed_position_count": 0,
        "details": {},
    }
    out.update(over)
    return out


def audit_accounting(db_path: str | None = None, *, now: float | None = None,
                     mark_source=None, max_mark_age_seconds: int = DEFAULT_MAX_MARK_AGE) -> dict:
    """Read-only accounting audit. Returns the canonical truth-model dict (see module docs).

    ``ok`` is True ONLY when status == "ok": consistent ledger, no negative cash, no invalid /
    duplicate / incoherent positions, and (if there are open positions) every one marked to a
    FRESH price so equity is verifiable. Missing/stale marks -> status "degraded" (equity
    UNVERIFIED). Wallet<->ledger drift / negative cash / invalid rows -> status "drift"
    (blocking). DB locked/unavailable -> status "error". Cannot compute -> status "unknown".
    NEVER writes, NEVER repairs."""
    now = time.time() if now is None else now
    path = _db_path(db_path)
    reasons: list[str] = []

    if not os.path.exists(path):
        return _blank("error", ["accounting_db_unavailable"], details={"db": path, "error": "missing"})
    try:
        conn = _ro_conn(path)
    except Exception as e:
        return _blank("error", ["accounting_db_unavailable"], details={"db": path, "error": str(e)})

    try:
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except Exception as e:
            return _blank("error", ["accounting_db_unavailable"], details={"db": path, "error": str(e)})
        if "paper_wallet" not in names or "paper_positions" not in names:
            return _blank("unknown", ["accounting_missing_table"],
                          details={"db": path, "have_wallet": "paper_wallet" in names,
                                   "have_positions": "paper_positions" in names})

        w = conn.execute("SELECT starting_bankroll, cash, realized_pnl FROM paper_wallet WHERE id=1").fetchone()
        if w is None:
            return _blank("unknown", ["accounting_equity_unknown"], details={"db": path, "error": "no wallet row"})
        starting = _f(w["starting_bankroll"], 0.0)
        cash = _f(w["cash"], 0.0)
        wallet_realized = _f(w["realized_pnl"], 0.0)

        open_rows = conn.execute(
            "SELECT market_id, side, stake, fill_price, shares, event_slug, end_date "
            "FROM paper_positions WHERE status='open'").fetchall()
        closed = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) r, COALESCE(SUM(stake),0) s, COALESCE(SUM(fee),0) f, "
            "COALESCE(SUM(payout),0) p, COUNT(*) c FROM paper_positions WHERE status IN ('settled','closed')"
        ).fetchone()
        all_agg = conn.execute(
            "SELECT COALESCE(SUM(stake),0) s, COALESCE(SUM(fee),0) f, "
            "COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN payout ELSE 0 END),0) p "
            "FROM paper_positions").fetchone()
    except sqlite3.OperationalError as e:
        try:
            conn.close()
        except Exception:
            pass
        return _blank("error", ["accounting_db_unavailable"], details={"db": path, "error": str(e)})

    # ── realized / ledger reconciliation ──
    ledger_realized = _f(closed["r"], 0.0)
    closed_n = int(closed["c"] or 0)
    ledger_cash = starting - _f(all_agg["s"], 0.0) - _f(all_agg["f"], 0.0) + _f(all_agg["p"], 0.0)
    cash_drift = cash - ledger_cash
    realized_drift = wallet_realized - ledger_realized
    drift = max(abs(cash_drift), abs(realized_drift))

    # ── open positions: cost basis, validity, coherence, marks ──
    open_cost = 0.0
    open_n = 0
    invalid = 0
    seen_markets: dict[str, int] = {}
    yes_by_event: dict[str, int] = {}
    expired = 0
    open_mark_value = 0.0
    mark_stale = 0
    marks_available = mark_source is not None
    for r in open_rows:
        open_n += 1
        side = (r["side"] or "").strip().upper()
        stake = _f(r["stake"])
        fill = _f(r["fill_price"])
        shares = _f(r["shares"])
        bad = (stake is None or stake <= 0 or fill is None or not (0.0 < fill < 1.0)
               or shares is None or shares <= 0 or side not in ("YES", "NO"))
        if bad:
            invalid += 1
        open_cost += (stake or 0.0)
        mid = r["market_id"]
        if mid is not None:
            seen_markets[mid] = seen_markets.get(mid, 0) + 1
        ev = r["event_slug"]
        if ev and side == "YES":
            yes_by_event[ev] = yes_by_event.get(ev, 0) + 1
        ts = _parse_ts(r["end_date"])
        if ts is not None and ts < now:
            expired += 1
        # mark-to-market
        price, is_stale = _resolve_mark(mark_source, mid, now, max_mark_age_seconds)
        if price is None or is_stale:
            mark_stale += 1
            open_mark_value += (stake or 0.0)        # provisional; equity is UNVERIFIED below
        else:
            side_price = price if side == "YES" else (1.0 - price)
            side_price = min(max(side_price, 0.0), 1.0)
            open_mark_value += (shares or 0.0) * side_price
    duplicate_open = sum(1 for c in seen_markets.values() if c > 1)
    multi_yes = sum(1 for c in yes_by_event.values() if c > 1)

    try:
        conn.close()
    except Exception:
        pass

    # equity is VERIFIABLE only if every open position has a fresh mark (or there are none)
    equity_verified = (open_n == 0) or (marks_available and mark_stale == 0)
    if equity_verified:
        equity = round(cash + open_mark_value, 6)
        unrealized = round(open_mark_value - open_cost, 6)
        total_pnl = round(equity - starting, 6)
    else:
        equity = unrealized = total_pnl = None

    # ── classify status (priority: error > drift/hard > unknown > degraded > ok) ──
    if cash < -DRIFT_TOLERANCE:
        reasons.append("accounting_negative_cash")
    if invalid:
        reasons.append("accounting_invalid_position")
    if duplicate_open:
        reasons.append("accounting_duplicate_open_position")
    if multi_yes:
        reasons.append("accounting_multiple_yes_same_event")
    if abs(realized_drift) > DRIFT_TOLERANCE:
        reasons.append("accounting_realized_pnl_mismatch")
    if abs(cash_drift) > DRIFT_TOLERANCE:
        reasons.append("accounting_equity_drift")
    hard = bool(reasons)
    if expired:
        reasons.append("accounting_unsettled_expired_position")
    if open_n and not marks_available:
        reasons.append("accounting_mark_price_missing")
    elif open_n and mark_stale:
        reasons.append("accounting_mark_price_stale")
    if not equity_verified and "accounting_mark_price_missing" not in reasons \
            and "accounting_mark_price_stale" not in reasons:
        reasons.append("accounting_equity_unknown")

    if hard:
        status = "drift"
    elif not equity_verified:
        status = "degraded"
    elif expired:
        status = "degraded"
    else:
        status = "ok"
        reasons = ["accounting_ok"]

    return {
        "ok": status == "ok",
        "status": status,
        "reasons": reasons,
        "starting_bankroll": round(starting, 6),
        "cash": round(cash, 6),
        "open_cost_basis": round(open_cost, 6),
        "open_mark_value": (round(open_mark_value, 6) if equity_verified else None),
        "realized_pnl": round(ledger_realized, 6),
        "unrealized_pnl": unrealized,
        "equity": equity,
        "total_pnl": total_pnl,
        "drift": round(drift, 6),
        "mark_stale_count": mark_stale,
        "open_position_count": open_n,
        "closed_position_count": closed_n,
        "details": {
            "db": path,
            "wallet_realized_pnl": round(wallet_realized, 6),
            "ledger_realized_pnl": round(ledger_realized, 6),
            "realized_drift": round(realized_drift, 6),
            "cash_drift": round(cash_drift, 6),
            "ledger_cash": round(ledger_cash, 6),
            "equity_from_wallet_state": round(cash + open_cost, 6),
            "equity_verified": equity_verified,
            "marks_available": marks_available,
            "invalid_positions": invalid,
            "duplicate_open_markets": duplicate_open,
            "multiple_yes_events": multi_yes,
            "unsettled_expired": expired,
        },
    }


# ── Gate-2 readiness (FAIL-CLOSED) ──────────────────────────────────────────────
def _settled_rows_ro(conn) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT question, realized_pnl, settled_at FROM paper_positions "
            "WHERE status IN ('settled','closed')").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _baseline_n_ro(conn) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM baseline_forecasts WHERE brier_score IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


CLV_MIN_N = 5   # mirror clv.mean_clv's default min_n


def _clv_summary_ro(conn):
    """READ-ONLY mean CLV over the LATEST clv_records row per (market_id, side). NEVER creates
    the table (the gate evaluator must not write). Returns {mean_clv, n} iff n >= CLV_MIN_N."""
    try:
        r = conn.execute(
            "SELECT COUNT(*) c, COALESCE(AVG(clv),0) m FROM ("
            "SELECT clv FROM clv_records t WHERE t.clv IS NOT NULL AND (t.market_id IS NULL OR t.id=("
            "SELECT MAX(id) FROM clv_records t2 WHERE t2.market_id IS t.market_id AND t2.side IS t.side)))"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    n = int(r["c"] or 0)
    if n < CLV_MIN_N:
        return None
    return {"mean_clv": round(float(r["m"] or 0.0), 6), "n": n}


def gate2_status(db_path: str | None = None, *, now: float | None = None, mark_source=None,
                 min_sample: int = GATE2_MIN_SAMPLE, min_days: float = GATE2_MIN_DAYS) -> dict:
    """The canonical Gate-2 (go-to-real-money readiness) gate — FAIL-CLOSED.

    Gate 2 passes ONLY when every required metric is present, fresh, and valid AND the paper
    book is genuinely profitable on VERIFIED (mark-aware) equity. Any missing / stale /
    unverifiable metric yields a specific ``gate2_*`` reason and a non-pass status. Read-only.
    """
    now = time.time() if now is None else now
    audit = audit_accounting(db_path, now=now, mark_source=mark_source)
    reasons: list[str] = []

    # 1) accounting must be verified & consistent
    if audit["status"] in ("error", "unknown"):
        reasons.append("gate2_accounting_unverified")
    elif audit["status"] == "drift":
        reasons.append("gate2_db_drift")
    elif audit["status"] == "degraded":
        reasons.append("gate2_accounting_unverified")   # equity unverified / stale marks

    path = _db_path(db_path)
    rows, baseline_n, mean_clv = [], 0, None
    try:
        conn = _ro_conn(path)
        try:
            rows = _settled_rows_ro(conn)
            baseline_n = _baseline_n_ro(conn)
            mean_clv = _clv_summary_ro(conn)        # read-only — never creates clv_records
        finally:
            conn.close()
    except Exception:
        if "gate2_accounting_unverified" not in reasons:
            reasons.append("gate2_accounting_unverified")

    n_settled = len(rows)
    # 2) sample size
    if n_settled < min_sample:
        reasons.append("gate2_insufficient_sample")
    # 3) time coverage
    times = sorted(t for t in (_parse_ts(r.get("settled_at")) for r in rows) if t is not None)
    span_days = (times[-1] - times[0]) / 86400.0 if len(times) >= 2 else 0.0
    if span_days < min_days:
        reasons.append("gate2_insufficient_time")
    # 4) baseline comparison exists
    if baseline_n <= 0:
        reasons.append("gate2_no_baseline")
    # 5) CLV computed from valid marks (read-only summary gathered above)
    if not mean_clv:
        reasons.append("gate2_clv_unverified")
    # 6) results segmented by theme/environment
    segments: dict[str, dict] = {}
    try:
        from harness.scoreboard import theme_of as _theme_of
    except Exception:
        _theme_of = lambda q: "other"  # noqa: E731
    for r in rows:
        seg = segments.setdefault(_theme_of(r.get("question")), {"n": 0, "pnl": 0.0})
        seg["n"] += 1
        seg["pnl"] += _f(r.get("realized_pnl"), 0.0)
    if not segments:
        reasons.append("gate2_unsegmented_results")
    # 7) max drawdown within limit (peak-to-trough of cumulative realized, in settle order)
    ordered = sorted(rows, key=lambda r: (_parse_ts(r.get("settled_at")) or 0.0))
    cum = peak = max_dd = 0.0
    for r in ordered:
        cum += _f(r.get("realized_pnl"), 0.0)
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    starting = audit.get("starting_bankroll") or 0.0
    if starting > 0 and max_dd > GATE2_MAX_DRAWDOWN_FRAC * starting:
        reasons.append("gate2_drawdown_exceeded")
    # 8) no single outlier trade dominates PnL
    pnls = [_f(r.get("realized_pnl"), 0.0) for r in rows]
    total_abs = sum(abs(p) for p in pnls)
    max_abs = max((abs(p) for p in pnls), default=0.0)
    outlier_frac = (max_abs / total_abs) if total_abs > 0 else 0.0
    if total_abs > 0 and n_settled > 1 and outlier_frac > GATE2_OUTLIER_FRAC:
        reasons.append("gate2_outlier_dominated")
    # 9) uncertainty (REPORTED, not a gate): mean +/- 95% CI of per-trade PnL
    uncertainty = {"available": False}
    if n_settled >= 2:
        mean = sum(pnls) / n_settled
        var = sum((p - mean) ** 2 for p in pnls) / (n_settled - 1)
        std = var ** 0.5
        se = std / (n_settled ** 0.5)
        uncertainty = {"available": True, "mean_pnl": round(mean, 6), "std_pnl": round(std, 6),
                       "ci95_low": round(mean - 1.96 * se, 6), "ci95_high": round(mean + 1.96 * se, 6)}

    # profitability on VERIFIED equity (None equity -> not profitable -> fail-closed)
    realized = audit.get("realized_pnl")
    equity = audit.get("equity")
    profitable = bool(realized is not None and realized > 0
                      and equity is not None and equity >= (audit.get("starting_bankroll") or 0.0))

    gate_pass = (not reasons) and profitable
    if gate_pass:
        status = "pass"
        out_reasons = ["gate2_pass"]
    else:
        status = "unknown" if audit["status"] in ("error", "unknown") else "fail"
        out_reasons = reasons or ["gate2_not_profitable"]

    return {
        "pass": gate_pass, "status": status, "reasons": out_reasons, "paper_only": True,
        "n_settled": n_settled, "min_sample": min_sample,
        "span_days": round(span_days, 3), "min_days": min_days,
        "realized_pnl": realized, "equity": equity, "total_pnl": audit.get("total_pnl"),
        "starting_bankroll": audit.get("starting_bankroll"),
        "baseline_n": baseline_n,
        "mean_clv": (mean_clv.get("mean_clv") if isinstance(mean_clv, dict) else None),
        "clv_n": (mean_clv.get("n") if isinstance(mean_clv, dict) else 0),
        "segments": segments, "max_drawdown": round(max_dd, 6),
        "outlier_frac": round(outlier_frac, 4), "uncertainty": uncertainty,
        "accounting_status": audit["status"], "accounting_reasons": audit["reasons"],
        "accounting_drift": audit.get("drift"),
    }


# ── journal / decision consistency (READ-ONLY) ─────────────────────────────────
def journal_consistency(db_path: str | None = None) -> dict:
    """Read-only decision-journal integrity checks. Proves no-bets / wallet-rejections /
    observe-only are not counted as trades, and that decisions<->positions are coherent.
    Returns {ok, status, checks:[(name, status, detail)], counts:{...}}. Never writes."""
    path = _db_path(db_path)
    checks: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {}

    def add(name, status, detail):
        checks.append((name, status, detail))

    if not os.path.exists(path):
        add("db_file", "FAIL", f"{path} missing")
        return {"ok": False, "status": "error", "checks": checks, "counts": counts}
    try:
        conn = _ro_conn(path)
    except Exception as e:
        add("db_open", "FAIL", str(e))
        return {"ok": False, "status": "error", "checks": checks, "counts": counts}
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "decisions" not in names:
            add("decisions_table", "UNKNOWN", "no decisions table")
            return {"ok": False, "status": "unknown", "checks": checks, "counts": counts}

        bets = conn.execute("SELECT COUNT(*) FROM decisions WHERE status='bet'").fetchone()[0]
        no_bets = conn.execute("SELECT COUNT(*) FROM decisions WHERE status='no_bet'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        counts.update(bets=bets, no_bets=no_bets, total_decisions=total)
        # no-bets must NOT be inside the bet count
        add("no_bet_not_a_bet", "OK" if bets + no_bets <= total else "FAIL",
            f"bets={bets} no_bets={no_bets} total={total} (bet count excludes no_bet)")

        miss_ts = conn.execute("SELECT COUNT(*) FROM decisions WHERE ts IS NULL OR ts=''").fetchone()[0]
        add("decision_timestamp", "WARN" if miss_ts else "OK",
            f"{miss_ts} decision(s) with no timestamp" if miss_ts else "all decisions timestamped")
        miss_mid = conn.execute("SELECT COUNT(*) FROM decisions WHERE market_id IS NULL OR market_id=''").fetchone()[0]
        add("decision_market_id", "WARN" if miss_mid else "OK",
            f"{miss_mid} decision(s) with no market_id" if miss_mid else "all decisions have a market_id")

        if "paper_positions" in names:
            # trade (position) whose market has NO decision row
            orphan_trades = conn.execute(
                "SELECT COUNT(*) FROM paper_positions p WHERE p.market_id IS NOT NULL AND "
                "NOT EXISTS (SELECT 1 FROM decisions d WHERE d.market_id = p.market_id)").fetchone()[0]
            add("trade_without_decision", "WARN" if orphan_trades else "OK",
                f"{orphan_trades} position(s) with no decision row" if orphan_trades
                else "every position has a decision")
            # a position whose market was ONLY ever a no_bet (never a bet) — a no-bet became a trade
            nobet_trades = conn.execute(
                "SELECT COUNT(*) FROM paper_positions p WHERE p.market_id IS NOT NULL AND "
                "EXISTS (SELECT 1 FROM decisions d WHERE d.market_id=p.market_id AND d.status='no_bet') AND "
                "NOT EXISTS (SELECT 1 FROM decisions d2 WHERE d2.market_id=p.market_id AND d2.status='bet')"
            ).fetchone()[0]
            add("no_bet_counted_as_trade", "FAIL" if nobet_trades else "OK",
                f"{nobet_trades} position(s) on a market that was only ever a no_bet" if nobet_trades
                else "no no-bet/rejected decision became a trade")
            # a 'bet' decision whose market has NO position (recorded a bet but no trade exists)
            bet_no_pos = conn.execute(
                "SELECT COUNT(*) FROM decisions d WHERE d.status='bet' AND d.market_id IS NOT NULL AND "
                "NOT EXISTS (SELECT 1 FROM paper_positions p WHERE p.market_id = d.market_id)").fetchone()[0]
            add("bet_without_position", "WARN" if bet_no_pos else "OK",
                f"{bet_no_pos} bet decision(s) with no matching position" if bet_no_pos
                else "every bet decision has a position")
    except Exception as e:
        add("journal", "WARN", f"consistency error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    n_fail = sum(1 for _, s, _ in checks if s == "FAIL")
    n_unknown = sum(1 for _, s, _ in checks if s == "UNKNOWN")
    status = "fail" if n_fail else ("unknown" if n_unknown else "ok")
    return {"ok": status == "ok", "status": status, "checks": checks, "counts": counts}
