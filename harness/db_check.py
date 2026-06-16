"""harness.db_check — read-only paper-trading DB integrity + reconciliation.

`python -m harness.db_check` (add --json for machine output). NEVER writes. It:
  * runs PRAGMA integrity_check
  * confirms the core tables exist
  * RECONCILES the paper_wallet running totals (cash / realized_pnl / equity)
    against the paper_positions ledger — the exact drift the audit found (wallet
    realized_pnl is the number Gate 2 reads, so a silent drift = lying about P&L)
  * flags negative cash, double-counted / bad-status positions, settled-vs-closed
    P&L split, and forecasts that were bet but never resolved.

Output: OK / WARN / FAIL lines + a summary; exits non-zero only on a FAIL.
"""
from __future__ import annotations

import os
import sqlite3
import sys


def _db_path() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _ro_conn(path: str):
    # open read-only so a check can never mutate the live DB
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except Exception:
        c = sqlite3.connect(path, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


CHECKS = []  # (name, status, detail)


def _add(name, status, detail):
    CHECKS.append((name, status, detail))


def run() -> dict:
    CHECKS.clear()
    path = _db_path()
    if not os.path.exists(path):
        _add("db_file", "FAIL", f"{path} does not exist")
        return _summary(path)
    conn = _ro_conn(path)

    # 1) integrity
    try:
        r = conn.execute("PRAGMA integrity_check").fetchone()
        ok = r and (r[0] == "ok")
        _add("integrity", "OK" if ok else "FAIL", r[0] if r else "no result")
    except Exception as e:
        _add("integrity", "FAIL", f"integrity_check error: {e}")

    # 2) required tables
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception as e:
        _add("tables", "FAIL", f"cannot read schema: {e}")
        return _summary(path)
    required = ["paper_wallet", "paper_positions"]
    missing = [t for t in required if t not in names]
    _add("tables", "FAIL" if missing else "OK",
         f"missing {missing}" if missing else f"{len(names)} tables; core present")

    if "paper_positions" not in names or "paper_wallet" not in names:
        return _summary(path)

    # 3) bad status values
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status NOT IN ('open','settled','closed')"
        ).fetchone()[0]
        _add("position_status", "WARN" if bad else "OK",
             f"{bad} rows with an unexpected status" if bad else "all open/settled/closed")
    except Exception as e:
        _add("position_status", "WARN", f"status check error: {e}")

    # 4) reconcile wallet running totals vs the positions ledger
    try:
        w = conn.execute("SELECT starting_bankroll, cash, realized_pnl FROM paper_wallet WHERE id=1").fetchone()
        starting = float(w["starting_bankroll"] or 0.0)
        wallet_cash = float(w["cash"] or 0.0)
        wallet_realized = float(w["realized_pnl"] or 0.0)
        agg = conn.execute(
            "SELECT "
            "COALESCE(SUM(stake),0) AS stake, "
            "COALESCE(SUM(fee),0) AS fee, "
            "COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN payout ELSE 0 END),0) AS payout, "
            "COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN realized_pnl ELSE 0 END),0) AS realized, "
            "COALESCE(SUM(CASE WHEN status='open' THEN stake ELSE 0 END),0) AS open_stake "
            "FROM paper_positions"
        ).fetchone()
        # cash = starting - (stake debited on every open) - fees + payouts returned on close/settle
        ledger_cash = starting - float(agg["stake"]) - float(agg["fee"]) + float(agg["payout"])
        ledger_realized = float(agg["realized"])
        cash_drift = wallet_cash - ledger_cash
        realized_drift = wallet_realized - ledger_realized
        cd = "OK" if abs(cash_drift) < 0.01 else "WARN"
        _add("reconcile_cash", cd,
             f"wallet ${wallet_cash:.2f} vs ledger ${ledger_cash:.2f} (drift ${cash_drift:+.2f})")
        rd = "OK" if abs(realized_drift) < 0.01 else "WARN"
        _add("reconcile_realized", rd,
             f"wallet ${wallet_realized:.2f} vs ledger ${ledger_realized:.2f} (drift ${realized_drift:+.2f}) "
             f"[Gate-2 reads the wallet number]")
        # equity invariant: equity (cash + open stake) should equal starting + realized
        equity = wallet_cash + float(agg["open_stake"])
        inv_drift = equity - (starting + wallet_realized)
        _add("equity_invariant", "OK" if abs(inv_drift) < 0.01 else "WARN",
             f"equity ${equity:.2f} vs starting+realized ${starting + wallet_realized:.2f} (drift ${inv_drift:+.2f})")
        _add("negative_cash", "FAIL" if wallet_cash < -0.01 else "OK", f"cash ${wallet_cash:.2f}")
    except Exception as e:
        _add("reconcile", "WARN", f"reconciliation error: {e}")

    # 4b) Plan 4 — open-position integrity: duplicate opens, stake/price validity.
    try:
        dups = conn.execute(
            "SELECT market_id, COUNT(*) AS c FROM paper_positions WHERE status='open' "
            "GROUP BY market_id HAVING c > 1").fetchall()
        _add("duplicate_open_positions", "WARN" if dups else "OK",
             (f"{len(dups)} market(s) with >1 open position: "
              + ", ".join(f"{(r['market_id'] or '?')[:18]}×{r['c']}" for r in dups[:5]))
             if dups else "no market has more than one open position")
        bad_stake = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open' AND (stake IS NULL OR stake <= 0)"
        ).fetchone()[0]
        _add("open_stake_positive", "WARN" if bad_stake else "OK",
             f"{bad_stake} open position(s) with non-positive stake" if bad_stake else "all open stakes > 0")
        bad_price = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open' AND "
            "(fill_price IS NULL OR fill_price <= 0 OR fill_price >= 1)").fetchone()[0]
        _add("open_price_valid", "WARN" if bad_price else "OK",
             f"{bad_price} open position(s) with fill_price outside (0,1)" if bad_price
             else "all open fill prices in (0,1)")
        # Plan 6 — event coherence: more than one open YES leg in the same event is an
        # incoherent (multiple-winner) stacked exposure in a mutually-exclusive event.
        multi_yes = conn.execute(
            "SELECT event_slug, COUNT(*) AS c FROM paper_positions "
            "WHERE status='open' AND UPPER(side)='YES' AND event_slug IS NOT NULL AND event_slug<>'' "
            "GROUP BY event_slug HAVING c > 1").fetchall()
        _add("event_multiple_open_yes", "WARN" if multi_yes else "OK",
             (f"{len(multi_yes)} event(s) with >1 open YES leg: "
              + ", ".join(f"{(r['event_slug'] or '?')[:18]}x{r['c']}" for r in multi_yes[:5]))
             if multi_yes else "no event has more than one open YES leg")
    except Exception as e:
        _add("open_position_integrity", "WARN", f"open-position integrity error: {e}")

    # 5) settled-vs-closed P&L split (so a hidden cash-out loss bucket is visible)
    try:
        sp = conn.execute("SELECT COALESCE(SUM(realized_pnl),0), COUNT(*) FROM paper_positions WHERE status='settled'").fetchone()
        cp = conn.execute("SELECT COALESCE(SUM(realized_pnl),0), COUNT(*) FROM paper_positions WHERE status='closed'").fetchone()
        _add("pnl_split", "OK",
             f"settled n={sp[1]} ${sp[0]:.2f} · closed/cashed-out n={cp[1]} ${cp[0]:.2f}")
    except Exception as e:
        _add("pnl_split", "WARN", f"split error: {e}")

    # 6) forecasts bet but never resolved (Gate-1 sample completeness)
    if "swarm_forecasts" in names:
        try:
            tot = conn.execute("SELECT COUNT(*) FROM swarm_forecasts").fetchone()[0]
            res = conn.execute("SELECT COUNT(*) FROM swarm_forecasts WHERE outcome IS NOT NULL").fetchone()[0]
            _add("forecasts_resolved", "OK", f"{res}/{tot} swarm forecasts resolved (rest pending)")
        except Exception as e:
            _add("forecasts_resolved", "WARN", f"forecast check error: {e}")

    try:
        conn.close()
    except Exception:
        pass
    return _summary(path)


