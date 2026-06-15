"""obs.transcript — LAYER 2 human-readable transcript, rendered PURELY from the
hash-chained event JSONL (NEVER the DB, never hand-written).

`build(run_id)` reads ``config.events_dir()/<run_id>.jsonl`` line by line and
renders ``config.transcripts_dir()/<run_id>.md`` — a readable narrative of one
run — then returns the rendered file's Path.

This module is DISTINCT from harness/transcript.py (which reads polyswarm.db and
writes ai_transcript.md). This one NEVER opens a sqlite connection: it imports
only obs.config and the stdlib, reads exactly one .jsonl file, and references
blob_refs (it does NOT inline full prompts/completions).

DETERMINISM CONTRACT (acceptance criterion 7): the same event log MUST produce a
BYTE-IDENTICAL transcript. There is NO generation timestamp, NO randomness, and
NO time/locale-dependent formatting. Stable ordering is derived entirely from the
event-log line order:
  * markets are emitted in first-appearance order,
  * forecasts within a market in first-appearance order,
  * every event within a bucket in its original log-line order,
  * dict/list payloads are serialized with sort_keys=True.
The output file is written with newline="\n" so line endings never depend on the
host OS. Event ``ts`` values that appear in the output are read straight from the
log (and so are identical for identical input); the renderer itself never calls
``datetime.now`` or any RNG.
"""

import json
from pathlib import Path

from . import config

_DASH = "—"  # em dash

_RUN_START = "run.start"
_RUN_END = "run.end"
_ERROR = "error"


# ── scalar / payload formatting (all deterministic) ──────────────────────────--
def _oneline(s):
    """Collapse any whitespace (newlines/tabs/runs of spaces) to single spaces."""
    if s is None:
        return ""
    try:
        return " ".join(str(s).split())
    except Exception:
        return ""


def _compact(obj):
    """Deterministic compact serialization of a dict/list/scalar payload."""
    if obj is None:
        return _DASH
    try:
        if isinstance(obj, str):
            return _oneline(obj)
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    except Exception:
        return _oneline(obj)


def _num(x, nd=4):
    """Format a number with `nd` decimals; ints stay int; None -> em dash."""
    if x is None:
        return _DASH
    try:
        if isinstance(x, bool):
            return "true" if x else "false"
        if isinstance(x, int):
            return str(x)
        return "{:.{nd}f}".format(float(x), nd=nd)
    except Exception:
        return _oneline(x)


def _pct(x):
    """Format a probability/edge in [0,1]-ish as a percentage; None -> em dash."""
    if x is None:
        return _DASH
    try:
        return "{:.1f}%".format(float(x) * 100.0)
    except Exception:
        return _oneline(x)


def _money(x):
    if x is None:
        return _DASH
    try:
        return "${:.2f}".format(float(x))
    except Exception:
        return _oneline(x)


def _code(x):
    """Inline-code a scalar; None -> em dash (not wrapped)."""
    if x is None:
        return _DASH
    s = _oneline(x)
    return "`{}`".format(s) if s else _DASH


# ── per-event renderers: each returns a list of markdown lines (no trailing \n)─
def _r_classify(e):
    out = [
        "- **classify** · label={} · included={} · market_id={}".format(
            _code(e.get("label")), e.get("included"), _code(e.get("market_id"))
        )
    ]
    reason = _oneline(e.get("reason"))
    if reason:
        out[0] += " — " + reason
    if e.get("signals") is not None:
        out.append("  - signals: " + _compact(e.get("signals")))
    return out


def _r_data_fetch(e):
    out = [
        "- **data.fetch** · source={} · endpoint={} · items={} · {}ms".format(
            _code(e.get("source")),
            _code(e.get("endpoint")),
            e.get("item_count"),
            _num(e.get("latency_ms"), 0),
        )
    ]
    if e.get("params") is not None:
        out.append("  - params: " + _compact(e.get("params")))
    ref = e.get("blob_ref")
    if ref:
        out.append("  - raw blob: {} (sha {})".format(_code(ref), _code(e.get("raw_hash"))))
    return out


def _r_forecast_start(e):
    return [
        "- **forecast.start** · market_price={} · question: {}".format(
            _pct(e.get("market_price")), _oneline(e.get("question")) or _DASH
        )
    ]


