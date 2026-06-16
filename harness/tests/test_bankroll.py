"""P9 — no-network tests for harness.bankroll (kill switch + exposure caps + MTM).

Covers:
  * can_trade: healthy -> ok; drawdown_pause; loss_limit; losing-streak cooldown
  * cooldown is OPINION-SCOPED (mechanical losses don't trigger it)
  * opinion_loss_streak counts consecutive opinion losses, resets on a win
  * exposure_ok: under cap ok; over theme/event cap blocks; 'other' theme exempt
  * mark_to_market_equity: marks open positions at a current price_map, else at-cost
  * fail-open / never raises
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_bank_")

from harness import wallet            # noqa: E402
from harness import bankroll as BK    # noqa: E402

_OP_Q = "Will candidate {} win the 2032 presidential election?"   # classifies opinion
_MECH_Q = "Will Bitcoin close above $100,000 on day {}?"          # classifies mechanical


def _reset(starting=1000.0, cash=None, realized=0.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    wallet.init_wallet(starting)
    if cash is not None or realized:
        conn = sqlite3.connect(os.environ["DATABASE_URL"])
        conn.execute("UPDATE paper_wallet SET cash=?, realized_pnl=? WHERE id=1",
                     (starting if cash is None else cash, realized))
        conn.commit()
        conn.close()


def _settled(question, realized_pnl, market_id, settled_at):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, "
        "stake, fill_price, shares, fee, status, outcome, payout, realized_pnl, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'settled', ?, ?, ?, ?)",
        (market_id, question, "YES", 0.6, 0.5, 0.1, 10.0, 0.51, 19.6, 0.0,
         (1.0 if realized_pnl > 0 else 0.0), max(realized_pnl + 10.0, 0.0), realized_pnl, settled_at),
    )
    conn.commit()
    conn.close()


def _open(question, market_id, stake, event_slug=None, side="YES"):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, "
        "stake, fill_price, shares, fee, status, event_slug) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (market_id, question, side, 0.6, 0.5, 0.1, stake, 0.51, stake / 0.51, 0.0, event_slug),
    )
    conn.commit()
    conn.close()


# ── can_trade kill switch ─────────────────────────────────────────────────────
def test_healthy_book_can_trade():
    _reset()
    ok, reason = BK.can_trade()
    assert ok is True, (ok, reason)


def test_drawdown_pause():
    # equity 700 vs starting 1000 -> 30% drawdown >= 25% -> pause
    _reset(starting=1000.0, cash=700.0, realized=-300.0)
    ok, reason = BK.can_trade()
    assert ok is False and reason.startswith("drawdown_pause"), (ok, reason)


def test_loss_limit():
    # keep equity high (so drawdown doesn't fire) but realized below -30% of start
    _reset(starting=1000.0, cash=1000.0, realized=-350.0)
    ok, reason = BK.can_trade()
    assert ok is False and reason.startswith("loss_limit"), (ok, reason)


def test_cooldown_after_opinion_losing_streak():
    _reset()
    for i in range(BK.COOLDOWN_STREAK):
        _settled(_OP_Q.format(i), -5.0, f"L{i}", settled_at=f"2026-06-1{i}T00:00:00")
    ok, reason = BK.can_trade()
    assert ok is False and reason.startswith("cooldown"), (ok, reason)


def test_cooldown_is_opinion_scoped():
    # a long streak of MECHANICAL losses must NOT trip the AI cooldown
    _reset()
    for i in range(BK.COOLDOWN_STREAK + 3):
        _settled(_MECH_Q.format(i), -5.0, f"M{i}", settled_at=f"2026-06-1{i}T00:00:00")
    assert BK.opinion_loss_streak() == 0
    ok, reason = BK.can_trade()
    assert ok is True, (ok, reason)


def test_streak_resets_on_win():
    _reset()
    # most-recent settled is a WIN -> streak 0 even with older losses
    _settled(_OP_Q.format(1), -5.0, "A", settled_at="2026-06-10T00:00:00")
    _settled(_OP_Q.format(2), -5.0, "B", settled_at="2026-06-11T00:00:00")
    _settled(_OP_Q.format(3), +9.0, "C", settled_at="2026-06-12T00:00:00")   # newest = win
    assert BK.opinion_loss_streak() == 0


# ── exposure caps ─────────────────────────────────────────────────────────────
def test_exposure_under_cap_ok():
    _reset()
    ok, reason, _ = BK.exposure_ok("elections", "evt-a", new_stake=10.0, bankroll=1000.0)
    assert ok is True and reason is None, (ok, reason)


def test_theme_exposure_cap_blocks():
    _reset()
    # 240 already open in 'elections'; +20 new vs 25% of 1000 = 250 -> 260 > 250 blocks
    for i in range(8):
        _open(_OP_Q.format(i), f"T{i}", stake=30.0)   # 8*30 = 240 in 'elections'
    ok, reason, detail = BK.exposure_ok("elections", None, new_stake=20.0, bankroll=1000.0)
    assert ok is False and reason == "theme_exposure_cap", (ok, reason, detail)


def test_other_theme_exempt_from_theme_cap():
    _reset()
    for i in range(8):
        _open("Some unrelated question " + str(i), f"O{i}", stake=30.0)  # theme 'other'
    ok, reason, _ = BK.exposure_ok("other", None, new_stake=500.0, bankroll=1000.0)
    assert ok is True, (ok, reason)


def test_event_exposure_cap_blocks():
    _reset()
    # 140 open in event 'evt-x'; +20 vs 15% of 1000 = 150 -> 160 > 150 blocks
    for i in range(7):
        _open(_OP_Q.format(i), f"E{i}", stake=20.0, event_slug="evt-x")
    ok, reason, _ = BK.exposure_ok("elections", "evt-x", new_stake=20.0, bankroll=1000.0)
    assert ok is False and reason == "event_exposure_cap", (ok, reason)


# ── mark-to-market ────────────────────────────────────────────────────────────
def test_mark_to_market_uses_price_map_else_cost():
    _reset(starting=1000.0, cash=900.0)
    _open(_OP_Q.format(1), "MTM1", stake=10.0, side="YES")  # ~19.6 shares @ 0.51
    # no price map -> at cost (stake contribution)
    base = BK.mark_to_market_equity()
    assert base["n_open"] == 1 and base["n_marked"] == 0
    # mark YES at 0.80 -> 19.6 shares * 0.80 = 15.69 > the 10 cost
    marked = BK.mark_to_market_equity(price_map={"MTM1": 0.80})
    assert marked["n_marked"] == 1
    assert marked["mtm_open_value"] > base["mtm_open_value"], (marked, base)
    assert marked["mtm_equity"] == round(marked["cash"] + marked["mtm_open_value"], 4)


def test_db_unavailable_fails_closed():
    """Plan 1: a missing/locked wallet DB is the money gate being UNAVAILABLE.
    can_trade + exposure_ok must BLOCK (was: fail-open to allow). The analytics-only
    helpers (opinion_loss_streak, mark_to_market_equity) still degrade softly because
    they never on their own approve a bet."""
    from harness import safety_gate as SG
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute("DROP TABLE IF EXISTS paper_positions")
    conn.execute("DROP TABLE IF EXISTS paper_wallet")
    conn.commit()
    conn.close()
    ct_ok, ct_reason = BK.can_trade()
    assert ct_ok is False and SG.is_fail_closed(ct_reason), (ct_ok, ct_reason)
    ex_ok, ex_reason, _ = BK.exposure_ok("x", "y", 10.0)
    assert ex_ok is False and SG.is_fail_closed(ex_reason), (ex_ok, ex_reason)
    # analytics helpers never raise and never themselves approve a bet
    assert BK.opinion_loss_streak() == 0
    assert isinstance(BK.mark_to_market_equity(), dict)


TESTS = [
    ("healthy_book_can_trade", test_healthy_book_can_trade),
    ("drawdown_pause", test_drawdown_pause),
    ("loss_limit", test_loss_limit),
    ("cooldown_after_opinion_losing_streak", test_cooldown_after_opinion_losing_streak),
    ("cooldown_is_opinion_scoped", test_cooldown_is_opinion_scoped),
    ("streak_resets_on_win", test_streak_resets_on_win),
    ("exposure_under_cap_ok", test_exposure_under_cap_ok),
    ("theme_exposure_cap_blocks", test_theme_exposure_cap_blocks),
    ("other_theme_exempt_from_theme_cap", test_other_theme_exempt_from_theme_cap),
    ("event_exposure_cap_blocks", test_event_exposure_cap_blocks),
    ("mark_to_market_uses_price_map_else_cost", test_mark_to_market_uses_price_map_else_cost),
    ("db_unavailable_fails_closed", test_db_unavailable_fails_closed),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