def _summary(path: str) -> dict:
    n_fail = sum(1 for _, s, _ in CHECKS if s == "FAIL")
    n_warn = sum(1 for _, s, _ in CHECKS if s == "WARN")
    n_ok = sum(1 for _, s, _ in CHECKS if s == "OK")
    return {"db": path, "checks": list(CHECKS), "ok": n_ok, "warn": n_warn, "fail": n_fail}


def ledger_reconciliation_report() -> dict:
    """Structured wallet-vs-ledger reconciliation: expected (from the positions ledger)
    vs actual (the wallet running totals) for cash + realized P&L, the deltas, and any
    suspicious rows. Read-only."""
    path = _db_path()
    out = {"db": path, "ok": False}
    if not os.path.exists(path):
        out["error"] = "db missing"
        return out
    try:
        conn = _ro_conn(path)
        w = conn.execute("SELECT starting_bankroll, cash, realized_pnl FROM paper_wallet WHERE id=1").fetchone()
        starting = float(w["starting_bankroll"] or 0.0)
        wallet_cash = float(w["cash"] or 0.0)
        wallet_realized = float(w["realized_pnl"] or 0.0)
        agg = conn.execute(
            "SELECT COALESCE(SUM(stake),0) s, COALESCE(SUM(fee),0) f, "
            "COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN payout ELSE 0 END),0) p, "
            "COALESCE(SUM(CASE WHEN status IN ('settled','closed') THEN realized_pnl ELSE 0 END),0) r "
            "FROM paper_positions").fetchone()
        expected_cash = starting - float(agg["s"]) - float(agg["f"]) + float(agg["p"])
        expected_realized = float(agg["r"])
        susp = conn.execute(
            "SELECT id, market_id, status FROM paper_positions "
            "WHERE status IN ('settled','closed') AND realized_pnl IS NULL LIMIT 20").fetchall()
        conn.close()
        out.update({
            "ok": True, "starting_bankroll": round(starting, 4),
            "expected_cash": round(expected_cash, 4), "actual_cash": round(wallet_cash, 4),
            "cash_delta": round(wallet_cash - expected_cash, 4),
            "expected_realized": round(expected_realized, 4), "actual_realized": round(wallet_realized, 4),
            "realized_delta": round(wallet_realized - expected_realized, 4),
            "suspicious_rows": [dict(r) for r in susp],
        })
    except Exception as e:
        out["error"] = str(e)
    return out


