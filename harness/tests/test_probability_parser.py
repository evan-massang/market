"""Plan 7 — STRICT LLM probability parser. Malformed output, years/dates, money/counts,
bare out-of-range numbers, and prose-without-probability-wording can NEVER become a
confident tradable probability, are NEVER clamped, and can NEVER reach a paper bet.

NO network, NO real LLM. Temp DB only. Run: python -m harness.tests.test_probability_parser
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_prob_parser_")
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["DEBATE_ROUNDS"] = "1"

from core.probability_parser import parse_probability_response as P  # noqa: E402
from core import probability_parser as PP                            # noqa: E402
from core.agent import AgentEstimate                                  # noqa: E402
import core.swarm as SW                                               # noqa: E402
from core.swarm import Swarm                                          # noqa: E402
from harness import predict_today as PT                               # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(t, expect, **k):
    r = P(t, source="test", **k)
    assert r["ok"] is True and abs(r["probability"] - expect) < 1e-6, (t, r)


def _bad(t, reason=None, **k):
    r = P(t, source="test", **k)
    assert r["ok"] is False and r["probability"] is None, (t, r)
    if reason:
        assert r["reason"] == reason, (t, r["reason"])


# ════════════════════════════════════════════════════════════════════════════
# (1-6) VALID
# ════════════════════════════════════════════════════════════════════════════
def test_valid_json_decimal():
    _ok('{"probability": 0.65}', 0.65)


def test_valid_p_yes():
    _ok('{"p_yes": 0.65}', 0.65)
    _ok('{"yes_probability": 0.65}', 0.65)


def test_valid_probability_percent_field():
    _ok('{"probability_percent": 65}', 0.65)


def test_valid_percent_unit():
    _ok('{"probability": 65, "unit": "percent"}', 0.65)


def test_valid_prose_percent():
    _ok("probability: 65%", 0.65)


def test_valid_prose_decimal_with_wording():
    _ok("I assign 0.65 probability", 0.65)
    _ok("Market resolves in 2026, probability 62%", 0.62)   # the year is ignored


# ════════════════════════════════════════════════════════════════════════════
# (7-24) INVALID — rejected, never clamped
# ════════════════════════════════════════════════════════════════════════════
def test_bare_number_rejected():
    _bad('{"probability": 65}', PP.OUT_OF_RANGE)     # bare 65, no percent/unit


def test_year_rejected():
    _bad('{"probability": 2026}', PP.OUT_OF_RANGE)
    _bad("June 15, 2026")
    _bad("The market resolves in 2026")


def test_over_one_rejected_not_clamped():
    _bad('{"probability": 1.2}', PP.OUT_OF_RANGE)
    _bad('{"probability": 1.4}', PP.OUT_OF_RANGE)


def test_negative_rejected():
    _bad('{"probability": -0.1}', PP.OUT_OF_RANGE)


def test_nonnumeric_rejected():
    _bad('{"probability": "likely"}')
    _bad('{"probability": "June 2026"}')


def test_malformed_json_rejected():
    _bad("{bad json")
    _bad('{"probability":')


def test_no_probability_wording_rejected():
    _bad("Price is 0.63, volume is 10000")          # no probability wording
    _bad("There is 2026 and 65%")                   # 65% not attached to probability wording


def test_conflicting_rejected():
    _bad("The odds are 0.62 and probability is 0.55", PP.AMBIGUOUS)
    _bad('{"probability": 0.6, "p_yes": 0.7}', PP.AMBIGUOUS)


def test_empty_and_whitespace_rejected():
    _bad("", PP.PARSE_ERROR)
    _bad("   ", PP.PARSE_ERROR)
    _bad('{}', PP.MISSING)


def test_exact_zero_one_rejected():
    _bad('{"probability": 1.0}', PP.OUT_OF_RANGE)
    _bad('{"probability": 0.0}', PP.OUT_OF_RANGE)


def test_nan_inf_rejected():
    _bad('{"probability": "NaN"}')
    _bad('{"probability": "Infinity"}')


def test_percent_out_of_range_not_clamped():
    _bad("probability 120%", PP.OUT_OF_RANGE)        # NOT converted to 1.0
    _bad("probability -5%", PP.OUT_OF_RANGE)


def test_require_json_rejects_prose():
    _bad("probability is 0.6", PP.PARSE_ERROR, require_json=True)
    _ok('{"probability": 0.6}', 0.6, require_json=True)


def test_fullwidth_unicode_digits_rejected():
    # adversarial caveat: fullwidth/Unicode digits must NOT be mis-scaled into an
    # in-range probability (re.ASCII makes \\d match only ASCII 0-9 -> they reject).
    _bad("probability ６５％")     # "probability ６５%"
    _bad("probability ２０２６％")  # "probability ２０２６%"
    _bad("probability ６５")           # "probability ６５"


# ════════════════════════════════════════════════════════════════════════════
# (25-28) integration — parse failure = agent/challenger failure (no fabrication)
# ════════════════════════════════════════════════════════════════════════════
class _FakeAgent:
    def __init__(self, i, prob=None, raises=False):
        self.agent_id, self.persona, self._prob, self._raises = f"a{i}", f"P{i}", prob, raises

    def estimate(self, question, context, debate_round=1, other_estimates=None):
        if self._raises:
            raise ValueError(f"agent {self.agent_id}: llm_probability_out_of_range (parse fail)")
        return AgentEstimate(agent_id=self.agent_id, persona=self.persona, probability=self._prob,
                             confidence=0.7, reasoning="x", key_factors=["x"], round=debate_round)


def test_one_valid_four_parse_fail_is_degraded_no_bet():
    agents = [_FakeAgent(0, prob=0.6)] + [_FakeAgent(i, raises=True) for i in range(1, 5)]
    with patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=agents).forecast("Q?", market_odds=0.5, market_id="Z1")
    assert res["allow_bet"] is False and res["degraded"] is True
    assert res["n_agents_succeeded"] == 1 and len(res["agent_failures"]) == 4


def test_all_parse_fail_aborts_swarm():
    with patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=[_FakeAgent(i, raises=True) for i in range(3)]).forecast("Q?", 0.5, "ZF")
    assert res["aborted"] is True and res["allow_bet"] is False
    assert res["method"] == "degraded_all_agents_failed"


def test_real_parser_failure_aborts_swarm_end_to_end():
    # the REAL Agent.estimate + REAL strict parser: a date reply makes every agent raise.
    import core.agent as A
    from agents.personas import build_swarm
    with patched(A, "_call_llm", lambda *a, **k: "June 15, 2026"), \
         patched(SW, "build_context", lambda q: ""):
        res = Swarm(agents=build_swarm(3)).forecast("Q?", market_odds=0.5, market_id="ZR")
    assert res["allow_bet"] is False and res["aborted"] is True   # no fabricated 0.5/1.0


def test_challenger_date_reply_returns_none_not_half():
    import harness.challenger as C
    with patched(C, "_hosted_configured", lambda: False), \
         patched(C, "_local_raw", lambda system, user, model=None: "June 2026"):
        assert C.single_llm_forecast("q") is None


# ════════════════════════════════════════════════════════════════════════════
# (29-32) parser failure cannot reach wallet; guard blocks; journaled
# ════════════════════════════════════════════════════════════════════════════
def test_parse_failed_swarm_blocks_at_guard():
    # a parse-failed (degraded) swarm meta is blocked by the Plan-2 swarm-health gate,
    # which both predict_today and sameday consult BEFORE sizing/wallet.
    meta = {"allow_bet": False, "aborted": False, "degraded": True, "n_agents_succeeded": 1,
            "n_agents_requested": 5, "method": "swarm", "consensus": 0.0}
    ok, reason = PT._p_swarm_health(meta)
    assert ok is False, (ok, reason)
    ok2, reason2 = PT._p_swarm_health(meta, prefix="sameday_swarm")
    assert ok2 is False, (ok2, reason2)


def test_parser_failure_skip_never_opens_position():
    import sqlite3
    from harness import wallet as W, journal
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(1000.0); journal.init_journal()

    def _no_open(*a, **k):
        raise AssertionError("opened a position on a parser-failure skip!")

    with patched(W, "open_position", _no_open):
        out = PT._skip("M", "Q", PP.SWARM_PARSE_FAILED_NO_BET, p=0.5, price=0.5)
    assert out is False
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE status='no_bet'").fetchall()
    conn.close()
    assert any(PP.SWARM_PARSE_FAILED_NO_BET in (r["why"] or "") for r in rows)


# ════════════════════════════════════════════════════════════════════════════
# (33-34) static enforcement — no first-number fallback / no clamp in tradeable path
# ════════════════════════════════════════════════════════════════════════════
def test_tradeable_paths_use_strict_parser():
    agent_src = open(os.path.join(_REPO, "core", "agent.py"), encoding="utf-8").read()
    chal_src = open(os.path.join(_REPO, "harness", "challenger.py"), encoding="utf-8").read()
    assert 'parse_probability_response(raw, source="agent")' in agent_src
    assert 'parse_probability_response(raw, source="challenger")' in chal_src
    # the tradeable probability no longer comes from the legacy first-number coercer
    assert '_coerce_prob(data.get("probability"))' not in agent_src
    assert "_coerce_prob(bare)" not in agent_src


def test_parser_has_no_clamp():
    src = open(os.path.join(_REPO, "core", "probability_parser.py"), encoding="utf-8").read()
    assert "min(max(" not in src and "max(min(" not in src, "parser must never clamp"


TESTS = [
    ("valid_json_decimal", test_valid_json_decimal),
    ("valid_p_yes", test_valid_p_yes),
    ("valid_probability_percent_field", test_valid_probability_percent_field),
    ("valid_percent_unit", test_valid_percent_unit),
    ("valid_prose_percent", test_valid_prose_percent),
    ("valid_prose_decimal_with_wording", test_valid_prose_decimal_with_wording),
    ("bare_number_rejected", test_bare_number_rejected),
    ("year_rejected", test_year_rejected),
    ("over_one_rejected_not_clamped", test_over_one_rejected_not_clamped),
    ("negative_rejected", test_negative_rejected),
    ("nonnumeric_rejected", test_nonnumeric_rejected),
    ("malformed_json_rejected", test_malformed_json_rejected),
    ("no_probability_wording_rejected", test_no_probability_wording_rejected),
    ("conflicting_rejected", test_conflicting_rejected),
    ("empty_and_whitespace_rejected", test_empty_and_whitespace_rejected),
    ("exact_zero_one_rejected", test_exact_zero_one_rejected),
    ("nan_inf_rejected", test_nan_inf_rejected),
    ("percent_out_of_range_not_clamped", test_percent_out_of_range_not_clamped),
    ("require_json_rejects_prose", test_require_json_rejects_prose),
    ("fullwidth_unicode_digits_rejected", test_fullwidth_unicode_digits_rejected),
    ("one_valid_four_parse_fail_is_degraded_no_bet", test_one_valid_four_parse_fail_is_degraded_no_bet),
    ("all_parse_fail_aborts_swarm", test_all_parse_fail_aborts_swarm),
    ("real_parser_failure_aborts_swarm_end_to_end", test_real_parser_failure_aborts_swarm_end_to_end),
    ("challenger_date_reply_returns_none_not_half", test_challenger_date_reply_returns_none_not_half),
    ("parse_failed_swarm_blocks_at_guard", test_parse_failed_swarm_blocks_at_guard),
    ("parser_failure_skip_never_opens_position", test_parser_failure_skip_never_opens_position),
    ("tradeable_paths_use_strict_parser", test_tradeable_paths_use_strict_parser),
    ("parser_has_no_clamp", test_parser_has_no_clamp),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
