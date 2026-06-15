"""Unit tests for the obs hash chain — obs.emit + obs.verify_chain.

Uses the obs suite's temp_obs_env (sets OBS_LOGS_DIR + OBS_ENABLED at runtime;
obs.config re-reads env on every call, so all writes land in a throwaway dir —
NO live logs/db touched, NO network). Verifies a genuine chain, four tamper
classes, and the OBS_ENABLED=0 kill switch.
Run:  python -m harness.tests.test_obs_chain
"""
from __future__ import annotations

import os
import sys

from harness import obs
from harness.obs.tests._util import temp_obs_env
from harness.tests._util import run_as_main

RUN_ID = "run_obs_chain"
N = 6


def _emit_genuine_chain():
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
    d = obs.config.events_dir()
    with open(d / (run_id + ".jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(d / (run_id + ".head"), "w", encoding="utf-8", newline="") as f:
        f.write(head or "")
    return run_id


def test_genuine_chain_verifies():
    with temp_obs_env(prefix="obs_chain_"):
        lines, head = _emit_genuine_chain()
        assert len(lines) == N, len(lines)
        v = obs.verify_chain(RUN_ID)
        assert v["ok"] is True and v["first_bad_index"] is None and v["n"] == N, v
        assert head == obs.line_sha(lines[-1])


def test_middle_tamper_detected():
    with temp_obs_env(prefix="obs_chain_"):
        lines, head = _emit_genuine_chain()
        mid = N // 2
        a = list(lines)
        a[mid] = a[mid] + " "
        v = obs.verify_chain(_write_copy("run_mid", a, head))
        assert v["ok"] is False and v["first_bad_index"] == mid + 1, v


def test_delete_detected():
    with temp_obs_env(prefix="obs_chain_"):
        lines, head = _emit_genuine_chain()
        mid = N // 2
        b = lines[:mid] + lines[mid + 1:]
        v = obs.verify_chain(_write_copy("run_del", b, head))
        assert v["ok"] is False and v["first_bad_index"] == mid and v["n"] == N - 1, v


def test_insert_detected():
    with temp_obs_env(prefix="obs_chain_"):
        lines, head = _emit_genuine_chain()
        mid = N // 2
        c = lines[:mid] + [lines[0]] + lines[mid:]
        v = obs.verify_chain(_write_copy("run_ins", c, head))
        assert v["ok"] is False and v["first_bad_index"] == mid and v["n"] == N + 1, v


def test_last_line_tamper_caught_by_head():
    with temp_obs_env(prefix="obs_chain_"):
        lines, head = _emit_genuine_chain()
        d = list(lines)
        d[-1] = d[-1] + " "
        v = obs.verify_chain(_write_copy("run_last", d, head))
        assert v["ok"] is False and v["first_bad_index"] == N - 1, v
        assert "last-line" in v["reason"], v


def test_kill_switch_emits_nothing():
    with temp_obs_env(prefix="obs_chain_"):
        os.environ["OBS_ENABLED"] = "0"
        try:
            with obs.run_ctx(run_id="run_off"):
                ev = obs.emit("run.start", config={}, seq=0)
            assert ev is None, ev
            path = obs.config.events_dir() / "run_off.jsonl"
            assert not path.exists(), "kill switch must write nothing"
        finally:
            os.environ["OBS_ENABLED"] = "1"


TESTS = [
    ("genuine_chain_verifies", test_genuine_chain_verifies),
    ("middle_tamper_detected", test_middle_tamper_detected),
    ("delete_detected", test_delete_detected),
    ("insert_detected", test_insert_detected),
    ("last_line_tamper_caught_by_head", test_last_line_tamper_caught_by_head),
    ("kill_switch_emits_nothing", test_kill_switch_emits_nothing),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
