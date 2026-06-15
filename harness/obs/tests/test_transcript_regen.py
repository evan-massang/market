"""C7 — transcript regeneration is deterministic and derived from the events.

Seeds a fixed event JSONL (emitted through the live hooks under nested id
contexts), then calls ``obs.transcript.build(run_id)`` TWICE and asserts:

  * the two rendered ``.md`` files are BYTE-IDENTICAL (the renderer has no
    generation timestamp / RNG / locale dependence — same log => same bytes),
  * the transcript content is DERIVED FROM the events, spot-checked by the
    forecast.final probability appearing in the rendered narrative (0.6234 ->
    "62.3%") together with the run id and the forecast.final marker.
"""

from harness import obs
from harness.obs import transcript as obs_transcript
from harness.obs.tests._util import temp_obs_env, run_as_main

RUN_ID = "run_c7_0001"
MARKET_ID = "mkt_c7"
FORECAST_ID = "fc_c7"
MODEL_PROB = 0.6234  # renders to "62.3%" via the transcript percentage formatter


def _seed_fixed_log():
    h = obs.hooks
    with obs.run_ctx(run_id=RUN_ID):
        h.on_run_start({"mode": "paper", "max_markets": 1}, 1000.0)
        with obs.market_ctx(market_id=MARKET_ID, question="Will C7 render the same twice?"):
            h.on_data_fetch("gamma", "/markets", {"limit": 1}, '{"raw":"ok"}', 1, 12.0)
            h.on_classify(MARKET_ID, "Will C7 render the same twice?", "opinion",
                          {"liquidity": 1000}, True, "liquid + resolvable")
            with obs.forecast_ctx(forecast_id=FORECAST_ID,
                                  question="Will C7 render the same twice?"):
                h.on_forecast_start(FORECAST_ID, MARKET_ID,
                                    "Will C7 render the same twice?", 0.41)
                with obs.agent_ctx(agent_id="agent_bull", role="agent",
                                   llm_call_id=obs.mint("llm")):
                    h.on_llm_call("ollama", "qwen2.5:7b", "You are a forecaster.",
                                  "Estimate P(yes).", "P(yes)=0.62", 40, 10, 800.0,
                                  "agent")
                    h.on_agent_estimate("agent_bull", FORECAST_ID, "bull",
                                        0.62, 0.7, "momentum", 1)
                h.on_blend(FORECAST_ID, "confidence_weighted", 0.5, MODEL_PROB, 0.82)
                h.on_forecast_final(FORECAST_ID, MARKET_ID, MODEL_PROB, 0.41,
                                    0.2134, 0.82, "lean yes")
                h.on_sizing(FORECAST_ID, "trd_c7", 1000.0, 0.2134, "YES",
                            0.05, 0.25, 0.02, 0.0125, 12.5, MODEL_PROB, 0.41)
                h.on_trade_open("trd_c7", MARKET_ID, FORECAST_ID, "YES",
                                12.5, 0.41, 0.001, 0.02)
                h.on_resolution(MARKET_ID, 1.0, "gamma_uma")
                h.on_trade_settle("trd_c7", MARKET_ID, 1.0, 30.49, 17.99,
                                  1000.0, 1017.99)
                h.on_score(FORECAST_ID, MARKET_ID, 0.1421, 0.3481)
        h.on_run_end({"markets": 1, "forecasts": 1, "trades": 1})


def test_transcript_regen():
    with temp_obs_env(prefix="obs_c7_"):
        _seed_fixed_log()

        p1 = obs_transcript.build(RUN_ID)
        b1 = p1.read_bytes()
        p2 = obs_transcript.build(RUN_ID)
        b2 = p2.read_bytes()

        # deterministic: same event log => byte-identical transcript
        assert str(p1) == str(p2), (p1, p2)
        assert b1 == b2, "two builds of the same log produced different bytes"
        assert b1, "transcript was empty"

        # derived from the events: spot-check the forecast.final probability,
        # the run id, and the forecast.final marker all appear.
        text = b1.decode("utf-8")
        assert RUN_ID in text, "run id missing from transcript"
        assert "forecast.final" in text, "forecast.final not rendered"
        assert "62.3%" in text, "forecast.final probability not derived into transcript"


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C7 test_transcript_regen", test_transcript_regen)]))
