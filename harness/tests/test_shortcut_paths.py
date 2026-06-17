"""Plan 3 — SHORTCUT BETTING PATHS. No path may open a paper position without the
shared safety stack (swarm-health → EV → risk → bankroll → exposure), or be
disabled-by-default / test-only.

NO network, NO real LLM. The loop / strategy drivers patch gamma/classifier/forecast
to synthetic offline stubs. Temp DB only. Run: python -m harness.tests.test_shortcut_paths
"""
from __future__ import annotations

import contextlib
import glob
import os
import re
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_shortcut_")
os.environ["LLM_PROVIDER"] = "ollama"

from harness import safe_bet as SAFE         # noqa: E402
from harness import predict_today as PT      # noqa: E402
from harness import wallet as W              # noqa: E402
from harness import loop as LP               # noqa: E402
from harness import strategy_bet as STRAT    # noqa: E402
from harness import journal                  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MKT = {"market_id": "0xshortcut01", "question": "Will the incumbent win the 2032 election?",
        "outcomes": ["Yes", "No"], "outcome_prices": [0.50, 0.50], "volume": 200_000.0,
        "liquidity": 40_000.0, "end_date": "2032-01-01T00:00:00Z", "event_slug": None, "raw": {}}

_HEALTHY_META = {"allow_bet": True, "aborted": False, "degraded": False,
                 "n_agents_succeeded": 5, "n_agents_requested": 5, "method": "swarm",
                 "consensus": 0.8, "consensus_status": "ok"}
_FALLBACK_META = {"allow_bet": False, "aborted": True, "degraded": True,
                  "n_agents_succeeded": 0, "n_agents_requested": 5,
                  "method": "degraded_all_agents_failed", "consensus": 0.0}


