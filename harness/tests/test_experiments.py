"""B4 — no-network unit tests for harness.experiments (parameter-experiment registry).

Temp DB only (make_temp_env). No network, no LLM. Verifies:
  * lazy 'baseline' creation: a fresh DB yields an active baseline whose params
    ARE the current live defaults (default == current behavior, no auto-switch)
  * register_experiment / set_active: one-active invariant, manual promotion only
  * params JSON round-trip (nested dict survives store -> read)
  * record_experiment_outcome de-dupe on (exp_key, market_id)
  * experiment_leaderboard ordering (by skill) + min_n filter
  * every public function is best-effort and never raises (even on a broken DB)

Run:  python -m harness.tests.test_experiments
"""
from __future__ import annotations

import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_experiments_")

from harness import experiments as EX                # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────
def _reset():
    """Drop both tables so each test starts clean (temp DB is shared in-process)."""
    import os, sqlite3
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    conn.execute(f"DROP TABLE IF EXISTS {EX._EXP_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {EX._OUT_TABLE}")
    conn.commit()
    conn.close()


def _fill(exp_key, n, model_brier, market_brier, pnl, start=0):
    """Record ``n`` distinct (deduped) outcomes for ``exp_key``."""
    inserted = 0
    for i in range(start, start + n):
        if EX.record_experiment_outcome(exp_key, f"{exp_key}-M{i}",
                                        model_brier, market_brier, pnl):
            inserted += 1
    return inserted


# ── tests ─────────────────────────────────────────────────────────────────────
def test_lazy_baseline_is_current_defaults_and_active():
    _reset()
    act = EX.active_experiment()
    assert act["exp_key"] == "baseline", act
    # params == the live defaults pulled from sizing + wallet (no-op tag).
    defaults = EX._current_defaults()
    assert act["params"] == defaults, (act["params"], defaults)
    # the floor / cap constants are the real ones (never looser than current).
    assert act["params"]["min_edge"] == 0.02, act
    assert act["params"]["cap"] == 0.02, act
    assert act["params"]["lambda"] == 0.25, act
    # baseline is recorded as active in the table.
    got = EX.get_experiment("baseline")
    assert got is not None and got["active"] is True, got
    # idempotent: a second call returns the same active baseline, still one active.
    act2 = EX.active_experiment()
    assert act2["exp_key"] == "baseline", act2


def test_register_does_not_auto_switch_active():
    _reset()
    # lazily create baseline + make it active
    assert EX.active_experiment()["exp_key"] == "baseline"
    # registering a candidate with active=False MUST NOT change the active one.
    assert EX.register_experiment("aggressive", {"min_edge": 0.05, "cap": 0.02}) is True
    assert EX.active_experiment()["exp_key"] == "baseline", "register must not auto-switch"
    cand = EX.get_experiment("aggressive")
    assert cand is not None and cand["active"] is False, cand


def test_set_active_promotes_and_keeps_single_active():
    _reset()
    EX.active_experiment()  # seed baseline (active)
    assert EX.register_experiment("expA", {"min_edge": 0.03}) is True
    assert EX.register_experiment("expB", {"min_edge": 0.04}) is True
    # promote expA — manual switch
    assert EX.set_active("expA") is True
    assert EX.active_experiment()["exp_key"] == "expA"
    # exactly one active row
    assert EX.get_experiment("expA")["active"] is True
    assert EX.get_experiment("expB")["active"] is False
    assert EX.get_experiment("baseline")["active"] is False
    # promote expB — baseline + expA flip off
    assert EX.set_active("expB") is True
    assert EX.active_experiment()["exp_key"] == "expB"
    assert EX.get_experiment("expA")["active"] is False
    # set_active on a missing key fails and changes nothing
    assert EX.set_active("nope") is False
    assert EX.active_experiment()["exp_key"] == "expB"


def test_register_active_true_promotes():
    _reset()
    EX.active_experiment()  # baseline active
    assert EX.register_experiment("promoted", {"min_edge": 0.03}, active=True) is True
    assert EX.active_experiment()["exp_key"] == "promoted"
    assert EX.get_experiment("baseline")["active"] is False


def test_params_json_round_trip():
    _reset()
    params = {"min_edge": 0.03, "cap": 0.02, "lambda": 0.25,
              "nested": {"a": [1, 2, 3], "b": True, "c": None}, "tag": "v2"}
    assert EX.register_experiment("rt", params) is True
    got = EX.get_experiment("rt")
    assert got is not None
    assert got["params"] == params, got["params"]
    # re-registering refreshes params (upsert), still not active.
    params2 = {"min_edge": 0.06}
    assert EX.register_experiment("rt", params2) is True
    assert EX.get_experiment("rt")["params"] == params2
    assert EX.get_experiment("rt")["active"] is False


