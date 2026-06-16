"""AUDIT fix — predict_today / sameday CLI parsers are argparse-strict.

Regression for: hand-rolled parsers IndexError-crashed on a trailing flag,
silently swallowed --dry-run, and treated an unknown command/flag as a silent
no-op. Now argparse rejects bad input loudly (SystemExit 2) and --dry-run is honored.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_cli_")

import harness.predict_today as PT  # noqa: E402
import harness.sameday as SD        # noqa: E402


def _exits(fn, argv):
    try:
        fn(argv)
        return None
    except SystemExit as e:
        return e.code


def test_predict_today_rejects_bad_args():
    # trailing flag with no value, bad float, unknown command -> argparse SystemExit(2)
    assert _exits(PT.main, ["once", "--max"]) == 2
    assert _exits(PT.main, ["--min-edge", "abc"]) == 2
    assert _exits(PT.main, ["bogus"]) == 2
    assert _exits(PT.main, ["--unknown-flag"]) == 2


def test_sameday_rejects_bad_command():
    assert _exits(SD.main, ["daemonn"]) == 2
    assert _exits(SD.main, ["--interval"]) == 2   # missing value


def test_predict_today_dry_run_threads_into_cfg():
    # --dry-run must reach LoopConfig.dry_run and short-circuit run_once (no real
    # forecast / network). We stub run_once to capture the cfg instead of running it.
    captured = {}

    def fake_run_once(cfg, **kw):
        captured["dry_run"] = getattr(cfg, "dry_run", None)

    with patched(PT, "run_once", fake_run_once):
        PT.main(["once", "--dry-run", "--max", "1"])
    assert captured.get("dry_run") is True, captured

    captured.clear()
    with patched(PT, "run_once", fake_run_once):
        PT.main(["once", "--max", "1"])
    assert captured.get("dry_run") is False, captured   # default off


TESTS = [
    ("predict_today_rejects_bad_args", test_predict_today_rejects_bad_args),
    ("sameday_rejects_bad_command", test_sameday_rejects_bad_command),
    ("predict_today_dry_run_threads_into_cfg", test_predict_today_dry_run_threads_into_cfg),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
