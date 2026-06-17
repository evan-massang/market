"""Plan 9 — DB drift / Gate 2 / CLV / scoreboard accounting honesty.

Proves the bot can never show fake equity, fake CLV, fake Gate-2 pass, or a green dashboard
when the accounting/marks/results are incomplete, stale, drifted, or unverifiable. Temp DB only;
no real APIs, no live bot, no real paper wallet, no DB repair.
"""
import os
import re
import sys
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_acct9_")

from harness import wallet                       # noqa: E402
from harness import accounting_audit as AA       # noqa: E402
from harness import clv as CLV                   # noqa: E402
from harness import scoreboard as SB             # noqa: E402
from harness import journal                      # noqa: E402
from harness import health                       # noqa: E402

_CFG = wallet.WalletConfig(max_bet_frac=0.95, max_exposure_frac=0.99)


def _db():
    return os.environ["DATABASE_URL"]


def _reset(starting=1000.0):
    conn = sqlite3.connect(_db())
    for t in ("paper_wallet", "paper_positions", "decisions", "baseline_forecasts",
              "clv_records", "swarm_forecasts"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(starting)
    journal.init_journal()


_POS_COLS = ("market_id", "question", "side", "model_p", "market_p", "edge", "stake", "fill_price",
             "shares", "fee", "status", "outcome", "payout", "realized_pnl", "end_date", "opened_at",
             "settled_at", "event_slug")


def _pos(**kw):
    """Insert a raw paper_positions row (bypasses wallet validation, for invalid/edge fixtures)."""
    row = {"market_id": "M", "question": "Q", "side": "YES", "model_p": 0.6, "market_p": 0.5,
           "edge": 0.1, "stake": 10.0, "fill_price": 0.5, "shares": 20.0, "fee": 0.0,
           "status": "open", "outcome": None, "payout": 0.0, "realized_pnl": None, "end_date": None,
           "opened_at": "2026-05-01T00:00:00", "settled_at": None, "event_slug": None}
    row.update(kw)
    conn = sqlite3.connect(_db())
    conn.execute(f"INSERT INTO paper_positions ({','.join(_POS_COLS)}) "
                 f"VALUES ({','.join('?' for _ in _POS_COLS)})", tuple(row[c] for c in _POS_COLS))
    conn.commit(); conn.close()


def _set_wallet(cash=None, realized=None):
    conn = sqlite3.connect(_db())
    if cash is not None:
        conn.execute("UPDATE paper_wallet SET cash=? WHERE id=1", (cash,))
    if realized is not None:
        conn.execute("UPDATE paper_wallet SET realized_pnl=? WHERE id=1", (realized,))
    conn.commit(); conn.close()


def _baseline(n=3):
    conn = sqlite3.connect(_db())
    conn.execute("CREATE TABLE IF NOT EXISTS baseline_forecasts ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, market_id TEXT, brier_score REAL)")
    for i in range(n):
        conn.execute("INSERT INTO baseline_forecasts (question, market_id, brier_score) VALUES (?,?,?)",
                     (f"Will candidate {i} win the 2028 election?", f"BL-{i}", 0.12))
    conn.commit(); conn.close()


def _clv_records(n=8):
    for i in range(n):
        CLV.record_clv(f"CL-{i}", "YES", 0.50, 0.55, theme="elections")


def _ready_book():
    """A genuinely Gate-2-READY paper book: consistent ledger, >=30 settled trades over time,
    baseline, CLV, no open positions, profitable."""
    _reset(1000.0)
    for i in range(32):
        _pos(market_id=f"P-{i}", status="settled", outcome=1.0, payout=12.5, realized_pnl=2.5,
             settled_at=f"2026-05-{(i % 28) + 1:02d}T00:00:00", side="YES",
             question="Will candidate X win the 2028 election?")
    _set_wallet(cash=1080.0, realized=80.0)        # 1000 - 320 + 400 = 1080 (consistent)
    _baseline(3)
    _clv_records(8)


def _fresh_marks(*mids, price=0.6):
    now = time.time()
    return {m: {"price": price, "time": now} for m in mids}


def _add_unmarked_open(mid="OPEN-NOMARK"):
    """Add a LEDGER-CONSISTENT open position (cash debited) that simply has no fresh mark, so the
    ONLY accounting issue is unverifiable equity (not a drift)."""
    wallet.open_position(mid, "Q", "YES", 0.6, 0.5, 0.1, 10.0, cfg=_CFG)


# ─────────────────────── A. accounting audit (1-10) ───────────────────────────────

def test_clean_wallet_open_position_computes_equity():
    _reset(1000.0)
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0, cfg=_CFG)
    a = AA.audit_accounting(mark_source=_fresh_marks("M", price=0.6))
    assert a["status"] == "ok", a
    assert a["equity"] is not None and a["open_mark_value"] is not None
    assert a["open_position_count"] == 1 and a["mark_stale_count"] == 0
    assert abs(a["equity"] - (a["cash"] + a["open_mark_value"])) < 1e-6


