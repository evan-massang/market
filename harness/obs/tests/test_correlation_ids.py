"""C1 — correlation-id propagation through the live hook wiring.

Drives a synthetic-but-COMPLETE pipeline (run.start ... run.end) through
``obs.hooks`` under nested ``obs.run_ctx`` / ``market_ctx`` / ``forecast_ctx`` /
``agent_ctx`` — exactly the contextvar mechanism the daemon uses — then re-reads
the persisted event JSONL and asserts:

  * EVERY event carries the run_id,
  * every market-scoped event (everything but run.start / run.end) carries the
    market_id,
  * every forecast-scoped event shares ONE forecast_id (and the only forecast_id
    seen anywhere is that one),
  * ``obs.verify_chain(run_id).ok`` is True (the chain the hooks wrote is intact).

A live ``predict_today ... --dry-run`` is too stubbed to exercise the swarm
chain, so this synthetic sequence proves the IDs propagate via contextvars the
same way the real call sites rely on.
"""

import json

from harness import obs
from harness.obs.tests._util import temp_obs_env, run_as_main

RUN_ID = "run_c1_0001"
MARKET_ID = "mkt_c1"
FORECAST_ID = "fc_c1"
TRADE_ID = "trd_c1"

# Events that must carry a forecast_id (forecast-scoped phases of the pipeline).
FORECAST_SCOPED = {
    "forecast.start",
    "llm.call",
    "agent.estimate",
    "debate.round",
    "blend.compute",
    "forecast.final",
    "sizing.decision",
    "score.brier",
}
# Run-only events (no market / forecast scope).
RUN_ONLY = {"run.start", "run.end"}


def _drive_pipeline():
    """Emit one complete pipeline through the hooks under nested id contexts."""
    h = obs.hooks
    with obs.run_ctx(run_id=RUN_ID):
        h.on_run_start({"mode": "paper", "max_markets": 1}, 1000.0)
        with obs.market_ctx(market_id=MARKET_ID, question="Will C1 pass?"):
            h.on_data_fetch("gamma", "/markets", {"limit": 1},
                            '{"raw":"ok"}', 1, 12.0)
            h.on_classify(MARKET_ID, "Will C1 pass?", "opinion",
                          {"liquidity": 1000}, True, "liquid + resolvable")
            with obs.forecast_ctx(forecast_id=FORECAST_ID, question="Will C1 pass?"):
                h.on_forecast_start(FORECAST_ID, MARKET_ID, "Will C1 pass?", 0.41)
                with obs.agent_ctx(agent_id="agent_bull", role="agent",
                                   llm_call_id=obs.mint("llm")):
                    h.on_llm_call("ollama", "qwen2.5:7b",
                                  "You are a forecaster.", "Estimate P(yes).",
                                  "P(yes)=0.62", 40, 10, 800.0, "agent")
                    h.on_agent_estimate("agent_bull", FORECAST_ID, "bull",
                                        0.62, 0.7, "momentum", 1)
                h.on_debate_round(FORECAST_ID, 1,
                                  [{"persona": "bull", "p": 0.62}])
                h.on_blend(FORECAST_ID, "confidence_weighted", 0.5, 0.6234, 0.82)
                h.on_forecast_final(FORECAST_ID, MARKET_ID, 0.6234, 0.41,
                                    0.2134, 0.82, "lean yes")
                h.on_sizing(FORECAST_ID, TRADE_ID, 1000.0, 0.2134, "YES",
                            0.05, 0.25, 0.02, 0.0125, 12.5, 0.6234, 0.41)
                h.on_trade_open(TRADE_ID, MARKET_ID, FORECAST_ID, "YES",
                                12.5, 0.41, 0.001, 0.02)
                h.on_resolution(MARKET_ID, 1.0, "gamma_uma")
                h.on_trade_settle(TRADE_ID, MARKET_ID, 1.0, 30.49, 17.99,
                                  1000.0, 1017.99)
                h.on_score(FORECAST_ID, MARKET_ID, 0.1421, 0.3481)
        h.on_run_end({"markets": 1, "forecasts": 1, "trades": 1})


def _read_events():
    path = obs.config.events_dir() / (RUN_ID + ".jsonl")
    events = []
    for ln in path.read_text(encoding="utf-8").split("\n"):
        ln = ln.strip()
        if ln:
            events.append(json.loads(ln))
    return events


def test_correlation_ids():
    with temp_obs_env(prefix="obs_c1_"):
        _drive_pipeline()
        events = _read_events()

        # Sanity: the full pipeline was actually recorded.
        names = {e["event"] for e in events}
        assert "run.start" in names and "run.end" in names, names
        assert FORECAST_SCOPED.issubset(names), FORECAST_SCOPED - names

        # (1) every event carries the run_id
        for e in events:
            assert e.get("run_id") == RUN_ID, ("run_id", e.get("event"), e.get("run_id"))

        # (2) every market-scoped event carries the market_id
        for e in events:
            if e["event"] in RUN_ONLY:
                continue
            assert e.get("market_id") == MARKET_ID, (
                "market_id", e.get("event"), e.get("market_id"))

        # (3) forecast-scoped events all share ONE forecast_id; and the only
        #     forecast_id seen anywhere is exactly that one.
        for e in events:
            if e["event"] in FORECAST_SCOPED:
                assert e.get("forecast_id") == FORECAST_ID, (
                    "forecast_id", e.get("event"), e.get("forecast_id"))
        distinct_fids = {e.get("forecast_id") for e in events
                         if e.get("forecast_id") is not None}
        assert distinct_fids == {FORECAST_ID}, distinct_fids

        # (4) the hash chain the hooks wrote verifies clean
        v = obs.verify_chain(RUN_ID)
        assert v["ok"] is True, v
        assert v["first_bad_index"] is None, v
        assert v["n"] == len(events), (v, len(events))


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C1 test_correlation_ids", test_correlation_ids)]))
