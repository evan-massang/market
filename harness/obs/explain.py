"""obs.explain — decision-trail reconstruction (explain / replay).

Acceptance criterion 2: the event log + content-addressed blobs are only useful
if a human can reconstruct *why* a given market was traded the way it was. This
module joins events across ALL run logs on their correlation IDs and renders the
complete pipeline trail as a readable, evidentiary string.

    explain(market_id)   -> the COMPLETE trail for a market, joining every event
                            (across every run file) that belongs to that market
                            and to the forecasts/trades it spawned.
    replay(forecast_id)  -> the same trail scoped to ONE forecast_id (every agent
                            prompt/response, debate round, blend math, sizing math,
                            the paper fill, settlement, and both Brier scores).

For llm.call lines the heavy text lives in blobs (only the hash is in the JSONL);
explain/replay pull the FULL prompt + completion back from
``obs.blobs.read_blob(...)`` on demand so the rendered trail is self-contained.

Join model
----------
Events carry correlation IDs (run_id, market_id, forecast_id, trade_id, agent_id,
llm_call_id, role). A forecast belongs to exactly one market; a trade belongs to
exactly one forecast/market. So:
  * explain(M) seeds {market_id == M}, then fix-points outward over forecast_id
    and trade_id WITHOUT ever adding a second market_id -> captures everything for
    M and nothing from other markets.
  * replay(F) seeds {forecast_id == F}, derives that forecast's market_id(s) and
    trade_id(s) directly (no fix-point), then folds in the market-singleton
    context events (data.fetch / classify.decision / resolution.observed) by
    market_id -- so sibling forecasts of the same market are never pulled in.

Everything here is read-only and fully guarded: a malformed line is skipped, a
missing blob is reported inline, and no entry point raises.

CLI:
    python -m harness.obs.explain explain <market_id>
    python -m harness.obs.explain replay  <forecast_id>
    python -m harness.obs.explain selftest         # isolated temp self-test
"""

import glob
import json
import os
import sys

from . import config
from . import blobs


# ── envelope / correlation-id vocabulary ─────────────────────────────────────--
# Order matters: this is the order IDs are printed on each event's `ids:` line.
ID_KEYS = (
    "run_id",
    "cycle_id",
    "market_id",
    "forecast_id",
    "trade_id",
    "agent_id",
    "role",
    "llm_call_id",
)

# Envelope keys rendered specially (header / ids line) and therefore excluded
# from the generic payload dump.
_ENVELOPE = {"event", "ts", "level", "schema_version", "prev_hash"} | set(ID_KEYS)

# Pipeline phase ranks (the canonical causal order from the spec). Events sharing
# a rank are ordered by timestamp, then by (file, line) to preserve emission order
# on a timestamp tie. Unknown events sort late but before run.end/error.
_RANK = {
    "run.start": 0,
    "data.fetch": 1,
    "classify.decision": 2,
    "forecast.start": 3,
    "agent.estimate": 4,
    "llm.call": 4,
    "debate.round": 5,
    "blend.compute": 6,
    "forecast.final": 7,
    "sizing.decision": 8,
    "trade.open": 9,
    "trade.skip": 9,
    "resolution.observed": 10,
    "trade.settle": 11,
    "score.brier": 12,
    "gate.eval": 13,
    "run.end": 14,
    "error": 16,
}
_RANK_UNKNOWN = 15

# Market-singleton context events: pulled into a replay() by market_id (they have
# no forecast_id of their own, yet they belong to the forecast's market).
_MARKET_CONTEXT = {"data.fetch", "classify.decision", "resolution.observed"}


# ── loading ──────────────────────────────────────────────────────────────────--
def _load_events():
    """Parse every events_dir()/*.jsonl line into dicts.

    Returns (events, stats) where each event dict is annotated with private
    ``_file`` / ``_lineno`` keys (used only for stable ordering) and stats is
    {'files': n, 'lines': n, 'parsed': n, 'skipped': n}.
    Never raises; *.head sidecars are ignored.
    """
    events = []
    stats = {"files": 0, "lines": 0, "parsed": 0, "skipped": 0}
    try:
        pattern = os.path.join(str(config.events_dir()), "*.jsonl")
        files = sorted(glob.glob(pattern))
    except Exception:
        files = []
    for path in files:
        stats["files"] += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, raw in enumerate(f):
                    if not raw.strip():
                        continue
                    stats["lines"] += 1
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            obj["_file"] = path
                            obj["_lineno"] = lineno
                            events.append(obj)
                            stats["parsed"] += 1
                        else:
                            stats["skipped"] += 1
                    except Exception:
                        stats["skipped"] += 1
        except Exception:
            continue
    return events, stats


