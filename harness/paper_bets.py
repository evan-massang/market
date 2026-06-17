"""harness/paper_bets.py — PAPER-ONLY active/settled bet assembly for the dashboard cockpit.

Read-only. Builds the active-bet / settled-bet / payout-preview / proof-timeline views the
dashboard needs, joining paper_positions (Plan 4 wallet) to the decision journal (Plan 9) and
decision_features (Plan 11) for the AI reasoning + gates behind each bet.

HONESTY:
  * Payout preview uses the EXACT wallet share model (binary $1-payout shares for BOTH YES and NO):
    payout_if_win = shares; profit_if_win = shares - stake - fee; max_loss = stake + fee.
    Invalid/ambiguous inputs return payout_unknown — never an invented number.
  * Market link is built ONLY from a stored event_slug → https://polymarket.com/event/<slug>;
    otherwise link_unavailable. No fake links.
  * A live mark is not fetched here (no network in the read path); current_price / unrealized_pnl /
    CLV are None ("unknown") unless a mark is supplied. No fake PnL.
  * Never trades, never opens positions, never mutates the wallet. paper_only=True everywhere.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

PAPER_ONLY = True
POLYMARKET_EVENT_URL = "https://polymarket.com/event/{slug}"

# core gates an OPENED paper bet provably cleared (it could not exist in the wallet otherwise —
# Plan 3 routes every open through the gated opener / the daemon inline stack).
CORE_GATES_PASSED = ["parser", "swarm_health", "ev_after_costs", "risk_guards",
                     "bankroll", "exposure", "wallet_atomic"]


def _db(db_path=None) -> str:
    return db_path or os.getenv("DATABASE_URL", "polyswarm.db").replace(
        "sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def market_url(event_slug):
    """(url, status). Only build a link from a real stored event_slug; else link_unavailable."""
    if event_slug and isinstance(event_slug, str) and event_slug.strip():
        return POLYMARKET_EVENT_URL.format(slug=event_slug.strip()), "ok"
    return None, "link_unavailable"


def compute_payout_preview(position: dict) -> dict:
    """Possible payout/profit-if-win + max loss for a paper position, from the verified wallet
    model (shares are $1 binary payout shares for BOTH sides). Never invents a number."""
    fill = _f(position.get("fill_price"))
    shares = _f(position.get("shares"))
    stake = _f(position.get("stake"))
    fee = _f(position.get("fee")) or 0.0
    if fill is None or not (0.0 < fill < 1.0):
        return {"ok": False, "possible_payout_if_win": None, "possible_profit_if_win": None,
                "max_loss": None, "reason": "invalid_position"}
    if shares is None or shares <= 0 or stake is None or stake <= 0:
        return {"ok": False, "possible_payout_if_win": None, "possible_profit_if_win": None,
                "max_loss": None, "reason": "payout_unknown"}
    payout = round(shares * 1.0, 6)
    return {"ok": True, "possible_payout_if_win": payout,
            "possible_profit_if_win": round(payout - stake - fee, 6),
            "max_loss": round(stake + fee, 6), "reason": "payout_ok"}


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def timer_for(end_date, *, now=None, status_text="open"):
    """(seconds_until_end, timer_status, end_time_iso). Honest about missing/invalid/ended times."""
    now = now or datetime.now(timezone.utc)
    dt = _parse_ts(end_date)
    if dt is None:
        return None, ("timer_unknown" if not end_date else "invalid_timer"), end_date
    try:
        secs = (dt - now).total_seconds()
    except Exception:
        return None, "invalid_timer", end_date
    if secs <= 0:
        return 0, "awaiting_settlement", dt.isoformat()
    return int(secs), "running", dt.isoformat()


def _evidence_bucket(q):
    q = _f(q)
    if q is None:
        return "unknown"
    return "high" if q >= 0.6 else "medium" if q >= 0.3 else "low"


def _decision_context(market_id, db_path=None) -> dict:
    """Recover the AI reasoning behind a market's bet: latest journal decision (why, model_p,
    signal, status) + latest decision_features (consensus, evidence, mirofish, challenger).
    Best-effort; missing fields stay None."""
    ctx = {"ai_reason": None, "forecast_probability": None, "challenger_probability": None,
           "mirofish_state": "unknown", "evidence_quality": "unknown", "consensus": None,
           "edge_after_costs": None, "blocked_by_gate": None, "decision_status": None}
    if not market_id:
        return ctx
    try:
        conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
        try:
            d = conn.execute("SELECT * FROM decisions WHERE market_id=? ORDER BY id DESC LIMIT 1",
                             (market_id,)).fetchone()
            if d:
                ctx["ai_reason"] = d["why"]
                ctx["forecast_probability"] = _f(d["model_p"])
                ctx["decision_status"] = d["status"]
            try:
                f = conn.execute("SELECT * FROM decision_features WHERE market_id=? "
                                 "ORDER BY id DESC LIMIT 1", (market_id,)).fetchone()
            except sqlite3.OperationalError:
                f = None
            if f:
                fd = dict(f)
                import json as _json
                try:
                    feats = _json.loads(fd.get("features_json") or "{}")
                except Exception:
                    feats = {}
                ctx["forecast_probability"] = ctx["forecast_probability"] or _f(fd.get("forecast_probability"))
                ctx["challenger_probability"] = _f(feats.get("challenger_probability"))
                ctx["mirofish_state"] = fd.get("mirofish_state") or feats.get("mirofish_state") or "unknown"
                ctx["evidence_quality"] = _evidence_bucket(feats.get("evidence_quality"))
                ctx["consensus"] = _f(feats.get("consensus"))
                ctx["edge_after_costs"] = _f(feats.get("edge_after_costs"))
                ctx["blocked_by_gate"] = fd.get("blocked_by_gate")
                ctx["ai_reason"] = ctx["ai_reason"] or fd.get("reason")
        finally:
            conn.close()
    except Exception:
        pass
    return ctx


def _rows(db_path, where):
    try:
        conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(f"SELECT * FROM paper_positions WHERE {where} ORDER BY id DESC").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def open_positions(db_path=None, *, now=None, mark_for=None) -> dict:
    """Active paper bets with links, countdown, payout preview, and AI reasoning. mark_for is an
    optional callable(position)->current_price for unrealized PnL/CLV; None ⇒ shown as unknown."""
    out = []
    for p in _rows(db_path, "status='open'"):
        url, url_status = market_url(p.get("event_slug"))
        payout = compute_payout_preview(p)
        secs, tstatus, end_iso = timer_for(p.get("end_date"), now=now)
        ctx = _decision_context(p.get("market_id"), db_path)
        cur = None
        if mark_for is not None:
            try:
                cur = _f(mark_for(p))
            except Exception:
                cur = None
        unreal = None
        if cur is not None and _f(p.get("shares")) is not None:
            # mark-to-market on $1 binary shares: value = shares*current_price - stake - fee
            unreal = round(_f(p["shares"]) * cur - _f(p.get("stake") or 0) - _f(p.get("fee") or 0), 6)
        out.append({
            "position_id": str(p.get("id")), "market_id": p.get("market_id"),
            "condition_id": None, "token_id": None, "slug": p.get("event_slug"),
            "question": p.get("question"), "url": url, "url_status": url_status,
            "side": p.get("side"), "entry_price": _f(p.get("fill_price")),
            "current_price": cur, "shares": _f(p.get("shares")), "stake": _f(p.get("stake")),
            "fee": _f(p.get("fee")),
            "possible_payout_if_win": payout["possible_payout_if_win"],
            "possible_profit_if_win": payout["possible_profit_if_win"],
            "max_loss": payout["max_loss"], "payout_reason": payout["reason"],
            "unrealized_pnl": unreal, "clv": None,
            "opened_at": p.get("opened_at"), "end_time": end_iso,
            "seconds_until_end": secs, "timer_status": tstatus, "status": p.get("status") or "open",
            "ai_reason": ctx["ai_reason"], "gates_passed": list(CORE_GATES_PASSED),
            "forecast_probability": ctx["forecast_probability"],
            "challenger_probability": ctx["challenger_probability"],
            "mirofish_state": ctx["mirofish_state"] or "unknown",
            "evidence_quality": ctx["evidence_quality"], "consensus": ctx["consensus"],
            "verified": (payout["ok"] and url_status == "ok"),
            "warnings": ([] if payout["ok"] else [payout["reason"]])
                        + ([] if url_status == "ok" else ["link_unavailable"]),
            "paper_only": True,
        })
    return {"paper_only": True, "generated_at": _now_iso(), "positions": out}


def settled_positions(db_path=None, *, limit=100) -> dict:
    out = []
    for p in _rows(db_path, "status IN ('settled','closed')")[:limit]:
        url, url_status = market_url(p.get("event_slug"))
        ctx = _decision_context(p.get("market_id"), db_path)
        outcome = p.get("outcome")
        won = None
        if outcome is not None:
            won = "won" if (_f(p.get("realized_pnl")) or 0) > 0 else "lost"
        final_price = None
        if outcome is not None:
            # final implied price for the side held: 1.0 if that side won else 0.0
            final_price = 1.0 if won == "won" else 0.0
        out.append({
            "position_id": str(p.get("id")), "market_id": p.get("market_id"),
            "question": p.get("question"), "url": url, "url_status": url_status,
            "side": p.get("side"), "entry_price": _f(p.get("fill_price")),
            "final_price": final_price, "outcome": (won or "unknown"),
            "stake": _f(p.get("stake")), "payout": _f(p.get("payout")),
            "realized_pnl": _f(p.get("realized_pnl")), "settled_at": p.get("settled_at"),
            "ai_reason": ctx["ai_reason"], "clv_final": None, "paper_only": True,
        })
    return {"paper_only": True, "generated_at": _now_iso(), "positions": out}


def _position_row(position_id, db_path=None):
    try:
        conn = sqlite3.connect(_db(db_path)); conn.row_factory = sqlite3.Row
        try:
            r = conn.execute("SELECT * FROM paper_positions WHERE id=?", (position_id,)).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None
    except Exception:
        return None


def proof_timeline(position_id, db_path=None) -> dict:
    """Per-bet proof. Each step is passed / blocked / missing_proof / not_applicable — a missing
    step is NEVER reported as a fake pass."""
    pos = _position_row(position_id, db_path)
    if not pos:
        return {"paper_only": True, "position_id": str(position_id), "market_id": None,
                "proof_status": "unknown", "timeline": [], "warnings": ["position_not_found"]}
    mid = pos.get("market_id")
    ctx = _decision_context(mid, db_path)
    opened = pos.get("status") in ("open", "settled", "closed")

    def step(name, source, ok, msg, ts=None, na=False, missing=False):
        st = "not_applicable" if na else ("missing_proof" if missing else ("passed" if ok else "blocked"))
        return {"step": name, "ts": ts, "source": source, "status": st, "message": msg, "data": {}}

    has_feat = ctx["consensus"] is not None or ctx["evidence_quality"] != "unknown" \
        or ctx["forecast_probability"] is not None
    # has a live_events record for this market arrived at the dashboard?
    dash_seen = False
    try:
        from harness import live_events as _le
        dash_seen = any((e.get("market_id") == mid) for e in _le.recent_events(500))
    except Exception:
        dash_seen = False

    tl = [
        step("candidate_ranked", "opportunity_ranker", has_feat,
             "decision features recorded" if has_feat else "no decision-feature record", missing=not has_feat),
        step("evidence_collected", "evidence_pack", ctx["evidence_quality"] != "unknown",
             f"evidence quality {ctx['evidence_quality']}", missing=ctx["evidence_quality"] == "unknown"),
        step("llm_agents_voted", "swarm", ctx["forecast_probability"] is not None,
             f"forecast p={ctx['forecast_probability']}", missing=ctx["forecast_probability"] is None),
        step("swarm_consensus", "swarm", ctx["consensus"] is not None,
             f"consensus {ctx['consensus']}", missing=ctx["consensus"] is None),
        step("challenger_checked", "challenger", ctx["challenger_probability"] is not None,
             f"challenger p={ctx['challenger_probability']}", missing=ctx["challenger_probability"] is None),
        step("mirofish_checked", "mirofish", ctx["mirofish_state"] not in (None, "unknown"),
             f"mirofish {ctx['mirofish_state']}",
             na=ctx["mirofish_state"] in (None, "unknown", "disabled")),
        step("ev_gate", "safe_bet", opened, "EV-after-costs gate passed (bet opened)", missing=not opened),
        step("risk_gate", "safe_bet", opened, "risk guard passed (bet opened)", missing=not opened),
        step("bankroll_gate", "safe_bet", opened, "bankroll gate passed (bet opened)", missing=not opened),
        step("exposure_gate", "safe_bet", opened, "exposure gate passed (bet opened)", missing=not opened),
        step("wallet_atomic_open", "wallet", opened,
             f"atomic open accepted (fill {pos.get('fill_price')}, {pos.get('shares')} shares)",
             ts=pos.get("opened_at"), missing=not opened),
        step("position_recorded", "wallet", bool(pos.get("id")),
             f"position #{pos.get('id')} recorded", ts=pos.get("opened_at")),
        step("dashboard_event", "dashboard", dash_seen,
             "live event seen for this market" if dash_seen else "no live event recorded for this market",
             missing=not dash_seen),
    ]
    n_missing = sum(1 for t in tl if t["status"] == "missing_proof")
    n_passed = sum(1 for t in tl if t["status"] == "passed")
    proof_status = ("complete" if n_missing == 0 else ("partial" if n_passed else "missing"))
    warnings = [t["step"] for t in tl if t["status"] == "missing_proof"]
    return {"paper_only": True, "position_id": str(position_id), "market_id": mid,
            "proof_status": proof_status, "timeline": tl, "warnings": warnings}
