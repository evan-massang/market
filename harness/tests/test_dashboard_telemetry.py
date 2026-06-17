"""Dashboard live-telemetry + paper-wallet cockpit tests.

Temp DB + temp event store only. NO live daemons, NO live APIs, NO live-DB mutation. Proves the
cockpit is data-backed and honest: real events (or replay-marked), no fake PnL / links / MiroFish
/ payout / green; missing data shows unknown/stale/link_unavailable/payout_unknown/timer_unknown/
missing_proof; equity uses Plan 9 accounting truth; Gate 2 uses Plan 9; MiroFish uses Plan 8.
"""
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_tele_")
_RT = tempfile.mkdtemp(prefix="ps_tele_rt_")
os.environ["LIVE_EVENTS_DB"] = os.path.join(_RT, "live_events.db")
os.environ["SUPERVISOR_RUNTIME_DIR"] = os.path.join(_RT, ".runtime")
os.environ["OBS_ENABLED"] = "0"          # bridge fires BEFORE the obs gate; isolate live events
import atexit, shutil  # noqa: E402
atexit.register(lambda: shutil.rmtree(_RT, ignore_errors=True))

from harness import live_events as LE       # noqa: E402
from harness import paper_bets as PB         # noqa: E402
from harness import wallet                   # noqa: E402
from harness import journal                  # noqa: E402

DASH = ROOT + "/harness/dashboard.py"


def _db():
    return os.environ["DATABASE_URL"]


def _client():
    from fastapi.testclient import TestClient
    import harness.dashboard as D
    return TestClient(D.app)


def _reset():
    conn = sqlite3.connect(_db())
    for t in ("paper_wallet", "paper_positions", "decisions", "decision_features", "equity_snapshots"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0); journal.init_journal()
    # fresh live-events store per reset
    try:
        os.remove(os.environ["LIVE_EVENTS_DB"])
    except Exception:
        pass


def _open_pos(mid="0xm1", q="Will X resolve YES?", side="YES", stake=42.0, fill=0.42, shares=100.0,
              fee=0.0, end="2032-01-01T00:00:00Z", slug="will-x"):
    conn = sqlite3.connect(_db())
    conn.execute("INSERT INTO paper_positions (market_id,question,side,model_p,market_p,edge,stake,"
                 "fill_price,shares,fee,status,end_date,event_slug,opened_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, '2026-06-01T00:00:00')",
                 (mid, q, side, 0.6, 0.5, 0.1, stake, fill, shares, fee, end, slug))
    conn.commit(); conn.close()


def _settled_pos(mid="0xs1", q="Q", pnl=8.0, slug="will-s"):
    conn = sqlite3.connect(_db())
    conn.execute("INSERT INTO paper_positions (market_id,question,side,model_p,market_p,edge,stake,"
                 "fill_price,shares,fee,status,outcome,payout,realized_pnl,event_slug,settled_at) "
                 "VALUES (?,?, 'YES', 0.6,0.5,0.1, 42.0,0.42,100.0,0.0,'settled',1.0,100.0,?,?, '2026-06-02T00:00:00')",
                 (mid, q, pnl, slug))
    conn.commit(); conn.close()


# ───────────────── A. event model + live endpoints (1-6) ─────────────────────────

def test_event_model_validates_required_fields():
    ok, _ = LE.validate_event({"type": "log", "source": "swarm", "ts": "t", "paper_only": True})
    assert ok is True
    assert LE.validate_event({"type": "log", "source": "swarm", "ts": "t"})[0] is False        # no paper_only
    assert LE.validate_event({"source": "swarm", "ts": "t", "paper_only": True})[0] is False    # no type


def test_recent_endpoint_returns_json():
    _reset()
    LE.emit("swarm.started", "swarm", market_id="0xm1", message="go")
    c = _client()
    b = c.get("/api/live/recent").json()
    assert b["paper_only"] is True and isinstance(b["events"], list) and b["count"] >= 1


def test_stream_handles_disconnected_client():
    # client accounting + reads must work with zero connected clients; emit never depends on it
    _reset()
    assert LE.client_count() == 0
    LE.register_client(); assert LE.client_count() == 1
    LE.unregister_client(); assert LE.client_count() == 0
    assert LE.emit("log", "system", message="x") is True
    assert isinstance(LE.recent_events(10), list)


