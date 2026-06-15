"""Swarm size-handling tests at sizes 1, 2, 3, 5, 12 — validates the herding fix.

By default this runs NO LLM and makes NO network call: it validates the
size-handling logic (build_swarm sizing + the aggregation / game-theory pipeline
on synthetic estimates), including the herding short-circuit at len<3 that the
fix relies on. A full Swarm.forecast is an INTEGRATION test that needs a live
Ollama; it self-skips unless POLYSWARM_RUN_LLM=1 is set (the runner leaves it
unset, so it is skipped).
Run:  python -m harness.tests.test_swarm_sizes
"""
from __future__ import annotations

import os
import sys

from harness.tests._util import make_temp_env, run_as_main

make_temp_env("ps_swarm_")
os.environ["LLM_PROVIDER"] = "ollama"   # lazy httpx client; NO network on Agent() init

from agents.personas import build_swarm, PERSONA_DEFINITIONS   # noqa: E402
from core.agent import AgentEstimate                            # noqa: E402
from core.aggregator import aggregate                           # noqa: E402
from core.game_theory import (                                  # noqa: E402
    detect_herding, nash_equilibrium_check, scoring_rule_analysis,
)

SIZES = [1, 2, 3, 5, 12]


def _ests(n):
    """n synthetic AgentEstimates with a spread of probabilities (std > 0)."""
    return [
        AgentEstimate(
            agent_id=f"a{i}", persona=f"Persona {i}",
            probability=min(0.95, 0.30 + 0.05 * i),
            confidence=0.60 + 0.01 * i,
            reasoning="synthetic", key_factors=["x"], round=1,
        )
        for i in range(n)
    ]


def test_build_swarm_sizes():
    for n in SIZES:
        agents = build_swarm(n)
        assert len(agents) == n, (n, len(agents))
    assert len(build_swarm(None)) == len(PERSONA_DEFINITIONS) == 12


def test_aggregate_handles_all_sizes():
    for n in SIZES:
        r = aggregate(_ests(n))
        assert 0.0 <= r["probability"] <= 1.0, (n, r["probability"])
        assert r["n_agents"] == n, (n, r["n_agents"])
        assert 0.0 <= r["consensus_score"] <= 1.0, (n, r)


def test_herding_fix_short_circuit_and_compute():
    # The fix: detect_herding short-circuits at len<3 (no HHI hallucination),
    # and computes a real score at len>=3 — at every size, never crashes.
    for n in (1, 2):
        h = detect_herding(_ests(n))
        assert h["herding_detected"] is False, (n, h)
        assert h.get("hhi") == 0, (n, h)
    for n in (3, 5, 12):
        h = detect_herding(_ests(n))
        assert "herding_score" in h, (n, h)
        assert isinstance(h["herding_detected"], bool), (n, h)


def test_game_theory_pipeline_all_sizes():
    # Each size must complete-or-clean-fail through the diagnostics the swarm runs.
    for n in SIZES:
        ests = _ests(n)
        nash = nash_equilibrium_check(ests)
        assert "stable" in nash and 0.0 <= nash["stability_score"] <= 1.0, (n, nash)
        scoring = scoring_rule_analysis(ests)
        assert scoring["n_strategic"] >= 0 and 0.0 <= scoring["mean_truthfulness"] <= 1.0, (n, scoring)


def test_swarm_forecast_integration():
    if os.getenv("POLYSWARM_RUN_LLM") != "1":
        print("    (skipped: integration — set POLYSWARM_RUN_LLM=1 with Ollama live)")
        return
    from core.swarm import Swarm
    for n in SIZES:
        swarm = Swarm(agents=build_swarm(n))
        result = swarm.forecast("Will this test market resolve YES?", market_odds=0.5,
                                market_id=f"INT-{n}")
        assert 0.0 <= float(result["probability"]) <= 1.0, (n, result.get("probability"))


TESTS = [
    ("build_swarm_sizes", test_build_swarm_sizes),
    ("aggregate_handles_all_sizes", test_aggregate_handles_all_sizes),
    ("herding_fix_short_circuit_and_compute", test_herding_fix_short_circuit_and_compute),
    ("game_theory_pipeline_all_sizes", test_game_theory_pipeline_all_sizes),
    ("swarm_forecast_integration", test_swarm_forecast_integration),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
