"""Plan 2 — SWARM DEGRADATION safety. A degraded / aborted / under-strength swarm
forecast must never look healthy and must never place a paper bet.

NO network, NO real LLM. The swarm-integration tests use FAKE agents and patch
core.swarm.build_context to "" so the full aggregation pipeline runs offline.
Temp DB only (make_temp_env). Run: python -m harness.tests.test_swarm_degradation
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_swarmdeg_")
os.environ["LLM_PROVIDER"] = "ollama"     # lazy httpx client; no network on Agent() init
os.environ["DEBATE_ROUNDS"] = "1"

from core import swarm_health as SH                 # noqa: E402
from core.aggregator import aggregate               # noqa: E402
from core.agent import AgentEstimate                # noqa: E402
import core.swarm as SW                             # noqa: E402
from core.swarm import Swarm                        # noqa: E402
from core import calibration as CAL                 # noqa: E402
from harness import predict_today as PT             # noqa: E402
from harness import sameday as SD                   # noqa: E402
from harness import wallet as W                     # noqa: E402
from harness import journal                         # noqa: E402

_Q = "Will the incumbent win the 2032 presidential election?"
_MID = "0xswarmdeg01"

_HEALTHY = {"allow_bet": True, "aborted": False, "degraded": False,
            "n_agents_succeeded": 5, "n_agents_requested": 5, "method": "swarm",
            "consensus": 0.8, "consensus_status": SH.CONSENSUS_OK}


def _ests(n, prob=None):
    """n synthetic estimates. If prob is None, spread them (std>0); else all equal."""
    return [AgentEstimate(agent_id=f"a{i}", persona=f"P{i}",
                          probability=(prob if prob is not None else min(0.95, 0.30 + 0.05 * i)),
                          confidence=0.7, reasoning="x", key_factors=["x"], round=1)
            for i in range(n)]


class _FakeAgent:
    def __init__(self, i, prob=0.6, fail=False):
        self.agent_id = f"fake{i}"
        self.persona = f"Fake {i}"
        self._prob = prob
        self._fail = fail

    def estimate(self, question, context, debate_round=1, other_estimates=None):
        if self._fail:
            raise RuntimeError("simulated agent failure")
        return AgentEstimate(agent_id=self.agent_id, persona=self.persona,
                             probability=self._prob, confidence=0.7,
                             reasoning="synthetic", key_factors=["x"], round=debate_round)


def _reset_wallet(starting=1000.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(starting)


# ════════════════════════════════════════════════════════════════════════════
# (1-4) AGGREGATOR — a lone survivor can't be consensus 1.0; small swarms damped
# ════════════════════════════════════════════════════════════════════════════
def test_agg_one_estimate_not_consensus_one():
    r = aggregate(_ests(1))
    assert r["consensus_score"] == 0.0, r          # NOT 1.0
    assert r["consensus_score"] < 1.0


def test_agg_one_estimate_marked_insufficient():
    r = aggregate(_ests(1))
    assert r["consensus_status"] == SH.CONSENSUS_INSUFFICIENT and r["consensus_degraded"] is True


def test_agg_two_estimates_not_max_confidence():
    # two PERFECTLY agreeing estimates would raw-consensus to 1.0; the dampener caps it.
    r = aggregate(_ests(2, prob=0.6))
    assert r["consensus_score"] < 1.0, r
    assert r["consensus_status"] == SH.CONSENSUS_LIMITED


def test_agg_three_estimates_normal_consensus():
    r = aggregate(_ests(3, prob=0.6))              # perfect agreement, 3 agents
    assert r["consensus_score"] == 1.0, r          # full strength at >= MIN_FOR_BET
    assert r["consensus_status"] == SH.CONSENSUS_OK and r["consensus_degraded"] is False


def test_agg_empty_is_safe():
    r = aggregate([])
    assert r["n_agents"] == 0 and r["consensus_score"] == 0.0
    assert r["consensus_status"] == SH.CONSENSUS_INSUFFICIENT


# ════════════════════════════════════════════════════════════════════════════
# swarm_health.assess — the betting policy (covers swarm cases 5-8 at the source)
# ════════════════════════════════════════════════════════════════════════════
def test_assess_policy_counts():
    z = SH.assess(5, 0)
    assert z["aborted"] and not z["allow_bet"] and z["degradation_reason"] == SH.NO_AGENTS_SUCCEEDED
    o = SH.assess(5, 1)
    assert not o["allow_bet"] and o["degraded"] and not o["aborted"]
    assert o["degradation_reason"] == SH.INSUFFICIENT_SURVIVING_AGENTS
    assert not SH.assess(5, 2)["allow_bet"]                       # 2 < 3 floor
    th = SH.assess(5, 3)
    assert th["allow_bet"] and th["degraded"]                    # 3>=3 AND strict majority of 5
    fv = SH.assess(5, 5)
    assert fv["allow_bet"] and not fv["degraded"]                # full healthy run
    assert not SH.assess(6, 3)["allow_bet"]                      # 3/6 is a coin-flip -> no bet
    assert SH.assess(6, 4)["allow_bet"] and SH.assess(6, 4)["degraded"]


# ════════════════════════════════════════════════════════════════════════════
# (5,6,8,9) SWARM.forecast — health metadata on every path (offline, fake agents)
# ════════════════════════════════════════════════════════════════════════════
def test_swarm_zero_agents_aborts():
    res = Swarm(agents=[]).forecast(_Q, market_odds=0.5, market_id="Z0")
    assert res["aborted"] is True and res["allow_bet"] is False
    assert res["n_agents_succeeded"] == 0 and res["degradation_reason"] == SH.NO_AGENTS_SUCCEEDED


def test_swarm_all_failed_aborts():
    with patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=[_FakeAgent(i, fail=True) for i in range(3)]).forecast(_Q, 0.5, "ZF")
    assert res["allow_bet"] is False and res["aborted"] is True
    assert res["method"] == SH.ALL_AGENTS_FAILED_METHOD
    assert len(res["agent_failures"]) >= 1 and "error" in res["agent_failures"][0]


def test_swarm_one_succeeds_is_degraded_no_bet():
    agents = [_FakeAgent(0, fail=True), _FakeAgent(1, fail=True), _FakeAgent(2, prob=0.6)]
    with patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=agents).forecast(_Q, 0.5, "Z1")
    assert res["allow_bet"] is False and res["degraded"] is True and res["aborted"] is False
    assert res["n_agents_succeeded"] == 1 and res["n_agents_requested"] == 3
    assert res["n_agents_failed"] == 2 and len(res["agent_failures"]) == 2
    assert res["consensus_score"] < 1.0          # a lone survivor is NOT max consensus


def test_swarm_three_succeed_allow_bet():
    agents = [_FakeAgent(0, prob=0.55), _FakeAgent(1, prob=0.60), _FakeAgent(2, prob=0.65)]
    with patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=agents).forecast(_Q, 0.5, "Z3")
    assert res["allow_bet"] is True and res["aborted"] is False
    assert res["n_agents_succeeded"] == 3 and res["n_agents_requested"] == 3
    assert 0.0 <= res["consensus_score"] <= 1.0


# ════════════════════════════════════════════════════════════════════════════
# (10-14) predict_today guard _p_swarm_health
# ════════════════════════════════════════════════════════════════════════════
def test_pt_guard_blocks_each_degraded_case():
    cases = [
        ({}, "swarm_missing_health_metadata_no_bet"),
        ({"allow_bet": True}, "swarm_missing_health_metadata_no_bet"),            # no n_agents_succeeded
        (dict(_HEALTHY, method=SH.ALL_AGENTS_FAILED_METHOD), "swarm_fallback_probability_no_bet"),
        (dict(_HEALTHY, aborted=True), "swarm_aborted_no_bet"),
        (dict(_HEALTHY, n_agents_succeeded=2, allow_bet=False), "swarm_insufficient_agents_no_bet"),
        (dict(_HEALTHY, n_agents_succeeded=1, allow_bet=False), "swarm_insufficient_agents_no_bet"),
        (dict(_HEALTHY, allow_bet=False), "swarm_degraded_no_bet"),
        (dict(_HEALTHY, n_agents_succeeded=None), "swarm_missing_health_metadata_no_bet"),
    ]
    for meta, expect in cases:
        ok, reason = PT._p_swarm_health(meta)
        assert ok is False and reason == expect, (meta, ok, reason)


def test_pt_guard_allows_healthy():
    ok, reason = PT._p_swarm_health(_HEALTHY)
    assert ok is True and reason == "ok"


def test_pt_skip_records_no_bet_and_never_opens():
    _reset_wallet(); journal.init_journal()

    def _no_open(*a, **k):
        raise AssertionError("wallet.open_position called on a degraded-swarm skip!")

    reason = "swarm_insufficient_agents_no_bet (agents 1/5, insufficient_surviving_agents)"
    with patched(W, "open_position", _no_open):
        out = PT._skip(_MID, _Q, reason, p=0.5, price=0.5)
    assert out is False
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE market_id=? AND status='no_bet'", (_MID,)).fetchall()
    conn.close()
    assert rows and any("swarm_insufficient_agents_no_bet" in (r["why"] or "") for r in rows)


# ════════════════════════════════════════════════════════════════════════════
# (15-18) sameday — same guard, sameday_swarm_* reasons; _ai_scout 5-tuple contract
# ════════════════════════════════════════════════════════════════════════════
def test_sameday_guard_uses_prefixed_reasons():
    ok, r = PT._p_swarm_health(dict(_HEALTHY, aborted=True), prefix="sameday_swarm")
    assert ok is False and r == "sameday_swarm_aborted_no_bet"
    ok2, r2 = PT._p_swarm_health(dict(_HEALTHY, n_agents_succeeded=1, allow_bet=False), prefix="sameday_swarm")
    assert ok2 is False and r2 == "sameday_swarm_insufficient_agents_no_bet"


def test_sameday_guard_allows_healthy():
    ok, r = PT._p_swarm_health(_HEALTHY, prefix="sameday_swarm")
    assert ok is True


def test_sameday_sd_skip_records_no_bet_and_never_opens():
    _reset_wallet(); journal.init_journal()

    def _no_open(*a, **k):
        raise AssertionError("wallet.open_position called on a degraded-swarm sameday skip!")

    with patched(W, "open_position", _no_open):
        out = SD._sd_skip(_MID, _Q, "sameday_swarm_aborted_no_bet", p=0.5, price=0.5, layer="swarm_health")
    assert out is False
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE market_id=? AND status='no_bet'", (_MID,)).fetchall()
    conn.close()
    assert rows and any("sameday_swarm_aborted_no_bet" in (r["why"] or "") for r in rows)


def test_ai_scout_returns_five_tuple_on_early_skip():
    # The call site unpacks 5 values; prove _ai_scout honors that contract. Force the
    # "already forecast — skip re-scout" early return (no swarm/LLM runs).
    with patched(CAL, "get_open_market_ids", lambda: {"MID5"}):
        out = SD._ai_scout({"market_id": "MID5", "question": _Q}, 0.5)
    assert isinstance(out, tuple) and len(out) == 5 and out == (None, None, None, None, None)


# ════════════════════════════════════════════════════════════════════════════
# (structural) the swarm-health guard precedes EVERY wallet.open_position
# ════════════════════════════════════════════════════════════════════════════
def test_guard_precedes_open_position_in_both_files():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel, gatecall in (("harness/predict_today.py", "sh_ok, sh_reason = _p_swarm_health(meta)"),
                          ("harness/sameday.py", "_sh_ok, _sh_r = _p_sh(health, prefix=\"sameday_swarm\")")):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            text = f.read()
        assert gatecall in text, f"{rel}: swarm-health guard call not found"
        gi = text.index(gatecall)
        opens = [i for i in range(len(text)) if text.startswith("wallet.open_position(", i)]
        assert opens, f"{rel}: no wallet.open_position found"
        assert gi < min(opens), f"{rel}: swarm-health guard is AFTER a wallet.open_position"


# ════════════════════════════════════════════════════════════════════════════
# (19-20) PERSISTENCE — degraded forecast stored as degraded; healthy is not
# ════════════════════════════════════════════════════════════════════════════
def _read_swarm_row(mid):
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM swarm_forecasts WHERE market_id=? ORDER BY id DESC", (mid,)).fetchone()
    conn.close()
    return row


def test_persist_degraded_marked():
    CAL.init_db()
    CAL.save_swarm_forecast(_Q, 0.5, 0.0, market_odds=0.5, market_id="DEG1",
                            degraded=True, n_agents_succeeded=1, n_agents_requested=5,
                            degradation_reason=SH.INSUFFICIENT_SURVIVING_AGENTS)
    row = _read_swarm_row("DEG1")
    assert row["degraded"] == 1 and row["n_agents_succeeded"] == 1 and row["n_agents_requested"] == 5
    assert row["degradation_reason"] == SH.INSUFFICIENT_SURVIVING_AGENTS


def test_persist_healthy_not_degraded():
    CAL.init_db()
    CAL.save_swarm_forecast(_Q, 0.6, 0.9, market_odds=0.5, market_id="HEAL1",
                            degraded=False, n_agents_succeeded=5, n_agents_requested=5)
    row = _read_swarm_row("HEAL1")
    assert row["degraded"] == 0 and row["n_agents_succeeded"] == 5


TESTS = [
    ("agg_one_estimate_not_consensus_one", test_agg_one_estimate_not_consensus_one),
    ("agg_one_estimate_marked_insufficient", test_agg_one_estimate_marked_insufficient),
    ("agg_two_estimates_not_max_confidence", test_agg_two_estimates_not_max_confidence),
    ("agg_three_estimates_normal_consensus", test_agg_three_estimates_normal_consensus),
    ("agg_empty_is_safe", test_agg_empty_is_safe),
    ("assess_policy_counts", test_assess_policy_counts),
    ("swarm_zero_agents_aborts", test_swarm_zero_agents_aborts),
    ("swarm_all_failed_aborts", test_swarm_all_failed_aborts),
    ("swarm_one_succeeds_is_degraded_no_bet", test_swarm_one_succeeds_is_degraded_no_bet),
    ("swarm_three_succeed_allow_bet", test_swarm_three_succeed_allow_bet),
    ("pt_guard_blocks_each_degraded_case", test_pt_guard_blocks_each_degraded_case),
    ("pt_guard_allows_healthy", test_pt_guard_allows_healthy),
    ("pt_skip_records_no_bet_and_never_opens", test_pt_skip_records_no_bet_and_never_opens),
    ("sameday_guard_uses_prefixed_reasons", test_sameday_guard_uses_prefixed_reasons),
    ("sameday_guard_allows_healthy", test_sameday_guard_allows_healthy),
    ("sameday_sd_skip_records_no_bet_and_never_opens", test_sameday_sd_skip_records_no_bet_and_never_opens),
    ("ai_scout_returns_five_tuple_on_early_skip", test_ai_scout_returns_five_tuple_on_early_skip),
    ("guard_precedes_open_position_in_both_files", test_guard_precedes_open_position_in_both_files),
    ("persist_degraded_marked", test_persist_degraded_marked),
    ("persist_healthy_not_degraded", test_persist_healthy_not_degraded),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