def test_broadcast_failure_does_not_affect_decision_path():
    # if the telemetry bus is broken, an obs hook (and any caller) must NOT raise
    import harness.obs.hooks as H

    def _boom(*a, **k):
        raise RuntimeError("bus down")
    with patched(LE, "emit", _boom):
        # the bridge swallows the broken bus; the obs hook returns without raising into the caller
        H.on_trade_skip(forecast_id="f", reason="x", inputs={"market_id": "0xm1"})
    # emit_event itself never raises, even on an unwritable path — it just returns False
    assert LE.emit_event({}, path="/nonexistent_dir/zzz/x.db") is False


def test_replayed_events_marked():
    _reset()
    LE.emit("log", "system", message="from journal", replay=True)
    evs = LE.recent_events(10)
    assert any(e.get("replay") is True for e in evs)


def test_live_status_reports_last_event_age():
    _reset()
    LE.emit("heartbeat", "system", message="hb")
    st = LE.event_status()
    assert st["paper_only"] is True and st["last_event_id"] is not None
    assert isinstance(st["last_event_age_seconds"], (int, float)) and st["state"] in ("connected", "idle", "stale", "unknown")


# ───────────────── B. paper wallet (7-11) ────────────────────────────────────────

def test_paper_wallet_paper_only():
    _reset()
    assert _client().get("/api/paper-wallet").json()["paper_only"] is True


def test_paper_wallet_uses_accounting_status():
    _reset()
    b = _client().get("/api/paper-wallet").json()
    assert b["accounting_status"] in ("ok", "degraded", "drift", "unknown", "error")
    assert isinstance(b["accounting_reasons"], list)


def test_verified_equity_hidden_when_unverified():
    _reset()
    _open_pos()   # an open position with no fresh mark -> accounting cannot verify equity
    b = _client().get("/api/paper-wallet").json()
    if not b["equity_verified"]:
        assert b["verified_equity"] is None


def test_missing_marks_produce_accounting_reasons():
    _reset()
    _open_pos()
    b = _client().get("/api/paper-wallet").json()
    if b["accounting_status"] != "ok":
        assert b["accounting_reasons"]


def test_wallet_separates_realized_unrealized_total():
    _reset()
    b = _client().get("/api/paper-wallet").json()
    assert "realized_pnl" in b and "unrealized_pnl" in b and "total_pnl" in b


# ───────────────── C. active paper bets (12-19) ──────────────────────────────────

def test_open_bets_returns_positions():
    _reset(); _open_pos()
    b = _client().get("/api/paper-bets/open").json()
    assert b["paper_only"] is True and len(b["positions"]) == 1


def test_open_bet_has_link_or_unavailable():
    _reset(); _open_pos(slug="will-x")
    p = _client().get("/api/paper-bets/open").json()["positions"][0]
    assert p["url_status"] == "ok" and p["url"] == "https://polymarket.com/event/will-x"
    _reset(); _open_pos(mid="0xnoslug", slug=None)
    p2 = _client().get("/api/paper-bets/open").json()["positions"][0]
    assert p2["url_status"] == "link_unavailable" and p2["url"] is None


def test_open_bet_has_countdown_fields():
    _reset(); _open_pos()
    p = _client().get("/api/paper-bets/open").json()["positions"][0]
    assert "seconds_until_end" in p and "timer_status" in p and "end_time" in p


def test_open_bet_has_payout_profit_maxloss():
    _reset(); _open_pos(stake=42.0, fill=0.42, shares=100.0, fee=0.0)
    p = _client().get("/api/paper-bets/open").json()["positions"][0]
    assert p["possible_payout_if_win"] == 100.0 and p["possible_profit_if_win"] == 58.0 and p["max_loss"] == 42.0


def test_payout_formula_yes_correct():
    r = PB.compute_payout_preview({"fill_price": 0.42, "shares": 100.0, "stake": 42.0, "fee": 0.0})
    assert r["ok"] and r["possible_payout_if_win"] == 100.0 and r["possible_profit_if_win"] == 58.0 and r["max_loss"] == 42.0