def _sort_key(e):
    rank = _RANK.get(e.get("event"), _RANK_UNKNOWN)
    return (rank, e.get("ts") or "", e.get("_file") or "", e.get("_lineno") or 0)


# ── selection ────────────────────────────────────────────────────────────────--
def _select_for_market(events, market_id):
    """Fix-point selection seeded by a single market_id.

    market_id stays pinned to the target; forecast_id / trade_id grow until
    stable. Because a forecast/trade belongs to exactly one market this captures
    the full market trail without leaking other markets.
    """
    forecast_ids = set()
    trade_ids = set()
    changed = True
    while changed:
        changed = False
        for e in events:
            mid = e.get("market_id")
            fid = e.get("forecast_id")
            tid = e.get("trade_id")
            related = (
                mid == market_id
                or (fid is not None and fid in forecast_ids)
                or (tid is not None and tid in trade_ids)
            )
            if not related:
                continue
            if fid is not None and fid not in forecast_ids:
                forecast_ids.add(fid)
                changed = True
            if tid is not None and tid not in trade_ids:
                trade_ids.add(tid)
                changed = True

    selected = [
        e
        for e in events
        if e.get("market_id") == market_id
        or (e.get("forecast_id") is not None and e.get("forecast_id") in forecast_ids)
        or (e.get("trade_id") is not None and e.get("trade_id") in trade_ids)
    ]
    return selected, {"market_id": {market_id}, "forecast_id": forecast_ids,
                      "trade_id": trade_ids}


def _select_for_forecast(events, forecast_id):
    """Selection scoped strictly to one forecast_id (no market-wide fix-point).

    Derives the forecast's market_id(s) and trade_id(s) directly, then folds in
    market-singleton context events (data.fetch / classify.decision /
    resolution.observed) by market_id. Sibling forecasts are never included.
    """
    market_ids = set()
    trade_ids = set()
    for e in events:
        if e.get("forecast_id") == forecast_id:
            if e.get("market_id") is not None:
                market_ids.add(e.get("market_id"))
            if e.get("trade_id") is not None:
                trade_ids.add(e.get("trade_id"))

    selected = []
    for e in events:
        ev = e.get("event")
        if e.get("forecast_id") == forecast_id:
            selected.append(e)
        elif e.get("trade_id") is not None and e.get("trade_id") in trade_ids:
            selected.append(e)
        elif ev in _MARKET_CONTEXT and e.get("market_id") in market_ids:
            selected.append(e)
    return selected, {"market_id": market_ids, "forecast_id": {forecast_id},
                      "trade_id": trade_ids}


# ── rendering ────────────────────────────────────────────────────────────────--
def _fmt_val(v):
    """Compact, deterministic rendering of a single payload value."""
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False, sort_keys=True)
        except Exception:
            return repr(v)
    return str(v)


def _indent_block(text, prefix="       | "):
    if text is None:
        text = ""
    return [prefix + ln for ln in str(text).split("\n")]


def _fmt_event(idx, e):
    """Render one event as a multi-line section (with blob join for llm.call)."""
    out = []
    ev = e.get("event", "?")
    ts = e.get("ts", "")
    out.append("[%02d] %-20s ts=%s" % (idx, ev, ts))

    id_parts = []
    for k in ID_KEYS:
        val = e.get(k)
        if val is not None:
            id_parts.append("%s=%s" % (k, val))
    if id_parts:
        out.append("     ids: " + "  ".join(id_parts))

    # Generic payload (everything that is not envelope / id / private).
    is_llm = ev == "llm.call"
    blob_keys = {"prompt_hash", "prompt_ref", "completion_hash", "completion_ref"}
    for k in sorted(e.keys()):
        if k.startswith("_") or k in _ENVELOPE:
            continue
        if is_llm and k in blob_keys:
            continue  # rendered in the dedicated blob block below
        out.append("     %s = %s" % (k, _fmt_val(e.get(k))))

    # llm.call: pull FULL prompt + completion text back from the blob store.
    if is_llm:
        ph = e.get("prompt_hash")
        ch = e.get("completion_hash")
        out.append("     prompt_hash = %s" % ph)
        out.append("     completion_hash = %s" % ch)
        ptext = blobs.read_blob(ph) if ph else None
        ctext = blobs.read_blob(ch) if ch else None
        out.append("     --- PROMPT (full text, joined from blob) ---")
        out.extend(
            _indent_block(
                ptext if ptext is not None else "<blob missing: %s>" % ph
            )
        )
        out.append("     --- COMPLETION (full text, joined from blob) ---")
        out.extend(
            _indent_block(
                ctext if ctext is not None else "<blob missing: %s>" % ch
            )
        )
    return "\n".join(out)


