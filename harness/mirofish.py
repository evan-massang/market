"""
MiroFish adapter — drive the RUNNING MiroFish backend headless to forecast a
single Polymarket opinion market via crowd simulation.

MiroFish is a multi-agent social-simulation engine. Its Flask backend is assumed
LIVE (default http://localhost:5001) with:
  * LLM  = local Ollama qwen2.5:7b
  * graph = Zep Cloud
both already configured server-side.

Public entry point
------------------
    forecast_market(question, description="", *, base="http://localhost:5001",
                    max_wait=1800) -> dict

Returns:
    {
        "ok": bool,
        "crowd_probability": float | None,   # P(market resolves YES), 0..1
        "report_markdown": str,              # MiroFish prediction report (prose)
        "simulation_id": str,
        "stage_reached": str,                # furthest pipeline stage reached
        "error": str,
    }

Pipeline (each async stage returns a task and is polled)
--------------------------------------------------------
  1. POST /api/graph/ontology/generate   (multipart) -> project_id   [SYNC, slow]
  2. POST /api/graph/build                -> task_id  -> poll task    -> graph_id
  3. POST /api/simulation/create          -> simulation_id
  4. POST /api/simulation/prepare         -> task_id  -> poll prepare/status
  5. POST /api/simulation/start           -> run      -> poll run-status
  6. POST /api/report/generate            -> report_id+task -> poll generate/status
  7. GET  /api/report/<report_id>         -> markdown_content
  8. ONE extra local-LLM call (core.agent) to read the report and emit
     {"probability": <0..1>}; regex fallback if the LLM is unavailable.

SPEED: this targets a 16GB CPU box running local Qwen, so EVERY config is the
smallest viable one: a short neutral seed (few entities -> few agents), a large
chunk_size (one episode), and max_rounds=1 (single simulation round). Getting one
end-to-end run to complete is prioritised over fidelity. The whole pipeline
respects `max_wait`; if it runs out, the furthest `stage_reached` is returned.

Stand-alone, read-only client: talks to MiroFish over HTTP with httpx and never
imports MiroFish internals. The only polyswarm dependency is the optional LLM
extraction helper in core.agent.
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
from typing import Any, Optional

import httpx

# ── stage names (returned verbatim in stage_reached) ──────────────────────────
STAGE_INIT = "init"
STAGE_PROJECT_CREATED = "project_created"      # ontology generated, project_id obtained
STAGE_GRAPH_BUILDING = "graph_building"
STAGE_GRAPH_BUILT = "graph_built"
STAGE_SIM_CREATED = "sim_created"
STAGE_SIM_PREPARING = "sim_preparing"
STAGE_SIM_PREPARED = "sim_prepared"
STAGE_SIM_RUNNING = "sim_running"
STAGE_SIM_DONE = "sim_done"
STAGE_REPORT_GENERATING = "report_generating"
STAGE_REPORT_DONE = "report_done"
STAGE_PROBABILITY_EXTRACTED = "probability_extracted"

# Smallest-viable defaults (overridable via kwargs).
_DEFAULT_MAX_ROUNDS = 1          # single simulation round
_DEFAULT_CHUNK_SIZE = 4000       # huge chunk -> ~1 episode -> fast graph build
_DEFAULT_CHUNK_OVERLAP = 0
_DEFAULT_PARALLEL_PROFILES = 3   # mild parallelism for profile generation

# Polling cadence (seconds).
_POLL_GRAPH = 5.0
_POLL_PREPARE = 5.0
_POLL_RUN = 8.0
_POLL_REPORT = 8.0

# The synchronous ontology call runs the LLM inline; give it generous headroom.
_ONTOLOGY_TIMEOUT = 900.0
_QUICK_TIMEOUT = 60.0


# ════════════════════════════════════════════════════════════════════════════
# low-level HTTP helpers (never raise to the caller; surface error in result)
# ════════════════════════════════════════════════════════════════════════════
class _MiroFishError(Exception):
    """Raised internally when MiroFish returns success=false or a bad HTTP code."""


def _unwrap(resp: httpx.Response) -> dict:
    """Validate a MiroFish JSON response and return its ``data`` payload.

    MiroFish always wraps responses as {"success": bool, "data"/"error": ...}.
    Raises _MiroFishError on HTTP error or success=false.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON body (e.g. 500 HTML)
        raise _MiroFishError(f"HTTP {resp.status_code}: non-JSON body: {resp.text[:300]}")
    if resp.status_code >= 400 or not body.get("success", False):
        err = body.get("error") or body.get("message") or f"HTTP {resp.status_code}"
        raise _MiroFishError(str(err))
    return body.get("data", {}) or {}


