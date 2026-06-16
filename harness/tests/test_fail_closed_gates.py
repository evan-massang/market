"""Plan 1 — FAIL-CLOSED money gates. The dedicated proof that a broken EV / risk /
bankroll / exposure check can NEVER place a paper bet.

NO network, NO LLM, temp DB only (make_temp_env). Every gate is forced into each
failure mode (module unavailable, raises, malformed result, DB unavailable, bad
input) and asserted to BLOCK with a ``*_fail_closed`` reason; the happy path is
asserted to still ALLOW and normal tightening blocks are asserted to keep their
own (non-fail-closed) reasons.

Run:  python -m harness.tests.test_fail_closed_gates
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_failclosed_")

from harness import predict_today as PT        # noqa: E402
from harness import sameday as SD               # noqa: E402
from harness import profitability as PROF       # noqa: E402
from harness import risk_guards as RG           # noqa: E402
from harness import bankroll as BK              # noqa: E402
from harness import wallet as W                 # noqa: E402
from harness import journal                     # noqa: E402
from harness import safety_gate as SG           # noqa: E402


_Q = "Will the incumbent win the 2032 presidential election?"
_MID = "0xfailclosed01"


def _boom(*a, **k):
    raise RuntimeError("simulated gate outage")


def _reset_wallet(starting=1000.0, cash=None, realized=0.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    W.init_wallet(starting)
    if cash is not None or realized:
        conn = sqlite3.connect(os.environ["DATABASE_URL"])
        conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=? WHERE id=1",
                     (starting if cash is None else cash, realized))
        conn.commit()
        conn.close()


def _clean_market():
    return {"market_id": _MID, "question": _Q, "price": 0.50,
            "outcome_prices": [0.50, 0.50], "volume": 200_000.0, "liquidity": 40_000.0,
            "end_date": "2032-01-01T00:00:00Z", "event_slug": None, "raw": {}}


# ════════════════════════════════════════════════════════════════════════════
# (1-3) EV GATE — unavailable / raises / invalid input  → BLOCK
# ════════════════════════════════════════════════════════════════════════════
def test_ev_module_unavailable_blocks():
    with patched(PT, "_profitability", None):
        ok, reason = PT._p7_ev_gate(0.65, 0.50, "YES")      # a HEALTHY bet
    assert ok is False and reason == SG.EV_UNAVAILABLE, (ok, reason)


def test_ev_gate_raises_blocks():
    with patched(PROF, "ev_gate", _boom):
        ok, reason = PT._p7_ev_gate(0.65, 0.50, "YES")
    assert ok is False and reason == SG.EV_ERROR, (ok, reason)


def test_ev_invalid_inputs_block():
    bad = [
        (float("nan"), 0.50, "YES"),    # NaN model_p
        (0.50, float("inf"), "YES"),    # inf market_p
        (0.50, 0.0, "YES"),             # market_p not strictly in (0,1)
        (0.50, 1.0, "YES"),             # market_p not strictly in (0,1)
        (1.5, 0.50, "YES"),             # model_p out of [0,1]
        (0.50, 0.50, "MAYBE"),          # bad side
        (None, 0.50, "YES"),            # None price
    ]
    for mp, kp, side in bad:
        ok, reason = PT._p7_ev_gate(mp, kp, side)
        assert ok is False and reason == SG.EV_INVALID, (mp, kp, side, ok, reason)


def test_ev_malformed_result_blocks():
    with patched(PROF, "ev_gate", lambda *a, **k: ("not_a_bool", "x")):
        ok, reason = PT._p7_ev_gate(0.65, 0.50, "YES")
    assert ok is False and reason == SG.EV_INVALID, (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# (4) RISK GUARD — unavailable / raises / malformed  → BLOCK
# ════════════════════════════════════════════════════════════════════════════
def test_risk_module_unavailable_blocks():
    with patched(PT, "_risk_guards", None):
        ok, reason = PT._p8_risk_guards(_clean_market(), "YES", _Q)
    assert ok is False and reason == SG.RISK_UNAVAILABLE, (ok, reason)


def test_risk_guard_raises_blocks():
    _reset_wallet()
    with patched(RG, "evaluate", _boom):
        ok, reason = PT._p8_risk_guards(_clean_market(), "YES", _Q)
    assert ok is False and reason == SG.RISK_ERROR, (ok, reason)


def test_risk_malformed_result_blocks():
    _reset_wallet()
    for bad in (None, "notadict", {}, {"no_allow_key": 1}, 42):
        with patched(RG, "evaluate", lambda *a, _b=bad, **k: _b):
            ok, reason = PT._p8_risk_guards(_clean_market(), "YES", _Q)
        assert ok is False and reason == SG.RISK_INVALID, (bad, ok, reason)


def test_risk_allow_not_true_blocks():
    # a verdict whose allow is a truthy non-True (e.g. 1) must NOT pass
    _reset_wallet()
    with patched(RG, "evaluate", lambda *a, **k: {"allow": 1, "blocking_reason": None}):
        ok, reason = PT._p8_risk_guards(_clean_market(), "YES", _Q)
    assert ok is False, (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# (5) BANKROLL kill switch — unavailable / raises / malformed  → BLOCK
# ════════════════════════════════════════════════════════════════════════════
def test_bankroll_module_unavailable_blocks():
    with patched(PT, "_bankroll", None):
        ok, reason = PT._p9_can_trade()
    assert ok is False and reason == SG.BANKROLL_UNAVAILABLE, (ok, reason)


def test_bankroll_can_trade_raises_blocks():
    with patched(BK, "can_trade", _boom):
        ok, reason = PT._p9_can_trade()
    assert ok is False and reason == SG.BANKROLL_ERROR, (ok, reason)


def test_bankroll_malformed_result_blocks():
    with patched(BK, "can_trade", lambda: "nope"):
        ok, reason = PT._p9_can_trade()
    assert ok is False and reason == SG.BANKROLL_INVALID, (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# (6) EXPOSURE cap — unavailable / raises / malformed  → BLOCK
# ════════════════════════════════════════════════════════════════════════════
def test_exposure_module_unavailable_blocks():
    with patched(PT, "_bankroll", None):
        ok, reason = PT._p9_exposure_ok(_Q, None, 10.0)
    assert ok is False and reason == SG.EXPOSURE_UNAVAILABLE, (ok, reason)


def test_exposure_raises_blocks():
    _reset_wallet()
    with patched(BK, "exposure_ok", _boom):
        ok, reason = PT._p9_exposure_ok(_Q, None, 10.0)
    assert ok is False and reason == SG.EXPOSURE_ERROR, (ok, reason)


def test_exposure_malformed_result_blocks():
    _reset_wallet()
    with patched(BK, "exposure_ok", lambda *a, **k: ("x", None, {})):   # ok not a bool
        ok, reason = PT._p9_exposure_ok(_Q, None, 10.0)
    assert ok is False and reason == SG.EXPOSURE_INVALID, (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# (13-16) LOWER-LEVEL fail-closed (risk_guards.evaluate / bankroll DB)
# ════════════════════════════════════════════════════════════════════════════
def test_risk_guards_internal_exception_blocks():
    from harness import market_quality as MQ
    _reset_wallet()
    with patched(MQ, "evaluate_market_quality", _boom):
        v = RG.evaluate(_clean_market(), "YES", _Q)
    assert v["allow"] is False and v["blocking_reason"] == SG.RISK_INTERNAL_ERROR, v


def test_can_trade_db_unavailable_blocks():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit(); conn.close()
    ok, reason = BK.can_trade()
    assert ok is False and SG.is_fail_closed(reason), (ok, reason)


def test_exposure_db_unavailable_blocks():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit(); conn.close()
    ok, reason, _ = BK.exposure_ok("elections", None, 10.0)   # bankroll read fails -> 0 -> block
    assert ok is False and SG.is_fail_closed(reason), (ok, reason)


def test_can_trade_uninitialized_wallet_blocks():
    # an EMPTY (no row) wallet has no baseline -> cannot run the kill switch -> block
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit(); conn.close()
    W.init_wallet(1000.0)
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("UPDATE paper_wallet SET starting_bankroll=0 WHERE id=1")  # zero baseline
    conn.commit(); conn.close()
    ok, reason = BK.can_trade()
    assert ok is False and reason == SG.BANKROLL_UNAVAILABLE, (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# (7,8) NO WALLET OPEN on a gate failure + the no-bet decision IS recorded
# ════════════════════════════════════════════════════════════════════════════
def test_skip_records_no_bet_and_never_opens_position():
    """predict_today._skip — the no-bet recorder used at EVERY gate-block site —
    records a 'no_bet' decision and returns False WITHOUT ever touching the wallet."""
    _reset_wallet()
    journal.init_journal()

    def _must_not_open(*a, **k):
        raise AssertionError("wallet.open_position called on a fail-closed skip!")

    with patched(W, "open_position", _must_not_open):
        out = PT._skip(_MID, _Q, SG.EV_ERROR, p=0.5, price=0.5)
    assert out is False
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE market_id=? AND status='no_bet'", (_MID,)).fetchall()
    conn.close()
    assert rows and any(SG.EV_ERROR in (r["why"] or "") for r in rows), [dict(r) for r in rows]


def test_sameday_sd_skip_records_no_bet():
    """sameday._sd_skip records the SAME no-bet trail (print+obs+journal). Used by
    every sameday money-gate branch, so a fail-closed gate there is visible too."""
    _reset_wallet()
    journal.init_journal()
    out = SD._sd_skip(_MID, _Q, SG.BANKROLL_ERROR, p=0.5, price=0.5, layer="bankroll")
    assert out is False
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE market_id=? AND status='no_bet'", (_MID,)).fetchall()
    conn.close()
    assert rows and any(SG.BANKROLL_ERROR in (r["why"] or "") for r in rows), [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
# (9) STRUCTURAL: every gate call site routes a block into skip/continue BEFORE
#     any wallet.open_position — proven by scanning the real source.
# ════════════════════════════════════════════════════════════════════════════
_GATES = ("_p7_ev_gate", "_p8_risk_guards", "_p9_can_trade", "_p9_exposure_ok")


def _scan_gate_guards(path):
    """For each line that assigns ``<lhs>, <reason> = <gate>(...)``, return whether the
    next non-blank line is an ``if not <lhs>:`` guard. Returns list of (lineno, ok)."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    results = []
    assign = re.compile(r"^\s*([A-Za-z_]\w*)\s*,\s*[A-Za-z_]\w*\s*=\s*_p[789]\w*\(")
    for i, ln in enumerate(lines):
        if not any(g in ln for g in _GATES):
            continue
        m = assign.match(ln)
        if not m:
            continue   # not a gate-result assignment (e.g. a def or import line)
        lhs = m.group(1)
        # find the next non-blank line
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        nxt = lines[j].strip() if j < len(lines) else ""
        results.append((i + 1, nxt == f"if not {lhs}:"))
    return results


