"""Shared helpers for the obs acceptance tests.

``temp_obs_env`` is the single isolation primitive every test uses. obs.config
reads OBS_LOGS_DIR / DATABASE_URL / OBS_ENABLED from the live environment on
*every* call, so setting them here fully redirects all obs writes (event JSONL,
blobs, errors, transcripts, and the sqlite evidence DB) into a throwaway temp
directory. The directory — and therefore the temp DB and logs — is removed on
exit, and the previous environment is restored. This works identically whether a
test is driven by pytest or by ``python -m harness.obs.tests.test_<x>`` (no
dependency on pytest fixtures / monkeypatch).
"""

import contextlib
import os
import shutil
import tempfile
import traceback


# Env vars obs honors; saved + restored around every test.
_OBS_ENV_KEYS = ("OBS_LOGS_DIR", "DATABASE_URL", "OBS_ENABLED")


@contextlib.contextmanager
def temp_obs_env(prefix="obs_test_", extra_env=None):
    """Redirect obs at a throwaway logs dir + sqlite DB; restore + clean up on exit.

    Yields the temp directory path (str). ``extra_env`` (optional dict) lets a
    test set additional env vars (e.g. fake API keys for the redaction test);
    those keys are saved and restored too.
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    keys = list(_OBS_ENV_KEYS) + list((extra_env or {}).keys())
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["OBS_LOGS_DIR"] = os.path.join(tmp, "logs")
        os.environ["DATABASE_URL"] = os.path.join(tmp, "polyswarm.db")
        os.environ["OBS_ENABLED"] = "1"
        for k, v in (extra_env or {}).items():
            os.environ[k] = v
        yield tmp
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def run_as_main(tests):
    """Run ``[(name, fn), ...]`` as a standalone suite.

    Each ``fn`` takes no args and asserts internally (raising on failure). Prints
    one ``PASS``/``FAIL`` line per test plus a summary and returns an exit code
    (0 iff every test passed) suitable for ``sys.exit``.
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
