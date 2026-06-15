"""C8 — evidence.pack freezes a forecast's exact evidence, replayably.

Drives the new ``obs.hooks.on_evidence_pack`` hook through the live wiring under
nested run / market / forecast id contexts (exactly as the daemon would), then
asserts:

  * the hook wrote a real, hash-chained ``evidence.pack`` JSONL line whose chain
    verifies clean (``obs.verify_chain``), carrying the per-source summary, the
    quality score, the source/item counts, the caller integrity ``content_hash``,
    and the ``forecast_id`` / ``market_id`` it belongs to;
  * the FULL ``pack_json`` was stored content-addressed in the blob store and
    ROUND-TRIPS byte-for-byte back from ``obs.blobs.read_blob`` via both the
    emitted ``blob_hash`` and the sha embedded in ``blob_ref`` — and that the
    caller's ``content_hash`` matches a fresh hash of the recovered pack;
  * ``explain(market_id)`` and ``replay(forecast_id)`` BOTH surface the
    evidence.pack event AND join the full evidence text back from the blob (the
    sentinel inside pack_json appears in the rendered trail) — so a human can see
    the exact evidence used at decision time.
"""

import hashlib
import json

from harness import obs
from harness.obs import explain as obs_explain
from harness.obs.tests._util import temp_obs_env, run_as_main

RUN_ID = "run_c8_0001"
MARKET_ID = "mkt_c8"
FORECAST_ID = "fc_c8"
SENTINEL = "EVIDENCE_PACK_C8_SENTINEL_7c1f9e"

# A realistic (but synthetic) evidence bundle: news + facts + microstructure.
_PACK = {
    "sources": [
        {"name": "gdelt", "items": 12, "note": SENTINEL + "_gdelt_headlines"},
        {"name": "wikipedia", "items": 2, "note": SENTINEL + "_background"},
        {"name": "signals", "items": 5, "note": SENTINEL + "_microstructure"},
    ],
    "summary": SENTINEL + "_overall_summary",
}
PACK_JSON = json.dumps(_PACK, sort_keys=True, ensure_ascii=False)
CONTENT_HASH = hashlib.sha256(PACK_JSON.encode("utf-8")).hexdigest()
SOURCES_SUMMARY = {"gdelt": 12, "wikipedia": 2, "signals": 5}
N_SOURCES = 3
TOTAL_ITEMS = 19
EVIDENCE_QUALITY = 0.73


def _evidence_pack_line():
    """Return the parsed evidence.pack JSONL line written under RUN_ID (or None)."""
    path = obs.config.events_dir() / (RUN_ID + ".jsonl")
    for raw in path.read_text(encoding="utf-8").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        if obj.get("event") == "evidence.pack":
            return obj
    return None


def test_evidence_pack():
    with temp_obs_env(prefix="obs_c8_"):
        h = obs.hooks

        with obs.run_ctx(run_id=RUN_ID):
            with obs.market_ctx(market_id=MARKET_ID, question="Will C8 round-trip?"):
                with obs.forecast_ctx(forecast_id=FORECAST_ID,
                                      question="Will C8 round-trip?"):
                    ev = h.on_evidence_pack(
                        forecast_id=FORECAST_ID,
                        market_id=MARKET_ID,
                        content_hash=CONTENT_HASH,
                        n_sources=N_SOURCES,
                        total_items=TOTAL_ITEMS,
                        evidence_quality=EVIDENCE_QUALITY,
                        sources_summary=SOURCES_SUMMARY,
                        pack_json=PACK_JSON,
                    )

        # the hook returned the emitted envelope (not a silent no-op)
        assert ev is not None, "on_evidence_pack returned None (hook no-op'd)"
        assert ev.get("event") == "evidence.pack", ev

        # the chain the hook wrote verifies clean (verifiable chain line)
        v = obs.verify_chain(RUN_ID)
        assert v["ok"] is True, v
        assert v["first_bad_index"] is None, v

        # the persisted line carries the full summary payload + correlation ids
        line = _evidence_pack_line()
        assert line is not None, "no evidence.pack line was written"
        assert line.get("forecast_id") == FORECAST_ID, line
        assert line.get("market_id") == MARKET_ID, line
        assert line.get("content_hash") == CONTENT_HASH, line
        assert line.get("n_sources") == N_SOURCES, line
        assert line.get("total_items") == TOTAL_ITEMS, line
        assert line.get("evidence_quality") == EVIDENCE_QUALITY, line
        assert line.get("sources_summary") == SOURCES_SUMMARY, line
        blob_hash = line.get("blob_hash")
        blob_ref = line.get("blob_ref")
        assert blob_hash, "evidence.pack line is missing blob_hash"
        assert blob_ref, "evidence.pack line is missing blob_ref"
        # blob_ref embeds the same content address as blob_hash
        assert blob_ref == "blobs/" + blob_hash, (blob_ref, blob_hash)

        # the blob ROUND-TRIPS byte-for-byte back to the original pack_json,
        # via both the emitted blob_hash and the sha embedded in blob_ref
        recovered = obs.blobs.read_blob(blob_hash)
        assert recovered == PACK_JSON, "blob did not round-trip to pack_json"
        recovered_via_ref = obs.blobs.read_blob(blob_ref.split("/")[-1])
        assert recovered_via_ref == PACK_JSON, "blob_ref did not round-trip"
        # the caller's integrity content_hash matches a fresh hash of the recovery
        assert hashlib.sha256(recovered.encode("utf-8")).hexdigest() == CONTENT_HASH

        # explain(market_id) / replay(forecast_id) BOTH surface the event AND
        # join the full evidence text back from the blob (sentinel present).
        exp = obs_explain.explain(MARKET_ID)
        rep = obs_explain.replay(FORECAST_ID)
        for name, trail in (("explain", exp), ("replay", rep)):
            assert "evidence.pack" in trail, "%s did not surface evidence.pack" % name
            assert CONTENT_HASH in trail, "%s missing content_hash" % name
            assert SENTINEL in trail, (
                "%s did not join the full evidence pack from the blob" % name)
            assert "EVIDENCE PACK (full text, joined from blob)" in trail, name


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C8 test_evidence_pack", test_evidence_pack)]))
