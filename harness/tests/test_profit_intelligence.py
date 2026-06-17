"""Plan 11 — PAPER-ONLY profit intelligence.

Proves profit intelligence improves ranking / explanation / learning while being PROVABLY
unable to: open a position, bypass a safety gate, leak future/settlement data into pre-forecast
ranking, or claim profitability without Gate 2. Temp DB only — NO live APIs / daemons / DB.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from harness.tests._util import make_temp_env, patched, run_as_main  # noqa: E402

make_temp_env("ps_pi_")

from harness import opportunity_ranker as RANK      # noqa: E402
from harness import decision_features as DF          # noqa: E402
from harness import edge_explainer as EXP            # noqa: E402
from harness import profit_intel as PI               # noqa: E402
from harness import wallet                           # noqa: E402
from harness import journal                          # noqa: E402


def _db():
    return os.environ["DATABASE_URL"]


def _reset():
    conn = sqlite3.connect(_db())
    for t in ("paper_positions", "decisions", "decision_features", "paper_wallet", "mirofish_runs"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit(); conn.close()
    wallet.init_wallet(1000.0)
    journal.init_journal()
    DF.init_features(_db())


def _settled(market_id, q, pnl, *, event_slug=None, stake=10.0, status="settled"):
    conn = sqlite3.connect(_db())
    conn.execute(
        "INSERT INTO paper_positions (market_id, question, side, model_p, market_p, edge, stake, "
        "fill_price, shares, fee, status, realized_pnl, event_slug, opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (market_id, q, "YES", 0.6, 0.5, 0.1, stake, 0.5, stake / 0.5, 0.0, status, pnl,
         event_slug, "2026-06-01T00:00:00"))
    conn.commit(); conn.close()


# ─────────────────────── A. candidate ranking (1-5) ───────────────────────────────

def _cand(**kw):
    base = {"market_id": "m", "question": "Will X happen?", "_label": "opinion",
            "_hl": 12.0, "liquidity": 6000.0, "volume": 20000.0, "outcome_prices": [0.5, 0.5]}
    base.update(kw)
    return base


def test_high_quality_ranks_higher():
    good = _cand(market_id="good", liquidity=7000.0, volume=30000.0, outcome_prices=[0.5, 0.5], _hl=12.0)
    poor = _cand(market_id="poor", liquidity=300.0, volume=600.0, outcome_prices=[0.42, 0.62], _hl=0.6)
    ranked = RANK.rank_candidates([poor, good])
    by = {r["market_id"]: r for r in ranked}
    assert by["good"]["rank_score"] > by["poor"]["rank_score"]
    assert ranked[0]["market_id"] == "good" and by["good"]["pre_forecast_only"] is True


def test_stale_candidate_blocked():
    r = RANK.rank_one(_cand(market_id="s", _stale=True, _stale_reason="no_recent_trade"))
    assert r["rank_bucket"] == "blocked" and r["rank_score"] == 0.0
    assert any("stale" in x for x in r["rank_reasons"])


def test_missing_data_cannot_rank_high():
    r = RANK.rank_one({"market_id": "bare", "question": "Q", "_label": "opinion"})  # no liq/vol/spread/time
    assert r["rank_bucket"] != "high" and r["rank_score"] < RANK._HIGH
    assert any("unknown" in x for x in r["rank_reasons"])


def test_ranking_ignores_future_outcome_fields():
    clean = _cand(market_id="x")
    leaked = _cand(market_id="x", outcome=1, payout=999.0, realized_pnl=500.0,
                   settled_at="2026-06-02", won=True, clv=0.5, final_price=0.99)
    assert RANK.rank_one(clean)["rank_score"] == RANK.rank_one(leaked)["rank_score"]


def test_ranking_cannot_override_safety_block():
    # a deterministic safety block (observe-only label backtest) is ranked blocked, never promoted
    r = RANK.rank_one(_cand(market_id="ob", _observe_only=True, liquidity=99999.0, volume=99999.0))
    assert r["rank_bucket"] == "blocked"


# ─────────────────────── B. decision features (6-10) ──────────────────────────────

def test_bet_decision_records_snapshot():
    _reset()
    DF.record_decision(action=DF.BET, reason="bet:edge", source="predict_today",
                       market_id="b1", question="Q", forecast_probability=0.62, price=0.5,
                       side="YES", edge_raw=0.12, edge_after_costs=0.08, db_path=_db())
    rows = DF.get_features(db_path=_db(), action="bet")
    assert len(rows) == 1 and rows[0]["market_id"] == "b1" and rows[0]["action"] == "bet"


def test_no_bet_decision_records_snapshot():
    _reset()
    DF.record_decision(action=DF.NO_BET, reason="no_data", source="predict_today",
                       market_id="n1", question="Q", db_path=_db())
    rows = DF.get_features(db_path=_db(), action="no_bet")
    assert len(rows) == 1 and rows[0]["reason"] == "no_data"


def test_snapshot_includes_paper_only():
    snap = DF.build_snapshot(market_id="m", action="bet")
    assert snap["paper_only"] is True
    _reset()
    DF.record_decision(action=DF.BET, market_id="p1", db_path=_db())
    assert DF.get_features(db_path=_db())[0]["paper_only"] == 1


def test_snapshot_includes_mirofish_state():
    snap = DF.build_snapshot(market_id="m", action="bet", mirofish_state="fresh_used",
                             mirofish_used=True, mirofish_contribution=0.3)
    assert snap["mirofish_state"] == "fresh_used" and snap["mirofish_used"] is True
    # also flows from a candidate dict's Plan-8 _mf_status
    _reset()
    DF.record_decision({"market_id": "mf", "_mf_status": {"state": "stale_result", "mirofish_used": False}},
                       action=DF.NO_BET, reason="mirofish_required_stale_no_bet", db_path=_db())
    r = DF.get_features(db_path=_db())[0]
    assert r["mirofish_state"] == "stale_result" and r["mirofish_used"] == 0


def test_snapshot_includes_accounting_and_gate2():
    snap = DF.build_snapshot(market_id="m", action="no_bet", accounting_status="drift",
                             gate2_status="fail")
    assert snap["accounting_status"] == "drift" and snap["gate2_status"] == "fail"
    _reset()
    DF.record_decision(action=DF.BET, market_id="ag", accounting_status="ok",
                       gate2_status="unknown", db_path=_db())
    r = DF.get_features(db_path=_db())[0]
    assert r["accounting_status"] == "ok" and r["gate2_status"] == "unknown"


# ─────────────────────── C. edge explanation (11-15) ──────────────────────────────

def test_explain_returns_factor_lists():
    e = EXP.explain_edge({"action": "bet", "side": "YES", "forecast_probability": 0.65,
                          "price": 0.5, "edge_raw": 0.15, "edge_after_costs": 0.09,
                          "consensus": 0.7, "evidence_quality": 0.6, "liquidity": 5000})
    assert isinstance(e["positive_factors"], list) and isinstance(e["negative_factors"], list)
    assert e["positive_factors"] and e["paper_only"] is True


def test_negative_ev_explains_no_bet():
    e = EXP.explain_edge({"action": "no_bet", "reason": "non-positive EV after costs",
                          "edge_raw": 0.03, "edge_after_costs": -0.01})
    assert e["why_no_bet"] and "not a missed win" in e["why_no_bet"].lower()   # framed as useful
    assert any("after-cost" in f for f in e["negative_factors"])


def test_low_evidence_explains_no_bet():
    e = EXP.explain_edge({"action": "no_bet", "reason": "low_evidence:0.12", "evidence_quality": 0.12})
    assert e["why_no_bet"] and any("evidence" in f for f in e["negative_factors"])


def test_no_unsafe_profit_language():
    blob = []
    for d in ({"action": "bet", "edge_after_costs": 0.5, "consensus": 0.99, "forecast_probability": 0.95, "price": 0.45},
              {"action": "no_bet", "reason": "no_edge"},
              {"action": "bet", "edge_raw": 0.2, "side": "NO"}):
        e = EXP.explain_edge(d)
        blob.append(" ".join(str(v) for v in e.values()))
    text = " ".join(blob).lower()
    for banned in EXP.BANNED_PHRASES:
        assert banned not in text, f"unsafe language leaked: {banned}"


def test_after_cost_ev_shown():
    e = EXP.explain_edge({"action": "bet", "edge_raw": 0.15, "edge_after_costs": 0.08})
    joined = " ".join(e["positive_factors"] + e["negative_factors"])
    assert "after-cost" in joined and "8.0%" in joined        # after-cost EV surfaced, not just raw


# ─────────────────────── D. no-bet intelligence (16-20) ───────────────────────────

def _seed_no_bets():
    _reset()
    for mid, why in [("a", "no_data"), ("a", "low_evidence:0.1"), ("b", "non-positive EV after costs"),
                     ("c", "swarm_degraded"), ("c", "swarm_degraded"), ("c", "consensus 0.40 < 0.50"),
                     ("d", "mirofish_required_stale_no_bet"), ("e", "stale: expired")]:
        journal.record_decision(mid, "Q", 0.5, 0.5, None, None, 0.0, None, "", "guard", "no_bet",
                                f"Guard skip: {why}.")
    journal.record_decision("z", "Q", 0.6, 0.5, 0.1, "YES", 10.0, 0.5, "swarm", "LONG", "bet", "a bet")


def test_no_bets_grouped_by_reason():
    _seed_no_bets()
    s = PI.summarize_no_bets(db_path=_db())
    assert s["by_reason"].get("evidence_low", 0) >= 2 and s["by_reason"].get("degraded_swarm", 0) >= 2


def test_top_blockers_computed():
    _seed_no_bets()
    s = PI.summarize_no_bets(db_path=_db())
    assert s["top_blockers"] and s["top_blockers"][0]["n"] >= s["top_blockers"][-1]["n"]


def test_no_bet_not_counted_as_trade():
    _seed_no_bets()
    s = PI.summarize_no_bets(db_path=_db())
    assert s["total_no_bets"] == 8        # the one 'bet' row is excluded


def test_recommendations_never_loosen_gates():
    _seed_no_bets()
    s = PI.summarize_no_bets(db_path=_db())
    for sug in s["suggestions"]:
        assert sug["loosens_safety_gate"] is False
        assert "loosen" not in sug["suggestion"].lower() and "bypass" not in sug["suggestion"].lower()
        assert "disable" not in sug["suggestion"].lower()


def test_repeated_market_no_bets_grouped():
    _seed_no_bets()
    s = PI.summarize_no_bets(db_path=_db())
    rep = {r["market_id"]: r["n"] for r in s["repeated_markets"]}
    assert rep.get("a") == 2 and rep.get("c") == 3


# ─────────────────────── E. post-trade learning (21-25) ───────────────────────────

def test_insufficient_sample_blocks_claims():
    _reset()
    _settled("s1", "Q", 5.0)
    s = PI.summarize_post_trade_learning(db_path=_db())
    assert s["insufficient_sample"] is True and s["performance_claim"] == "insufficient_sample"
    assert s["profitable_claim_allowed"] is False


def test_accounting_unverified_blocks_claim():
    _reset()
    for i in range(25):
        _settled(f"s{i}", "Q", 1.0)
    conn = sqlite3.connect(_db()); conn.execute("UPDATE paper_wallet SET cash = cash + 500 WHERE id=1")
    conn.commit(); conn.close()
    s = PI.summarize_post_trade_learning(db_path=_db())
    assert s["accounting_unverified"] is True and s["performance_claim"] != "gate2_pass_paper"


def test_stale_clv_blocks_clv_claim():
    _reset()
    _settled("s1", "Q", 5.0)
    s = PI.summarize_post_trade_learning(db_path=_db())
    assert s["clv_unverified"] is True and s["clv"]["overall"] is None


def test_realized_unrealized_total_separated():
    _reset()
    _settled("s1", "Q", 5.0)
    s = PI.summarize_post_trade_learning(db_path=_db())
    assert "realized_pnl" in s and "unrealized_pnl" in s and "total_pnl" in s


def test_open_marks_separate_from_settled():
    _reset()
    _settled("o1", "Q", 0.0, status="open")
    _settled("o2", "Q", 0.0, status="open")
    _settled("s1", "Q", 3.0)
    _settled("s2", "Q", -2.0)
    s = PI.summarize_post_trade_learning(db_path=_db())
    assert s["open_position_count"] == 2 and s["sample_size"] == 2


def test_theme_pnl_withholds_small_sample_winrate():
    # ADVERSARIAL: a single/tiny-sample theme must NOT show a '100% win rate' result.
    _reset()
    _settled("t1", "Will Bitcoin hit 100k this year?", 5.0)
    _settled("t2", "Will Bitcoin hit 100k this year?", 3.0)   # n=2 < MIN_SEGMENT_N
    tp = PI.summarize_post_trade_learning(db_path=_db())["by_theme_pnl"]
    assert tp, "expected a theme bucket"
    for theme, d in tp.items():
        assert d["win_rate"] is None and d["win_rate_available"] is False
        assert "insufficient_sample" in (d.get("warning") or "")


# ─────────────────────── F. attribution (26-30) ───────────────────────────────────

def _seed_attrib():
    _reset()
    _settled("e1", "Q", 5.0, event_slug="elec")
    _settled("e2", "Q", -2.0, event_slug="elec")
    _settled("n1", "Q", 3.0)
    DF.record_decision(action=DF.BET, market_id="e1", source="predict_today", liquidity=6000,
                       spread=0.01, consensus=0.8, db_path=_db())
    DF.record_decision(action=DF.BET, market_id="n1", source="sameday", liquidity=400,
                       spread=0.06, consensus=0.4, db_path=_db())


def test_display_surfaces_withhold_small_sample_winrate():
    # ADVERSARIAL r2: the guard must reach EVERY theme-pnl DISPLAY surface (command_center /
    # metrics / scoreboard), not just profit_intel — no '100% win rate' from n=1 on the dashboard.
    _reset()
    _settled("d1", "Will the Fed cut rates in March?", 5.0)
    _settled("d2", "Will the Fed cut rates in March?", 4.0)   # n=2 < MIN_SEGMENT_N
    from harness import command_center as CC
    from harness import metrics as MET
    from harness import scoreboard as SB
    surfaces = [lambda: CC.theme_label_performance().get("by_theme", {}),
                lambda: MET.full_report().get("theme_pnl", {}),
                lambda: SB.profitability_report().get("theme_pnl", {})]
    for surf in surfaces:
        tp = surf() or {}
        for theme, d in tp.items():
            assert d.get("win_rate") is None, f"small-sample win_rate leaked: {theme}={d}"


def test_attribution_source_uses_sample_sizes():
    _seed_attrib()
    a = PI.attribution(db_path=_db())
    assert "by_source" in a and all("n" in v for v in a["by_source"].values())


def test_attribution_mirofish_segmented():
    _seed_attrib()
    with patched(PI, "_mirofish_used_index", lambda db_path=None: {"e1": True}):
        a = PI.attribution(db_path=_db())
    assert "used" in a["by_mirofish_used"] and "not_used" in a["by_mirofish_used"]


def test_attribution_bucket_segmentation():
    _seed_attrib()
    a = PI.attribution(db_path=_db())
    assert a["by_liquidity_bucket"] and a["by_spread_bucket"]
    assert "deep(>=5k)" in a["by_liquidity_bucket"] or "thin(<1k)" in a["by_liquidity_bucket"]


def test_attribution_event_vs_non_event():
    _seed_attrib()
    a = PI.attribution(db_path=_db())
    assert "event" in a["by_event"] and "non_event" in a["by_event"]
    assert a["by_event"]["event"]["n"] == 2 and a["by_event"]["non_event"]["n"] == 1


def test_attribution_insufficient_sample_warnings():
    _seed_attrib()
    a = PI.attribution(db_path=_db())
    # tiny segments must withhold win-rate and carry a warning
    ev = a["by_event"]["event"]
    assert ev["win_rate"] is None and ev["warning"] and "insufficient_sample" in ev["warning"]


# ─────────────────────── G. dashboard / report (31-35) ────────────────────────────

def test_report_returns_paper_only():
    _reset()
    rep = PI.profit_intelligence_report(db_path=_db())
    assert rep["paper_only"] is True


def test_report_returns_accounting_status():
    _reset()
    rep = PI.profit_intelligence_report(db_path=_db())
    assert "accounting_status" in rep


def test_report_returns_gate2_status():
    _reset()
    rep = PI.profit_intelligence_report(db_path=_db())
    assert "gate2_status" in rep and "gate2_pass" in rep


def test_report_not_profitable_unless_gate2_pass():
    _reset()
    _settled("s1", "Q", 5.0)
    rep = PI.profit_intelligence_report(db_path=_db())
    assert rep["gate2_pass"] is False and rep["profitable_claim_allowed"] is False
    # with no Gate-2 pass the headline is an HONEST learning state, never a profitability claim
    assert rep["headline"] in ("insufficient_sample", "accounting_unverified", "learning")
    assert rep["post_trade_learning"]["performance_claim"] != "gate2_pass_paper"
    assert any("Gate 2 not pass" in w for w in rep["warnings"])


def test_report_missing_db_does_not_crash():
    missing = os.path.join(os.path.dirname(_db()), "no_such_pi.db")
    rep = PI.profit_intelligence_report(db_path=missing)
    assert isinstance(rep, dict) and rep["paper_only"] is True


# ─────────────────────── H. static scans (36-40) ──────────────────────────────────

def _src(name):
    return open(os.path.join(ROOT, "harness", name), encoding="utf-8").read()


PI_MODULES = ("opportunity_ranker.py", "profit_intel.py", "decision_features.py", "edge_explainer.py")


def test_static_ranker_never_opens_position():
    # the ranker must never CALL into the wallet (docstring may mention it; an import/call may not)
    src = _src("opportunity_ranker.py")
    assert "import wallet" not in src and "from harness import wallet" not in src
    assert "wallet.open_position" not in src and ".open_position(" not in src


def test_static_profit_intel_never_bypasses_safe_bet():
    # check for CALLS, not the metric name 'open_position_count' or docstrings naming the rule
    for m in PI_MODULES:
        src = _src(m)
        assert "safe_bet(" not in src
        assert "wallet.open_position" not in src and ".open_position(" not in src


def test_static_no_unsafe_profit_wording():
    # the three data modules contain NO unsafe profit language anywhere.
    for m in ("opportunity_ranker.py", "profit_intel.py", "decision_features.py"):
        low = _src(m).lower()
        for banned in ("guaranteed", "free money", "safe profit", "risk-free"):
            assert banned not in low, f"{m}: {banned}"
    # edge_explainer names them ONLY to DEFINE what is forbidden (BANNED_PHRASES) — and a
    # behavioural test (no_unsafe_profit_language) proves the OUTPUT never contains them.
    assert all(b in EXP.BANNED_PHRASES for b in ("guaranteed", "free money", "safe profit", "risk-free"))


def test_static_no_future_fields_in_ranking():
    # prove the ranking code never ACCESSES a settlement/outcome field (bare names in the
    # FUTURE_FIELDS / PRE_FORECAST_FIELDS *definitions* are documentation, not accesses).
    future = ("outcome", "payout", "realized_pnl", "settled_at", "won", "clv", "final_price",
              "close_price", "resolution", "result")
    src = _src("opportunity_ranker.py")
    for f in future:
        for access in (f'.get("{f}")', f".get('{f}')", f'["{f}"]', f"['{f}']"):
            assert access not in src, f"future field ACCESSED in ranking: {access}"


def test_static_paper_only_present():
    for m in PI_MODULES:
        src = _src(m)
        assert "PAPER_ONLY_PROFIT_INTELLIGENCE" in src and "paper_only" in src


TESTS = [
    ("high_quality_ranks_higher", test_high_quality_ranks_higher),
    ("stale_candidate_blocked", test_stale_candidate_blocked),
    ("missing_data_cannot_rank_high", test_missing_data_cannot_rank_high),
    ("ranking_ignores_future_outcome_fields", test_ranking_ignores_future_outcome_fields),
    ("ranking_cannot_override_safety_block", test_ranking_cannot_override_safety_block),
    ("bet_decision_records_snapshot", test_bet_decision_records_snapshot),
    ("no_bet_decision_records_snapshot", test_no_bet_decision_records_snapshot),
    ("snapshot_includes_paper_only", test_snapshot_includes_paper_only),
    ("snapshot_includes_mirofish_state", test_snapshot_includes_mirofish_state),
    ("snapshot_includes_accounting_and_gate2", test_snapshot_includes_accounting_and_gate2),
    ("explain_returns_factor_lists", test_explain_returns_factor_lists),
    ("negative_ev_explains_no_bet", test_negative_ev_explains_no_bet),
    ("low_evidence_explains_no_bet", test_low_evidence_explains_no_bet),
    ("no_unsafe_profit_language", test_no_unsafe_profit_language),
    ("after_cost_ev_shown", test_after_cost_ev_shown),
    ("no_bets_grouped_by_reason", test_no_bets_grouped_by_reason),
    ("top_blockers_computed", test_top_blockers_computed),
    ("no_bet_not_counted_as_trade", test_no_bet_not_counted_as_trade),
    ("recommendations_never_loosen_gates", test_recommendations_never_loosen_gates),
    ("repeated_market_no_bets_grouped", test_repeated_market_no_bets_grouped),
    ("insufficient_sample_blocks_claims", test_insufficient_sample_blocks_claims),
    ("accounting_unverified_blocks_claim", test_accounting_unverified_blocks_claim),
    ("stale_clv_blocks_clv_claim", test_stale_clv_blocks_clv_claim),
    ("realized_unrealized_total_separated", test_realized_unrealized_total_separated),
    ("open_marks_separate_from_settled", test_open_marks_separate_from_settled),
    ("theme_pnl_withholds_small_sample_winrate", test_theme_pnl_withholds_small_sample_winrate),
    ("display_surfaces_withhold_small_sample_winrate", test_display_surfaces_withhold_small_sample_winrate),
    ("attribution_source_uses_sample_sizes", test_attribution_source_uses_sample_sizes),
    ("attribution_mirofish_segmented", test_attribution_mirofish_segmented),
    ("attribution_bucket_segmentation", test_attribution_bucket_segmentation),
    ("attribution_event_vs_non_event", test_attribution_event_vs_non_event),
    ("attribution_insufficient_sample_warnings", test_attribution_insufficient_sample_warnings),
    ("report_returns_paper_only", test_report_returns_paper_only),
    ("report_returns_accounting_status", test_report_returns_accounting_status),
    ("report_returns_gate2_status", test_report_returns_gate2_status),
    ("report_not_profitable_unless_gate2_pass", test_report_not_profitable_unless_gate2_pass),
    ("report_missing_db_does_not_crash", test_report_missing_db_does_not_crash),
    ("static_ranker_never_opens_position", test_static_ranker_never_opens_position),
    ("static_profit_intel_never_bypasses_safe_bet", test_static_profit_intel_never_bypasses_safe_bet),
    ("static_no_unsafe_profit_wording", test_static_no_unsafe_profit_wording),
    ("static_no_future_fields_in_ranking", test_static_no_future_fields_in_ranking),
    ("static_paper_only_present", test_static_paper_only_present),
]

if __name__ == "__main__":
    sys.exit(run_as_main(TESTS))
