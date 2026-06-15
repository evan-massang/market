"""C5 — the gate evaluator is read-only and write-isolated.

Snapshots sha256+mtime+size of the temp DB and a recursive (relpath -> sha)
listing of the temp logs tree EXCLUDING ``logs/gate/``. Runs
``obs.gate.evaluate()`` and the module CLI (``gate.main(["--json"])``), then
re-snapshots and asserts:

  * the DB sha256 (and size + mtime) are IDENTICAL — the gate touched the DB only
    through a ``mode=ro`` handle,
  * NO file under events/ errors/ blobs/ transcripts/ changed or appeared,
  * only ``logs/gate/`` gained file(s) (the verdict log lives there, never in
    events/ nor the DB).
"""

import contextlib
import hashlib
import io
import os
import sqlite3

from harness import obs
from harness.obs import gate as obs_gate
from harness.obs.tests._util import temp_obs_env, run_as_main

_EVIDENCE_SUBDIRS = ("events", "errors", "blobs", "transcripts")


def _make_db():
    """Create a minimal real sqlite DB (mode=ro never creates one)."""
    path = obs.config.resolve_db_path()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('built_by', 'C5')")
        conn.commit()
    finally:
        conn.close()
    return path


def _db_snapshot(path):
    b = path.read_bytes()
    return (hashlib.sha256(b).hexdigest(), len(b), os.path.getmtime(path))


def _logs_snapshot():
    """relpath -> sha256 for every file under logs/, EXCLUDING logs/gate/."""
    root = obs.config.LOGS_DIR()
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "gate" or rel.startswith("gate/"):
            continue
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _gate_files():
    g = obs.config.LOGS_DIR() / "gate"
    return {p.name for p in g.iterdir()} if g.exists() else set()


def test_gate_readonly():
    with temp_obs_env(prefix="obs_c5_"):
        db_path = _make_db()

        # Materialize the evidence subdirs and drop pre-existing files so
        # "changed/appeared" detection is meaningful.
        obs.config.transcripts_dir()
        ev = obs.config.events_dir()
        bl = obs.config.blobs_dir()
        obs.config.errors_dir()
        (ev / "preexisting.jsonl").write_bytes(b'{"event":"run.start"}\n')
        (bl / ("a" * 64)).write_bytes(b"pre-existing blob content")

        db_before = _db_snapshot(db_path)
        logs_before = _logs_snapshot()
        gate_before = _gate_files()

        # Exercise the API and the CLI (both must stay read-only).
        verdict = obs_gate.evaluate()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = obs_gate.main(["--json"])
        assert rc == 0, rc

        db_after = _db_snapshot(db_path)
        logs_after = _logs_snapshot()
        gate_after = _gate_files()

        # DB byte-identical (sha is the headline guarantee; size + mtime too)
        assert db_after[0] == db_before[0], ("db sha changed", db_before, db_after)
        assert db_after[1] == db_before[1], ("db size changed", db_before, db_after)
        assert db_after[2] == db_before[2], ("db mtime changed", db_before, db_after)

        # No evidence file changed or appeared anywhere outside logs/gate/
        assert logs_after == logs_before, {
            "appeared": sorted(set(logs_after) - set(logs_before)),
            "removed": sorted(set(logs_before) - set(logs_after)),
            "changed": sorted(k for k in logs_before
                              if k in logs_after and logs_after[k] != logs_before[k]),
        }
        for sub in _EVIDENCE_SUBDIRS:
            assert not any(rel.split("/")[0] == sub for rel in
                           (set(logs_after) - set(logs_before))), sub

        # Only logs/gate/ may grow — and it did (the verdict was logged there).
        assert gate_after - gate_before, "gate verdict log was not written"
        assert verdict["read_only"] is True and verdict["db_mode"] == "ro", verdict


if __name__ == "__main__":
    import sys
    sys.exit(run_as_main([("C5 test_gate_readonly", test_gate_readonly)]))