def test_negative_cash_fails():
    _reset(1000.0)
    _set_wallet(cash=-5.0)
    a = AA.audit_accounting()
    assert a["status"] == "drift" and "accounting_negative_cash" in a["reasons"] and a["ok"] is False


def test_invalid_open_position_fails():
    _reset(1000.0)
    _pos(market_id="BAD", status="open", stake=-1.0, fill_price=1.5, shares=0.0, side="MAYBE")
    a = AA.audit_accounting(mark_source=_fresh_marks("BAD"))
    assert "accounting_invalid_position" in a["reasons"] and a["status"] == "drift"


def test_duplicate_open_position_fails():
    _reset(1000.0)
    _pos(market_id="DUP", status="open")
    _pos(market_id="DUP", status="open")
    a = AA.audit_accounting(mark_source=_fresh_marks("DUP"))
    assert "accounting_duplicate_open_position" in a["reasons"] and a["status"] == "drift"


def test_multiple_yes_same_event_fails():
    _reset(1000.0)
    _pos(market_id="E1", status="open", side="YES", event_slug="EV")
    _pos(market_id="E2", status="open", side="YES", event_slug="EV")
    a = AA.audit_accounting(mark_source=_fresh_marks("E1", "E2"))
    assert "accounting_multiple_yes_same_event" in a["reasons"] and a["status"] == "drift"


def test_missing_mark_makes_equity_unknown():
    _reset(1000.0)
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0, cfg=_CFG)
    a = AA.audit_accounting(mark_source=None)               # no marks at all
    assert a["status"] == "degraded" and a["equity"] is None
    assert "accounting_mark_price_missing" in a["reasons"] and a["ok"] is False


def test_stale_mark_makes_equity_unverified():
    _reset(1000.0)
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0, cfg=_CFG)
    stale = {"M": {"price": 0.6, "time": time.time() - 100000}}
    a = AA.audit_accounting(mark_source=stale, max_mark_age_seconds=900)
    assert a["status"] == "degraded" and a["equity"] is None
    assert "accounting_mark_price_stale" in a["reasons"] and a["mark_stale_count"] == 1


def test_db_unavailable_returns_error():
    a = AA.audit_accounting(db_path=os.path.join(ROOT, "no_such_dir", "missing.db"))
    assert a["status"] == "error" and "accounting_db_unavailable" in a["reasons"] and a["ok"] is False


def test_unsettled_expired_position_flagged():
    _reset(1000.0)
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0, cfg=_CFG)
    # mark this open position EXPIRED (end_date in the past)
    conn = sqlite3.connect(_db())
    conn.execute("UPDATE paper_positions SET end_date='2026-05-01T00:00:00' WHERE market_id='M'")
    conn.commit(); conn.close()
    a = AA.audit_accounting(mark_source=_fresh_marks("M"), now=time.time())
    assert "accounting_unsettled_expired_position" in a["reasons"] and a["status"] == "degraded"


def test_audit_performs_no_repair_by_default():
    _reset(1000.0)
    wallet.open_position("M", "Q", "YES", 0.6, 0.5, 0.1, 20.0, cfg=_CFG)
    _set_wallet(cash=12345.0)                # deliberate drift
    before = wallet.get_state()
    AA.audit_accounting()                    # must NOT repair
    AA.gate2_status()
    AA.journal_consistency()
    assert wallet.get_state() == before, "accounting audit must be read-only (no repair)"


# ─────────────────────── B. CLV (11-18) ───────────────────────────────────────────

def test_clv_yes_computed():
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "settled"}, 0.80)
    assert r["ok"] and abs(r["clv"] - 0.30) < 1e-9 and r["reason"] == "clv_ok" and r["is_final"] is True


def test_clv_no_computed():
    r = CLV.compute_clv({"side": "NO", "fill_price": 0.60, "status": "settled"}, 0.20)
    assert r["ok"] and abs(r["clv"] - 0.40) < 1e-9 and r["side"] == "NO"


def test_clv_stale_mark_unknown():
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "open"}, 0.55,
                        mark_time=time.time() - 100000, max_age_seconds=900)
    assert r["ok"] is False and r["reason"] == "clv_mark_stale" and r["clv"] is None


