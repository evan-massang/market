"""obs.ids — correlation id minting + contextvars-based id propagation.

A single ContextVar holds a dict of active correlation ids. The *_ctx
contextmanagers merge new ids on enter and restore the previous dict on exit
via ContextVar.set/reset tokens (so they nest correctly and are coroutine /
thread safe). current() returns a copy of the merged active dict.

Recognized id keys: run_id, cycle_id, market_id, forecast_id, agent_id, role,
llm_call_id. Arbitrary extra keys are allowed and simply merged through.
"""

import contextvars
from contextlib import contextmanager
from uuid import uuid4

_ACTIVE = contextvars.ContextVar("obs_active_ids", default={})


def mint(prefix):
    """Return '<prefix>_<10 hex chars from uuid4>'. uuid4 avoids clock/random pitfalls."""
    try:
        return "{}_{}".format(prefix, uuid4().hex[:10])
    except Exception:
        # Never raise; degrade to a stable-ish fallback.
        return "{}_{}".format(prefix, "0000000000")


def current():
    """Return a *copy* of the merged active id dict (empty {} if nothing set)."""
    try:
        return dict(_ACTIVE.get())
    except Exception:
        return {}


@contextmanager
def _push(**ids):
    """Merge `ids` (dropping None values) over the current dict; restore on exit."""
    token = None
    try:
        try:
            prev = _ACTIVE.get()
        except Exception:
            prev = {}
        merged = dict(prev)
        for k, v in ids.items():
            if v is not None:
                merged[k] = v
        try:
            token = _ACTIVE.set(merged)
        except Exception:
            token = None
        yield merged
    finally:
        if token is not None:
            try:
                _ACTIVE.reset(token)
            except Exception:
                pass


@contextmanager
def run_ctx(**ids):
    with _push(**ids) as m:
        yield m


@contextmanager
def cycle_ctx(**ids):
    with _push(**ids) as m:
        yield m


@contextmanager
def market_ctx(**ids):
    with _push(**ids) as m:
        yield m


@contextmanager
def forecast_ctx(**ids):
    with _push(**ids) as m:
        yield m


@contextmanager
def agent_ctx(**ids):
    with _push(**ids) as m:
        yield m
