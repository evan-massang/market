"""Plan 11 — profit intelligence (PAPER-ONLY).

READ-ONLY learning analytics over the EXISTING tables (decisions, paper_positions,
decision_features, clv/mirofish) plus the Plan 9 truth functions (audit_accounting,
gate2_status, clv.mean_clv). It NEVER opens a position, never calls wallet/safe_bet, never
bypasses a gate, and never claims profitability without Gate 2 (Plan 9) saying pass.

Surfaces:
  * summarize_no_bets()            — group no-bets by reason → learning, not failure
  * summarize_post_trade_learning() — CLV + realized/unrealized/total w/ honesty flags
  * attribution()                  — segment performance w/ sample-size warnings
  * profit_intelligence_report()   — assemble all of the above for the dashboard/CLI
"""
from __future__ import annotations

import os
import sqlite3

PAPER_ONLY_PROFIT_INTELLIGENCE = True

# Sample-size floors below which we refuse to make a performance claim (paper-only learning).
MIN_SETTLED_FOR_CLAIM = 20
MIN_SEGMENT_N = 10

DB_PATH = os.getenv("DATABASE_URL", "polyswarm.db").replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")


def _db(db_path=None) -> str:
    return db_path or os.getenv("DATABASE_URL", "polyswarm.db").replace(
        "sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


# ── no-bet reason classification ────────────────────────────────────────────────
# canonical buckets the plan asks for. order matters (first match wins).
_NO_BET_BUCKETS = [
    ("negative_ev", ("non-positive ev", "negative ev", "ev_after_costs", "ev<=0", "ev gate",
                     "ev_negative", "after-cost")),
    ("no_edge", ("no edge", "edge below", "edge_below", "below threshold", "min_edge", "no_bet edge")),
    ("risk_guard", ("risk guard", "risk_guard", "high spread", "high_spread", "low liquidity",
                    "low_liquidity", "correlation", "bad theme", "bad_theme", "drawdown")),
    ("bankroll", ("bankroll", "insufficient cash", "insufficient_cash", "too small", "min_stake")),
    ("exposure", ("exposure", "concentration", "theme cap", "event cap", "max_same")),
    ("degraded_swarm", ("swarm_degraded", "swarm_aborted", "swarm_insufficient", "swarm_fallback",
                        "swarm_missing", "consensus", "divergence", "degraded")),
    ("parser_failure", ("parse", "parser", "probability_parse", "unparseable")),
    ("mirofish_required_unavailable", ("mirofish",)),
    ("evidence_low", ("no_data", "low_evidence", "evidence")),
    ("stale_market", ("stale",)),
    ("event_incoherence", ("event", "leg", "coheren", "basket", "arb")),
    ("wallet_rejection", ("wallet_rejected", "wallet rejected", "wallet")),
    ("observe_only", ("observe_only", "observe-only")),
    ("accounting_or_dashboard_unsafe", ("accounting", "gate2", "unsafe", "audit")),
    ("mechanical", ("mechanical",)),
]


def classify_no_bet_reason(reason) -> str:
    r = (reason or "").lower()
    if not r:
        return "unknown"
    for bucket, needles in _NO_BET_BUCKETS:
        if any(n in r for n in needles):
            return bucket
    return "unknown"


def _no_bet_suggestion(bucket: str) -> str:
    """A PAPER-ONLY improvement idea for a blocker bucket. NEVER suggests loosening a gate."""
    m = {
        "evidence_low": "Improve/seek additional evidence sources for these themes before forecasting.",
        "stale_market": "Tighten the scanner's staleness filter so these never reach the forecaster.",
        "degraded_swarm": "Investigate swarm degradation (agent failures / low consensus) — a capacity issue, not a gate to relax.",
        "mirofish_required_unavailable": "Improve MiroFish backend availability/freshness; the requirement stays strict.",
        "no_edge": "Expected: most markets are efficient. Focus the scanner on higher-disagreement candidates.",
        "negative_ev": "Expected and correct: after-cost EV ≤ 0 must stay a hard skip. Prioritise tighter-spread markets.",
        "risk_guard": "Prefer deeper-liquidity, tighter-spread markets up front (ranking) — guards stay strict.",
        "exposure": "Diversify candidate themes/events so concentration caps bind less often.",
        "event_incoherence": "Improve event-basket coherence detection upstream; the coherence gate stays strict.",
    }
    return m.get(bucket, "Review these no-bets for patterns; every safety gate remains in force.")


def summarize_no_bets(db_path=None, *, since=None, limit=2000) -> dict:
    """Group recent no-bet decisions by reason → a learning summary (NOT a failure tally)."""
    rows = _safe(lambda: _load_decisions(db_path, status="no_bet", since=since, limit=limit), [])
    by_bucket: dict[str, int] = {}
    by_market: dict[str, int] = {}
    examples: dict[str, str] = {}
    for r in rows:
        bucket = classify_no_bet_reason(r.get("why"))
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        examples.setdefault(bucket, r.get("why") or "")
        mid = r.get("market_id")
        if mid:
            by_market[mid] = by_market.get(mid, 0) + 1
    top_blockers = sorted(by_bucket.items(), key=lambda kv: kv[1], reverse=True)
    repeated_markets = sorted(((m, n) for m, n in by_market.items() if n >= 2),
                              key=lambda kv: kv[1], reverse=True)[:20]
    suggestions = [{"blocker": b, "n": n, "suggestion": _no_bet_suggestion(b),
                    "loosens_safety_gate": False} for b, n in top_blockers[:6]]
    return {
        "total_no_bets": len(rows),
        "by_reason": by_bucket,
        "top_blockers": [{"reason": b, "n": n, "example": examples.get(b, "")} for b, n in top_blockers],
        "repeated_markets": [{"market_id": m, "n": n} for m, n in repeated_markets],
        "repeated_failure_modes": [b for b, n in top_blockers if n >= 3],
        "suggestions": suggestions,
        "note": "No-bets are the safety/EV logic working — not losses. No suggestion loosens a gate.",
        "paper_only": True,
    }


# ── post-trade learning ─────────────────────────────────────────────────────────
def summarize_post_trade_learning(db_path=None, *, since=None) -> dict:
    """Honest post-trade learning from Plan 9 truth + CLV. Refuses strong claims on thin data."""
    from harness import accounting_audit as _acct
    from harness import clv as _clv
    from harness import adaptive as _adp

    audit = _safe(lambda: _acct.audit_accounting(db_path), {"status": "unknown"})
    g2 = _safe(lambda: _acct.gate2_status(db_path), {"status": "unknown", "pass": False})
    overall_clv = _safe(lambda: _clv.mean_clv(min_n=5), None)
    by_theme_clv = _safe(lambda: _clv.clv_by_theme(min_n=5), {})
    theme_pnl = _safe(lambda: guarded_theme_pnl(opinion_only=False), {})

    n_settled = int(g2.get("n_settled") or 0)
    acc_status = audit.get("status", "unknown")
    accounting_unverified = acc_status != "ok"
    clv_unverified = overall_clv is None
    insufficient_sample = n_settled < MIN_SETTLED_FOR_CLAIM

    # the ONLY place a "profitable" hint is even allowed is a Gate-2 pass (Plan 9) — and it is
    # STILL paper. Otherwise the claim is strictly learning / watching / insufficient.
    if insufficient_sample:
        performance_claim = "insufficient_sample"
    elif accounting_unverified:
        performance_claim = "accounting_unverified"
    elif bool(g2.get("pass")):
        performance_claim = "gate2_pass_paper"
    else:
        performance_claim = "learning"

    warnings = []
    if insufficient_sample:
        warnings.append(f"insufficient_sample: {n_settled} settled < {MIN_SETTLED_FOR_CLAIM} needed for a claim")
    if accounting_unverified:
        warnings.append(f"accounting_unverified: status={acc_status}")
    if clv_unverified:
        warnings.append("clv_unverified: not enough resolved CLV points (or stale marks)")
    if (audit.get("mark_stale_count") or 0) > 0:
        warnings.append(f"{audit.get('mark_stale_count')} open positions have STALE marks (unrealized unverified)")

    return {
        "sample_size": n_settled,
        "open_position_count": audit.get("open_position_count"),
        "realized_pnl": audit.get("realized_pnl"),
        "unrealized_pnl": audit.get("unrealized_pnl"),     # None when marks unverified (Plan 9)
        "total_pnl": audit.get("total_pnl"),
        "equity": audit.get("equity"),
        "clv": {
            "overall": overall_clv,                        # None → clv_unverified
            "by_theme": by_theme_clv,
            "final_vs_nonfinal_note": "overall mean_clv is settled (FINAL) CLV; open marks are non-final and excluded from claims",
        },
        "by_theme_pnl": theme_pnl,
        "gate2": {"status": g2.get("status"), "pass": bool(g2.get("pass")), "reasons": g2.get("reasons")},
        "accounting_status": acc_status,
        "insufficient_sample": insufficient_sample,
        "accounting_unverified": accounting_unverified,
        "clv_unverified": clv_unverified,
        "performance_claim": performance_claim,
        "profitable_claim_allowed": bool(g2.get("pass")),
        "warnings": warnings,
        "paper_only": True,
    }


# ── attribution ─────────────────────────────────────────────────────────────────
def _guard_theme_pnl(tp: dict) -> dict:
    """Withhold RATE claims (win_rate, roi) for themes below MIN_SEGMENT_N settled trades. A
    single-trade '100% win rate' must never be presented as a result. Raw facts (n, realized_pnl)
    are kept. Post-processes adaptive.theme_pnl WITHOUT modifying it (the sizing path depends on
    theme_pnl unchanged)."""
    out = {}
    for theme, d in (tp or {}).items():
        d = dict(d)
        n = int(d.get("n") or 0)
        if n < MIN_SEGMENT_N:
            d["win_rate"] = None
            d["roi"] = None
            d["win_rate_available"] = False
            d["warning"] = f"insufficient_sample (n={n} < {MIN_SEGMENT_N}) — win-rate/ROI withheld"
        else:
            d["win_rate_available"] = True
            d["warning"] = None
        out[theme] = d
    return out


def guarded_theme_pnl(opinion_only: bool = False) -> dict:
    """Public, DISPLAY-SAFE theme P&L: adaptive.theme_pnl with small-sample win-rate/ROI withheld
    (MIN_SEGMENT_N). Use this anywhere theme P&L reaches a dashboard/report. It post-processes a
    COPY — adaptive.theme_pnl (and therefore adaptive_min_edge / sizing) is never modified."""
    from harness import adaptive as _adp
    return _guard_theme_pnl(_safe(lambda: _adp.theme_pnl(opinion_only=opinion_only), {}))


def _bucket_liquidity(v):
    if v is None:
        return "unknown"
    v = float(v)
    return "deep(>=5k)" if v >= 5000 else "mid(1k-5k)" if v >= 1000 else "thin(<1k)"


def _bucket_spread(v):
    if v is None:
        return "unknown"
    v = float(v)
    return "tight(<2%)" if v < 0.02 else "mid(2-5%)" if v < 0.05 else "wide(>=5%)"


def attribution(db_path=None, *, since=None) -> dict:
    """Segment SETTLED paper performance by source / mirofish-used / event / theme / buckets,
    each with a sample size and a warning when the sample is too small to trust."""
    from harness import adaptive as _adp

    settled = _safe(lambda: _load_settled(db_path), [])
    feats = _safe(lambda: _feature_index(db_path), {})
    mf_used = _safe(lambda: _mirofish_used_index(db_path), {})

    segs: dict[str, dict[str, dict]] = {"source": {}, "mirofish_used": {}, "event": {},
                                        "liquidity_bucket": {}, "spread_bucket": {},
                                        "confidence_bucket": {}}

    def _add(dim, key, pnl, win):
        d = segs[dim].setdefault(key, {"n": 0, "realized_pnl": 0.0, "n_win": 0})
        d["n"] += 1
        d["realized_pnl"] += pnl
        if win:
            d["n_win"] += 1

    for p in settled:
        mid = p.get("market_id")
        pnl = float(p.get("realized_pnl") or 0.0)
        win = pnl > 0
        f = feats.get(mid, {})
        _add("source", f.get("source") or "unknown", pnl, win)
        _add("mirofish_used", "used" if mf_used.get(mid) else "not_used", pnl, win)
        _add("event", "event" if p.get("event_slug") else "non_event", pnl, win)
        _add("liquidity_bucket", _bucket_liquidity(f.get("liquidity")), pnl, win)
        _add("spread_bucket", _bucket_spread(f.get("spread")), pnl, win)
        conf = f.get("consensus")
        _add("confidence_bucket",
             "unknown" if conf is None else ("high(>=0.7)" if conf >= 0.7 else "mid(0.5-0.7)" if conf >= 0.5 else "low(<0.5)"),
             pnl, win)

    def _finalize(dim):
        out = {}
        for key, d in segs[dim].items():
            n = d["n"]
            enough = n >= MIN_SEGMENT_N
            out[key] = {
                "n": n,
                "realized_pnl": round(d["realized_pnl"], 6),
                "win_rate": (round(d["n_win"] / n, 4) if (enough and n) else None),
                "win_rate_available": enough,
                "warning": (None if enough else f"insufficient_sample (n={n} < {MIN_SEGMENT_N}) — win-rate withheld"),
            }
        return out

    return {
        "by_source": _finalize("source"),
        "by_mirofish_used": _finalize("mirofish_used"),
        "by_event": _finalize("event"),
        "by_liquidity_bucket": _finalize("liquidity_bucket"),
        "by_spread_bucket": _finalize("spread_bucket"),
        "by_confidence_bucket": _finalize("confidence_bucket"),
        "by_theme_pnl": _safe(lambda: guarded_theme_pnl(opinion_only=False), {}),
        "total_settled": len(settled),
        "note": "win-rate is shown only for segments with enough settled trades; everything else is paper-only learning.",
        "paper_only": True,
    }


# ── top-level report ────────────────────────────────────────────────────────────
def profit_intelligence_report(db_path=None) -> dict:
    """Assemble the paper-only profit-intelligence surface. NEVER says 'profitable' unless
    Gate 2 (Plan 9) is pass; otherwise shows learning / insufficient-sample / watching."""
    from harness import accounting_audit as _acct

    audit = _safe(lambda: _acct.audit_accounting(db_path), {"status": "unknown"})
    g2 = _safe(lambda: _acct.gate2_status(db_path), {"status": "unknown", "pass": False})
    no_bets = _safe(lambda: summarize_no_bets(db_path), {"paper_only": True})
    post_trade = _safe(lambda: summarize_post_trade_learning(db_path), {"paper_only": True})
    attrib = _safe(lambda: attribution(db_path), {"paper_only": True})
    recent = _safe(lambda: _recent_decision_signals(db_path), [])

    gate2_pass = bool(g2.get("pass"))
    # the headline label is fail-closed: profitable ONLY on a Gate-2 pass, else honest learning.
    if not gate2_pass:
        if (post_trade.get("sample_size") or 0) < MIN_SETTLED_FOR_CLAIM:
            headline = "insufficient_sample"
        elif post_trade.get("accounting_unverified"):
            headline = "accounting_unverified"
        else:
            headline = "learning"
    else:
        headline = "gate2_pass_paper"

    warnings = list(post_trade.get("warnings") or [])
    if not gate2_pass:
        warnings.append("Gate 2 not pass — no profitability claim is made (paper-only learning).")

    return {
        "paper_only": True,
        "headline": headline,                       # never literally "profitable" w/o gate2 pass
        "profitable_claim_allowed": gate2_pass,
        "accounting_status": audit.get("status"),
        "gate2_status": g2.get("status"),
        "gate2_pass": gate2_pass,
        "top_candidate_reasons": recent,
        "no_bet_intelligence": no_bets,
        "post_trade_learning": post_trade,
        "attribution": attrib,
        "warnings": warnings,
        "needs_more_data": (post_trade.get("sample_size") or 0) < MIN_SETTLED_FOR_CLAIM,
    }


# ── low-level read helpers (read-only, best-effort) ─────────────────────────────
def _load_decisions(db_path=None, *, status=None, since=None, limit=2000) -> list[dict]:
    conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
    try:
        q = "SELECT ts, market_id, question, model_p, market_p, edge, side, stake, fill_price, " \
            "regime, signal, status, why FROM decisions"
        clauses, args = [], []
        if status:
            clauses.append("status=?"); args.append(status)
        if since:
            clauses.append("ts>=?"); args.append(since)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        rows = conn.execute(q, tuple(args)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _load_settled(db_path=None) -> list[dict]:
    conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT market_id, question, side, model_p, market_p, edge, stake, realized_pnl, "
            "event_slug, status FROM paper_positions WHERE status IN ('settled','closed')"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _feature_index(db_path=None) -> dict:
    """market_id -> latest bet/no-bet decision_features (source, liquidity, spread, consensus)."""
    out: dict[str, dict] = {}
    conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT market_id, source, features_json FROM decision_features ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    import json as _json
    for r in rows:
        mid = r["market_id"]
        if not mid:
            continue
        try:
            f = _json.loads(r["features_json"] or "{}")
        except Exception:
            f = {}
        out[mid] = {"source": r["source"] or f.get("source"),
                    "liquidity": f.get("liquidity"), "spread": f.get("spread"),
                    "consensus": f.get("consensus")}
    return out


def _mirofish_used_index(db_path=None) -> dict:
    """market_id -> True if any mirofish run for it was canonically USED (Plan 8). Best-effort."""
    out: dict[str, bool] = {}
    try:
        from harness import mirofish_validate as _mfv
        from harness import mirofish_status as _mfs
    except Exception:
        return out
    conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT DISTINCT market_id FROM mirofish_runs").fetchall()
    except sqlite3.OperationalError:
        conn.close(); return out
    mids = [r["market_id"] for r in rows if r["market_id"]]
    conn.close()
    for mid in mids:
        runs = _safe(lambda mid=mid: _mfv.get_runs(mid, 10), [])
        used = False
        for run in (runs or []):
            try:
                if _mfs.state_from_row(run) == _mfs.FRESH_USED:
                    used = True; break
            except Exception:
                continue
        out[mid] = used
    return out


def _recent_decision_signals(db_path=None, limit=8) -> list[dict]:
    """Recent decision rationales (paper) for the dashboard's 'current candidate reasons' panel."""
    rows = _safe(lambda: _load_decisions(db_path, limit=limit), [])
    out = []
    for r in rows:
        out.append({"market_id": r.get("market_id"), "question": r.get("question"),
                    "action": r.get("status"), "reason": r.get("why"),
                    "reason_bucket": (classify_no_bet_reason(r.get("why")) if r.get("status") == "no_bet" else "bet")})
    return out