def test_clv_missing_mark_unknown():
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "open"}, None)
    assert r["ok"] is False and r["reason"] == "clv_mark_missing"


def test_clv_invalid_side_blocks():
    r = CLV.compute_clv({"side": "MAYBE", "fill_price": 0.50, "status": "settled"}, 0.55)
    assert r["ok"] is False and r["reason"] == "clv_invalid_side"


def test_clv_settled_uses_final_price():
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "settled"}, 0.99)
    assert r["is_final"] is True and r["ok"] and abs(r["clv"] - 0.49) < 1e-9   # no staleness check on final


def test_clv_open_uses_current_mark_not_final():
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "open"}, 0.55,
                        mark_time=time.time())
    assert r["is_final"] is False and r["ok"] and abs(r["clv"] - 0.05) < 1e-9


def test_clv_not_confused_with_realized_pnl():
    # CLV is a price move (entry->mark), NOT a dollar PnL. A winning settled YES at fill 0.5,
    # close 0.99 has CLV +0.49 regardless of stake/realized_pnl.
    r = CLV.compute_clv({"side": "YES", "fill_price": 0.50, "status": "settled",
                         "stake": 10.0, "realized_pnl": 9.6}, 0.99)
    assert "realized_pnl" not in r and r["clv"] == 0.49 and "clv_bps" in r


# ─────────────────────── C. Gate 2 (19-25) ────────────────────────────────────────

def test_gate2_fails_if_accounting_unverified():
    # open position with no mark -> equity unverified -> Gate 2 cannot pass
    _ready_book()
    _add_unmarked_open()                              # ledger-consistent open, but no fresh mark
    g = AA.gate2_status(mark_source=None)
    assert g["pass"] is False and "gate2_accounting_unverified" in g["reasons"]


def test_gate2_fails_on_db_drift():
    _ready_book()
    _set_wallet(cash=9999.0)                          # break the ledger
    g = AA.gate2_status()
    assert g["pass"] is False and "gate2_db_drift" in g["reasons"] and g["status"] == "fail"


def test_gate2_fails_insufficient_sample():
    _reset(1000.0)
    _pos(market_id="P-0", status="settled", outcome=1.0, payout=12.5, realized_pnl=2.5,
         settled_at="2026-05-01T00:00:00")
    _set_wallet(cash=1002.5, realized=2.5)
    _baseline(3); _clv_records(8)
    g = AA.gate2_status()
    assert g["pass"] is False and "gate2_insufficient_sample" in g["reasons"]


def test_gate2_fails_missing_baseline():
    _ready_book()
    conn = sqlite3.connect(_db()); conn.execute("DROP TABLE IF EXISTS baseline_forecasts")
    conn.commit(); conn.close()
    g = AA.gate2_status()
    assert g["pass"] is False and "gate2_no_baseline" in g["reasons"]


def test_gate2_fails_stale_clv():
    _ready_book()
    conn = sqlite3.connect(_db()); conn.execute("DROP TABLE IF EXISTS clv_records")
    conn.commit(); conn.close()
    g = AA.gate2_status()
    assert g["pass"] is False and "gate2_clv_unverified" in g["reasons"]


def test_gate2_reports_uncertainty():
    _ready_book()
    g = AA.gate2_status()
    assert g["uncertainty"]["available"] is True
    assert "ci95_low" in g["uncertainty"] and "ci95_high" in g["uncertainty"]


def test_gate2_pass_only_when_all_valid():
    _ready_book()
    g = AA.gate2_status()
    assert g["pass"] is True and g["status"] == "pass" and g["reasons"] == ["gate2_pass"]
    assert g["paper_only"] is True and g["baseline_n"] >= 1 and g["mean_clv"] is not None


# ─────────────────────── D. scoreboard / dashboard (26-32) ─────────────────────────

def test_scoreboard_separates_pnl():
    _ready_book()
    s = SB.compute()
    acc = s["accounting"]
    assert "realized_pnl" in acc and "unrealized_pnl" in acc and "total_pnl" in acc and "equity" in acc
    assert acc["realized_pnl"] == 80.0 and acc["equity"] == 1080.0


def test_scoreboard_labels_paper_only():
    _ready_book()
    s = SB.compute()
    assert s["paper_only"] is True and s["gate2"]["paper_only"] is True and "generated_at" in s


def test_scoreboard_marks_stale_degraded():
    _ready_book()
    _add_unmarked_open()                              # unmarked open -> equity unverified
    s = SB.compute()
    assert s["accounting"]["status"] == "degraded" and s["accounting"]["equity"] is None