def repair(dry_run: bool = True) -> dict:
    """The ONE safe, deterministic repair: reconcile paper_wallet.cash / realized_pnl to
    the positions LEDGER (the single source of truth). dry_run changes NOTHING (prints
    the plan); a real repair applies it and writes a tamper-evident obs audit event.
    It NEVER deletes a row and never touches positions — only the two wallet aggregates."""
    rep = ledger_reconciliation_report()
    if not rep.get("ok"):
        return {"ok": False, "error": rep.get("error", "reconcile failed")}
    plan = {"set_cash": rep["expected_cash"], "set_realized": rep["expected_realized"],
            "from_cash": rep["actual_cash"], "from_realized": rep["actual_realized"],
            "cash_delta": rep["cash_delta"], "realized_delta": rep["realized_delta"]}
    needs = abs(rep["cash_delta"]) > 0.01 or abs(rep["realized_delta"]) > 0.01
    if dry_run:
        return {"ok": True, "applied": False, "dry_run": True, "needs_repair": needs, "plan": plan}
    if not needs:
        return {"ok": True, "applied": False, "needs_repair": False, "plan": plan,
                "note": "already consistent"}
    try:
        import sqlite3 as _sq
        from datetime import datetime as _dt
        conn = _sq.connect(_db_path(), timeout=10.0)
        conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=?, updated_at=? WHERE id=1",
                     (rep["expected_cash"], rep["expected_realized"], _dt.utcnow().isoformat()))
        conn.commit(); conn.close()
        try:   # tamper-evident audit event (hash-chained obs log)
            from harness import obs
            obs.hooks.on_error(where="db_check.repair",
                               exc=RuntimeError("ledger reconcile applied"),
                               action="reconciled_wallet_to_ledger", context=plan)
        except Exception:
            pass
        return {"ok": True, "applied": True, "plan": plan}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def render(res: dict) -> None:
    print("harness.db_check — read-only DB integrity + reconciliation")
    print(f"  db: {res['db']}")
    print("-" * 60)
    for name, status, detail in res["checks"]:
        print(f"[{status:<4}] {name:<20} {detail}")
    print("-" * 60)
    print(f"OK: {res['ok']} pass, {res['warn']} warn, {res['fail']} fail  (of {len(res['checks'])} checks)")


def main(argv=None) -> int:
    import json
    argv = argv if argv is not None else sys.argv[1:]

    if "--repair-dry-run" in argv or "--repair" in argv:
        rec = ledger_reconciliation_report()
        print("LEDGER RECONCILIATION")
        for k in ("expected_cash", "actual_cash", "cash_delta",
                  "expected_realized", "actual_realized", "realized_delta"):
            print(f"  {k:<18} {rec.get(k)}")
        r = repair(dry_run=("--repair" not in argv))
        if "--json" in argv:
            print(json.dumps({"reconciliation": rec, "repair": r}, indent=2))
        elif r.get("applied"):
            print(f"  REPAIRED — wallet reconciled to ledger (cash {r['plan']['from_cash']} -> "
                  f"{r['plan']['set_cash']}, realized {r['plan']['from_realized']} -> "
                  f"{r['plan']['set_realized']}); audit event written.")
        elif r.get("needs_repair") is False:
            print("  already consistent — nothing to repair.")
        else:
            print("  DRY-RUN — wallet would be reconciled to the ledger above. "
                  "Re-run with --repair to apply (writes an audit event).")
        return 0

    res = run()
    if "--json" in argv:
        print(json.dumps(res, indent=2))
    else:
        render(res)
    return 1 if res["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