def _r_llm_call(e):
    out = [
        "- **llm.call** [{}] · {}/{} · in={} out={} · {}ms · retries={}".format(
            _oneline(e.get("role")) or _DASH,
            _oneline(e.get("provider")) or _DASH,
            _oneline(e.get("model")) or _DASH,
            e.get("tokens_in"),
            e.get("tokens_out"),
            _num(e.get("latency_ms"), 0),
            e.get("retries"),
        )
    ]
    out.append(
        "  - prompt blob: {} · completion blob: {}".format(
            _code(e.get("prompt_ref")), _code(e.get("completion_ref"))
        )
    )
    if e.get("error"):
        out.append("  - error: " + _oneline(e.get("error")))
    return out


def _r_agent_estimate(e):
    out = [
        "- **agent.estimate** [{} · {}] round {} · p={} conf={}".format(
            _oneline(e.get("persona")) or _DASH,
            _code(e.get("agent_id")),
            e.get("round"),
            _pct(e.get("probability")),
            _num(e.get("confidence"), 2),
        )
    ]
    reasoning = _oneline(e.get("reasoning"))
    if reasoning:
        out.append("  - " + reasoning)
    return out


def _r_debate_round(e):
    ests = e.get("estimates")
    n = len(ests) if isinstance(ests, (list, tuple)) else _DASH
    return ["- **debate.round** {} · {} estimates".format(e.get("round_n"), n)]


def _r_blend(e):
    return [
        "- **blend.compute** · method={} · prior={} → p={} · consensus={}".format(
            _code(e.get("method")),
            _pct(e.get("prior")),
            _pct(e.get("output_probability")),
            _num(e.get("consensus_score"), 3),
        )
    ]


def _r_forecast_final(e):
    out = [
        "- **forecast.final** · model {} vs market {} · edge {} · consensus {}".format(
            _pct(e.get("model_probability")),
            _pct(e.get("market_probability")),
            _pct(e.get("edge")),
            _num(e.get("consensus"), 3),
        )
    ]
    out.append("  - record_hash: " + _code(e.get("record_hash")))
    summary = _oneline(e.get("reasoning_summary"))
    if summary:
        out.append("  - summary: " + summary)
    return out


def _r_sizing(e):
    return [
        "- **sizing.decision** · side={} · kelly*={} · λ={} · cap={} · "
        "fraction={} · stake={} (p={}, c={})".format(
            _code(e.get("side")),
            _num(e.get("kelly_f_star"), 4),
            _num(e.get("lam"), 4),
            _num(e.get("cap"), 4),
            _num(e.get("final_fraction"), 4),
            _money(e.get("stake")),
            _num(e.get("p"), 4),
            _num(e.get("c"), 4),
        )
    ]


def _r_trade_open(e):
    return [
        "- **trade.open** · {} stake {} @ {} · slippage={} · fee={} · trade_id={} [{}]".format(
            _code(e.get("side")),
            _money(e.get("stake")),
            _num(e.get("fill_price"), 4),
            _num(e.get("slippage"), 4),
            _num(e.get("fee"), 4),
            _code(e.get("trade_id")),
            _oneline(e.get("mode")) or _DASH,
        )
    ]


def _r_trade_skip(e):
    out = ["- **trade.skip** · reason: " + (_oneline(e.get("reason")) or _DASH)]
    if e.get("inputs") is not None:
        out.append("  - inputs: " + _compact(e.get("inputs")))
    return out


def _r_resolution(e):
    return [
        "- **resolution.observed** · outcome={} · source={}".format(
            _num(e.get("outcome"), 4), _code(e.get("source"))
        )
    ]


def _r_trade_settle(e):
    return [
        "- **trade.settle** · trade_id={} · outcome={} · payout={} · pnl={} · "
        "bankroll {} → {}".format(
            _code(e.get("trade_id")),
            _num(e.get("outcome"), 4),
            _money(e.get("payout")),
            _money(e.get("realized_pnl")),
            _money(e.get("bankroll_before")),
            _money(e.get("bankroll_after")),
        )
    ]


def _r_score(e):
    return [
        "- **score.brier** · model_brier={} · market_brier={}".format(
            _num(e.get("model_brier"), 4), _num(e.get("market_brier"), 4)
        )
    ]