def _post_json(client: httpx.Client, path: str, payload: dict, timeout: float = _QUICK_TIMEOUT) -> dict:
    return _unwrap(client.post(path, json=payload, timeout=timeout))


def _get(client: httpx.Client, path: str, timeout: float = _QUICK_TIMEOUT) -> dict:
    return _unwrap(client.get(path, timeout=timeout))


# ════════════════════════════════════════════════════════════════════════════
# seed material
# ════════════════════════════════════════════════════════════════════════════
def _build_seed_text(question: str, description: str) -> str:
    """Construct a short, NEUTRAL seed document for MiroFish to build a world around.

    Kept deliberately compact: a smaller seed yields fewer Zep entities, hence
    fewer OASIS agents, which is the dominant speed lever on a CPU box.
    """
    desc = (description or "").strip()
    parts = [
        f"# Prediction Topic\n",
        f"The following is a real-world question that a crowd of people is discussing "
        f"and forming opinions about:\n\n",
        f"**{question.strip()}**\n",
    ]
    if desc:
        parts.append(f"\n## Background / resolution context\n\n{desc}\n")
    parts.append(
        "\n## Framing\n\n"
        "This is an open, uncertain event. Different people hold different views on "
        "whether it will happen. Some are optimistic it resolves YES, others are "
        "skeptical and expect NO. Discussion should surface the competing arguments, "
        "the key drivers, and the overall balance of opinion in the crowd.\n"
    )
    return "".join(parts)


def _build_requirement(question: str) -> str:
    """The simulation requirement = ask MiroFish to predict P(YES) for this market."""
    return (
        "Simulate how a crowd discusses and reasons about this event, then predict "
        "the probability (between 0 and 1) that the market resolves YES for the "
        f"question: \"{question.strip()}\". In the final report, state the crowd's "
        "overall estimated probability of a YES resolution and the main reasons for it."
    )


# ════════════════════════════════════════════════════════════════════════════
# probability extraction from the finished report
# ════════════════════════════════════════════════════════════════════════════
def _extract_probability(report_markdown: str, question: str) -> Optional[float]:
    """Read the finished report and return P(YES) in [0,1], or None.

    Primary path: ONE local-LLM call via polyswarm core.agent (same Ollama/Qwen the
    swarm uses). Fallback: regex scan of the markdown for an explicit probability
    or percentage. Never raises.
    """
    prob = _extract_probability_llm(report_markdown, question)
    if prob is not None:
        return prob
    return _extract_probability_regex(report_markdown)


