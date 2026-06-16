"""harness/environment.py — classify a record's environment so test/demo/benchmark
data can NEVER contaminate the live profitability gates (audit Phase 3).

Real Polymarket markets are identified by a `0x…` condition-id market_id. Anything
else (`TEST`, `bench-test`, `demo-*`, etc.) is non-live and is EXCLUDED from Gate 1
(forecast quality) and Gate 2 (trading profit) by default. Callers can opt in with
include_test / include_demo / environment="all".
"""
from __future__ import annotations

import re

_REAL = re.compile(r"^0x[0-9a-fA-F]{6,}")
# explicit test/demo/benchmark markers in a market_id. We exclude ONLY ids that match
# one of these — never "anything non-0x" (that would wrongly drop synthetic/legacy real
# ids). Real Polymarket markets are 0x condition ids and are always live.
_TEST_PAT = re.compile(r"(benchmark|bench|demo|test|mock|example|fake|sample)", re.I)

# environments treated as "live" for the gates by default
LIVE = ("live", "paper_live")


def is_real_market(market_id) -> bool:
    """True iff market_id looks like a real Polymarket on-chain condition id (0x…).
    Used to decide whether to hit Gamma (only real markets get a resolution fetch)."""
    return bool(market_id and _REAL.match(str(market_id)))


def classify(market_id) -> str:
    """Best-effort environment: paper_live (real 0x or unmarked id) | benchmark | demo
    | test. Only an explicit test/demo/bench marker in the id makes it non-live."""
    if is_real_market(market_id):
        return "paper_live"
    m = _TEST_PAT.search(str(market_id or ""))
    if not m:
        return "paper_live"            # unmarked non-0x id (e.g. a synthetic OPN-*) -> live
    k = m.group(1).lower()
    if "bench" in k:
        return "benchmark"
    if "demo" in k:
        return "demo"
    return "test"


def is_live(market_id, *, include_test=False, include_demo=False, environment=None) -> bool:
    """Should this market_id count toward the live gates?

    Defaults to live/paper_live only. environment='all' includes everything;
    include_test / include_demo selectively include those buckets."""
    if environment == "all":
        return True
    env = classify(market_id)
    if env in LIVE:
        return True
    if include_test and env in ("test", "benchmark"):
        return True
    if include_demo and env == "demo":
        return True
    return False