@contextlib.contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _reset_wallet(starting=1000.0):
    conn = sqlite3.connect(os.environ["DATABASE_URL"])
    for t in ("paper_wallet", "paper_positions"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    W.init_wallet(starting)
    journal.init_journal()


def _decisions(reason_substr=None, status="no_bet"):
    conn = sqlite3.connect(os.environ["DATABASE_URL"]); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM decisions WHERE status=?", (status,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    if reason_substr is None:
        return rows
    return [r for r in rows if reason_substr in (r["why"] or "")]


def _allow(*a, **k):
    return True, "ok"


def _fillresult_open(*a, **k):
    # positional: market_id, question, side, model_p, market_p, edge, stake
    return W.FillResult(opened=True, reason="ok", position_id=1, side=a[2],
                        fill_price=0.51, shares=10.0, stake=a[6], fee=0.0)


# ════════════════════════════════════════════════════════════════════════════
# safe_bet — the shared gate: all pass → opens ONCE; any gate blocks → no open
# ════════════════════════════════════════════════════════════════════════════
def test_safebet_all_gates_pass_opens_once():
    _reset_wallet()
    calls = []

    def _spy(*a, **k):
        calls.append((a, k)); return _fillresult_open(*a, **k)

    with patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": (True, "ok")), \
         patched(PT, "_p7_ev_gate", lambda *a, **k: (True, "positive_ev_after_costs")), \
         patched(PT, "_p8_risk_guards", _allow), patched(PT, "_p9_can_trade", _allow), \
         patched(PT, "_p9_exposure_ok", _allow), patched(W, "open_position", _spy):
        out = SAFE.open_position_if_safe(source="loop", market=_MKT, side="YES",
                                         probability=0.65, price=0.50, stake=10.0,
                                         forecast_meta=_HEALTHY_META)
    assert out["opened"] is True and len(calls) == 1, (out, calls)


def test_safebet_each_gate_blocks_no_open():
    _reset_wallet()

    def _no_open(*a, **k):
        raise AssertionError("open_position called despite a blocking gate!")

    gates = [
        ("_p7_ev_gate", lambda *a, **k: (False, "neg_ev_after_costs"), "neg_ev_after_costs"),
        ("_p8_risk_guards", lambda *a, **k: (False, "risk_guards_error_fail_closed"), "risk_guards_error_fail_closed"),
        ("_p9_can_trade", lambda *a, **k: (False, "bankroll_error_fail_closed"), "bankroll_error_fail_closed"),
        ("_p9_exposure_ok", lambda *a, **k: (False, "exposure_error_fail_closed"), "exposure_error_fail_closed"),
    ]
    for name, blocker, expect in gates:
        with patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": (True, "ok")), \
             patched(PT, "_p7_ev_gate", _allow), patched(PT, "_p8_risk_guards", _allow), \
             patched(PT, "_p9_can_trade", _allow), patched(PT, "_p9_exposure_ok", _allow), \
             patched(PT, name, blocker), patched(W, "open_position", _no_open):
            out = SAFE.open_position_if_safe(source="loop", market=_MKT, side="YES",
                                             probability=0.65, price=0.50, stake=10.0,
                                             forecast_meta=_HEALTHY_META)
        assert out["opened"] is False and out["reason"] == expect, (name, out)


def test_safebet_ai_source_missing_health_blocks():
    _reset_wallet()

    def _no_open(*a, **k):
        raise AssertionError("opened despite missing swarm health!")

    with patched(W, "open_position", _no_open):
        out = SAFE.open_position_if_safe(source="loop", market=_MKT, side="YES",
                                         probability=0.65, price=0.50, stake=10.0,
                                         forecast_meta=None)
    assert out["opened"] is False and "missing_health_metadata" in out["reason"], out


def test_safebet_required_mirofish_blocks_shortcut_path():
    # FINAL-AUDIT FIX (Plan 3↔Plan 8 gap): when MiroFish is REQUIRED, a shortcut/manual bet with
    # NO fresh, market-matched report must block in safe_bet EXACTLY as predict_today/sameday do.
    # (No-op when not required — the default — is proven by test_safebet_all_gates_pass_opens_once.)
    _reset_wallet()

    def _no_open(*a, **k):
        raise AssertionError("opened despite REQUIRED MiroFish missing on a shortcut path!")

    with _env(MIROFISH_MODE="required"), \
         patched(PT, "_p_swarm_health", lambda meta, prefix="swarm": (True, "ok")), \
         patched(W, "open_position", _no_open):
        out = SAFE.open_position_if_safe(source="place_bet", market=_MKT, side="YES",
                                         probability=0.65, price=0.50, stake=10.0,
                                         forecast_meta=_HEALTHY_META)
    assert out["opened"] is False and "mirofish" in out["reason"].lower(), out
    assert _decisions("mirofish"), "required-MiroFish no_bet not recorded for the shortcut path"


# ════════════════════════════════════════════════════════════════════════════
# strategy_bet — disabled by default; routed + gated when enabled
# ════════════════════════════════════════════════════════════════════════════
def test_strategy_bet_disabled_by_default_no_open_no_fetch():
    _reset_wallet()

    def _boom_fetch(*a, **k):
        raise AssertionError("fetched markets despite strategy_bet disabled!")

    def _boom_open(*a, **k):
        raise AssertionError("open_position called despite strategy_bet disabled!")

    with _env(ENABLE_STRATEGY_BET=None), \
         patched(STRAT.gamma, "fetch_active_markets", _boom_fetch), \
         patched(W, "open_position", _boom_open):
        out = STRAT.main([])
    assert out == SAFE.STRATEGY_DISABLED, out


def _drive_strategy(decision, price):
    """Run strategy_bet.main() ENABLED over ONE synthetic market with a given decision."""
    _reset_wallet()
    mkt = dict(_MKT, market_id="0xstrat01")
    with _env(ENABLE_STRATEGY_BET="true"), \
         patched(STRAT.gamma, "fetch_active_markets", lambda limit=1500: [mkt]), \
         patched(STRAT, "_hours_left", lambda m: 48.0), \
         patched(STRAT.classifier, "tag_market", lambda m: SimpleNamespace(label="opinion")), \
         patched(STRAT.gamma, "yes_price", lambda m: price), \
         patched(STRAT.strategy, "decide_bet", lambda p: decision):
        STRAT.main([])


def test_strategy_bet_missing_probability_no_bet():
    # decide_bet returns a side but NO est_true_yes -> must not pretend / must not bet
    def _no_open(*a, **k):
        raise AssertionError("opened despite missing est_true_yes!")
    with patched(W, "open_position", _no_open):
        _drive_strategy(SimpleNamespace(side="YES", fraction=0.05, est_true_yes=None, reason="x"), price=0.95)
    assert _decisions(SAFE.STRATEGY_MISSING_EV_PROB), "missing-ev no_bet not recorded"


def test_strategy_bet_enabled_ev_blocks_longshot():
    # backing a 0.95 YES longshot with est_true_yes 0.95 is negative-EV after slippage -> blocked
    def _no_open(*a, **k):
        raise AssertionError("opened a negative-EV longshot!")
    with patched(W, "open_position", _no_open):
        _drive_strategy(SimpleNamespace(side="YES", fraction=0.05, est_true_yes=0.95, reason="back fav"), price=0.95)
    # no position opened; gate-blocked decision recorded
    assert W.get_open_positions() == []


def test_strategy_bet_enabled_opens_only_after_gates_pass():
    # fading a 0.10 longshot to NO with est_true_yes 0.02 is +EV -> passes the full stack -> opens
    _drive_strategy(SimpleNamespace(side="NO", fraction=0.05, est_true_yes=0.02, reason="fade longshot"), price=0.10)
    assert len(W.get_open_positions()) == 1, "a fully-gated +EV strategy bet should open exactly one position"


# ════════════════════════════════════════════════════════════════════════════
# legacy loop.run_once — betting disabled by default; fallback blocked; gated when enabled
# ════════════════════════════════════════════════════════════════════════════
def _drive_run_once(meta, *, enable_legacy, p=0.70, price=0.50, raise_on_open=False):
    _reset_wallet()
    cfg = LP.LoopConfig()
    cfg.max_markets = 1
    cfg.max_days_to_resolution = 0   # disable the far-out filter for the synthetic 2032 market
    cls = SimpleNamespace(label="opinion", signals=None)
    stubs = [
        patched(LP.gamma, "fetch_active_markets", lambda limit=150: [dict(_MKT)]),
        patched(LP.classifier, "should_forecast", lambda m, **k: (True, cls)),
        patched(LP.gamma, "yes_price", lambda m: price),
        patched(LP, "_build_enrichment", lambda m, c: ""),
        patched(LP, "_forecast", lambda m, pr, c, enr="": (p, dict(meta))),
    ]
    if raise_on_open:
        def _no_open(*a, **k):
            raise AssertionError("open_position called when it must not!")
        stubs.append(patched(W, "open_position", _no_open))
    env = {"ENABLE_LEGACY_LOOP_BETTING": "true" if enable_legacy else None}
    with _env(**env), contextlib.ExitStack() as es:
        for s in stubs:
            es.enter_context(s)
        return LP.run_once(cfg)


def test_legacy_loop_disabled_by_default_no_open():
    summary = _drive_run_once(_HEALTHY_META, enable_legacy=False, raise_on_open=True)
    assert summary["opened"] == 0
    assert _decisions(SAFE.LEGACY_LOOP_DISABLED), "legacy-loop disabled no_bet not recorded"


def test_legacy_loop_fallback_probability_no_bet():
    summary = _drive_run_once(_FALLBACK_META, enable_legacy=True, raise_on_open=True)
    assert summary["opened"] == 0
    assert _decisions(SAFE.LEGACY_LOOP_FALLBACK), "legacy-loop fallback no_bet not recorded"


def test_legacy_loop_enabled_healthy_opens_via_safe_bet():
    summary = _drive_run_once(_HEALTHY_META, enable_legacy=True, p=0.70, price=0.50)
    assert summary["opened"] == 1 and len(W.get_open_positions()) == 1, summary


# ════════════════════════════════════════════════════════════════════════════
# repo-wide: no UNCONTROLLED wallet.open_position outside the allowed files
# ════════════════════════════════════════════════════════════════════════════
def test_no_uncontrolled_open_position_calls():
    allowed = {"safe_bet.py", "predict_today.py", "sameday.py"}   # wallet.py has the def, not a call
    pat = re.compile(r"wallet\.open_position\(")
    offenders = []
    for base in ("harness", "core"):
        for path in glob.glob(os.path.join(_REPO, base, "**", "*.py"), recursive=True):
            low = path.replace("\\", "/").lower()
            if "/tests/" in low or os.path.basename(path).startswith("test_"):
                continue
            with open(path, encoding="utf-8") as f:
                if pat.search(f.read()) and os.path.basename(path) not in allowed:
                    offenders.append(os.path.relpath(path, _REPO))
    assert not offenders, f"uncontrolled wallet.open_position in: {offenders}"


def test_strategy_and_loop_gate_before_open():
    # strategy_bet gates on strategy_bet_enabled() and never calls wallet.open_position directly
    with open(os.path.join(_REPO, "harness", "strategy_bet.py"), encoding="utf-8") as f:
        strat = f.read()
    assert "safe_bet.strategy_bet_enabled()" in strat
    assert "wallet.open_position(" not in strat
    assert "safe_bet.open_position_if_safe(" in strat
    # loop gates on legacy_loop_betting_enabled() and routes opens through safe_bet
    with open(os.path.join(_REPO, "harness", "loop.py"), encoding="utf-8") as f:
        loop = f.read()
    assert "safe_bet.legacy_loop_betting_enabled()" in loop
    assert "wallet.open_position(" not in loop
    assert "safe_bet.open_position_if_safe(" in loop


# ════════════════════════════════════════════════════════════════════════════
# launchers / supervisor do not schedule the shortcut betting paths by default
# ════════════════════════════════════════════════════════════════════════════
def test_harness_pass_bat_does_not_run_strategy_by_default():
    with open(os.path.join(_REPO, "harness_pass.bat"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    active = [ln for ln in lines if ln.strip() and not ln.strip().upper().startswith("REM")]
    # no ACTIVE (uncommented) line may invoke strategy_bet
    assert not any("strategy_bet" in ln for ln in active), active
    # settlement-only is still scheduled
    assert any("harness.loop settle" in ln for ln in active)


def test_supervisor_services_do_not_run_shortcut_betting():
    with open(os.path.join(_REPO, "harness", "services.py"), encoding="utf-8") as f:
        svc = f.read()
    assert "strategy_bet" not in svc
    # the supervisor must not run the legacy loop betting commands ('loop run' / 'loop daemon')
    assert "harness.loop\", \"run\"" not in svc and "harness.loop\", \"daemon\"" not in svc


TESTS = [
    ("safebet_all_gates_pass_opens_once", test_safebet_all_gates_pass_opens_once),
    ("safebet_each_gate_blocks_no_open", test_safebet_each_gate_blocks_no_open),
    ("safebet_ai_source_missing_health_blocks", test_safebet_ai_source_missing_health_blocks),
    ("safebet_required_mirofish_blocks_shortcut_path", test_safebet_required_mirofish_blocks_shortcut_path),
    ("strategy_bet_disabled_by_default_no_open_no_fetch", test_strategy_bet_disabled_by_default_no_open_no_fetch),
    ("strategy_bet_missing_probability_no_bet", test_strategy_bet_missing_probability_no_bet),
    ("strategy_bet_enabled_ev_blocks_longshot", test_strategy_bet_enabled_ev_blocks_longshot),
    ("strategy_bet_enabled_opens_only_after_gates_pass", test_strategy_bet_enabled_opens_only_after_gates_pass),
    ("legacy_loop_disabled_by_default_no_open", test_legacy_loop_disabled_by_default_no_open),
    ("legacy_loop_fallback_probability_no_bet", test_legacy_loop_fallback_probability_no_bet),
    ("legacy_loop_enabled_healthy_opens_via_safe_bet", test_legacy_loop_enabled_healthy_opens_via_safe_bet),
    ("no_uncontrolled_open_position_calls", test_no_uncontrolled_open_position_calls),
    ("strategy_and_loop_gate_before_open", test_strategy_and_loop_gate_before_open),
    ("harness_pass_bat_does_not_run_strategy_by_default", test_harness_pass_bat_does_not_run_strategy_by_default),
    ("supervisor_services_do_not_run_shortcut_betting", test_supervisor_services_do_not_run_shortcut_betting),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
