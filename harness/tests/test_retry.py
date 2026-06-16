"""Tests for harness.retry — deterministic (fake sleep), no real waiting / network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import run_as_main  # noqa: E402
from harness import retry  # noqa: E402


class Boom(Exception):
    pass


class Fatal(Exception):
    pass


def _flaky(fail_times):
    state = {"n": 0}

    def fn():
        if state["n"] < fail_times:
            state["n"] += 1
            raise Boom(f"transient {state['n']}")
        return "ok"
    return fn, state


def test_succeeds_after_temporary_failure():
    waits = []
    fn, st = _flaky(2)                       # fails twice, then succeeds
    out = retry.call_with_retry(fn, attempts=3, base=0.5, sleep_fn=waits.append)
    assert out == "ok" and st["n"] == 2
    assert len(waits) == 2                   # two backoff waits (between 3 tries)
    assert waits[1] > waits[0]               # exponential


def test_stops_after_max_attempts():
    fn, st = _flaky(99)                      # always fails
    waits = []
    raised = None
    try:
        retry.call_with_retry(fn, attempts=3, sleep_fn=waits.append)
    except Boom as e:
        raised = e
    assert raised is not None                # re-raises the last error
    assert st["n"] == 3                       # exactly `attempts` tries
    assert len(waits) == 2                    # waits only BETWEEN tries


def test_give_up_raises_immediately():
    def fn():
        raise Fatal("do not retry me")
    waits = []
    raised = None
    try:
        retry.call_with_retry(fn, attempts=5, retry_on=(Exception,), give_up=(Fatal,),
                              sleep_fn=waits.append)
    except Fatal:
        raised = True
    assert raised and waits == []            # no retries, no waits


def test_only_retries_listed_exceptions():
    def fn():
        raise Fatal("not in retry_on")
    raised = None
    try:
        retry.call_with_retry(fn, attempts=5, retry_on=(Boom,), sleep_fn=lambda *_: None)
    except Fatal:
        raised = True
    assert raised                            # Fatal not in retry_on -> raised on first try


def test_decorator_form():
    calls = {"n": 0}

    @retry.retrying(attempts=3)
    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise Boom("x")
        return 42
    # patch sleep so the decorator doesn't actually wait
    import time as _t
    old = _t.sleep
    _t.sleep = lambda *_: None
    try:
        assert fn() == 42 and calls["n"] == 2
    finally:
        _t.sleep = old


def test_backoff_capped():
    fn, _ = _flaky(99)
    waits = []
    try:
        retry.call_with_retry(fn, attempts=6, base=1.0, max_backoff=4.0, jitter=0.0,
                              sleep_fn=waits.append)
    except Boom:
        pass
    assert max(waits) <= 4.0                  # capped


TESTS = [
    ("succeeds_after_temporary_failure", test_succeeds_after_temporary_failure),
    ("stops_after_max_attempts", test_stops_after_max_attempts),
    ("give_up_raises_immediately", test_give_up_raises_immediately),
    ("only_retries_listed_exceptions", test_only_retries_listed_exceptions),
    ("decorator_form", test_decorator_form),
    ("backoff_capped", test_backoff_capped),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
