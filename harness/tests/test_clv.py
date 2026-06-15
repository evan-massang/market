"""B2 — no-network unit tests for harness.clv (CLV + edge-decay analytics).

Temp DB only (make_temp_env). No network, no LLM. Verifies:
  * YES CLV sign: closing > entry  -> positive (the line moved toward us)
  * NO  CLV sign: closing < entry  -> positive
  * mean_clv aggregation + pct_positive over a mix of rows
  * below min_n -> mean_clv returns None; clv_by_theme drops the thin theme
  * clv_by_theme splits by theme and respects min_n
  * de-dupe on read: an exact re-record counts ONCE
  * record_clv rejects bad input (unknown side / missing price) without raising
  * edge_decay_report buckets synthetic resolved rows by lead time, sane stats
  * edge_decay_report on a fresh DB (no paper_positions) -> {} (never raises)

Run:  python -m harness.tests.test_clv
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_clv_")

from harness import clv as CLV                          # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Drop both tables so each test starts clean (temp DB shared in-process)."""
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(f"DROP TABLE IF EXISTS {CLV._TABLE}")
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.commit()
    conn.close()


def _seed_positions(rows):
    """Insert synthetic settled paper_positions rows for edge-decay tests.

    rows: list of dicts with model_p, market_p, stake, realized_pnl, opened_at, end_date.
    """
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, question TEXT, side TEXT,
            model_p REAL, market_p REAL, edge REAL,
            stake REAL, fill_price REAL, shares REAL, fee REAL,
            status TEXT DEFAULT 'open',
            outcome REAL, payout REAL, realized_pnl REAL,
            end_date TEXT, opened_at TEXT DEFAULT CURRENT_TIMESTAMP, settled_at TEXT
        )"""
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO paper_positions (market_id, side, model_p, market_p, edge, "
            "stake, realized_pnl, status, opened_at, end_date) "
            "VALUES (?,?,?,?,?,?,?, 'settled', ?, ?)",
            (f"M{i}", r.get("side", "YES"), r.get("model_p"), r.get("market_p"),
             r.get("edge"), r["stake"], r["realized_pnl"],
             r.get("opened_at"), r.get("end_date")),
        )
    conn.commit()
    conn.close()


# ── CLV sign ──────────────────────────────────────────────────────────────────
def test_yes_clv_sign_positive_when_line_rises():
    _reset()
    # YES bought at 0.40, closes at 0.55 -> line moved toward us -> +0.15
    assert CLV.record_clv("Y1", "YES", 0.40, 0.55, theme="elections") is True
    s = CLV.mean_clv(min_n=1)
    assert s is not None, s
    assert abs(s["mean_clv"] - 0.15) < 1e-9, s
    assert s["pct_positive"] == 1.0, s


def test_no_clv_sign_positive_when_line_falls():
    _reset()
    # NO bought at 0.60, closes at 0.40 -> line fell -> we beat the close -> +0.20
    assert CLV.record_clv("N1", "NO", 0.60, 0.40, theme="geopolitics") is True
    s = CLV.mean_clv(min_n=1)
    assert s is not None, s
    assert abs(s["mean_clv"] - 0.20) < 1e-9, s
    assert s["pct_positive"] == 1.0, s

    # ...and a NO whose line ROSE is negative CLV (we got run over)
    _reset()
    assert CLV.record_clv("N2", "NO", 0.40, 0.60, theme="geopolitics") is True
    s2 = CLV.mean_clv(min_n=1)
    assert abs(s2["mean_clv"] - (-0.20)) < 1e-9, s2
    assert s2["pct_positive"] == 0.0, s2


# ── aggregation + pct_positive ──────────────────────────────────────────────────
def test_mean_clv_aggregation_and_pct_positive():
    _reset()
    # 3 positive, 1 negative, 1 flat(=0, NOT counted positive)
    assert CLV.record_clv("A1", "YES", 0.30, 0.40)   # +0.10
    assert CLV.record_clv("A2", "YES", 0.50, 0.60)   # +0.10
    assert CLV.record_clv("A3", "NO", 0.70, 0.50)    # +0.20
    assert CLV.record_clv("A4", "YES", 0.50, 0.40)   # -0.10
    assert CLV.record_clv("A5", "YES", 0.50, 0.50)   #  0.00
    s = CLV.mean_clv(min_n=5)
    assert s is not None, s
    assert s["n"] == 5, s
    # mean = (0.10 + 0.10 + 0.20 - 0.10 + 0.0) / 5 = 0.06
    assert abs(s["mean_clv"] - 0.06) < 1e-9, s
    # only the 3 strictly-positive count
    assert abs(s["pct_positive"] - 0.6) < 1e-9, s


def test_below_min_n_returns_none():
    _reset()
    for i in range(3):
        CLV.record_clv(f"B{i}", "YES", 0.30, 0.40)
    assert CLV.mean_clv(min_n=5) is None       # 3 < 5
    s = CLV.mean_clv(min_n=3)                   # bar lowered -> visible
    assert s is not None and s["n"] == 3, s


def test_clv_by_theme_splits_and_respects_min_n():
    _reset()
    for i in range(5):
        CLV.record_clv(f"E{i}", "YES", 0.30, 0.40, theme="elections")   # +0.10 each
    for i in range(2):
        CLV.record_clv(f"G{i}", "NO", 0.60, 0.50, theme="geopolitics")  # +0.10 each
    by = CLV.clv_by_theme(min_n=5)
    assert "elections" in by, by
    assert by["elections"]["n"] == 5, by
    assert abs(by["elections"]["mean_clv"] - 0.10) < 1e-9, by
    assert "geopolitics" not in by, by                                  # 2 < 5
    # NULL theme buckets under 'other'
    _reset()
    for i in range(5):
        CLV.record_clv(f"O{i}", "YES", 0.30, 0.40)                      # theme None
    by2 = CLV.clv_by_theme(min_n=5)
    assert "other" in by2, by2
    assert by2["other"]["n"] == 5, by2


# ── de-dupe + bad input ─────────────────────────────────────────────────────────
def test_dedupe_on_read():
    _reset()
    assert CLV.record_clv("DUP", "YES", 0.40, 0.55, theme="culture") is True
    # exact re-record (same market_id + side + closing) is skipped
    assert CLV.record_clv("DUP", "YES", 0.40, 0.55, theme="culture") is False
    s = CLV.mean_clv(min_n=1)
    assert s["n"] == 1, s


def test_bad_input_is_rejected_without_raising():
    _reset()
    assert CLV.record_clv("X1", "MAYBE", 0.4, 0.5) is False     # unknown side
    assert CLV.record_clv("X2", "YES", None, 0.5) is False      # missing entry
    assert CLV.record_clv("X3", "YES", 0.4, None) is False      # missing closing
    assert CLV.mean_clv(min_n=1) is None                        # nothing recorded


# ── edge decay ──────────────────────────────────────────────────────────────────
def test_edge_decay_report_buckets_and_stats():
    _reset()
    base = datetime(2026, 6, 1, 12, 0, 0)

    def opened(d):
        return base.isoformat()

    def end(days):
        return (base + timedelta(days=days)).isoformat()

    # near-term winners (lead <=1d), mid-term mixed (1-7d), long-term loser (>30d)
    _seed_positions([
        # <=1d: predicted edge 0.10, +50% return
        {"model_p": 0.60, "market_p": 0.50, "stake": 10.0, "realized_pnl": 5.0,
         "opened_at": base.isoformat(), "end_date": end(0.5)},
        {"model_p": 0.40, "market_p": 0.50, "stake": 10.0, "realized_pnl": 3.0,
         "opened_at": base.isoformat(), "end_date": end(1.0)},
        # 1-7d: predicted edge 0.20, one win one loss
        {"model_p": 0.70, "market_p": 0.50, "stake": 10.0, "realized_pnl": 8.0,
         "opened_at": base.isoformat(), "end_date": end(3.0)},
        {"model_p": 0.30, "market_p": 0.50, "stake": 10.0, "realized_pnl": -10.0,
         "opened_at": base.isoformat(), "end_date": end(5.0)},
        # >30d: predicted edge 0.15, lost money (decayed edge)
        {"model_p": 0.65, "market_p": 0.50, "stake": 10.0, "realized_pnl": -4.0,
         "opened_at": base.isoformat(), "end_date": end(45.0)},
    ])

    rep = CLV.edge_decay_report()
    assert isinstance(rep, dict) and rep, rep
    assert "<=1d" in rep and "1-7d" in rep and ">30d" in rep, rep

    near = rep["<=1d"]
    assert near["n"] == 2, near
    assert abs(near["mean_predicted_edge"] - 0.10) < 1e-9, near
    # returns: +0.5 and +0.3 -> mean +0.4
    assert abs(near["mean_realized_return"] - 0.40) < 1e-9, near
    assert near["pct_profitable"] == 1.0, near
    assert near["edge_capture"] is not None, near

    mid = rep["1-7d"]
    assert mid["n"] == 2, mid
    assert abs(mid["mean_predicted_edge"] - 0.20) < 1e-9, mid
    # returns: +0.8 and -1.0 -> mean -0.1 ; one of two profitable
    assert abs(mid["mean_realized_return"] - (-0.10)) < 1e-9, mid
    assert mid["pct_profitable"] == 0.5, mid

    far = rep[">30d"]
    assert far["n"] == 1, far
    assert far["pct_profitable"] == 0.0, far       # decayed edge lost money


def test_edge_decay_unknown_bucket_when_no_dates():
    _reset()
    _seed_positions([
        {"model_p": 0.60, "market_p": 0.50, "stake": 10.0, "realized_pnl": 2.0,
         "opened_at": None, "end_date": None},
    ])
    rep = CLV.edge_decay_report()
    assert "unknown" in rep, rep
    assert rep["unknown"]["n"] == 1, rep


def test_edge_decay_empty_db_is_empty_dict():
    _reset()
    # no paper_positions table at all -> {} (never raises)
    assert CLV.edge_decay_report() == {}
    # and CLV aggregations on an empty DB are safe too
    assert CLV.mean_clv(min_n=5) is None
    assert CLV.clv_by_theme(min_n=5) == {}


TESTS = [
    ("yes_clv_sign_positive_when_line_rises", test_yes_clv_sign_positive_when_line_rises),
    ("no_clv_sign_positive_when_line_falls", test_no_clv_sign_positive_when_line_falls),
    ("mean_clv_aggregation_and_pct_positive", test_mean_clv_aggregation_and_pct_positive),
    ("below_min_n_returns_none", test_below_min_n_returns_none),
    ("clv_by_theme_splits_and_respects_min_n", test_clv_by_theme_splits_and_respects_min_n),
    ("dedupe_on_read", test_dedupe_on_read),
    ("bad_input_is_rejected_without_raising", test_bad_input_is_rejected_without_raising),
    ("edge_decay_report_buckets_and_stats", test_edge_decay_report_buckets_and_stats),
    ("edge_decay_unknown_bucket_when_no_dates", test_edge_decay_unknown_bucket_when_no_dates),
    ("edge_decay_empty_db_is_empty_dict", test_edge_decay_empty_db_is_empty_dict),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
