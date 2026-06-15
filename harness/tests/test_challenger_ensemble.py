"""Unit tests for harness.challenger B3 multi-challenger ensemble.

NO-NETWORK / NO-LLM: single_llm_forecast is MONKEYPATCHED (via _util.patched) to
return fixed values per model. We NEVER construct an LLM client or hit a network.

Proves:
  * single-model DEFAULT roster -> mean == that one forecast & n == 1
    (the cold-start numeric-identity invariant: default ensemble == today's bp).
  * multi-model -> mean == arithmetic average of the per-model probabilities.
  * a None-returning model is SKIPPED (n drops, mean over the survivors).
  * a model that RAISES is SKIPPED best-effort (never propagates).
  * all-None -> mean is None & n == 0.
  * CHALLENGER_MODELS env is parsed (comma-split, trimmed, blanks dropped) and
    drives the default roster of ensemble_forecast(models=None).

Run:  python -m harness.tests.test_challenger_ensemble
"""
from __future__ import annotations

import os
import sys

from harness.tests._util import make_temp_env, patched, run_as_main

# Isolate DB + obs logs to a throwaway dir BEFORE importing challenger (it binds
# DB_PATH at import). Pin MODEL_FAST so the default roster is deterministic and
# HERMETIC (no core.agent import needed to resolve the single default model).
make_temp_env("ps_challenger_ensemble_")
os.environ["MODEL_FAST"] = "model-default"
# Guarantee no hosted/multi-model env leaks in from the outer shell.
for _k in ("CHALLENGER_MODELS", "CHALLENGER_API_KEY", "CHALLENGER_BASE_URL", "CHALLENGER_MODEL"):
    os.environ.pop(_k, None)

from harness import challenger  # noqa: E402

EPS = 1e-9


def _stub(mapping, default=None):
    """Build a fake single_llm_forecast that returns mapping[model] (else default).

    Signature mirrors the real one so ensemble_forecast can call it transparently.
    """
    def fake(question, market_odds=None, extra_context="", model=None):
        return mapping.get(model, default)
    return fake


def _set_env(**kv):
    """Set/clear env vars; return a restore() to undo. Plain os.environ, no LLM."""
    sentinel = object()
    saved = {k: os.environ.get(k, sentinel) for k in kv}

    def restore():
        for k, old in saved.items():
            if old is sentinel:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return restore


# ── challenger_models() roster resolution ────────────────────────────────────-

def test_default_roster_single_model():
    restore = _set_env(CHALLENGER_MODELS=None, MODEL_FAST="model-default")
    try:
        roster = challenger.challenger_models()
        assert roster == ["model-default"], roster   # EXACTLY one element today
    finally:
        restore()


def test_challenger_models_env_parsed():
    restore = _set_env(CHALLENGER_MODELS="m1, m2 ,m3,")  # spaces + trailing blank
    try:
        roster = challenger.challenger_models()
        assert roster == ["m1", "m2", "m3"], roster     # trimmed, blanks dropped
    finally:
        restore()


# ── ensemble_forecast() behaviour ────────────────────────────────────────────-

def test_single_model_default_numeric_identity():
    """Default (one model) -> mean == that single forecast, n == 1. Cold-start
    invariant: identical to today's bp = single_llm_forecast(model=default)."""
    restore = _set_env(CHALLENGER_MODELS=None, MODEL_FAST="model-default")
    try:
        fake = _stub({"model-default": 0.62})
        with patched(challenger, "single_llm_forecast", fake):
            single = challenger.single_llm_forecast("Q?", model="model-default")
            res = challenger.ensemble_forecast("Q?")
        assert res["n"] == 1, res
        assert res["mean"] == 0.62, res                 # EXACT (n==1, no fp drift)
        assert res["mean"] == single, res               # == today's single bp
        assert res["probs"] == [0.62], res
        assert res["models"] == ["model-default"], res
        assert res["per_model"] == {"model-default": 0.62}, res
    finally:
        restore()


def test_multi_model_mean_is_average():
    models = ["a", "b", "c"]
    fake = _stub({"a": 0.4, "b": 0.6, "c": 0.8})
    with patched(challenger, "single_llm_forecast", fake):
        res = challenger.ensemble_forecast("Q?", models=models)
    assert res["n"] == 3, res
    assert abs(res["mean"] - 0.6) < EPS, res            # (0.4+0.6+0.8)/3
    assert res["probs"] == [0.4, 0.6, 0.8], res
    assert res["models"] == ["a", "b", "c"], res
    assert res["per_model"] == {"a": 0.4, "b": 0.6, "c": 0.8}, res


def test_none_model_is_skipped():
    models = ["a", "b", "c"]
    fake = _stub({"a": 0.4, "c": 0.8})                  # "b" -> None (skipped)
    with patched(challenger, "single_llm_forecast", fake):
        res = challenger.ensemble_forecast("Q?", models=models)
    assert res["n"] == 2, res                           # n drops from 3 -> 2
    assert abs(res["mean"] - 0.6) < EPS, res            # mean over survivors only
    assert res["probs"] == [0.4, 0.8], res
    assert "b" not in res["per_model"], res
    assert res["per_model"] == {"a": 0.4, "c": 0.8}, res
    assert res["models"] == ["a", "b", "c"], res        # roster records the attempt


def test_raising_model_is_skipped_best_effort():
    models = ["a", "boom", "c"]

    def fake(question, market_odds=None, extra_context="", model=None):
        if model == "boom":
            raise RuntimeError("simulated provider error")
        return {"a": 0.4, "c": 0.8}[model]

    with patched(challenger, "single_llm_forecast", fake):
        res = challenger.ensemble_forecast("Q?", models=models)
    assert res["n"] == 2, res
    assert abs(res["mean"] - 0.6) < EPS, res
    assert res["per_model"] == {"a": 0.4, "c": 0.8}, res


def test_all_models_none_mean_is_none():
    models = ["a", "b"]
    fake = _stub({}, default=None)                      # every model fails
    with patched(challenger, "single_llm_forecast", fake):
        res = challenger.ensemble_forecast("Q?", models=models)
    assert res["mean"] is None, res
    assert res["n"] == 0, res
    assert res["probs"] == [], res
    assert res["per_model"] == {}, res
    assert res["models"] == ["a", "b"], res


def test_models_none_uses_challenger_models_env():
    """ensemble_forecast(models=None) sources its roster from CHALLENGER_MODELS."""
    restore = _set_env(CHALLENGER_MODELS="x,y")
    try:
        fake = _stub({"x": 0.3, "y": 0.5})
        with patched(challenger, "single_llm_forecast", fake):
            res = challenger.ensemble_forecast("Q?")
        assert res["n"] == 2, res
        assert abs(res["mean"] - 0.4) < EPS, res        # (0.3+0.5)/2
        assert res["models"] == ["x", "y"], res
        assert res["per_model"] == {"x": 0.3, "y": 0.5}, res
    finally:
        restore()


TESTS = [
    ("default_roster_single_model", test_default_roster_single_model),
    ("challenger_models_env_parsed", test_challenger_models_env_parsed),
    ("single_model_default_numeric_identity", test_single_model_default_numeric_identity),
    ("multi_model_mean_is_average", test_multi_model_mean_is_average),
    ("none_model_is_skipped", test_none_model_is_skipped),
    ("raising_model_is_skipped_best_effort", test_raising_model_is_skipped_best_effort),
    ("all_models_none_mean_is_none", test_all_models_none_mean_is_none),
    ("models_none_uses_challenger_models_env", test_models_none_uses_challenger_models_env),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