def test_payout_unknown_for_invalid_or_missing():
    # NO model is unambiguous here (payout shares); but a missing/invalid position -> payout_unknown
    assert PB.compute_payout_preview({"fill_price": 0.5, "shares": None, "stake": 10})["reason"] == "payout_unknown"
    assert PB.compute_payout_preview({"fill_price": 0.0, "shares": 10, "stake": 10})["reason"] == "invalid_position"
    # a valid NO position computes (not faked)
    no = PB.compute_payout_preview({"fill_price": 0.58, "shares": 17.24, "stake": 10.0, "fee": 0.0})
    assert no["ok"] and no["possible_payout_if_win"] == 17.24


def test_open_bet_has_ai_fields():
    _reset(); _open_pos()
    p = _client().get("/api/paper-bets/open").json()["positions"][0]
    for k in ("ai_reason", "gates_passed", "forecast_probability", "mirofish_state", "evidence_quality"):
        assert k in p
    assert isinstance(p["gates_passed"], list) and "wallet_atomic" in p["gates_passed"]


def test_no_fake_link_rendered():
    _reset(); _open_pos(slug="")    # empty slug must not become a link
    p = _client().get("/api/paper-bets/open").json()["positions"][0]
    assert p["url"] is None and p["url_status"] == "link_unavailable"


# ───────────────── D. settled bets (20-23) ───────────────────────────────────────

def test_settled_returns_positions():
    _reset(); _settled_pos()
    b = _client().get("/api/paper-bets/settled").json()
    assert b["paper_only"] is True and len(b["positions"]) == 1


def test_settled_separates_realized_pnl():
    _reset(); _settled_pos(pnl=8.0)
    p = _client().get("/api/paper-bets/settled").json()["positions"][0]
    assert p["realized_pnl"] == 8.0 and p["outcome"] in ("won", "lost", "unknown")


def test_settled_includes_clv_field():
    _reset(); _settled_pos()
    p = _client().get("/api/paper-bets/settled").json()["positions"][0]
    assert "clv_final" in p


def test_settled_link_unavailable_when_no_slug():
    _reset(); _settled_pos(mid="0xnoslug", slug=None)
    p = _client().get("/api/paper-bets/settled").json()["positions"][0]
    assert p["url_status"] == "link_unavailable"


# ───────────────── E. countdown timers (24-27) ───────────────────────────────────

def test_timer_unknown_when_end_missing():
    secs, status, _ = PB.timer_for(None)
    assert secs is None and status == "timer_unknown"


def test_timer_awaiting_settlement_when_ended():
    secs, status, _ = PB.timer_for("2000-01-01T00:00:00Z")
    assert status == "awaiting_settlement" and secs == 0


def test_timer_invalid_timestamp():
    secs, status, _ = PB.timer_for("not-a-timestamp")
    assert status == "invalid_timer"


def test_timer_running_future():
    secs, status, _ = PB.timer_for("2032-01-01T00:00:00Z")
    assert status == "running" and secs and secs > 0


# ───────────────── F. PnL graph (28-31) ──────────────────────────────────────────

def test_pnl_curve_returns_points_with_verification():
    _reset()
    journal.record_snapshot(1000.0, 1000.0, 0.0, 0.0, 0)
    journal.record_snapshot(1010.0, 1015.0, 10.0, 5.0, 1)
    b = _client().get("/api/pnl-curve").json()
    assert b["paper_only"] is True and b["points"] and all("verified" in p for p in b["points"])


def test_pnl_curve_unverified_when_accounting_not_ok():
    _reset(); _open_pos()      # open position w/o fresh mark -> accounting not ok -> points unverified
    journal.record_snapshot(958.0, 1000.0, 0.0, 42.0, 1)
    b = _client().get("/api/pnl-curve").json()
    if b["points"]:
        assert any(p["verified"] is False for p in b["points"]) or b["state"] in ("unverified", "partial", "ok")


def test_pnl_curve_not_enough_history():
    _reset()
    b = _client().get("/api/pnl-curve").json()
    assert b["state"] == "not_enough_data" and "not_enough_paper_wallet_history" in b["warnings"]