def test_every_gate_call_is_guarded_before_open():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("harness/predict_today.py", "harness/sameday.py"):
        scanned = _scan_gate_guards(os.path.join(root, rel))
        assert scanned, f"no gate-result assignments found in {rel} (scan broke?)"
        bad = [ln for ln, ok in scanned if not ok]
        assert not bad, f"{rel}: gate result at line(s) {bad} not immediately guarded by `if not <ok>:`"


# ════════════════════════════════════════════════════════════════════════════
# (Phase 10) REGRESSION — the happy path still ALLOWS; normal blocks keep their
#            own (NON fail-closed) reasons.
# ════════════════════════════════════════════════════════════════════════════
def test_happy_path_all_gates_allow():
    _reset_wallet(starting=1000.0)
    # EV: a healthy +edge bet passes
    ok, reason = PT._p7_ev_gate(0.65, 0.50, "YES")
    assert ok is True and not SG.is_fail_closed(reason), (ok, reason)
    # risk: a clean liquid market in a healthy book passes
    ok, reason = PT._p8_risk_guards(_clean_market(), "YES", _Q)
    assert ok is True and not SG.is_fail_closed(reason), (ok, reason)
    # bankroll: a healthy book passes
    ok, reason = PT._p9_can_trade()
    assert ok is True and not SG.is_fail_closed(reason), (ok, reason)
    # exposure: a small new stake in an empty book passes
    ok, reason = PT._p9_exposure_ok(_Q, None, 10.0)
    assert ok is True and not SG.is_fail_closed(reason), (ok, reason)