def _render(title, scope_label, scope_value, selected, found_ids, stats):
    bar = "=" * 70
    lines = [bar, "%s — %s=%s" % (title, scope_label, scope_value), bar]

    def _fmt_set(s):
        vals = sorted(str(x) for x in s if x is not None)
        return ", ".join(vals) if vals else "(none)"

    run_ids = sorted({e.get("run_id") for e in selected if e.get("run_id")})
    run_files = sorted({e.get("_file") for e in selected if e.get("_file")})

    lines.append("Scope (correlation IDs joined):")
    lines.append("  market_id   : " + _fmt_set(found_ids.get("market_id", set())))
    lines.append("  forecast_id : " + _fmt_set(found_ids.get("forecast_id", set())))
    lines.append("  trade_id    : " + _fmt_set(found_ids.get("trade_id", set())))
    lines.append("  run_id      : " + (", ".join(run_ids) if run_ids else "(none)"))
    lines.append("Run files joined (%d):" % len(run_files))
    for rf in run_files:
        lines.append("  - " + rf)
    lines.append(
        "Events in trail: %d   (scanned %d files / %d lines, %d malformed skipped)"
        % (len(selected), stats.get("files", 0), stats.get("lines", 0),
           stats.get("skipped", 0))
    )
    lines.append("-" * 70)

    if not selected:
        lines.append("")
        lines.append("No events found for %s=%s in %s"
                     % (scope_label, scope_value, str(config.events_dir())))
        return "\n".join(lines)

    for i, e in enumerate(selected, 1):
        lines.append("")
        lines.append(_fmt_event(i, e))
    lines.append("")
    lines.append(bar)
    return "\n".join(lines)


# ── public API ───────────────────────────────────────────────────────────────--
def explain(market_id):
    """Reconstruct the COMPLETE decision trail for a market across all run logs.

    Joins data.fetch -> classify.decision -> forecast.start ->
    (agent.estimate / llm.call / debate.round) -> blend.compute ->
    forecast.final -> sizing.decision -> trade.open|trade.skip ->
    resolution.observed -> trade.settle -> score.brier on correlation IDs, in
    pipeline order. Returns a readable multi-section string; never raises.
    """
    try:
        market_id = str(market_id) if market_id is not None else market_id
        events, stats = _load_events()
        selected, found = _select_for_market(events, market_id)
        selected.sort(key=_sort_key)
        return _render("EXPLAIN", "market_id", market_id, selected, found, stats)
    except Exception as exc:  # pragma: no cover - defensive
        return "explain(%r) failed: %r" % (market_id, exc)


def replay(forecast_id):
    """Reconstruct the decision trail scoped to one forecast_id.

    Every agent prompt/response (with full blob text), debate rounds, the blend
    math, sizing math, the paper fill, settlement, and both Brier scores — joined
    across all run logs in pipeline order. Returns a string; never raises.
    """
    try:
        forecast_id = str(forecast_id) if forecast_id is not None else forecast_id
        events, stats = _load_events()
        selected, found = _select_for_forecast(events, forecast_id)
        selected.sort(key=_sort_key)
        return _render("REPLAY", "forecast_id", forecast_id, selected, found, stats)
    except Exception as exc:  # pragma: no cover - defensive
        return "replay(%r) failed: %r" % (forecast_id, exc)


