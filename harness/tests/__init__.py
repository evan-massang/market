"""P1.4/P1.5 — no-network unit-test suite for the Polymarket paper harness.

Every module here is runnable BOTH ways:

    python -m harness.tests.test_<x>      # prints PASS/FAIL, non-zero exit on fail
    pytest harness/tests/test_<x>.py      # via the def test_* functions

NO network, NO live LLM, NO live DB/logs. Each file redirects DATABASE_URL +
OBS_LOGS_DIR at a throwaway temp dir via :func:`_util.make_temp_env` (called at
module top, BEFORE importing any harness DB module, because wallet/scoreboard/
challenger/journal/core.calibration bind their sqlite path at import time), or
monkeypatches the network I/O (gamma.*) so a candidate scan / settlement runs
fully offline.
"""
