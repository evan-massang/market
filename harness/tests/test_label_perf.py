"""P4B — no-network unit tests for harness.label_perf (label backtest + observe-only).

Temp DB only (make_temp_env). No network, no LLM. Verifies:
  * a label with n>=min_n that LOSES money / doesn't beat the market -> observe-only
  * a label with n>=min_n that WINS and beats the market           -> NOT observe-only
  * label_performance aggregates n / brier / market_brier / pnl / win_rate correctly
  * below min_n -> never observe-only (and excluded from label_performance)
  * de-dupe on read: re-recording the same market_id counts ONCE

Run:  python -m harness.tests.test_label_perf
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_label_perf_")

from harness import label_perf as LP                # noqa: E402


# ── sample builders ───────────────────────────────────────────────────────────
def _losing(i):
    # outcome=0: model says 0.9 (Brier 0.81, BAD), market says 0.2 (Brier 0.04, good),
    # and the bet lost money. -> loses AND fails to beat the market.
    return LP.record_classification_outcome(
        f"L{i}", "Will the bad thing happen?", "crypto-prices", "mechanical",
        model_p=0.9, market_p=0.2, outcome=0.0, pnl=-5.0)


def _winning(i):
    # outcome=1: model says 0.95 (Brier 0.0025, GREAT), market says 0.6 (Brier 0.16),
    # and the bet made money. -> wins AND beats the market.
    return LP.record_classification_outcome(
        f"W{i}", "Will the good thing happen?", "elections", "opinion",
        model_p=0.95, market_p=0.6, outcome=1.0, pnl=4.0)


def _reset():
    """Drop the table so each test starts clean (temp DB is shared in-process)."""
    import os, sqlite3
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(f"DROP TABLE IF EXISTS {LP._TABLE}")
    conn.commit()
    conn.close()


# ── tests ─────────────────────────────────────────────────────────────────────
def test_losing_label_is_observe_only():
    _reset()
    for i in range(12):
        assert _losing(i) is True
    assert LP.should_observe_only("crypto-prices", min_n=10) is True

    perf = LP.label_performance(min_n=10)
    assert "crypto-prices" in perf
    s = perf["crypto-prices"]
    assert s["n"] == 12, s
    assert abs(s["total_pnl"] - (-60.0)) < 1e-9, s
    assert s["win_rate"] == 0.0, s
    # model Brier (0.81) is worse than market Brier (0.04)
    assert abs(s["mean_brier"] - 0.81) < 1e-9, s
    assert abs(s["mean_market_brier"] - 0.04) < 1e-9, s
    assert s["mean_brier"] >= s["mean_market_brier"]


def test_winning_label_is_not_observe_only():
    _reset()
    for i in range(12):
        assert _winning(i) is True
    assert LP.should_observe_only("elections", min_n=10) is False

    s = LP.label_performance(min_n=10)["elections"]
    assert s["n"] == 12, s
    assert abs(s["total_pnl"] - 48.0) < 1e-9, s
    assert s["win_rate"] == 1.0, s
    assert abs(s["mean_brier"] - 0.0025) < 1e-9, s
    assert abs(s["mean_market_brier"] - 0.16) < 1e-9, s
    assert s["mean_brier"] < s["mean_market_brier"]


def test_below_min_n_is_not_observe_only():
    _reset()
    for i in range(3):
        _losing(i)
    # n=3 < min_n=10 -> not observe-only despite losing
    assert LP.should_observe_only("crypto-prices", min_n=10) is False
    # ...and excluded from the default-min_n performance view
    assert "crypto-prices" not in LP.label_performance(min_n=10)
    # but visible (and observe-only) when the bar is lowered
    assert "crypto-prices" in LP.label_performance(min_n=1)
    assert LP.should_observe_only("crypto-prices", min_n=1) is True


def test_unknown_label_is_not_observe_only():
    _reset()
    assert LP.should_observe_only("never-recorded", min_n=10) is False
    assert LP.label_performance(min_n=10) == {}


def test_dedupe_on_read():
    _reset()
    # First insert for a market id succeeds; an exact re-record (same id + outcome)
    # is skipped, and even if it weren't, read-side de-dupe keeps ONE row per market.
    assert LP.record_classification_outcome(
        "DUP-1", "q", "weather", "mechanical", 0.7, 0.5, 1.0, 2.0) is True
    assert LP.record_classification_outcome(
        "DUP-1", "q", "weather", "mechanical", 0.7, 0.5, 1.0, 2.0) is False
    s = LP.label_performance(min_n=1)["weather"]
    assert s["n"] == 1, s
    assert abs(s["total_pnl"] - 2.0) < 1e-9, s


def test_record_handles_missing_model_p():
    _reset()
    # model_p None -> brier NULL, but the row still records and counts.
    assert LP.record_classification_outcome(
        "NM-1", "q", "sports", "mechanical", None, 0.4, 1.0, 1.0) is True
    s = LP.label_performance(min_n=1)["sports"]
    assert s["n"] == 1, s
    assert s["mean_brier"] is None, s            # no model Brier available
    assert abs(s["mean_market_brier"] - 0.36) < 1e-9, s  # (0.4-1)**2


TESTS = [
    ("losing_label_is_observe_only", test_losing_label_is_observe_only),
    ("winning_label_is_not_observe_only", test_winning_label_is_not_observe_only),
    ("below_min_n_is_not_observe_only", test_below_min_n_is_not_observe_only),
    ("unknown_label_is_not_observe_only", test_unknown_label_is_not_observe_only),
    ("dedupe_on_read", test_dedupe_on_read),
    ("record_handles_missing_model_p", test_record_handles_missing_model_p),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