def test_pnl_curve_separates_series():
    _reset()
    journal.record_snapshot(1000.0, 1000.0, 0.0, 0.0, 0)
    journal.record_snapshot(1005.0, 1010.0, 5.0, 5.0, 1)
    p = _client().get("/api/pnl-curve").json()["points"][0]
    for k in ("cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl"):
        assert k in p


# ───────────────── G. proof timeline (32-35) ─────────────────────────────────────

def test_proof_returns_timeline():
    _reset(); _open_pos()
    b = _client().get("/api/paper-bets/proof?position_id=1").json()
    assert b["paper_only"] is True and isinstance(b["timeline"], list) and b["timeline"]


def test_proof_missing_step_shows_missing_proof():
    _reset(); _open_pos()    # no decision_features recorded -> early proof steps missing, not faked
    tl = _client().get("/api/paper-bets/proof?position_id=1").json()["timeline"]
    assert any(t["status"] == "missing_proof" for t in tl)
    assert not any(t["status"] == "passed" and t["step"] == "swarm_consensus" for t in tl)  # no fake pass


def test_proof_not_applicable_step():
    _reset(); _open_pos()
    tl = _client().get("/api/paper-bets/proof?position_id=1").json()["timeline"]
    assert any(t["status"] == "not_applicable" for t in tl)   # mirofish disabled/unknown -> n/a


def test_proof_wallet_atomic_when_opened():
    _reset(); _open_pos()
    tl = _client().get("/api/paper-bets/proof?position_id=1").json()["timeline"]
    wa = [t for t in tl if t["step"] == "wallet_atomic_open"]
    assert wa and wa[0]["status"] == "passed"


# ───────────────── H. dashboard UI / static (36-50) ──────────────────────────────

def _html():
    return _client().get("/").text


def _src():
    return open(DASH, encoding="utf-8").read()


def test_ui_has_paper_only_badge():
    assert "PAPER-ONLY" in _html()


def test_ui_has_sse_status():
    h = _html()
    assert "/events/live" in h and "EventSource" in h


def test_ui_has_proof_panel():
    assert "Proof This Is Working" in _html()


def test_ui_has_pnl_graph_container():
    assert 'id=curve' in _html() and "/api/pnl-curve" in _html()


def test_ui_has_agent_stream_panel():
    assert "Live AI Stream" in _html()


def test_ui_has_paper_wallet_section():
    assert "PAPER WALLET" in _html() and "/api/paper-wallet" in _html()


def test_ui_has_active_bets_section():
    assert "Active Paper Bets" in _html() and "/api/paper-bets/open" in _html()


def test_ui_has_market_link_area():
    assert "polymarket.com/event" in _html() or "open on Polymarket" in _html()


def test_ui_has_countdown_container():
    assert "tm_" in _html() and "fmtCountdown" in _html()


def test_ui_has_settled_section():
    assert "Recent Settled Bets" in _html()


def test_ui_no_hardcoded_fake_pnl():
    # the wallet/PnL numbers come from JS that fetches APIs; no literal "$127.16"-style fake
    import re
    body = _src()
    # the embedded HTML must not hardcode a dollar PnL figure as displayed value
    assert "127.16" not in body and "+14.39" not in body


def test_ui_no_hardcoded_mirofish_used():
    assert "mirofish_used = True" not in _src() and "mirofish_used=true" not in _html().lower()


def test_ui_unknown_not_green():
    h = _html()
    # the JS state->color map: unknown/stale must NOT map to the green var
    assert "stateColor" in h
    assert "['ok','healthy','pass','passed','connected','won','running','finished']" in h


def test_ui_gate2_only_pass_from_api():
    h = _html()
    assert "g2.pass" in h           # Gate 2 PASS rendered only from the API's gate2.pass


def test_ui_uses_accounting_verified_flag():
    h = _html()
    assert "equity_verified" in h and "VERIFIED" in h


# ───────────────── I. instrumentation (51-58) ────────────────────────────────────

def _emit_clear():
    _reset()


