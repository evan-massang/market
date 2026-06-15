"""Unit tests for settlement: gamma.resolution_outcome (PURE) + loop.settle_resolved.

resolution_outcome is tested with crafted normalized dicts (zero monkeypatch).
settle_resolved is driven offline by monkeypatching gamma.fetch_market_by_condition_id
(the only network call) — NO HTTP, temp DB.
Run:  python -m harness.tests.test_settlement
"""
from __future__ import annotations

import os
import sqlite3
import sys

from harness.tests._util import make_temp_env, run_as_main, patched

make_temp_env("ps_settlement_")

from harness import gamma                       # noqa: E402
from harness import wallet as W                 # noqa: E402
from harness import challenger                  # noqa: E402
from harness import journal                     # noqa: E402
from harness import loop                        # noqa: E402
from core import calibration                    # noqa: E402


# ── gamma.resolution_outcome — PURE ───────────────────────────────────────────
def _mkt(closed, prices, outcomes=("Yes", "No"), uma=None):
    raw = {"closed": closed}
    if uma is not None:
        raw["umaResolutionStatus"] = uma
    return {"raw": raw, "outcomes": list(outcomes), "outcome_prices": list(prices)}


def test_resolution_outcome_pure():
    assert gamma.resolution_outcome(_mkt(True, [0.995, 0.005])) == 1.0
    assert gamma.resolution_outcome(_mkt(True, [0.005, 0.995])) == 0.0
    # string "true" coercion
    assert gamma.resolution_outcome(_mkt("true", [0.99, 0.01])) == 1.0
    # UMA proposed snap (threshold 0.985), not yet closed
    assert gamma.resolution_outcome(_mkt(False, [0.99, 0.01], uma="proposed")) == 1.0
    # not result_in -> None
    assert gamma.resolution_outcome({"raw": {"closed": False}}) is None
    # closed but mid-blip price -> None (no snap)
    assert gamma.resolution_outcome(_mkt(True, [0.6, 0.4])) is None
    # UMA proposed but below the tighter 0.985 threshold -> None
    assert gamma.resolution_outcome(_mkt(False, [0.98, 0.02], uma="proposed")) is None


def test_resolution_outcome_no_yes_label():
    # No 'Yes' label: fall back to the highest-priced outcome if it clearly won.
    assert gamma.resolution_outcome(_mkt(True, [0.01, 0.99], outcomes=("Team A", "No"))) == 0.0
    # highest-priced is neither yes nor no -> None
    assert gamma.resolution_outcome(_mkt(True, [0.01, 0.99], outcomes=("Team A", "Team B"))) is None


# ── loop.settle_resolved — temp DB, gamma monkeypatched ───────────────────────
def _rebuild_db():
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("swarm_forecasts", "forecasts", "baseline_forecasts",
              "paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    calibration.init_db()
    W.init_wallet(1000.0)
    challenger.init_baseline_db()
    journal.init_journal()


def _seed(mid, q="Will X win?"):
    W.open_position(mid, q, "YES", 0.70, 0.50, 0.20, 10.0)
    calibration.save_swarm_forecast(q, 0.70, 0.80, 0.50, market_id=mid)
    challenger.save_baseline(mid, q, 0.65, 0.50)


def _brier_for(mid):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT brier_score FROM swarm_forecasts WHERE market_id=?",
                       (mid,)).fetchone()
    base = conn.execute("SELECT outcome FROM baseline_forecasts WHERE market_id=?",
                        (mid,)).fetchone()
    conn.close()
    return (row["brier_score"] if row else None), (base["outcome"] if base else None)


def test_settle_resolved_yes_win():
    _rebuild_db()
    _seed("CID-1")
    closed_yes = {"raw": {"closed": True}, "outcomes": ["Yes", "No"],
                  "outcome_prices": [0.99, 0.01]}
    with patched(loop.gamma, "fetch_market_by_condition_id", lambda mid, **k: closed_yes):
        res = loop.settle_resolved(loop.LoopConfig())
    assert len(res) == 1, res
    r = res[0]
    assert r["outcome"] == 1.0 and r["positions"] == 1 and r["realized_pnl"] > 0, r
    assert W.get_open_positions() == [], "position should be settled"
    brier, base_outcome = _brier_for("CID-1")
    assert brier is not None, "swarm_forecasts.brier_score not written"
    assert base_outcome == 1.0, "challenger baseline not resolved"


def test_settle_market_not_found_stays_open():
    _rebuild_db()
    _seed("CID-2")
    with patched(loop.gamma, "fetch_market_by_condition_id", lambda mid, **k: None):
        res = loop.settle_resolved(loop.LoopConfig())
    assert res == [], res
    assert len(W.get_open_positions()) == 1, "position must remain open when not found"


def test_settle_not_resolved_stays_open():
    _rebuild_db()
    _seed("CID-3")
    unresolved = {"raw": {"closed": False}, "outcomes": ["Yes", "No"],
                  "outcome_prices": [0.6, 0.4]}
    with patched(loop.gamma, "fetch_market_by_condition_id", lambda mid, **k: unresolved):
        res = loop.settle_resolved(loop.LoopConfig())
    assert res == [], res
    assert len(W.get_open_positions()) == 1, "position must remain open when unresolved"


TESTS = [
    ("resolution_outcome_pure", test_resolution_outcome_pure),
    ("resolution_outcome_no_yes_label", test_resolution_outcome_no_yes_label),
    ("settle_resolved_yes_win", test_settle_resolved_yes_win),
    ("settle_market_not_found_stays_open", test_settle_market_not_found_stays_open),
    ("settle_not_resolved_stays_open", test_settle_not_resolved_stays_open),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