def test_normal_blocks_keep_their_own_reasons():
    # EV below breakeven blocks with neg_ev_after_costs (a normal tightening, NOT fail-closed)
    ok, reason = PT._p7_ev_gate(0.505, 0.50, "YES")
    assert ok is False and reason == PROF.REJECT_REASON and not SG.is_fail_closed(reason), (ok, reason)
    # bankroll drawdown blocks with drawdown_pause (not fail-closed)
    _reset_wallet(starting=1000.0, cash=700.0, realized=-300.0)
    ok, reason = PT._p9_can_trade()
    assert ok is False and reason.startswith("drawdown_pause") and not SG.is_fail_closed(reason), (ok, reason)
    # exposure over the theme cap blocks with theme_exposure_cap (not fail-closed)
    ok, reason, _ = BK.exposure_ok("elections", None, new_stake=300.0, bankroll=1000.0)
    assert ok is False and reason == "theme_exposure_cap" and not SG.is_fail_closed(reason), (ok, reason)


# ════════════════════════════════════════════════════════════════════════════
# safety_gate.coerce unit behavior (the shared validator)
# ════════════════════════════════════════════════════════════════════════════
def test_safety_gate_coerce_only_explicit_true_passes():
    c = SG.coerce
    assert c((True, "ok"), gate="g", block_reason="b") == (True, "ok")
    assert c((True, ""), gate="g", block_reason="b") == (True, "ok")          # empty reason -> "ok"
    assert c((False, "neg_ev"), gate="g", block_reason="b") == (False, "neg_ev")
    assert c((False, None), gate="g", block_reason="b") == (False, "g_blocked")
    # malformed shapes ALL block with block_reason
    for bad in (None, "x", 1, (), (True,), (1, "ok"), ("yes", "ok"), [True, "ok"]):
        assert c(bad, gate="g", block_reason="b") == (False, "b"), bad