def _extract_probability_llm(report_markdown: str, question: str) -> Optional[float]:
    try:
        # Make core.* importable when this module is imported from the harness pkg.
        _here = os.path.dirname(os.path.abspath(__file__))
        _root = os.path.dirname(_here)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        # Ensure LLM_PROVIDER etc. are loaded from polyswarm/.env if present.
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(os.path.join(_root, ".env"))
        except Exception:  # noqa: BLE001
            pass

        from core.agent import _get_llm_client, _call_llm  # type: ignore

        provider, client = _get_llm_client()
        # Cap the report length so the local model isn't overwhelmed.
        excerpt = report_markdown.strip()
        if len(excerpt) > 8000:
            excerpt = excerpt[:8000]
        system = (
            "You read a multi-agent social-simulation prediction report and extract a "
            "single calibrated probability that the underlying market resolves YES. "
            "Respond with ONLY a JSON object of the form {\"probability\": <number "
            "between 0 and 1>} and nothing else."
        )
        user = (
            f"Market question: {question.strip()}\n\n"
            f"Simulation report (Markdown):\n\"\"\"\n{excerpt}\n\"\"\"\n\n"
            "Based ONLY on this report, what probability (0.0-1.0) should we assign to "
            "the market resolving YES? Output only {\"probability\": <0..1>}."
        )
        raw = _call_llm(provider, client, system, user, max_tokens=120)
        return _parse_probability_blob(raw)
    except Exception:  # noqa: BLE001 - LLM unavailable / import error -> fallback
        return None


def _parse_probability_blob(raw: str) -> Optional[float]:
    """Pull a probability out of an LLM reply that should be {"probability": x}."""
    if not raw:
        return None
    # Try a JSON object containing "probability".
    for m in re.finditer(r"\{[^{}]*\}", raw, re.DOTALL):
        try:
            obj = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and "probability" in obj:
            return _coerce_prob(obj["probability"])
    # Bare "probability": 0.42 anywhere.
    m = re.search(r"probability\D{0,12}?(-?\d+(?:\.\d+)?\s*%?)", raw, re.IGNORECASE)
    if m:
        return _coerce_prob(m.group(1))
    # Last resort: first number / percentage in the reply.
    return _extract_probability_regex(raw)


