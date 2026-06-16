"""harness/retry.py — one shared retry/backoff helper for external calls.

Timeout is the caller's job (each httpx call passes its own); this adds the retry
policy: exponential backoff + jitter, bounded attempts, and a clear give-up. Used by
Gamma/GDELT/Wikipedia/MiroFish so a transient blip doesn't cost a whole daemon cycle —
and so a permanently-failing source gives up fast instead of hanging.

Deterministic in tests: pass sleep_fn (default time.sleep) and rng (default a tiny
fixed jitter) so no real waiting / randomness is required.
"""
from __future__ import annotations

import time as _time

# defaults overridable via env (Phase 9 config) without touching call sites
import os


def _envf(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envi(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


DEFAULT_ATTEMPTS = _envi("RETRY_MAX_ATTEMPTS", 3)
DEFAULT_BASE = _envf("RETRY_BASE_SECONDS", 0.5)
DEFAULT_MAX_BACKOFF = _envf("RETRY_MAX_BACKOFF_SECONDS", 8.0)


def call_with_retry(fn, *args, attempts=None, base=None, max_backoff=None,
                    retry_on=(Exception,), give_up=(), sleep_fn=None, jitter=0.1, **kwargs):
    """Call fn(*args, **kwargs), retrying on `retry_on` with exponential backoff + jitter.

    - attempts: total tries (>=1). base: first backoff. max_backoff: cap per wait.
    - give_up: exception types that should NOT be retried (raise immediately).
    - Re-raises the last exception after the final attempt.
    """
    attempts = attempts or DEFAULT_ATTEMPTS
    base = DEFAULT_BASE if base is None else base
    max_backoff = DEFAULT_MAX_BACKOFF if max_backoff is None else max_backoff
    sleep_fn = sleep_fn or _time.sleep
    last = None
    for i in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except give_up:
            raise
        except retry_on as e:
            last = e
            if i + 1 >= attempts:
                break
            wait = min(base * (2 ** i), max_backoff) + (jitter * (i + 1))
            sleep_fn(wait)
    if last is not None:
        raise last
    raise RuntimeError("call_with_retry: no attempts made")


def retrying(attempts=None, base=None, max_backoff=None, retry_on=(Exception,), give_up=()):
    """Decorator form of call_with_retry."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            return call_with_retry(fn, *args, attempts=attempts, base=base,
                                   max_backoff=max_backoff, retry_on=retry_on,
                                   give_up=give_up, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "retrying")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        return wrapper
    return deco