def _r_gate(e):
    return [
        "- **gate.eval** · n_resolved={} · model_brier_mean={} · market_brier_mean={} · "
        "paper_pnl={} · gate1={} gate2={} overall={}".format(
            e.get("n_resolved"),
            _num(e.get("model_brier_mean"), 4),
            _num(e.get("market_brier_mean"), 4),
            _money(e.get("paper_pnl")),
            e.get("gate1_pass"),
            e.get("gate2_pass"),
            e.get("overall_pass"),
        )
    ]


_RENDERERS = {
    "classify.decision": _r_classify,
    "data.fetch": _r_data_fetch,
    "forecast.start": _r_forecast_start,
    "llm.call": _r_llm_call,
    "agent.estimate": _r_agent_estimate,
    "debate.round": _r_debate_round,
    "blend.compute": _r_blend,
    "forecast.final": _r_forecast_final,
    "sizing.decision": _r_sizing,
    "trade.open": _r_trade_open,
    "trade.skip": _r_trade_skip,
    "resolution.observed": _r_resolution,
    "trade.settle": _r_trade_settle,
    "score.brier": _r_score,
    "gate.eval": _r_gate,
}


def _render_event(e):
    """Dispatch one event dict to its renderer; unknown types get a generic line.

    Guarded: a renderer that raises yields a deterministic fallback line rather
    than aborting the whole transcript.
    """
    name = e.get("event")
    try:
        fn = _RENDERERS.get(name)
        if fn is not None:
            return fn(e)
    except Exception:
        pass
    # Generic fallback for unrecognized (but valid) events — deterministic.
    extra = {
        k: v
        for k, v in sorted(e.items())
        if k not in ("event", "ts", "level", "schema_version", "prev_hash")
    }
    return ["- **{}** · {}".format(name, _compact(extra) if extra else "")]


# ── parsing & grouping ────────────────────────────────────────────────────────
def _read_events(run_id):
    """Return (events, n_malformed). Reads ONE .jsonl file; never opens a DB."""
    path = config.events_dir() / (str(run_id) + ".jsonl")
    events = []
    n_malformed = 0
    try:
        if not path.exists():
            return events, n_malformed
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace") if raw else ""
        for ln in text.split("\n"):
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    events.append(obj)
                else:
                    n_malformed += 1
            except Exception:
                n_malformed += 1
    except Exception:
        pass
    return events, n_malformed


def _question_for_market(market_level, forecasts_by_market, mid):
    """First non-empty `question` seen (log order) among this market's events."""
    seq = list(market_level.get(mid, []))
    for fid_events in forecasts_by_market.get(mid, {}).values():
        seq.extend(fid_events)
    # Preserve log order across the combined set is not required for the title;
    # the first non-empty question is deterministic given deterministic inputs.
    for e in market_level.get(mid, []):
        q = _oneline(e.get("question"))
        if q:
            return q
    for fid, fid_events in forecasts_by_market.get(mid, {}).items():
        for e in fid_events:
            q = _oneline(e.get("question"))
            if q:
                return q
    return ""


# ── header / summary ────────────────────────────────────────────────────────--
def _render_header(run_id, run_start):
    L = ["## Run", "", "- run_id: " + _code(run_id)]
    if run_start is None:
        L.append("- _no run.start event in log_")
        L.append("")
        return L

    if run_start.get("ts"):
        L.append("- started: " + _code(run_start.get("ts")))

    repro = run_start.get("reproducibility") or {}
    if isinstance(repro, dict):
        L.append(
            "- git: {} (dirty={})".format(
                _code(repro.get("git_sha")), repro.get("git_dirty")
            )
        )
        L.append("- code_version: " + _code(repro.get("code_version")))
        L.append(
            "- provider: {} · model_fast: {} · model_deep: {} · debate_rounds: {}".format(
                _code(repro.get("provider")),
                _code(repro.get("model_fast")),
                _code(repro.get("model_deep")),
                _code(repro.get("debate_rounds")),
            )
        )
        L.append(
            "- seed: {} · deterministic: {}".format(
                repro.get("seed"), repro.get("deterministic")
            )
        )

    L.append("- bankroll: " + _money(run_start.get("bankroll")))

    cfg = run_start.get("config")
    if isinstance(cfg, dict) and cfg:
        L.append("- config:")
        for k in sorted(cfg.keys(), key=lambda x: str(x)):
            L.append("  - {}: {}".format(k, _compact(cfg[k])))
    elif cfg is not None:
        L.append("- config: " + _compact(cfg))
    L.append("")
    return L