def test_outcome_dedupe_on_exp_key_and_market():
    _reset()
    assert EX.record_experiment_outcome("e1", "MKT-1", 0.1, 0.2, 3.0) is True
    # same (exp_key, market_id) -> skipped
    assert EX.record_experiment_outcome("e1", "MKT-1", 0.1, 0.2, 3.0) is False
    # same market_id under a DIFFERENT exp_key -> allowed (different key)
    assert EX.record_experiment_outcome("e2", "MKT-1", 0.1, 0.2, 3.0) is True
    # different market under same exp -> allowed
    assert EX.record_experiment_outcome("e1", "MKT-2", 0.1, 0.2, 3.0) is True
    lb = EX.experiment_leaderboard(min_n=1)
    by_key = {d["exp_key"]: d for d in lb}
    assert by_key["e1"]["n"] == 2, by_key["e1"]   # MKT-1 + MKT-2, dup dropped
    assert by_key["e2"]["n"] == 1, by_key["e2"]


def test_leaderboard_min_n_filter_and_ordering():
    _reset()
    # SKILLED: model brier 0.05 well below market brier 0.25 -> skill +0.20
    _fill("skilled", 12, model_brier=0.05, market_brier=0.25, pnl=4.0)
    # MEH: model brier 0.20 vs market 0.25 -> skill +0.05
    _fill("meh", 12, model_brier=0.20, market_brier=0.25, pnl=1.0)
    # BAD: model brier 0.30 worse than market 0.25 -> skill -0.05
    _fill("bad", 12, model_brier=0.30, market_brier=0.25, pnl=-2.0)
    # THIN: only 3 rows -> below min_n, excluded
    _fill("thin", 3, model_brier=0.01, market_brier=0.25, pnl=9.0)

    lb = EX.experiment_leaderboard(min_n=10)
    keys = [d["exp_key"] for d in lb]
    assert "thin" not in keys, keys           # min_n filter
    assert keys == ["skilled", "meh", "bad"], keys  # sorted by skill desc

    top = lb[0]
    assert top["exp_key"] == "skilled"
    assert top["n"] == 12, top
    assert abs(top["mean_model_brier"] - 0.05) < 1e-9, top
    assert abs(top["mean_market_brier"] - 0.25) < 1e-9, top
    assert abs(top["total_pnl"] - 48.0) < 1e-9, top

    # lowering the bar surfaces the thin experiment (and it ranks top on skill).
    lb_all = EX.experiment_leaderboard(min_n=1)
    assert "thin" in [d["exp_key"] for d in lb_all]
    assert lb_all[0]["exp_key"] == "thin"


def test_leaderboard_empty_and_unknown_safe():
    _reset()
    assert EX.experiment_leaderboard(min_n=10) == []
    assert EX.get_experiment("never-registered") is None
    assert EX.get_experiment("") is None
    # empty / falsy keys are rejected, not raised
    assert EX.register_experiment("", {"x": 1}) is False
    assert EX.set_active("") is False
    assert EX.record_experiment_outcome("", "M", 0.1, 0.2, 1.0) is False


def test_never_raises_on_unserializable_params():
    _reset()
    # a set is not JSON-serializable -> register returns False, does not raise.
    assert EX.register_experiment("weird", {"bad": {1, 2, 3}}) is False
    assert EX.get_experiment("weird") is None


def test_outcome_handles_none_briers():
    _reset()
    # missing briers still record; leaderboard reports None means + summed pnl.
    assert EX.record_experiment_outcome("np", "M1", None, None, 2.0) is True
    assert EX.record_experiment_outcome("np", "M2", None, None, 3.0) is True
    d = EX.experiment_leaderboard(min_n=1)[0]
    assert d["exp_key"] == "np", d
    assert d["mean_model_brier"] is None and d["mean_market_brier"] is None, d
    assert abs(d["total_pnl"] - 5.0) < 1e-9, d


TESTS = [
    ("lazy_baseline_is_current_defaults_and_active", test_lazy_baseline_is_current_defaults_and_active),
    ("register_does_not_auto_switch_active", test_register_does_not_auto_switch_active),
    ("set_active_promotes_and_keeps_single_active", test_set_active_promotes_and_keeps_single_active),
    ("register_active_true_promotes", test_register_active_true_promotes),
    ("params_json_round_trip", test_params_json_round_trip),
    ("outcome_dedupe_on_exp_key_and_market", test_outcome_dedupe_on_exp_key_and_market),
    ("leaderboard_min_n_filter_and_ordering", test_leaderboard_min_n_filter_and_ordering),
    ("leaderboard_empty_and_unknown_safe", test_leaderboard_empty_and_unknown_safe),
    ("never_raises_on_unserializable_params", test_never_raises_on_unserializable_params),
    ("outcome_handles_none_briers", test_outcome_handles_none_briers),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
