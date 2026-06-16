"""AUDIT fix — dashboard endpoints exist and never 500, even on an EMPTY DB.

Covers audit #14 (missing /health, /debug, /errors, /decisions/recent) and the
"dashboard must not crash because the DB has no data yet" requirement (#15).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, run_as_main  # noqa: E402

make_temp_env("ps_dash_")

# fresh wallet/journal so the DB exists but is essentially empty
from harness import wallet, journal  # noqa: E402
wallet.init_wallet(1000.0)
journal.init_journal()

import harness.dashboard as D  # noqa: E402

try:
    from fastapi.testclient import TestClient
    _client = TestClient(D.app)
except Exception:
    _client = None


def _get(path):
    assert _client is not None, "TestClient unavailable"
    return _client.get(path)


def test_new_endpoints_exist_and_200_on_empty_db():
    for path in ["/health", "/decisions/recent", "/errors", "/debug"]:
        r = _get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert isinstance(r.json(), dict), (path, type(r.json()))


def test_api_state_survives_empty_db():
    r = _get("/api/state")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert "wallet" in body and "positions" in body
    assert isinstance(body["positions"], list)   # [] on empty, not a 500


def test_command_center_survives_empty_db():
    r = _get("/api/command_center")
    assert r.status_code == 200, r.status_code


def test_debug_is_secret_free_and_paper():
    r = _get("/debug")
    body = r.json()
    assert "PAPER" in body.get("trading", "")
    # never leak an API key value in the debug surface
    blob = str(body).lower()
    assert "api_key" not in blob or "sk-" not in blob


TESTS = [
    ("new_endpoints_exist_and_200_on_empty_db", test_new_endpoints_exist_and_200_on_empty_db),
    ("api_state_survives_empty_db", test_api_state_survives_empty_db),
    ("command_center_survives_empty_db", test_command_center_survives_empty_db),
    ("debug_is_secret_free_and_paper", test_debug_is_secret_free_and_paper),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