def _render_summary(run_end, n_events, n_malformed):
    L = ["## Run summary", ""]
    L.append("- events rendered: {}".format(n_events))
    if n_malformed:
        L.append("- unparseable lines skipped: {}".format(n_malformed))
    if run_end is None:
        L.append("- _no run.end event in log_")
        L.append("")
        return L
    if run_end.get("ts"):
        L.append("- ended: " + _code(run_end.get("ts")))
    counters = run_end.get("counters")
    if isinstance(counters, dict) and counters:
        L.append("- counters:")
        for k in sorted(counters.keys(), key=lambda x: str(x)):
            L.append("  - {}: {}".format(k, _compact(counters[k])))
    elif counters is not None:
        L.append("- counters: " + _compact(counters))
    L.append("")
    return L


# ── public API ────────────────────────────────────────────────────────────────
def build(run_id):
    """Render ``config.transcripts_dir()/<run_id>.md`` from the event JSONL.

    Returns the Path to the rendered file. Reads ONLY the event log (never the
    DB). Deterministic: the same log always yields byte-identical output.
    """
    out_path = config.transcripts_dir() / (str(run_id) + ".md")
    try:
        events, n_malformed = _read_events(run_id)

        run_start = None
        run_end = None
        for e in events:
            ev = e.get("event")
            if ev == _RUN_START and run_start is None:
                run_start = e
            elif ev == _RUN_END:
                run_end = e  # last run.end wins

        errors = [e for e in events if e.get("event") == _ERROR]
        body = [
            e
            for e in events
            if e.get("event") not in (_RUN_START, _RUN_END, _ERROR)
        ]

        # Group: market_id -> { forecast_id -> [events] } and market-level events
        # (those without a forecast_id). Orphans have neither id.
        market_order = []  # market_ids in first-appearance order
        seen_markets = set()
        market_level = {}  # market_id -> [events without forecast_id]
        forecasts_by_market = {}  # market_id -> {forecast_id: [events]}
        forecast_order = {}  # market_id -> [forecast_ids in first-appearance order]
        orphans = []  # no market_id and no forecast_id

        # First, map each forecast_id to a market_id via the first market_id
        # observed on any of its events (or via the event's own market_id).
        fid_to_market = {}
        for e in body:
            fid = e.get("forecast_id")
            mid = e.get("market_id")
            if fid and mid and fid not in fid_to_market:
                fid_to_market[fid] = mid

        for e in body:
            fid = e.get("forecast_id")
            mid = e.get("market_id")
            if fid and not mid:
                mid = fid_to_market.get(fid)
            if mid:
                if mid not in seen_markets:
                    seen_markets.add(mid)
                    market_order.append(mid)
                    market_level[mid] = []
                    forecasts_by_market[mid] = {}
                    forecast_order[mid] = []
                if fid:
                    if fid not in forecasts_by_market[mid]:
                        forecasts_by_market[mid][fid] = []
                        forecast_order[mid].append(fid)
                    forecasts_by_market[mid][fid].append(e)
                else:
                    market_level[mid].append(e)
            elif fid:
                # forecast with no resolvable market — synthetic bucket
                key = "(no market)"
                if key not in seen_markets:
                    seen_markets.add(key)
                    market_order.append(key)
                    market_level[key] = []
                    forecasts_by_market[key] = {}
                    forecast_order[key] = []
                if fid not in forecasts_by_market[key]:
                    forecasts_by_market[key][fid] = []
                    forecast_order[key].append(fid)
                forecasts_by_market[key][fid].append(e)
            else:
                orphans.append(e)

        L = []
        L.append("# Transcript — run " + str(run_id))
        L.append("")
        L.append(
            "_Rendered purely from the hash-chained event log "
            "(events/{}.jsonl). No DB access; deterministic output._".format(run_id)
        )
        L.append("")

        L.extend(_render_header(run_id, run_start))

        if orphans:
            L.append("## Run-level events")
            L.append("")
            for e in orphans:
                L.extend(_render_event(e))
            L.append("")

        if market_order:
            L.append("## Markets")
            L.append("")
            for mid in market_order:
                title = _question_for_market(market_level, forecasts_by_market, mid)
                if title:
                    L.append("### Market {} — {}".format(_code(mid), title))
                else:
                    L.append("### Market {}".format(_code(mid)))
                L.append("")
                # market-level events (classify, data.fetch, resolution w/o fid…)
                for e in market_level.get(mid, []):
                    L.extend(_render_event(e))
                if market_level.get(mid):
                    L.append("")
                # per-forecast blocks, in first-appearance order
                for fid in forecast_order.get(mid, []):
                    L.append("#### Forecast {}".format(_code(fid)))
                    L.append("")
                    for e in forecasts_by_market[mid][fid]:
                        L.extend(_render_event(e))
                    L.append("")

        L.append("## Errors")
        L.append("")
        if errors:
            for e in errors:
                where = _oneline(e.get("where")) or _DASH
                line = "- **[{}]** {} — {}".format(
                    _oneline(e.get("level")) or "ERROR",
                    where,
                    _oneline(e.get("error")) or _DASH,
                )
                L.append(line)
                action = _oneline(e.get("action"))
                if action:
                    L.append("  - action: " + action)
                ctx = e.get("context")
                if ctx is not None:
                    L.append("  - context: " + _compact(ctx))
        else:
            L.append("_No errors recorded._")
        L.append("")

        L.extend(_render_summary(run_end, len(events), n_malformed))

        text = "\n".join(L)
        if not text.endswith("\n"):
            text += "\n"
    except Exception as exc:  # never raise; emit a deterministic stub
        text = (
            "# Transcript — run {}\n\n"
            "_Failed to render transcript: {!r}_\n".format(run_id, exc)
        )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except Exception:
        pass
    return out_path