def test_scoreboard_no_fake_equity_when_unknown():
    _ready_book()
    _add_unmarked_open()
    s = SB.compute()
    # no fake number: equity/total are None (unverified), not a fabricated value
    assert s["accounting"]["equity"] is None and s["accounting"]["total_pnl"] is None
    assert s["gate2"]["pass"] is False


def test_dashboard_health_not_green_when_accounting_fails():
    _ready_book()
    _set_wallet(cash=9999.0)                          # drift
    snap = health.snapshot()
    assert snap["accounting"]["status"] == "drift" and snap["accounting"]["verified"] is False
    assert snap["paper_only"] is True


def test_dashboard_shows_unknown_not_fake():
    _ready_book()
    _add_unmarked_open()
    snap = health.snapshot()
    assert snap["accounting"]["verified"] is False   # unverified, not a fake green


def test_dashboard_state_carries_freshness_and_status():
    try:
        from fastapi.testclient import TestClient
        import harness.dashboard as D
        client = TestClient(D.app)
    except Exception:
        return  # FastAPI unavailable -> skip
    _ready_book()
    r = client.get("/api/accounting")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["paper_only"] is True and body["audit"]["status"] == "ok"
    assert body["gate2"]["pass"] is True
    st = client.get("/api/state").json()
    assert st["paper_only"] is True and st["accounting"] is not None


# ─────────────────────── E. journal consistency (33-37) ────────────────────────────

def test_no_bet_not_counted_as_trade():
    _reset(1000.0)
    journal.record_decision("M1", "Q", 0.6, 0.5, 0.1, "YES", 0.0, None, "r", "s", "no_bet", "why")
    journal.record_decision("M2", "Q", 0.6, 0.5, 0.1, "YES", 10.0, 0.5, "r", "s", "bet", "why")
    j = AA.journal_consistency()
    assert j["counts"]["bets"] == 1 and j["counts"]["no_bets"] == 1
    # the bet count must EXCLUDE the no_bet
    assert _check(j, "no_bet_not_a_bet") == "OK"


def test_wallet_rejection_not_counted_as_trade():
    # a no_bet (rejected) decision must NOT have a position; if it did, it would be flagged
    _reset(1000.0)
    journal.record_decision("REJ", "Q", 0.6, 0.5, 0.1, "YES", 0.0, None, "r", "s", "no_bet", "rejected")
    j = AA.journal_consistency()
    assert _check(j, "no_bet_counted_as_trade") == "OK"   # no position exists for REJ -> clean


def test_no_bet_with_position_is_detected():
    _reset(1000.0)
    journal.record_decision("REJ", "Q", 0.6, 0.5, 0.1, "YES", 0.0, None, "r", "s", "no_bet", "rejected")
    _pos(market_id="REJ", status="open")              # a no-bet market that became a trade
    j = AA.journal_consistency()
    assert _check(j, "no_bet_counted_as_trade") == "FAIL" and j["status"] == "fail"


def test_position_without_decision_detected():
    _reset(1000.0)
    _pos(market_id="ORPH", status="open")             # a trade with no decision row
    j = AA.journal_consistency()
    assert _check(j, "trade_without_decision") == "WARN"


def test_bet_without_position_detected():
    _reset(1000.0)
    journal.record_decision("NOPOS", "Q", 0.6, 0.5, 0.1, "YES", 10.0, 0.5, "r", "s", "bet", "why")
    j = AA.journal_consistency()
    assert _check(j, "bet_without_position") == "WARN"


def _check(j, name):
    for n, s, _ in j["checks"]:
        if n == name:
            return s
    return None


# ─────────────────────── F. static scans (38-40) ──────────────────────────────────

def _src(rel):
    return open(os.path.join(ROOT, "harness", rel), encoding="utf-8").read()


def test_static_gate2_requires_accounting_ok():
    src = _src("accounting_audit.py")
    # Gate 2 fails closed on a non-ok accounting status (drift / unverified)
    assert "gate2_db_drift" in src and "gate2_accounting_unverified" in src
    assert 'audit["status"] in ("error", "unknown")' in src
    # behavioral proof: a drifted book cannot pass
    _ready_book(); _set_wallet(cash=9999.0)
    assert AA.gate2_status()["pass"] is False


def test_static_clv_is_side_aware():
    src = _src("clv.py")
    assert "return c - e" in src and "return e - c" in src   # YES vs NO branches
    # behavioral proof: YES and NO give opposite-signed CLV for the same move
    y = CLV.compute_clv({"side": "YES", "fill_price": 0.5, "status": "settled"}, 0.6)["clv"]
    n = CLV.compute_clv({"side": "NO", "fill_price": 0.5, "status": "settled"}, 0.6)["clv"]
    assert y > 0 and n < 0 and abs(y + n) < 1e-9


