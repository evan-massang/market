"""Plan 4 — WALLET ATOMICITY + RACE SAFETY. wallet.open_position must be atomic:
read→check→guarded-debit→insert→commit in ONE BEGIN IMMEDIATE transaction, so two
concurrent opens can never overdraw cash, bypass the exposure cap, or duplicate a
market open; a failure on either step rolls the whole thing back.

NO network, NO LLM. Temp DB only. Concurrency via threads against the temp SQLite DB.
Run: python -m harness.tests.test_wallet_atomic
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_wallet_atomic_")
os.environ["LLM_PROVIDER"] = "ollama"

from harness import wallet as W            # noqa: E402
from harness.wallet import WalletConfig    # noqa: E402
from harness import predict_today as PT    # noqa: E402
from harness import safe_bet as SAFE       # noqa: E402

_HI = WalletConfig(max_bet_frac=1.0, max_exposure_frac=1.0)   # caps out of the way


def _reset(starting=1000.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(starting)


def _cash():
    return W.get_state()["cash"]


def _n_open(market_id=None):
    pos = W.get_open_positions()
    return len([p for p in pos if market_id is None or p["market_id"] == market_id])


# ════════════════════════════════════════════════════════════════════════════
# (1-6) single-call atomic behavior
# ════════════════════════════════════════════════════════════════════════════
def test_success_debits_and_inserts():
    _reset()
    fr = W.open_position("M1", "Q", "YES", 0.6, 0.50, 0.1, 20.0)
    assert fr.opened and fr.reason == "filled", fr
    assert abs(_cash() - 980.0) < 1e-6 and _n_open("M1") == 1


def test_insufficient_cash_blocks_cash_unchanged():
    _reset(starting=5.0)
    fr = W.open_position("M1", "Q", "YES", 0.6, 0.50, 0.1, 20.0, cfg=_HI)
    assert fr.opened is False and fr.reason == "wallet_insufficient_cash", fr
    assert abs(_cash() - 5.0) < 1e-6 and _n_open() == 0


def test_exposure_cap_blocks_cash_unchanged():
    _reset()
    fr = W.open_position("M1", "Q", "YES", 0.6, 0.50, 0.1, 20.0,
                         cfg=WalletConfig(max_bet_frac=1.0, max_exposure_frac=0.001))
    assert fr.opened is False and fr.reason == "wallet_exposure_cap_exceeded", fr
    assert abs(_cash() - 1000.0) < 1e-6 and _n_open() == 0


def test_invalid_side_blocks():
    _reset()
    fr = W.open_position("M1", "Q", "MAYBE", 0.6, 0.50, 0.1, 20.0)
    assert fr.opened is False and fr.reason == "wallet_invalid_side"
    assert abs(_cash() - 1000.0) < 1e-6 and _n_open() == 0


def test_invalid_price_blocks():
    _reset()
    for bad in (0.0, 1.0, 1.5, -0.1, float("nan")):
        fr = W.open_position("M1", "Q", "YES", 0.6, bad, 0.1, 20.0)
        assert fr.opened is False and fr.reason == "wallet_invalid_price", (bad, fr)
    assert abs(_cash() - 1000.0) < 1e-6 and _n_open() == 0


def test_invalid_stake_blocks():
    _reset()
    for bad in (0.0, -5.0, float("nan")):
        fr = W.open_position("M1", "Q", "YES", 0.6, 0.50, 0.1, bad)
        assert fr.opened is False and fr.reason == "wallet_invalid_stake", (bad, fr)
    assert abs(_cash() - 1000.0) < 1e-6 and _n_open() == 0


# ════════════════════════════════════════════════════════════════════════════
# (7-8) duplicate-open policy
# ════════════════════════════════════════════════════════════════════════════
def test_duplicate_open_blocked_by_default():
    _reset()   # _HI caps so the DUPLICATE check (not the per-bet cap) is what blocks
    a = W.open_position("DUP", "Q", "YES", 0.6, 0.50, 0.1, 20.0, cfg=_HI)
    assert a.opened, a
    cash_after_first = _cash()
    b = W.open_position("DUP", "Q", "NO", 0.6, 0.50, 0.1, 20.0, cfg=_HI)   # same market
    assert b.opened is False and b.reason == "wallet_duplicate_open_blocked", b
    # duplicate did NOT debit cash and did NOT insert a second open row
    assert abs(_cash() - cash_after_first) < 1e-6
    assert _n_open("DUP") == 1


def test_duplicate_open_allowed_when_explicit():
    _reset()
    assert W.open_position("DUP", "Q", "YES", 0.6, 0.50, 0.1, 20.0, cfg=_HI).opened
    b = W.open_position("DUP", "Q", "YES", 0.6, 0.50, 0.1, 20.0, cfg=_HI, allow_duplicate=True)
    assert b.opened and _n_open("DUP") == 2


# ════════════════════════════════════════════════════════════════════════════
# (9-11) rollback / DB-fault behavior
# ════════════════════════════════════════════════════════════════════════════
def test_insert_failure_rolls_back_debit():
    _reset()
    real_connect = sqlite3.connect

    class _Proxy:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("INSERT INTO PAPER_POSITIONS"):
                raise sqlite3.IntegrityError("simulated insert failure after debit")
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._c, n)

    def _fake_connect(*a, **k):
        return _Proxy(real_connect(*a, **k))

    cash_before = _cash()
    with patched(W.sqlite3, "connect", _fake_connect):
        fr = W.open_position("MINS", "Q", "YES", 0.6, 0.50, 0.1, 20.0)
    assert fr.opened is False and "rolled_back" in fr.reason, fr
    assert abs(_cash() - cash_before) < 1e-6, "debit must be rolled back"   # cash unchanged
    assert _n_open() == 0, "no position may be inserted"


def test_db_locked_returns_failure_no_partial_write():
    _reset()
    real_connect = sqlite3.connect

    class _Proxy:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._c, n)

    def _fake_connect(*a, **k):
        return _Proxy(real_connect(*a, **k))

    cash_before = _cash()
    with patched(W.sqlite3, "connect", _fake_connect):
        fr = W.open_position("MLCK", "Q", "YES", 0.6, 0.50, 0.1, 20.0)
    assert fr.opened is False and fr.reason == "wallet_db_locked_or_unavailable", fr
    assert abs(_cash() - cash_before) < 1e-6 and _n_open() == 0


def test_guarded_update_is_present():
    # structural: the cash debit is guarded (WHERE ... cash >= ?) + rowcount-checked,
    # and the whole op runs under BEGIN IMMEDIATE.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "harness", "wallet.py"), encoding="utf-8") as f:
        src = f.read()
    assert "BEGIN IMMEDIATE" in src
    assert "WHERE id=1 AND cash >= ?" in src
    assert "cur.rowcount != 1" in src
    assert "busy_timeout" in src


# ════════════════════════════════════════════════════════════════════════════
# (12-15) CONCURRENCY — two simultaneous opens via threads
# ════════════════════════════════════════════════════════════════════════════
def _race(targets):
    """Run len(targets) open_position calls concurrently; return {i: FillResult}."""
    results = {}
    barrier = threading.Barrier(len(targets))

    def _go(i, kwargs):
        barrier.wait()   # release all threads at once → maximize contention
        results[i] = W.open_position(**kwargs)

    threads = [threading.Thread(target=_go, args=(i, kw)) for i, kw in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_opens_cannot_overdraw_cash():
    _reset(starting=20.0)   # room for EXACTLY one 20-stake bet
    base = dict(question="Q", side="YES", model_p=0.6, market_p=0.50, edge=0.1, stake=20.0, cfg=_HI)
    res = _race([dict(base, market_id="A"), dict(base, market_id="B")])
    opened = [r for r in res.values() if r.opened]
    assert len(opened) == 1, [r.reason for r in res.values()]
    st = W.get_state()
    assert st["cash"] >= 0.0 and abs(st["cash"]) < 1e-6, st          # exactly one debit, never negative
    assert _n_open() == 1


def test_concurrent_opens_cannot_bypass_exposure_cap():
    _reset(starting=1000.0)
    cfg = WalletConfig(max_bet_frac=1.0, max_exposure_frac=0.02)     # equity 1000 -> cap 20
    base = dict(question="Q", side="YES", model_p=0.6, market_p=0.50, edge=0.1, stake=20.0, cfg=cfg)
    res = _race([dict(base, market_id="A"), dict(base, market_id="B")])
    opened = [r for r in res.values() if r.opened]
    assert len(opened) == 1, [r.reason for r in res.values()]        # second trips the exposure cap
    assert _n_open() == 1 and W.get_open_exposure() <= 20.0 + 1e-6


def test_concurrent_opens_cannot_duplicate_same_market():
    _reset(starting=1000.0)
    base = dict(market_id="DUP", question="Q", side="YES", model_p=0.6, market_p=0.50, edge=0.1, stake=20.0, cfg=_HI)
    res = _race([dict(base), dict(base)])
    opened = [r for r in res.values() if r.opened]
    assert len(opened) == 1, [r.reason for r in res.values()]
    assert _n_open("DUP") == 1, "a market may have at most one open position"


def test_concurrent_opens_different_markets_both_succeed():
    _reset(starting=1000.0)
    base = dict(question="Q", side="YES", model_p=0.6, market_p=0.50, edge=0.1, stake=20.0, cfg=_HI)
    res = _race([dict(base, market_id="A"), dict(base, market_id="B")])
    opened = [r for r in res.values() if r.opened]
    assert len(opened) == 2, [r.reason for r in res.values()]
    assert abs(_cash() - 960.0) < 1e-6 and _n_open() == 2


# ════════════════════════════════════════════════════════════════════════════
# (16-17) callers treat wallet rejection as no-bet, not success
# ════════════════════════════════════════════════════════════════════════════
def test_safe_bet_handles_wallet_rejection_as_no_bet():
    _reset(starting=5.0)   # too little cash; gates patched to allow so we reach the wallet

    def _allow(*a, **k):
        return True, "ok"

    with patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": (True, "ok")), \
         patched(PT, "_p7_ev_gate", _allow), patched(PT, "_p8_risk_guards", _allow), \
         patched(PT, "_p9_can_trade", _allow), patched(PT, "_p9_exposure_ok", _allow):
        out = SAFE.open_position_if_safe(
            source="loop", market={"market_id": "M1", "question": "Q", "event_slug": None},
            side="YES", probability=0.65, price=0.50, stake=20.0,
            forecast_meta={"allow_bet": True, "n_agents_succeeded": 5})
    assert out["opened"] is False and "wallet_insufficient_cash" in out["reason"], out
    assert _n_open() == 0


def test_predict_today_and_sameday_check_fr_opened():
    # source-scan: both daemons branch on fr.opened (never treat a rejection as a bet)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("harness/predict_today.py", "harness/sameday.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            src = f.read()
        assert "if fr.opened" in src, f"{rel} must branch on fr.opened"


TESTS = [
    ("success_debits_and_inserts", test_success_debits_and_inserts),
    ("insufficient_cash_blocks_cash_unchanged", test_insufficient_cash_blocks_cash_unchanged),
    ("exposure_cap_blocks_cash_unchanged", test_exposure_cap_blocks_cash_unchanged),
    ("invalid_side_blocks", test_invalid_side_blocks),
    ("invalid_price_blocks", test_invalid_price_blocks),
    ("invalid_stake_blocks", test_invalid_stake_blocks),
    ("duplicate_open_blocked_by_default", test_duplicate_open_blocked_by_default),
    ("duplicate_open_allowed_when_explicit", test_duplicate_open_allowed_when_explicit),
    ("insert_failure_rolls_back_debit", test_insert_failure_rolls_back_debit),
    ("db_locked_returns_failure_no_partial_write", test_db_locked_returns_failure_no_partial_write),
    ("guarded_update_is_present", test_guarded_update_is_present),
    ("concurrent_opens_cannot_overdraw_cash", test_concurrent_opens_cannot_overdraw_cash),
    ("concurrent_opens_cannot_bypass_exposure_cap", test_concurrent_opens_cannot_bypass_exposure_cap),
    ("concurrent_opens_cannot_duplicate_same_market", test_concurrent_opens_cannot_duplicate_same_market),
    ("concurrent_opens_different_markets_both_succeed", test_concurrent_opens_different_markets_both_succeed),
    ("safe_bet_handles_wallet_rejection_as_no_bet", test_safe_bet_handles_wallet_rejection_as_no_bet),
    ("predict_today_and_sameday_check_fr_opened", test_predict_today_and_sameday_check_fr_opened),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
