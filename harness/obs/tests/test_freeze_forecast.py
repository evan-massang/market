"""C3 — a frozen forecast cannot be altered by a later outcome.

Emits ``forecast.final`` (which freezes a row in obs_forecasts with a record_hash
and writes a hash-chained JSONL line), captures the exact JSONL line bytes + the
DB record_hash, then APPENDS ``resolution.observed`` + ``score.brier`` for the
SAME forecast/market. Re-reads and asserts:

  * the forecast.final JSONL line bytes are UNCHANGED (appends never rewrite it),
  * the obs_forecasts.record_hash is UNCHANGED,
  * a raw UPDATE and a raw DELETE on obs_forecasts both RAISE (the append-only
    triggers fire RAISE(ABORT)), and the row survives intact.

Proves that learning the outcome later can never retro-edit the frozen forecast.
"""

import json
import sqlite3

from harness import obs
from harness.obs.tests._util import temp_obs_env, run_as_main

RUN_ID = "run_c3_0001"
MARKET_ID = "mkt_c3"
FORECAST_ID = "fc_c3"


def _forecast_final_line_bytes():
    """Return the exact bytes of the single forecast.final JSONL line."""
    path = obs.config.events_dir() / (RUN_ID + ".jsonl")
    raw = path.read_bytes()
    for chunk in raw.split(b"\n"):
        if not chunk.strip():
            continue
        try:
            if json.loads(chunk.decode("utf-8")).get("event") == "forecast.final":
                return chunk
        except Exception:
            continue
    return None


def _db_record_hash():
    conn = sqlite3.connect(str(obs.config.resolve_db_path()))
    try:
        row = conn.execute(
            "SELECT record_hash, model_probability FROM obs_forecasts "
            "WHERE forecast_id=?", (FORECAST_ID,)).fetchone()
    finally:
        conn.close()
    return row  # (record_hash, model_probability) or None


def test_freeze_forecast():
    with temp_obs_env(prefix="obs_c3_"):
        h = obs.hooks

        # Freeze the forecast (event line + frozen DB row, BEFORE any outcome).
        with obs.run_ctx(run_id=RUN_ID):
            with obs.market_ctx(market_id=MARKET_ID, question="Frozen?"):
                with obs.forecast_ctx(forecast_id=FORECAST_ID, question="Frozen?"):
                    h.on_forecast_final(FORECAST_ID, MARKET_ID, 0.6234, 0.41,
                                        0.2134, 0.82, "lean yes")

                    line_before = _forecast_final_line_bytes()
                    row_before = _db_record_hash()
                    assert line_before is not None, "no forecast.final line written"
                    assert row_before is not None, "forecast was not frozen in DB"
                    rh_before, prob_before = row_before[0], row_before[1]
                    assert rh_before, "record_hash not stored"
                    # The event line's record_hash matches the frozen DB row's.
                    assert json.loads(line_before.decode("utf-8"))["record_hash"] == rh_before

                    # The outcome arrives LATER: append resolution + score for
                    # the same forecast/market.
                    h.on_resolution(MARKET_ID, 1.0, "gamma_uma")
                    h.on_score(FORECAST_ID, MARKET_ID, 0.1421, 0.3481)

        # (a) the forecast.final JSONL line is byte-for-byte unchanged
        line_after = _forecast_final_line_bytes()
        assert line_after == line_before, "forecast.final JSONL line was mutated"

        # (b) the obs_forecasts record_hash (and probability) are unchanged
        row_after = _db_record_hash()
        assert row_after is not None
        assert row_after[0] == rh_before, "record_hash changed after resolution"
        assert row_after[1] == prob_before, "frozen probability changed"

        # (c) a raw UPDATE and a raw DELETE both raise (trigger ABORT) and leave
        #     the row intact.
        for sql, params in (
            ("UPDATE obs_forecasts SET model_probability=0.999 WHERE forecast_id=?",
             (FORECAST_ID,)),
            ("DELETE FROM obs_forecasts WHERE forecast_id=?", (FORECAST_ID,)),
        ):
            conn = sqlite3.connect(str(obs.config.resolve_db_path()))
            raised = None
            try:
                conn.execute(sql, params)
                conn.commit()
            except sqlite3.Error as e:
                raised = e
            finally:
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn.close()
            assert raised is not None, "expected ABORT but %r succeeded" % sql
            assert "append-only" in str(raised), (
                "trigger message missing: %r" % (str(raised),))

        # row still intact and unmodified after the rejected writes
        row_final = _db_record_hash()
        assert row_final is not None, "row vanished despite append-only DELETE guard"
        assert row_final[0] == rh_before and row_final[1] == prob_before


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C3 test_freeze_forecast", test_freeze_forecast)]))
