"""C4 — the hash chain (+ .head trust anchor) is tamper-evident.

Emits N events through ``obs.emit`` (producing a genuine prev_hash chain and a
.head sidecar exactly as the daemon does) and verifies the chain is clean. Then,
on INDEPENDENT copies written under fresh run ids, performs four tampers and
asserts ``verify_chain`` rejects each with a sensible first_bad_index / reason:

  (a) tamper a MIDDLE line      -> mismatch detected at the following line,
  (b) DELETE a line             -> link to the deleted line breaks,
  (c) INSERT a line             -> the inserted line's prev_hash mismatches,
  (d) tamper the LAST line      -> invisible to the link chain, caught by the
                                   out-of-band .head sidecar ("last-line tamper").
"""

from harness import obs
from harness.obs.tests._util import temp_obs_env, run_as_main

RUN_ID = "run_c4_genuine"
N = 6
GENESIS = "0" * 64


def _emit_genuine_chain():
    """Emit N chained events under RUN_ID; return (lines, head_str)."""
    with obs.run_ctx(run_id=RUN_ID):
        obs.emit("run.start", config={"i": 0}, seq=0)
        obs.emit("data.fetch", source="gamma", seq=1)
        obs.emit("classify.decision", market_id="m", seq=2)
        obs.emit("forecast.final", forecast_id="f", model_probability=0.5, seq=3)
        obs.emit("score.brier", seq=4)
        obs.emit("run.end", seq=5)

    d = obs.config.events_dir()
    lines = [ln for ln in (d / (RUN_ID + ".jsonl")).read_text(
        encoding="utf-8").split("\n") if ln.strip()]
    head = (d / (RUN_ID + ".head")).read_text(encoding="utf-8").strip()
    return lines, head


def _write_copy(run_id, lines, head):
    """Write a tampered copy (lines + head sidecar) under a fresh run id."""
    d = obs.config.events_dir()
    with open(d / (run_id + ".jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(d / (run_id + ".head"), "w", encoding="utf-8", newline="") as f:
        f.write(head or "")
    return run_id


def test_hash_chain():
    with temp_obs_env(prefix="obs_c4_"):
        lines, head = _emit_genuine_chain()
        assert len(lines) == N, ("expected %d lines, got %d" % (N, len(lines)))

        # genuine chain verifies clean
        v = obs.verify_chain(RUN_ID)
        assert v["ok"] is True and v["first_bad_index"] is None and v["n"] == N, v
        # the genuine head pins the genuine last line
        assert head == obs.line_sha(lines[-1])

        mid = N // 2  # 3 — a true middle line (not first, not last)

        # (a) tamper a middle line: appending a byte changes its sha, so the NEXT
        #     line's prev_hash no longer matches -> caught at mid+1.
        a = list(lines)
        a[mid] = a[mid] + " "
        rid_a = _write_copy("run_c4_mid", a, head)
        va = obs.verify_chain(rid_a)
        assert va["ok"] is False, va
        assert va["first_bad_index"] == mid + 1, va

        # (b) delete a middle line: the successor now links to the wrong
        #     predecessor -> caught at the deleted line's position.
        b = lines[:mid] + lines[mid + 1:]
        rid_b = _write_copy("run_c4_del", b, head)
        vb = obs.verify_chain(rid_b)
        assert vb["ok"] is False, vb
        assert vb["first_bad_index"] == mid, vb
        assert vb["n"] == N - 1, vb

        # (c) insert a foreign line (genesis-prev) in the middle: its prev_hash
        #     mismatches the real predecessor -> caught at the insert position.
        c = lines[:mid] + [lines[0]] + lines[mid:]
        rid_c = _write_copy("run_c4_ins", c, head)
        vc = obs.verify_chain(rid_c)
        assert vc["ok"] is False, vc
        assert vc["first_bad_index"] == mid, vc
        assert vc["n"] == N + 1, vc

        # (d) tamper the LAST line: the link chain can't see it (nothing
        #     downstream commits its hash); the genuine .head catches it.
        d = list(lines)
        d[-1] = d[-1] + " "
        rid_d = _write_copy("run_c4_last", d, head)  # head = genuine last-line sha
        vd = obs.verify_chain(rid_d)
        assert vd["ok"] is False, vd
        assert vd["first_bad_index"] == N - 1, vd
        assert "last-line" in vd["reason"], vd


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C4 test_hash_chain", test_hash_chain)]))
