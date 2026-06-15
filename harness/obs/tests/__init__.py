"""obs acceptance-criteria test suite (C1–C7).

Each test module is runnable BOTH as

    python -m harness.obs.tests.test_<x>     # prints PASS/FAIL, non-zero on fail

and under pytest (the ``def test_*`` functions). Every test redirects obs at a
FRESH temp OBS_LOGS_DIR + DATABASE_URL via :func:`_util.temp_obs_env`, so the
live ``polyswarm.db`` and ``polyswarm/logs`` are never read or written.
"""