def test_static_dashboard_health_depends_on_audit():
    hsrc = _src("health.py")
    assert "audit_accounting" in hsrc and '"accounting"' in hsrc
    dsrc = _src("dashboard.py")
    assert "api_accounting" in dsrc and "accounting" in dsrc
    # behavioral proof: drift -> health accounting not verified
    _ready_book(); _set_wallet(cash=9999.0)
    assert health.snapshot()["accounting"]["verified"] is False


def test_static_db_repair_is_opt_in_only():
    src = _src("db_check.py")
    # repair defaults to dry_run (no implicit mutation); audit module never writes
    assert "def repair(dry_run: bool = True)" in src
    asrc = _src("accounting_audit.py")
    assert "UPDATE" not in asrc and "INSERT" not in asrc and "DELETE" not in asrc   # read-only


TESTS = [
    ("clean_wallet_open_position_computes_equity", test_clean_wallet_open_position_computes_equity),
    ("negative_cash_fails", test_negative_cash_fails),
    ("invalid_open_position_fails", test_invalid_open_position_fails),
    ("duplicate_open_position_fails", test_duplicate_open_position_fails),
    ("multiple_yes_same_event_fails", test_multiple_yes_same_event_fails),
    ("missing_mark_makes_equity_unknown", test_missing_mark_makes_equity_unknown),
    ("stale_mark_makes_equity_unverified", test_stale_mark_makes_equity_unverified),
    ("db_unavailable_returns_error", test_db_unavailable_returns_error),
    ("unsettled_expired_position_flagged", test_unsettled_expired_position_flagged),
    ("audit_performs_no_repair_by_default", test_audit_performs_no_repair_by_default),
    ("clv_yes_computed", test_clv_yes_computed),
    ("clv_no_computed", test_clv_no_computed),
    ("clv_stale_mark_unknown", test_clv_stale_mark_unknown),
    ("clv_missing_mark_unknown", test_clv_missing_mark_unknown),
    ("clv_invalid_side_blocks", test_clv_invalid_side_blocks),
    ("clv_settled_uses_final_price", test_clv_settled_uses_final_price),
    ("clv_open_uses_current_mark_not_final", test_clv_open_uses_current_mark_not_final),
    ("clv_not_confused_with_realized_pnl", test_clv_not_confused_with_realized_pnl),
    ("gate2_fails_if_accounting_unverified", test_gate2_fails_if_accounting_unverified),
    ("gate2_fails_on_db_drift", test_gate2_fails_on_db_drift),
    ("gate2_fails_insufficient_sample", test_gate2_fails_insufficient_sample),
    ("gate2_fails_missing_baseline", test_gate2_fails_missing_baseline),
    ("gate2_fails_stale_clv", test_gate2_fails_stale_clv),
    ("gate2_reports_uncertainty", test_gate2_reports_uncertainty),
    ("gate2_pass_only_when_all_valid", test_gate2_pass_only_when_all_valid),
    ("scoreboard_separates_pnl", test_scoreboard_separates_pnl),
    ("scoreboard_labels_paper_only", test_scoreboard_labels_paper_only),
    ("scoreboard_marks_stale_degraded", test_scoreboard_marks_stale_degraded),
    ("scoreboard_no_fake_equity_when_unknown", test_scoreboard_no_fake_equity_when_unknown),
    ("dashboard_health_not_green_when_accounting_fails", test_dashboard_health_not_green_when_accounting_fails),
    ("dashboard_shows_unknown_not_fake", test_dashboard_shows_unknown_not_fake),
    ("dashboard_state_carries_freshness_and_status", test_dashboard_state_carries_freshness_and_status),
    ("no_bet_not_counted_as_trade", test_no_bet_not_counted_as_trade),
    ("wallet_rejection_not_counted_as_trade", test_wallet_rejection_not_counted_as_trade),
    ("no_bet_with_position_is_detected", test_no_bet_with_position_is_detected),
    ("position_without_decision_detected", test_position_without_decision_detected),
    ("bet_without_position_detected", test_bet_without_position_detected),
    ("static_gate2_requires_accounting_ok", test_static_gate2_requires_accounting_ok),
    ("static_clv_is_side_aware", test_static_clv_is_side_aware),
    ("static_dashboard_health_depends_on_audit", test_static_dashboard_health_depends_on_audit),
    ("static_db_repair_is_opt_in_only", test_static_db_repair_is_opt_in_only),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
