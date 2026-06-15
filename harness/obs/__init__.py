"""obs — self-contained observability for the Polymarket harness.

Facade re-exporting the public surface. Every submodule import is guarded so a
single broken submodule cannot make `from harness import obs` raise. Imports
must NEVER raise; public entry points degrade to guarded no-ops.

Honor OBS_ENABLED (default '1'; '0' => emit/hooks are no-ops).
"""

from contextlib import contextmanager as _contextmanager

# ── event log ──────────────────────────────────────────────────────────────---
try:
    from .eventlog import emit, verify_chain, line_sha
except Exception:  # pragma: no cover - defensive fallback
    def emit(*args, **kwargs):
        return None

    def verify_chain(*args, **kwargs):
        return {
            "ok": False,
            "first_bad_index": None,
            "reason": "eventlog unavailable",
            "n": 0,
        }

    def line_sha(*args, **kwargs):
        return None


# ── correlation ids ─────────────────────────────────────────────────────────--
try:
    from .ids import (
        run_ctx,
        cycle_ctx,
        market_ctx,
        forecast_ctx,
        agent_ctx,
        current,
        mint,
    )
except Exception:  # pragma: no cover - defensive fallback
    def current():
        return {}

    def mint(prefix):
        return str(prefix) + "_unavailable"

    @_contextmanager
    def _noop_ctx(**ids):
        yield {}

    run_ctx = cycle_ctx = market_ctx = forecast_ctx = agent_ctx = _noop_ctx


# ── submodules ─────────────────────────────────────────────────────────────---
try:
    from . import config
except Exception:  # pragma: no cover
    config = None

try:
    from . import redact
except Exception:  # pragma: no cover
    redact = None

try:
    from . import blobs
except Exception:  # pragma: no cover
    blobs = None

try:
    from . import codeversion
except Exception:  # pragma: no cover
    codeversion = None

try:
    from . import evidence
except Exception:  # pragma: no cover
    evidence = None

try:
    from . import hooks
except Exception:  # pragma: no cover
    hooks = None

try:
    from . import transcript
except Exception:  # pragma: no cover
    transcript = None


def enabled():
    """Return True unless OBS_ENABLED=='0'. Degrades to False if config is broken."""
    try:
        if config is not None:
            return config.enabled()
    except Exception:
        pass
    return False


__all__ = [
    "emit",
    "verify_chain",
    "line_sha",
    "run_ctx",
    "cycle_ctx",
    "market_ctx",
    "forecast_ctx",
    "agent_ctx",
    "current",
    "mint",
    "hooks",
    "evidence",
    "blobs",
    "redact",
    "codeversion",
    "config",
    "transcript",
    "enabled",
]
