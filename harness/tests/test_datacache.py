"""B1 — no-network unit tests for harness.datacache (persistent SQLite k/v cache).

Temp DB only (make_temp_env). No network, no LLM, no sleeping / wall-clock waits.
Verifies:
  * set / get roundtrip for JSON-serializable values (dict, list, str, int)
  * expiry: a row whose fetched_at is far in the past reads back as None
    (simulated by writing an OLD fetched_at directly — no sleep, no clock games)
  * miss: an unknown key -> None
  * corrupt payload (invalid JSON in the row) -> None, never crashes
  * persistence across two SEPARATE sqlite connections (proves restart-survival)
  * make_key is stable + distinguishes different parts
  * best-effort: non-serializable value / bad ttl -> False, never raises
  * ttl_s <= 0 is treated as already-expired

Run:  python -m harness.tests.test_datacache
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta

from harness.tests._util import make_temp_env, run_as_main

_TMP = make_temp_env("ps_datacache_")

import os                                              # noqa: E402

from harness import datacache as DC                    # noqa: E402

_DB = os.environ["DATABASE_URL"]


# ── helpers ─────────────────────────────────────────────────────────────────--
def _reset():
    """Drop the table so each test starts clean (temp DB is shared in-process)."""
    conn = sqlite3.connect(_DB)
    conn.execute(f"DROP TABLE IF EXISTS {DC._TABLE}")
    conn.commit()
    conn.close()


def _set_old_fetched_at(key, days_ago):
    """Rewrite a row's fetched_at to `days_ago` in the past (no sleeping)."""
    old = (datetime.utcnow() - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(_DB)
    conn.execute(f"UPDATE {DC._TABLE} SET fetched_at=? WHERE key=?", (old, key))
    conn.commit()
    conn.close()


# ── tests ─────────────────────────────────────────────────────────────────────
def test_set_get_roundtrip():
    _reset()
    cases = [
        ("k-dict", {"a": 1, "b": [2, 3], "c": {"d": "e"}}),
        ("k-list", [1, "two", 3.0, None, True]),
        ("k-str", "hello world"),
        ("k-int", 42),
    ]
    for key, val in cases:
        assert DC.cache_set(key, val, "test", 3600) is True, key
    for key, val in cases:
        got = DC.cache_get(key)
        assert got == val, (key, got, val)


def test_miss_returns_none():
    _reset()
    assert DC.cache_get("never-stored") is None
    assert DC.cache_get("") is None
    assert DC.cache_get(None) is None


def test_expiry_reads_as_miss():
    _reset()
    assert DC.cache_set("exp", {"v": 1}, "test", ttl_s=3600) is True
    # fresh -> hit
    assert DC.cache_get("exp") == {"v": 1}
    # age the row past its 1h ttl by rewriting fetched_at to 2 days ago
    _set_old_fetched_at("exp", days_ago=2)
    assert DC.cache_get("exp") is None
    # a long ttl on the same old row -> fresh again (proves it is ttl, not deletion)
    conn = sqlite3.connect(_DB)
    conn.execute(f"UPDATE {DC._TABLE} SET ttl_s=? WHERE key=?", (10 * 86400, "exp"))
    conn.commit()
    conn.close()
    assert DC.cache_get("exp") == {"v": 1}


def test_zero_ttl_is_expired():
    _reset()
    assert DC.cache_set("z", "v", "test", ttl_s=0) is True
    assert DC.cache_get("z") is None


def test_corrupt_payload_returns_none():
    _reset()
    DC.init_db()
    # write a row with a non-JSON payload directly (and a fresh fetched_at)
    conn = sqlite3.connect(_DB)
    conn.execute(
        f"INSERT OR REPLACE INTO {DC._TABLE} "
        f"(key, source, payload_json, fetched_at, ttl_s) VALUES (?,?,?,?,?)",
        ("bad", "test", "}{ not json {{{", datetime.utcnow().isoformat(), 3600),
    )
    conn.commit()
    conn.close()
    assert DC.cache_get("bad") is None  # must NOT raise


def test_non_serializable_value_returns_false():
    _reset()
    assert DC.cache_set("ns", object(), "test", 3600) is False
    assert DC.cache_get("ns") is None
    assert DC.cache_set("bt", {1, 2, 3}, "test", 3600) is False  # set() not JSON


def test_bad_ttl_returns_false():
    _reset()
    assert DC.cache_set("bt2", "v", "test", "not-an-int") is False
    assert DC.cache_get("bt2") is None


def test_persistence_across_connections():
    _reset()
    # write via the module (its own connection), then read via a brand-new raw
    # sqlite3 connection — proves the value lives on disk and survives a restart.
    assert DC.cache_set("persist", {"x": 99}, "test", 3600) is True
    conn = sqlite3.connect(_DB)
    row = conn.execute(
        f"SELECT payload_json FROM {DC._TABLE} WHERE key=?", ("persist",)
    ).fetchone()
    conn.close()
    assert row is not None
    import json
    assert json.loads(row[0]) == {"x": 99}
    # and a fresh cache_get (its own new connection) still returns it
    assert DC.cache_get("persist") == {"x": 99}


def test_upsert_replaces():
    _reset()
    assert DC.cache_set("u", {"v": 1}, "test", 3600) is True
    assert DC.cache_set("u", {"v": 2}, "test", 3600) is True
    assert DC.cache_get("u") == {"v": 2}
    # exactly one row for the key
    conn = sqlite3.connect(_DB)
    n = conn.execute(f"SELECT COUNT(*) FROM {DC._TABLE} WHERE key=?", ("u",)).fetchone()[0]
    conn.close()
    assert n == 1, n


def test_make_key_stable_and_distinct():
    a = DC.make_key("gdelt", "iran", "peace")
    b = DC.make_key("gdelt", "iran", "peace")
    c = DC.make_key("gdelt", "iran", "war")
    d = DC.make_key("wiki", "iran", "peace")
    assert a == b
    assert a != c
    assert a != d
    assert isinstance(a, str) and a.startswith("gdelt:")


TESTS = [
    ("set_get_roundtrip", test_set_get_roundtrip),
    ("miss_returns_none", test_miss_returns_none),
    ("expiry_reads_as_miss", test_expiry_reads_as_miss),
    ("zero_ttl_is_expired", test_zero_ttl_is_expired),
    ("corrupt_payload_returns_none", test_corrupt_payload_returns_none),
    ("non_serializable_value_returns_false", test_non_serializable_value_returns_false),
    ("bad_ttl_returns_false", test_bad_ttl_returns_false),
    ("persistence_across_connections", test_persistence_across_connections),
    ("upsert_replaces", test_upsert_replaces),
    ("make_key_stable_and_distinct", test_make_key_stable_and_distinct),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
