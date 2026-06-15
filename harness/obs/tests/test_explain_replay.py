"""C2 — explain(market_id) / replay(forecast_id) reconstruct the decision trail.

Seeds a COMPLETE trail for one market + forecast through ``obs.hooks`` (across
two run files, the resolution arriving in a later run), including a real prompt
BLOB referenced by an ``llm.call`` line. Then asserts that:

  * ``explain(market_id)`` reconstructs the trail — the distinctive
    forecast.final probability, the sizing stake, and the FULL prompt text pulled
    back from the content-addressed blob are all present, joined across BOTH run
    files in causal order;
  * ``replay(forecast_id)`` reconstructs the same trail scoped to the one
    forecast (forecast.final probs + sizing stake + blob prompt text present).
"""

from harness import obs
from harness.obs import explain as obs_explain
from harness.obs.tests._util import temp_obs_env, run_as_main

SENTINEL = "SENTINEL_PROMPT_C2_UNIQUE_8f3a2b"
MODEL_PROB = 0.6234   # distinctive forecast.final probability
MARKET_PROB = 0.4100
STAKE = 12.5          # distinctive sizing stake
MODEL_BRIER = 0.1421
MARKET_BRIER = 0.3481


def _seed_trail():
    """Emit a full market+forecast trail across two runs; returns the ids used."""
    h = obs.hooks
    market_id = obs.mint("mkt")
    forecast_id = obs.mint("fc")
    trade_id = obs.mint("trd")
    run_a = obs.mint("run")
    run_b = obs.mint("run")

    # run A: data -> classify -> forecast pipeline -> sizing -> paper fill
    with obs.run_ctx(run_id=run_a):
        with obs.market_ctx(market_id=market_id, question="Will X happen by 2026?"):
            h.on_data_fetch("gamma", "/markets", {"id": market_id},
                            '{"raw":"ok"}', 1, 12.3)
            h.on_classify(market_id, "Will X happen by 2026?", "opinion",
                          {"liquidity": 1000}, True, "liquid + resolvable")
            with obs.forecast_ctx(forecast_id=forecast_id,
                                  question="Will X happen by 2026?"):
                h.on_forecast_start(forecast_id, market_id,
                                    "Will X happen by 2026?", MARKET_PROB)
                with obs.agent_ctx(agent_id="agent_bull", role="agent",
                                   llm_call_id=obs.mint("llm")):
                    h.on_llm_call("ollama", "qwen2.5:7b",
                                  "You are a careful forecaster.",
                                  SENTINEL + " Estimate P(yes) for: Will X happen?",
                                  "P(yes)=0.62 because ...", 42, 11, 850.0, "agent")
                    h.on_agent_estimate("agent_bull", forecast_id, "bull",
                                        0.62, 0.7, "momentum favours yes", 1)
                h.on_debate_round(forecast_id, 1, [{"persona": "bull", "p": 0.62}])
                h.on_blend(forecast_id, "confidence_weighted", 0.5,
                           MODEL_PROB, 0.82)
                h.on_forecast_final(forecast_id, market_id, MODEL_PROB,
                                    MARKET_PROB, MODEL_PROB - MARKET_PROB, 0.82,
                                    "bull/bear blend favours yes")
                h.on_sizing(forecast_id, trade_id, 1000.0,
                            MODEL_PROB - MARKET_PROB, "YES", 0.05, 0.25, 0.02,
                            0.0125, STAKE, MODEL_PROB, MARKET_PROB)
                h.on_trade_open(trade_id, market_id, forecast_id, "YES",
                                STAKE, MARKET_PROB, 0.001, 0.02)

    # run B: resolution + settlement + scoring happen later, in a different run
    with obs.run_ctx(run_id=run_b):
        with obs.market_ctx(market_id=market_id):
            with obs.forecast_ctx(forecast_id=forecast_id):
                h.on_resolution(market_id, 1.0, "gamma_uma")
            h.on_trade_settle(trade_id, market_id, 1.0, 30.49, 17.99,
                              1000.0, 1017.99)
            h.on_score(forecast_id, market_id, MODEL_BRIER, MARKET_BRIER)

    return market_id, forecast_id, trade_id, run_a, run_b


def test_explain_replay():
    with temp_obs_env(prefix="obs_c2_"):
        market_id, forecast_id, trade_id, run_a, run_b = _seed_trail()

        exp = obs_explain.explain(market_id)
        rep = obs_explain.replay(forecast_id)

        # explain(market_id): full trail reconstructed
        assert str(MODEL_PROB) in exp, "forecast.final prob missing from explain"
        assert ("stake = %s" % STAKE) in exp, "sizing stake missing from explain"
        assert SENTINEL in exp, "blob prompt text not joined into explain"
        assert run_a in exp and run_b in exp, "explain did not join both run files"
        assert market_id in exp and forecast_id in exp and trade_id in exp
        assert "forecast.final" in exp and "trade.settle" in exp
        assert str(MODEL_BRIER) in exp and str(MARKET_BRIER) in exp
        # causal ordering: data.fetch precedes forecast.final precedes score.brier
        assert exp.find("data.fetch") < exp.find("forecast.final") < exp.find("score.brier")

        # replay(forecast_id): same trail, scoped to the one forecast
        assert str(MODEL_PROB) in rep, "forecast.final prob missing from replay"
        assert ("stake = %s" % STAKE) in rep, "sizing stake missing from replay"
        assert SENTINEL in rep, "blob prompt text not joined into replay"
        assert "blend.compute" in rep and "debate.round" in rep
        assert "trade.open" in rep and "trade.settle" in rep
        assert str(MODEL_BRIER) in rep and str(MARKET_BRIER) in rep


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C2 test_explain_replay", test_explain_replay)]))
