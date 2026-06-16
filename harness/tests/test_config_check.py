"""Tests for harness.config_check — config audit, secrets masked, never writes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import run_as_main  # noqa: E402
from harness import config_check as CC       # noqa: E402


def _status(res, name):
    for n, s, _ in res["rows"]:
        if n == name:
            return s
    return None


def _detail(res, name):
    for n, _, d in res["rows"]:
        if n == name:
            return d
    return None


def _envfile(lines):
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines))
    return path


def test_known_optional_present_is_ok():
    p = _envfile(["MIN_EV_AFTER_COSTS=0.02", "RETRY_MAX_ATTEMPTS=3"])
    try:
        res = CC.run(env_file=p)
        assert _status(res, "MIN_EV_AFTER_COSTS") == "OK"
        assert res["fail"] == 0
    finally:
        os.remove(p)


def test_unread_advertised_var_warns():
    res = CC.run(env_file=_envfile([]))
    assert _status(res, "SWARM_SIZE") == "WARN"
    assert "not read" in _detail(res, "SWARM_SIZE").lower()


def test_unknown_var_warns():
    p = _envfile(["TOTALLY_MADE_UP_VAR=1"])
    try:
        res = CC.run(env_file=p)
        assert _status(res, "TOTALLY_MADE_UP_VAR") == "WARN"
        assert res["warn"] >= 1
    finally:
        os.remove(p)


def test_secret_is_masked():
    p = _envfile(["MANUS_API_KEY=super-secret-xyz-123"])
    try:
        res = CC.run(env_file=p)
        # the value must NEVER appear; status OK but masked
        blob = " ".join(d for _, _, d in res["rows"])
        assert "super-secret-xyz-123" not in blob
        assert "masked" in (_detail(res, "MANUS_API_KEY") or "").lower()
    finally:
        os.remove(p)


def test_run_is_read_only():
    # config_check must not create/modify any file
    p = _envfile(["LLM_PROVIDER=ollama"])
    try:
        before = os.path.getmtime(p)
        CC.run(env_file=p)
        assert os.path.getmtime(p) == before
    finally:
        os.remove(p)


TESTS = [
    ("known_optional_present_is_ok", test_known_optional_present_is_ok),
    ("unread_advertised_var_warns", test_unread_advertised_var_warns),
    ("unknown_var_warns", test_unknown_var_warns),
    ("secret_is_masked", test_secret_is_masked),
    ("run_is_read_only", test_run_is_read_only),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