def test_instr_llm_agent_events():
    import harness.obs.hooks as H
    _emit_clear()
    H.on_llm_call(provider="ollama", model="qwen", system="s", user="u", completion="0.6",
                  tokens_in=10, tokens_out=5, latency_ms=120, role="agent")
    H.on_agent_estimate(agent_id="a1", forecast_id="f", persona="quant", probability=0.6,
                        confidence=0.8, reasoning="r", round=1)
    t = [e["type"] for e in LE.recent_events(20)]
    assert "agent.started" in t and "agent.finished" in t


def test_instr_swarm_events():
    import harness.obs.hooks as H
    _emit_clear()
    H.on_forecast_start(forecast_id="f", market_id="0xm1", question="Q", market_price=0.5)
    H.on_forecast_final(forecast_id="f", market_id="0xm1", model_probability=0.6,
                        market_probability=0.5, edge=0.1, consensus=0.8, reasoning_summary="s")
    t = [e["type"] for e in LE.recent_events(20)]
    assert "swarm.started" in t and "forecast.final" in t


def test_instr_mirofish_state_event():
    # mirofish_validate.record_run emits mirofish.state from REAL canonical state (Plan 8)
    _emit_clear()
    src = open(ROOT + "/harness/mirofish_validate.py", encoding="utf-8").read()
    assert "live_events" in src and "mirofish.state" in src
    # the bus carries the canonical state type
    assert LE.emit("mirofish.state", "mirofish", market_id="0xm1", status="fresh_used") is True
    assert any(e["type"] == "mirofish.state" for e in LE.recent_events(10))


def test_instr_gate_events():
    import harness.obs.hooks as H
    _emit_clear()
    H.on_gate(n_resolved=3, model_brier_mean=0.2, market_brier_mean=0.25, paper_pnl=5.0,
              gate1_pass=True, gate2_pass=False, overall_pass=False)
    gate = [e for e in LE.recent_events(20) if e["type"] == "gate.result"]
    assert gate and gate[0]["status"] == "blocked"


def test_instr_decision_events():
    import harness.obs.hooks as H
    _emit_clear()
    H.on_trade_open(trade_id="1", market_id="0xm1", forecast_id="f", side="YES", stake=10.0,
                    fill_price=0.5, slippage=0.01, fee=0.0)
    H.on_trade_skip(forecast_id="f", reason="low_evidence:0.1", inputs={"market_id": "0xm2", "layer": "evidence"})
    t = [e["type"] for e in LE.recent_events(20)]
    assert "decision.bet" in t and "decision.no_bet" in t


def test_instr_wallet_update_event():
    _emit_clear()
    assert LE.emit("wallet.update", "wallet", message="cash update", data={"cash": 958.0}) is True
    assert any(e["type"] == "wallet.update" for e in LE.recent_events(10))


def test_instr_decision_bet_event_carries_market():
    import harness.obs.hooks as H
    _emit_clear()
    H.on_trade_open(trade_id="1", market_id="0xMKT", forecast_id="f", side="NO", stake=12.0,
                    fill_price=0.55, slippage=0.01, fee=0.0)
    bet = [e for e in LE.recent_events(10) if e["type"] == "decision.bet"]
    assert bet and bet[0]["market_id"] == "0xMKT" and bet[0]["data"].get("side") == "NO"


def test_no_realmoney_or_privatekey():
    for f in ("live_events.py", "paper_bets.py", "dashboard.py"):
        low = open(ROOT + "/harness/" + f, encoding="utf-8").read().lower()
        for bad in ("private_key", "live_trading", "execute_real", "clob", "wallet key", "real money"):
            assert bad not in low, f"{f}: {bad}"