# ── self-test ────────────────────────────────────────────────────────────────--
def _selftest():
    """Isolated, self-contained verification of explain() + replay().

    Sets a FRESH temp OBS_LOGS_DIR + DATABASE_URL (never touches the live
    polyswarm.db or logs/), synthesizes a COMPLETE event trail for one
    market_id + forecast_id across TWO run files (forecast pipeline in run A,
    resolution/settle/score in run B), stores a real prompt blob referenced by
    an llm.call line, then asserts explain()/replay() recover the forecast.final
    probabilities, the sizing stake, and the FULL prompt text from the blob.
    Returns (ok: bool, report: str).
    """
    import tempfile
    import uuid

    tmp = tempfile.mkdtemp(prefix="obs_explain_selftest_")
    os.environ["OBS_ENABLED"] = "1"
    os.environ["OBS_LOGS_DIR"] = tmp
    os.environ["DATABASE_URL"] = os.path.join(tmp, "selftest.db")

    # Import here so the env above is already in place.
    from . import hooks
    from . import ids

    sentinel = "SENTINEL_PROMPT_%s" % uuid.uuid4().hex
    market_id = ids.mint("mkt")
    forecast_id = ids.mint("fc")
    trade_id = ids.mint("trd")
    run_a = ids.mint("run")
    run_b = ids.mint("run")

    model_prob = 0.6234          # distinctive forecast.final probability
    market_prob = 0.4100
    stake = 12.5                 # distinctive sizing stake
    model_brier = 0.1421
    market_brier = 0.3481

    # ── run A: data -> classify -> forecast pipeline -> trade.open ──────────--
    with ids.run_ctx(run_id=run_a):
        with ids.market_ctx(market_id=market_id, question="Will X happen by 2026?"):
            hooks.on_data_fetch(
                source="gamma", endpoint="/markets", params={"id": market_id},
                raw_text='{"raw":"' + sentinel + '_FETCH"}', item_count=1,
                latency_ms=12.3,
            )
            hooks.on_classify(
                market_id=market_id, question="Will X happen by 2026?",
                label="binary", signals={"liquidity": 1000}, included=True,
                reason="liquid + resolvable",
            )
            with ids.forecast_ctx(forecast_id=forecast_id,
                                  question="Will X happen by 2026?"):
                hooks.on_forecast_start(
                    forecast_id=forecast_id, market_id=market_id,
                    question="Will X happen by 2026?", market_price=market_prob,
                )
                # llm.call: store FULL prompt via blob, reference its hash.
                with ids.agent_ctx(agent_id="agent_bull", role="agent",
                                   llm_call_id=ids.mint("llm")):
                    hooks.on_llm_call(
                        provider="ollama", model="qwen2.5:7b",
                        system="You are a careful forecaster.",
                        user=sentinel + " Estimate P(yes) for: Will X happen?",
                        completion="P(yes)=0.65 because ...",
                        tokens_in=42, tokens_out=11, latency_ms=850.0, role="agent",
                    )
                    hooks.on_agent_estimate(
                        agent_id="agent_bull", forecast_id=forecast_id,
                        persona="bull", probability=0.65, confidence=0.7,
                        reasoning="momentum favours yes", round=1,
                    )
                with ids.agent_ctx(agent_id="agent_bear", role="agent",
                                   llm_call_id=ids.mint("llm")):
                    hooks.on_llm_call(
                        provider="ollama", model="qwen2.5:7b",
                        system="You are a skeptical forecaster.",
                        user="Counter-argue: Will X happen?",
                        completion="P(yes)=0.58 because ...",
                        tokens_in=39, tokens_out=9, latency_ms=810.0, role="agent",
                    )
                    hooks.on_agent_estimate(
                        agent_id="agent_bear", forecast_id=forecast_id,
                        persona="bear", probability=0.58, confidence=0.6,
                        reasoning="base rate is lower", round=1,
                    )
                hooks.on_debate_round(
                    forecast_id=forecast_id, round_n=1,
                    estimates=[{"persona": "bull", "p": 0.65},
                               {"persona": "bear", "p": 0.58}],
                )
                hooks.on_blend(
                    forecast_id=forecast_id, method="confidence_weighted",
                    prior=0.5, output_probability=model_prob, consensus_score=0.82,
                )
                hooks.on_forecast_final(
                    forecast_id=forecast_id, market_id=market_id,
                    model_probability=model_prob, market_probability=market_prob,
                    edge=model_prob - market_prob, consensus=0.82,
                    reasoning_summary="bull/bear blend favours yes",
                )
                hooks.on_sizing(
                    forecast_id=forecast_id, trade_id=trade_id, bankroll=1000.0,
                    edge=model_prob - market_prob, side="YES", kelly_f_star=0.05,
                    lam=0.25, cap=0.02, final_fraction=0.0125, stake=stake,
                    p=model_prob, c=market_prob,
                )
                hooks.on_trade_open(
                    trade_id=trade_id, market_id=market_id, forecast_id=forecast_id,
                    side="YES", stake=stake, fill_price=market_prob, slippage=0.001,
                    fee=0.02,
                )

    # ── run B: settlement happens later, in a different run/process ─────────--
    with ids.run_ctx(run_id=run_b):
        with ids.market_ctx(market_id=market_id):
            with ids.forecast_ctx(forecast_id=forecast_id):
                hooks.on_resolution(market_id=market_id, outcome=1.0,
                                    source="gamma_uma")
            hooks.on_trade_settle(
                trade_id=trade_id, market_id=market_id, outcome=1.0, payout=30.49,
                realized_pnl=17.99, bankroll_before=1000.0, bankroll_after=1017.99,
            )
            hooks.on_score(
                forecast_id=forecast_id, market_id=market_id,
                model_brier=model_brier, market_brier=market_brier,
            )

    exp = explain(market_id)
    rep = replay(forecast_id)

    checks = []

    def _chk(name, cond):
        checks.append((name, bool(cond)))

    # explain(market_id) assertions
    _chk("explain: forecast.final model probability present",
         str(model_prob) in exp)
    _chk("explain: sizing stake present", ("stake = %s" % stake) in exp
         or str(stake) in exp)
    _chk("explain: FULL prompt text recovered from blob (blob join)",
         sentinel in exp)
    _chk("explain: joins across both run files",
         run_a in exp and run_b in exp)
    _chk("explain: market_id intact", market_id in exp)
    _chk("explain: forecast_id intact", forecast_id in exp)
    _chk("explain: trade_id intact", trade_id in exp)
    _chk("explain: settlement joined (trade.settle)", "trade.settle" in exp)
    _chk("explain: both Brier scores joined", str(model_brier) in exp
         and str(market_brier) in exp)
    _chk("explain: causal order data.fetch precedes forecast.final",
         exp.find("data.fetch") < exp.find("forecast.final")
         and exp.find("forecast.final") < exp.find("score.brier"))

    # replay(forecast_id) assertions
    _chk("replay: forecast.final model probability present",
         str(model_prob) in rep)
    _chk("replay: sizing stake present", ("stake = %s" % stake) in rep
         or str(stake) in rep)
    _chk("replay: FULL prompt text recovered from blob (blob join)",
         sentinel in rep)
    _chk("replay: both Brier scores present", str(model_brier) in rep
         and str(market_brier) in rep)
    _chk("replay: paper fill present (trade.open)", "trade.open" in rep)
    _chk("replay: settlement present (trade.settle)", "trade.settle" in rep)
    _chk("replay: debate round present", "debate.round" in rep)
    _chk("replay: blend math present", "blend.compute" in rep)

    ok = all(passed for _, passed in checks)
    report_lines = ["SELF-TEST temp dir: " + tmp, ""]
    for name, passed in checks:
        report_lines.append(("  [PASS] " if passed else "  [FAIL] ") + name)
    report_lines.append("")
    report_lines.append("RESULT: " + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return ok, "\n".join(report_lines)


# ── CLI ──────────────────────────────────────────────────────────────────────--
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(
            "usage: python -m harness.obs.explain "
            "{explain <market_id> | replay <forecast_id> | selftest}\n"
        )
        return 2
    cmd = argv[0]
    if cmd == "explain":
        if len(argv) < 2:
            sys.stderr.write("usage: explain <market_id>\n")
            return 2
        print(explain(argv[1]))
        return 0
    if cmd == "replay":
        if len(argv) < 2:
            sys.stderr.write("usage: replay <forecast_id>\n")
            return 2
        print(replay(argv[1]))
        return 0
    if cmd == "selftest":
        ok, report = _selftest()
        print(report)
        return 0 if ok else 1
    sys.stderr.write("unknown command: %s\n" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
