"""Shared, dependency-free helpers for the harness unit tests.

The single isolation primitive is :func:`make_temp_env`: it points DATABASE_URL
and OBS_LOGS_DIR at a fresh temp directory so NO live polyswarm.db / logs are
touched. It MUST be called at module import time, BEFORE importing any module
that captures its sqlite path at import (wallet, scoreboard, challenger, journal,
core.calibration all do ``DB_PATH = os.getenv("DATABASE_URL", ...)`` once).

obs is the exception: harness/obs/config.py re-reads the env on every call, so
setting OBS_LOGS_DIR/DATABASE_URL here also redirects all obs writes.

Everything here is stdlib-only and works identically under pytest or under
``python -m harness.tests.test_<x>`` (no pytest fixtures / monkeypatch needed).
"""
from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import tempfile
import traceback


def make_temp_env(prefix: str = "ps_test_", *, obs_enabled: str = "1") -> str:
    """Create a throwaway temp dir; point DATABASE_URL + OBS_LOGS_DIR at it.

    Returns the temp dir path. The dir (temp DB + temp logs) is removed at
    interpreter exit. Call this BEFORE importing harness DB modules.
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.environ["DATABASE_URL"] = os.path.join(tmp, "polyswarm_test.db")
    os.environ["OBS_LOGS_DIR"] = os.path.join(tmp, "logs")
    os.environ["OBS_ENABLED"] = obs_enabled
    # Default to local Ollama so any incidental Agent() construction never demands
    # an API key (it builds a lazy httpx client and makes NO network call on init).
    os.environ.setdefault("LLM_PROVIDER", "ollama")
    atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
    return tmp


@contextlib.contextmanager
def patched(obj, name: str, value):
    """Temporarily set ``obj.name = value`` (manual monkeypatch), restore on exit.

    Used to redirect a module's network calls (e.g. gamma.fetch_*) to an offline
    stub so a test makes NO network request.
    """
    sentinel = object()
    old = getattr(obj, name, sentinel)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if old is sentinel:
            try:
                delattr(obj, name)
            except AttributeError:
                pass
        else:
            setattr(obj, name, old)


def run_as_main(tests):
    """Run ``[(name, fn), ...]`` as a standalone suite.

    Each ``fn`` takes no args and asserts internally (raising on failure). Prints
    one PASS/FAIL line per test plus a summary; returns an exit code (0 iff every
    test passed) suitable for ``sys.exit``.
    """
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception:
            failures += 1
            print("FAIL  " + name)
            traceback.print_exc()
    total = len(tests)
    print("\n%d/%d passed" % (total - failures, total))
    return 1 if failures else 0
