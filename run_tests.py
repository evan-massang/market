#!/usr/bin/env python
"""No-network test runner for the Polymarket paper harness.

Discovers every ``test_*.py`` under harness/tests/ AND harness/obs/tests/ and
runs each in its OWN subprocess (``python -m harness.tests.test_<x>``). A
separate process per file is REQUIRED: wallet/scoreboard/challenger/journal/
core.calibration each bind their sqlite DB_PATH at import time, so two DB tests
sharing one interpreter would desync onto the first file's temp DB. Isolation
also guarantees the obs-chain temp env never bleeds into a DB test.

Every test sets its own temp DATABASE_URL + temp OBS_LOGS_DIR and makes NO
network call (gamma.* is monkeypatched where needed). The LLM integration test
in test_swarm_sizes self-skips unless --llm is passed.

Usage:
    python run_tests.py [--no-llm | --llm] [-v]

Exit code 0 iff every test module exits 0.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_DIRS = [
    (ROOT / "harness" / "tests", "harness.tests"),
    (ROOT / "harness" / "obs" / "tests", "harness.obs.tests"),
]


def _python() -> str:
    """Prefer the project venv interpreter; fall back to the current one."""
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    venv_posix = ROOT / ".venv" / "bin" / "python"
    if venv_posix.exists():
        return str(venv_posix)
    return sys.executable


def discover() -> list[str]:
    mods: list[str] = []
    for d, pkg in TEST_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("test_*.py")):
            mods.append(f"{pkg}.{f.stem}")
    return mods


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    run_llm = "--llm" in argv
    verbose = "-v" in argv or "--verbose" in argv

    py = _python()
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["POLYSWARM_RUN_LLM"] = "1" if run_llm else "0"
    # Make sure obs has a default-on switch; individual tests still redirect logs.
    env.setdefault("OBS_ENABLED", "1")

    mods = discover()
    if not mods:
        print("no test modules found", file=sys.stderr)
        return 1

    print("=" * 70)
    print(f" POLYMARKET HARNESS — no-network test suite ({len(mods)} modules)")
    print(f" python: {py}")
    print(f" llm integration: {'ON' if run_llm else 'OFF (skipped)'}")
    print("=" * 70)

    results: list[tuple[str, bool, str]] = []
    for mod in mods:
        proc = subprocess.run(
            [py, "-m", mod], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        ok = proc.returncode == 0
        out = proc.stdout or ""
        results.append((mod, ok, out))
        status = "PASS" if ok else "FAIL"
        print(f"\n[{status}] {mod}  (exit {proc.returncode})")
        if verbose or not ok:
            tail = "\n".join(out.rstrip().splitlines()[-25:])
            if tail.strip():
                print("\n".join("    " + ln for ln in tail.splitlines()))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 70)
    print(f" SUMMARY: {passed}/{len(results)} modules passed" + (f", {failed} FAILED" if failed else ""))
    for mod, ok, _ in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {mod}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