# ── self-test ─────────────────────────────────────────────────────────────────
def _selftest():
    """Hand-write a valid event JSONL, build() twice, assert byte-identical and
    that no sqlite connection was ever opened. Returns a result string."""
    import os
    import sqlite3
    import tempfile
    import hashlib

    tmp = tempfile.mkdtemp(prefix="obs_transcript_selftest_")
    os.environ["OBS_LOGS_DIR"] = tmp
    os.environ["OBS_ENABLED"] = "1"
    # Point the DB somewhere temp too — even though we must never touch it.
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./" + os.path.join(
        tmp, "should_never_be_opened.db"
    ).replace("\\", "/")

    run_id = "run_selftest0001"

    def _sha(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # Build a valid hash-chained log (prev_hash links exactly like eventlog).
    raw_events = [
        {
            "event": "run.start",
            "level": "INFO",
            "config": {"mode": "paper", "max_markets": 3, "alpha": 0.5},
            "bankroll": 1000.0,
            "reproducibility": {
                "git_sha": "abc123def456",
                "git_dirty": False,
                "code_version": "deadbeef" * 8,
                "provider": "ollama",
                "model_fast": "qwen2.5:7b",
                "model_deep": "qwen2.5:7b",
                "debate_rounds": "2",
                "seed": None,
                "deterministic": False,
            },
        },
        {
            "event": "data.fetch",
            "level": "INFO",
            "source": "gamma",
            "endpoint": "/markets",
            "params": {"limit": 50},
            "raw_hash": "f" * 64,
            "blob_ref": "blobs/" + "f" * 64,
            "item_count": 12,
            "latency_ms": 134,
        },
        {
            "event": "classify.decision",
            "level": "INFO",
            "market_id": "mkt_1",
            "question": "Will it rain tomorrow?",
            "label": "weather",
            "signals": {"liquidity": 4200, "days_to_resolve": 1},
            "included": True,
            "reason": "liquid + near-term",
        },
        {
            "event": "forecast.start",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "question": "Will it rain tomorrow?",
            "market_price": 0.62,
        },
        {
            "event": "llm.call",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "role": "agent",
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "prompt_hash": "a" * 64,
            "prompt_ref": "blobs/" + "a" * 64,
            "completion_hash": "b" * 64,
            "completion_ref": "blobs/" + "b" * 64,
            "tokens_in": 540,
            "tokens_out": 88,
            "latency_ms": 2100,
            "retries": 0,
            "error": None,
            "cost_usd": 0,
        },
        {
            "event": "agent.estimate",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "agent_id": "ag_a",
            "persona": "meteorologist",
            "probability": 0.7,
            "confidence": 0.8,
            "reasoning": "Front moving in overnight.\nHigh humidity.",
            "round": 1,
        },
        {
            "event": "agent.estimate",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "agent_id": "ag_b",
            "persona": "skeptic",
            "probability": 0.55,
            "confidence": 0.6,
            "reasoning": "Models disagree.",
            "round": 1,
        },
        {
            "event": "debate.round",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "round_n": 1,
            "estimates": [{"agent_id": "ag_a", "p": 0.7}, {"agent_id": "ag_b", "p": 0.55}],
        },
        {
            "event": "blend.compute",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "method": "logit_pool",
            "prior": 0.62,
            "output_probability": 0.66,
            "consensus_score": 0.74,
        },
        {
            "event": "forecast.final",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "model_probability": 0.66,
            "market_probability": 0.62,
            "edge": 0.04,
            "consensus": 0.74,
            "reasoning_summary": "Lean YES; modest edge.",
            "record_hash": "c" * 64,
        },
        {
            "event": "sizing.decision",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "trade_id": "tr_1",
            "bankroll": 1000.0,
            "edge": 0.04,
            "side": "YES",
            "kelly_f_star": 0.105,
            "lam": 0.5,
            "cap": 0.05,
            "final_fraction": 0.0525,
            "stake": 52.5,
            "p": 0.66,
            "c": 0.62,
        },
        {
            "event": "trade.open",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "trade_id": "tr_1",
            "side": "YES",
            "stake": 52.5,
            "fill_price": 0.625,
            "slippage": 0.005,
            "fee": 0.1,
            "mode": "paper",
        },
        {
            "event": "resolution.observed",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "outcome": 1.0,
            "source": "gamma",
        },
        {
            "event": "trade.settle",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "trade_id": "tr_1",
            "outcome": 1.0,
            "payout": 84.0,
            "realized_pnl": 31.5,
            "bankroll_before": 1000.0,
            "bankroll_after": 1031.5,
        },
        {
            "event": "score.brier",
            "level": "INFO",
            "market_id": "mkt_1",
            "forecast_id": "fc_1",
            "model_brier": 0.1156,
            "market_brier": 0.1444,
        },
        {
            "event": "error",
            "level": "ERROR",
            "market_id": "mkt_2",
            "where": "gamma.fetch",
            "error": "Timeout()",
            "action": "skip-market",
            "context": {"market_id": "mkt_2"},
            "traceback": "Traceback (most recent call last):\n  ...",
        },
        {
            "event": "run.end",
            "level": "INFO",
            "counters": {"markets": 1, "forecasts": 1, "trades": 1, "errors": 1},
        },
    ]

    # Write with the exact envelope shape eventlog uses, with a valid prev chain.
    events_dir = config.events_dir()
    log_path = events_dir / (run_id + ".jsonl")
    prev = "0" * 64
    lines = []
    for env in raw_events:
        env = dict(env)
        env["schema_version"] = config.SCHEMA_VERSION
        env["run_id"] = run_id
        env["ts"] = "2026-06-15T00:00:00.000+00:00"
        env["prev_hash"] = prev
        line = json.dumps(env, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        lines.append(line)
        prev = _sha(line)
    with open(log_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # Spy on sqlite3.connect to PROVE the DB is never opened.
    opened = []
    _orig_connect = sqlite3.connect

    def _spy_connect(*a, **k):
        opened.append((a, k))
        return _orig_connect(*a, **k)

    sqlite3.connect = _spy_connect
    try:
        p1 = build(run_id)
        b1 = Path(p1).read_bytes()
        p2 = build(run_id)
        b2 = Path(p2).read_bytes()
    finally:
        sqlite3.connect = _orig_connect

    identical = b1 == b2
    no_db = len(opened) == 0
    nonempty = len(b1) > 0

    results = []
    results.append("file: " + str(p1))
    results.append("byte_identical_across_two_builds: " + str(identical))
    results.append("sqlite_connections_opened: " + str(len(opened)))
    results.append("never_opened_db: " + str(no_db))
    results.append("output_bytes: " + str(len(b1)))
    ok = identical and no_db and nonempty
    results.append("SELFTEST_PASS: " + str(ok))
    return "\n".join(results), ok, b1.decode("utf-8", errors="replace")


if __name__ == "__main__":
    report, ok, rendered = _selftest()
    print(report)
    print("\n----- rendered transcript -----\n")
    print(rendered)
    import sys as _sys

    _sys.exit(0 if ok else 1)