def _coerce_prob(value: Any) -> Optional[float]:
    """Coerce a number or '42%' / '0.42' string into a [0,1] float."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            s = value.strip()
            is_pct = s.endswith("%")
            num = float(s.rstrip("%").strip())
            if is_pct:
                num /= 100.0
        else:
            num = float(value)
    except (TypeError, ValueError):
        return None
    # A value in (1, 100] is almost certainly a percent expressed without a sign.
    if num > 1.0 and num <= 100.0:
        num /= 100.0
    if num < 0.0 or num > 1.0:
        return None
    return num


def _extract_probability_regex(text: str) -> Optional[float]:
    """Heuristic scan for an explicit probability/percentage near YES-ish wording."""
    if not text:
        return None
    # Prefer numbers that sit near 'probability'/'likelihood'/'chance'/'YES'.
    near = re.search(
        r"(?:probability|likelihood|chance|odds|estimate)[^\d%]{0,40}?(\d{1,3}(?:\.\d+)?\s*%|0?\.\d+)",
        text, re.IGNORECASE | re.DOTALL,
    )
    if near:
        p = _coerce_prob(near.group(1))
        if p is not None:
            return p
    # Otherwise the first percentage anywhere.
    pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", text)
    if pct:
        p = _coerce_prob(pct.group(1) + "%")
        if p is not None:
            return p
    # Otherwise a bare 0.xx decimal.
    dec = re.search(r"\b(0?\.\d+)\b", text)
    if dec:
        return _coerce_prob(dec.group(1))
    return None


# ════════════════════════════════════════════════════════════════════════════
# polling helpers
# ════════════════════════════════════════════════════════════════════════════
def _deadline_left(deadline: float) -> float:
    return deadline - time.monotonic()


def _poll_graph_task(client: httpx.Client, task_id: str, deadline: float) -> dict:
    """Poll GET /api/graph/task/<id> until completed/failed or deadline. Returns task dict."""
    while True:
        task = _get(client, f"/api/graph/task/{task_id}")
        status = task.get("status")
        if status in ("completed", "failed"):
            return task
        if _deadline_left(deadline) <= 0:
            return task  # caller treats non-completed as timeout
        time.sleep(min(_POLL_GRAPH, max(1.0, _deadline_left(deadline))))


def _poll_prepare(client: httpx.Client, task_id: Optional[str], simulation_id: str, deadline: float) -> dict:
    """Poll POST /api/simulation/prepare/status until ready/completed/failed or deadline."""
    payload = {"simulation_id": simulation_id}
    if task_id:
        payload["task_id"] = task_id
    while True:
        st = _post_json(client, "/api/simulation/prepare/status", payload)
        status = st.get("status")
        if st.get("already_prepared") or status in ("ready", "completed", "failed"):
            return st
        if _deadline_left(deadline) <= 0:
            return st
        time.sleep(min(_POLL_PREPARE, max(1.0, _deadline_left(deadline))))


def _poll_run(client: httpx.Client, simulation_id: str, deadline: float) -> dict:
    """Poll GET /api/simulation/<id>/run-status until completed/stopped/failed or deadline."""
    last = {}
    while True:
        last = _get(client, f"/api/simulation/{simulation_id}/run-status")
        status = last.get("runner_status")
        if status in ("completed", "stopped", "failed"):
            return last
        if _deadline_left(deadline) <= 0:
            return last
        time.sleep(min(_POLL_RUN, max(1.0, _deadline_left(deadline))))


def _poll_report(client: httpx.Client, task_id: Optional[str], simulation_id: str, deadline: float) -> dict:
    """Poll POST /api/report/generate/status until completed/failed or deadline."""
    payload = {"simulation_id": simulation_id}
    if task_id:
        payload["task_id"] = task_id
    while True:
        st = _post_json(client, "/api/report/generate/status", payload)
        status = st.get("status")
        if st.get("already_completed") or status in ("completed", "failed"):
            return st
        if _deadline_left(deadline) <= 0:
            return st
        time.sleep(min(_POLL_REPORT, max(1.0, _deadline_left(deadline))))


# ════════════════════════════════════════════════════════════════════════════
# main entry point
# ════════════════════════════════════════════════════════════════════════════
def forecast_market(
    question: str,
    description: str = "",
    *,
    base: str = "http://localhost:5001",
    max_wait: int = 1800,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    platform: str = "reddit",
    project_name: Optional[str] = None,
    verbose: bool = False,
) -> dict:
    """Drive the full MiroFish pipeline for ONE market and extract P(YES).

    Args:
        question:     the market question (e.g. "Will <X> win the 2026 election?").
        description:  optional resolution / background text.
        base:         MiroFish backend base URL.
        max_wait:     total wall-clock budget in seconds across all stages.
        max_rounds:   simulation rounds (smallest viable = 1).
        platform:     which platform to run ("reddit" is the single fastest path;
                      "parallel" runs both). Both profile sets are always prepared.
        project_name: optional MiroFish project name.
        verbose:      print stage progress to stderr.

    Returns the result dict documented in the module docstring. Never raises.
    """
    result = {
        "ok": False,
        "crowd_probability": None,
        "report_markdown": "",
        "simulation_id": "",
        "stage_reached": STAGE_INIT,
        "error": "",
    }

    def log(msg: str) -> None:
        if verbose:
            print(f"[mirofish] {msg}", file=sys.stderr, flush=True)

    if not question or not question.strip():
        result["error"] = "question is empty"
        return result

    deadline = time.monotonic() + float(max_wait)
    base = base.rstrip("/")
    seed_text = _build_seed_text(question, description)
    requirement = _build_requirement(question)
    pname = project_name or f"poly_{abs(hash(question)) % 10_000_000}"

    try:
        with httpx.Client(base_url=base, timeout=_QUICK_TIMEOUT) as client:
            # ── 1. ontology / project creation (multipart, SYNC + slow) ──────
            log("stage 1/7: generating ontology + project (synchronous LLM call)...")
            files = {"files": ("seed.md", seed_text.encode("utf-8"), "text/markdown")}
            form = {
                "simulation_requirement": requirement,
                "project_name": pname,
                "additional_context": "Keep the simulated world small and focused on this single event.",
            }
            resp = client.post(
                "/api/graph/ontology/generate",
                files=files, data=form, timeout=_ONTOLOGY_TIMEOUT,
            )
            data = _unwrap(resp)
            project_id = data.get("project_id")
            if not project_id:
                result["error"] = "ontology/generate returned no project_id"
                return result
            result["stage_reached"] = STAGE_PROJECT_CREATED
            log(f"  project_id={project_id} "
                f"(entity_types={len(data.get('ontology', {}).get('entity_types', []))})")

            # ── 2. graph build (async -> poll task) ──────────────────────────
            log("stage 2/7: building Zep knowledge graph...")
            data = _post_json(client, "/api/graph/build", {
                "project_id": project_id,
                "graph_name": pname,
                "chunk_size": _DEFAULT_CHUNK_SIZE,
                "chunk_overlap": _DEFAULT_CHUNK_OVERLAP,
            })
            build_task_id = data.get("task_id")
            result["stage_reached"] = STAGE_GRAPH_BUILDING
            task = _poll_graph_task(client, build_task_id, deadline)
            if task.get("status") != "completed":
                result["error"] = (
                    f"graph build did not complete (status={task.get('status')}, "
                    f"msg={task.get('message')}, err={task.get('error')})"
                )
                return result
            graph_id = (task.get("result") or {}).get("graph_id")
            result["stage_reached"] = STAGE_GRAPH_BUILT
            log(f"  graph built: graph_id={graph_id}, "
                f"nodes={(task.get('result') or {}).get('node_count')}")

            # ── 3. create simulation ─────────────────────────────────────────
            log("stage 3/7: creating simulation...")
            data = _post_json(client, "/api/simulation/create", {
                "project_id": project_id,
                # Both platforms enabled so prepare emits BOTH profile files
                # (the 'prepared' check requires reddit_profiles.json AND
                # twitter_profiles.csv). We still only RUN one platform below.
                "enable_twitter": True,
                "enable_reddit": True,
            })
            simulation_id = data.get("simulation_id")
            if not simulation_id:
                result["error"] = "simulation/create returned no simulation_id"
                return result
            result["simulation_id"] = simulation_id
            result["stage_reached"] = STAGE_SIM_CREATED
            log(f"  simulation_id={simulation_id}")

            # ── 4. prepare simulation (async -> poll prepare/status) ─────────
            log("stage 4/7: preparing simulation (profiles + config via LLM)...")
            data = _post_json(client, "/api/simulation/prepare", {
                "simulation_id": simulation_id,
                "use_llm_for_profiles": True,
                "parallel_profile_count": _DEFAULT_PARALLEL_PROFILES,
            })
            prepare_task_id = data.get("task_id")
            result["stage_reached"] = STAGE_SIM_PREPARING
            if data.get("already_prepared"):
                log("  already prepared")
            else:
                log(f"  prepare task={prepare_task_id}, "
                    f"expected_agents={data.get('expected_entities_count')}")
            st = _poll_prepare(client, prepare_task_id, simulation_id, deadline)
            prepared = bool(st.get("already_prepared")) or st.get("status") in ("ready", "completed")
            if not prepared:
                result["error"] = (
                    f"simulation prepare did not finish (status={st.get('status')}, "
                    f"msg={st.get('message')}, err={st.get('error')})"
                )
                return result
            result["stage_reached"] = STAGE_SIM_PREPARED
            log("  simulation prepared")

            # ── 5. start simulation (subprocess -> poll run-status) ──────────
            log(f"stage 5/7: starting simulation (platform={platform}, max_rounds={max_rounds})...")
            _post_json(client, "/api/simulation/start", {
                "simulation_id": simulation_id,
                "platform": platform,
                "max_rounds": int(max_rounds),
                "force": True,
            })
            result["stage_reached"] = STAGE_SIM_RUNNING
            run = _poll_run(client, simulation_id, deadline)
            run_status = run.get("runner_status")
            if run_status not in ("completed", "stopped"):
                result["error"] = (
                    f"simulation run did not complete (runner_status={run_status}, "
                    f"round={run.get('current_round')}/{run.get('total_rounds')})"
                )
                return result
            result["stage_reached"] = STAGE_SIM_DONE
            log(f"  simulation finished: status={run_status}, "
                f"rounds={run.get('current_round')}/{run.get('total_rounds')}, "
                f"actions={run.get('total_actions_count')}")

            # ── 6. generate report (async -> poll generate/status) ───────────
            log("stage 6/7: generating prediction report...")
            data = _post_json(client, "/api/report/generate", {
                "simulation_id": simulation_id,
            })
            report_id = data.get("report_id")
            report_task_id = data.get("task_id")
            result["stage_reached"] = STAGE_REPORT_GENERATING
            log(f"  report_id={report_id}, task={report_task_id}")
            st = _poll_report(client, report_task_id, simulation_id, deadline)
            if not (st.get("already_completed") or st.get("status") == "completed"):
                result["error"] = (
                    f"report generation did not complete (status={st.get('status')}, "
                    f"msg={st.get('message')}, err={st.get('error')})"
                )
                return result
            # status payload may carry the report_id (e.g. already_completed path)
            report_id = st.get("report_id") or report_id

            # ── 7. fetch report markdown ─────────────────────────────────────
            report = _get(client, f"/api/report/{report_id}")
            markdown = report.get("markdown_content") or ""
            result["report_markdown"] = markdown
            result["stage_reached"] = STAGE_REPORT_DONE
            log(f"  report fetched: {len(markdown)} chars, status={report.get('status')}")
            if not markdown.strip():
                result["error"] = "report completed but markdown_content was empty"
                return result

        # ── 8. extract probability (one extra local-LLM call + regex fallback)
        log("stage 7/7: extracting YES probability from report...")
        prob = _extract_probability(markdown, question)
        result["crowd_probability"] = prob
        if prob is not None:
            result["stage_reached"] = STAGE_PROBABILITY_EXTRACTED
        result["ok"] = True
        log(f"  crowd_probability={prob}")
        return result

    except _MiroFishError as e:
        result["error"] = f"MiroFish API error at {result['stage_reached']}: {e}"
        return result
    except httpx.HTTPError as e:
        result["error"] = f"HTTP/transport error at {result['stage_reached']}: {e}"
        return result
    except Exception as e:  # noqa: BLE001 - defensive: never raise to caller
        result["error"] = f"unexpected error at {result['stage_reached']}: {type(e).__name__}: {e}"
        return result


# ── CLI for quick manual testing ──────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python mirofish.py \"<question>\" [\"<description>\"] "
              "[--max-wait SECONDS] [--rounds N] [--platform reddit|twitter|parallel]",
              file=sys.stderr)
        return 2
    question = argv[0]
    description = ""
    max_wait = 1800
    rounds = _DEFAULT_MAX_ROUNDS
    platform = "reddit"
    i = 1
    if i < len(argv) and not argv[i].startswith("--"):
        description = argv[i]
        i += 1
    while i < len(argv):
        if argv[i] == "--max-wait" and i + 1 < len(argv):
            max_wait = int(argv[i + 1]); i += 2
        elif argv[i] == "--rounds" and i + 1 < len(argv):
            rounds = int(argv[i + 1]); i += 2
        elif argv[i] == "--platform" and i + 1 < len(argv):
            platform = argv[i + 1]; i += 2
        else:
            i += 1
    out = forecast_market(
        question, description,
        max_wait=max_wait, max_rounds=rounds, platform=platform, verbose=True,
    )
    printable = dict(out)
    md = printable.get("report_markdown") or ""
    printable["report_markdown_len"] = len(md)
    printable["report_markdown"] = (md[:1500] + " ...[truncated]") if len(md) > 1500 else md
    print(json.dumps(printable, indent=2, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