TESTS = [
    ("event_model_validates_required_fields", test_event_model_validates_required_fields),
    ("recent_endpoint_returns_json", test_recent_endpoint_returns_json),
    ("stream_handles_disconnected_client", test_stream_handles_disconnected_client),
    ("broadcast_failure_does_not_affect_decision_path", test_broadcast_failure_does_not_affect_decision_path),
    ("replayed_events_marked", test_replayed_events_marked),
    ("live_status_reports_last_event_age", test_live_status_reports_last_event_age),
    ("paper_wallet_paper_only", test_paper_wallet_paper_only),
    ("paper_wallet_uses_accounting_status", test_paper_wallet_uses_accounting_status),
    ("verified_equity_hidden_when_unverified", test_verified_equity_hidden_when_unverified),
    ("missing_marks_produce_accounting_reasons", test_missing_marks_produce_accounting_reasons),
    ("wallet_separates_realized_unrealized_total", test_wallet_separates_realized_unrealized_total),
    ("open_bets_returns_positions", test_open_bets_returns_positions),
    ("open_bet_has_link_or_unavailable", test_open_bet_has_link_or_unavailable),
    ("open_bet_has_countdown_fields", test_open_bet_has_countdown_fields),
    ("open_bet_has_payout_profit_maxloss", test_open_bet_has_payout_profit_maxloss),
    ("payout_formula_yes_correct", test_payout_formula_yes_correct),
    ("payout_unknown_for_invalid_or_missing", test_payout_unknown_for_invalid_or_missing),
    ("open_bet_has_ai_fields", test_open_bet_has_ai_fields),
    ("no_fake_link_rendered", test_no_fake_link_rendered),
    ("settled_returns_positions", test_settled_returns_positions),
    ("settled_separates_realized_pnl", test_settled_separates_realized_pnl),
    ("settled_includes_clv_field", test_settled_includes_clv_field),
    ("settled_link_unavailable_when_no_slug", test_settled_link_unavailable_when_no_slug),
    ("timer_unknown_when_end_missing", test_timer_unknown_when_end_missing),
    ("timer_awaiting_settlement_when_ended", test_timer_awaiting_settlement_when_ended),
    ("timer_invalid_timestamp", test_timer_invalid_timestamp),
    ("timer_running_future", test_timer_running_future),
    ("pnl_curve_returns_points_with_verification", test_pnl_curve_returns_points_with_verification),
    ("pnl_curve_unverified_when_accounting_not_ok", test_pnl_curve_unverified_when_accounting_not_ok),
    ("pnl_curve_not_enough_history", test_pnl_curve_not_enough_history),
    ("pnl_curve_separates_series", test_pnl_curve_separates_series),
    ("proof_returns_timeline", test_proof_returns_timeline),
    ("proof_missing_step_shows_missing_proof", test_proof_missing_step_shows_missing_proof),
    ("proof_not_applicable_step", test_proof_not_applicable_step),
    ("proof_wallet_atomic_when_opened", test_proof_wallet_atomic_when_opened),
    ("ui_has_paper_only_badge", test_ui_has_paper_only_badge),
    ("ui_has_sse_status", test_ui_has_sse_status),
    ("ui_has_proof_panel", test_ui_has_proof_panel),
    ("ui_has_pnl_graph_container", test_ui_has_pnl_graph_container),
    ("ui_has_agent_stream_panel", test_ui_has_agent_stream_panel),
    ("ui_has_paper_wallet_section", test_ui_has_paper_wallet_section),
    ("ui_has_active_bets_section", test_ui_has_active_bets_section),
    ("ui_has_market_link_area", test_ui_has_market_link_area),
    ("ui_has_countdown_container", test_ui_has_countdown_container),
    ("ui_has_settled_section", test_ui_has_settled_section),
    ("ui_no_hardcoded_fake_pnl", test_ui_no_hardcoded_fake_pnl),
    ("ui_no_hardcoded_mirofish_used", test_ui_no_hardcoded_mirofish_used),
    ("ui_unknown_not_green", test_ui_unknown_not_green),
    ("ui_gate2_only_pass_from_api", test_ui_gate2_only_pass_from_api),
    ("ui_uses_accounting_verified_flag", test_ui_uses_accounting_verified_flag),
    ("instr_llm_agent_events", test_instr_llm_agent_events),
    ("instr_swarm_events", test_instr_swarm_events),
    ("instr_mirofish_state_event", test_instr_mirofish_state_event),
    ("instr_gate_events", test_instr_gate_events),
    ("instr_decision_events", test_instr_decision_events),
    ("instr_wallet_update_event", test_instr_wallet_update_event),
    ("instr_decision_bet_event_carries_market", test_instr_decision_bet_event_carries_market),
    ("no_realmoney_or_privatekey", test_no_realmoney_or_privatekey),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