TESTS = [
    ("ev_module_unavailable_blocks", test_ev_module_unavailable_blocks),
    ("ev_gate_raises_blocks", test_ev_gate_raises_blocks),
    ("ev_invalid_inputs_block", test_ev_invalid_inputs_block),
    ("ev_malformed_result_blocks", test_ev_malformed_result_blocks),
    ("risk_module_unavailable_blocks", test_risk_module_unavailable_blocks),
    ("risk_guard_raises_blocks", test_risk_guard_raises_blocks),
    ("risk_malformed_result_blocks", test_risk_malformed_result_blocks),
    ("risk_allow_not_true_blocks", test_risk_allow_not_true_blocks),
    ("bankroll_module_unavailable_blocks", test_bankroll_module_unavailable_blocks),
    ("bankroll_can_trade_raises_blocks", test_bankroll_can_trade_raises_blocks),
    ("bankroll_malformed_result_blocks", test_bankroll_malformed_result_blocks),
    ("exposure_module_unavailable_blocks", test_exposure_module_unavailable_blocks),
    ("exposure_raises_blocks", test_exposure_raises_blocks),
    ("exposure_malformed_result_blocks", test_exposure_malformed_result_blocks),
    ("risk_guards_internal_exception_blocks", test_risk_guards_internal_exception_blocks),
    ("can_trade_db_unavailable_blocks", test_can_trade_db_unavailable_blocks),
    ("exposure_db_unavailable_blocks", test_exposure_db_unavailable_blocks),
    ("can_trade_uninitialized_wallet_blocks", test_can_trade_uninitialized_wallet_blocks),
    ("skip_records_no_bet_and_never_opens_position", test_skip_records_no_bet_and_never_opens_position),
    ("sameday_sd_skip_records_no_bet", test_sameday_sd_skip_records_no_bet),
    ("every_gate_call_is_guarded_before_open", test_every_gate_call_is_guarded_before_open),
    ("happy_path_all_gates_allow", test_happy_path_all_gates_allow),
    ("normal_blocks_keep_their_own_reasons", test_normal_blocks_keep_their_own_reasons),
    ("safety_gate_coerce_only_explicit_true_passes", test_safety_gate_coerce_only_explicit_true_passes),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
