"""harness/mirofish_validate.py — strict MiroFish result contract + freshness validation.

Makes MiroFish HONEST: every market either gets a fresh, market-specific report or is
explicitly marked degraded/unusable with a recorded reason. A stale (e.g. June-13) report
is NEVER accepted for a later forecast, an empty/weak report is rejected, and the pipeline
can REQUIRE a usable report before betting. Paper-only; never fakes success.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


# ── config (Phase 2) ────────────────────────────────────────────────────────────
def _flag(name, default):
    v = os.getenv(name)
    return default if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _valid_threshold() -> float:
    # the question-match gate may only be made STRICTER than the vetted default 0.30, never
    # weaker: out-of-range OR tiny-positive values (which would silently disable the gate)
    # fall back to / are floored at the default. Effective threshold is always in [0.30, 1.0].
    v = _f("MIROFISH_MATCH_THRESHOLD", 0.30)
    return max(v, 0.30) if 0.0 < v <= 1.0 else 0.30


def config() -> dict:
    return {
        "FORCE_FRESH": _flag("MIROFISH_FORCE_FRESH", True),
        "ALLOW_CACHE": _flag("MIROFISH_ALLOW_CACHE", False),
        "MAX_AGE": _f("MIROFISH_MAX_REPORT_AGE_SECONDS", 900),
        "MIN_POSTS": int(_f("MIROFISH_MIN_POSTS", 3)),
        "MIN_CHARS": int(_f("MIROFISH_MIN_REPORT_CHARS", 500)),
        "REQUIRE_QUESTION_MATCH": _flag("MIROFISH_REQUIRE_QUESTION_MATCH", True),
        "REQUIRE_PROBABILITY": _flag("MIROFISH_REQUIRE_PROBABILITY", False),
        "MODE": (os.getenv("MIROFISH_MODE", "degraded") or "degraded").strip().lower(),
        "MATCH_THRESHOLD": _valid_threshold(),
    }


# ── the strict result contract (Phase 1) ────────────────────────────────────────
@dataclass
class MiroFishResult:
    market_id: str = ""
    question: str = ""
    question_hash: str = ""
    ok: bool = False                 # backend completed technically
    usable: bool = False             # fresh + market-specific + non-empty + passes checks
    degraded: bool = True            # MiroFish failed/weak -> observe-only unless config allows
    requested_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    report_generated_at: str | None = None
    report_age_seconds: float | None = None
    simulation_id: str = ""
    report_id: str = ""
    project_id: str = ""
    stage_reached: str = ""
    crowd_probability: float | None = None
    n_posts: int = 0
    report_markdown: str = ""
    report_markdown_hash: str = ""
    freshness_status: str = "missing"   # fresh | stale | unknown | missing | failed
    question_match_score: float = 0.0
    error: str | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["report_markdown"] = self.report_markdown[:400]   # keep the row small
        return d


# ── identity (Phase 2): force a FRESH project per market+run+timestamp ────────────
def fresh_project_name(market_id: str, run_id: str | None = None, now_iso: str | None = None) -> str:
    mid = (str(market_id or "x"))[:14].replace("0x", "")
    rid = (str(run_id or "run"))[-8:]
    stamp = (now_iso or _now()).replace(":", "").replace("-", "")[-12:]
    return f"poly_{mid}_{rid}_{stamp}"


def question_hash(question: str) -> str:
    return hashlib.sha256((question or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_STOP = {"will", "the", "a", "an", "of", "to", "in", "on", "by", "for", "be", "is", "are",
         "and", "or", "win", "2024", "2025", "2026", "2027", "2028", "this", "that", "his", "her"}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-zA-Z][a-zA-Z']+", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def question_match_score(question: str, report_markdown: str, posts) -> float:
    """0..1 overlap of the question's content words with the report + crowd posts."""
    q = _tokens(question)
    if not q:
        return 0.0
    blob = (report_markdown or "") + " " + " ".join(posts or [])
    hay = _tokens(blob)
    if not hay:
        return 0.0
    return round(len(q & hay) / len(q), 4)


def _parse_ts(s):
    if not s:
        return None
    try:
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── build + validate (Phases 1, 3) ───────────────────────────────────────────────
def build_result(raw: dict, market_id: str, question: str, requested_at: str,
                 sig: dict | None = None, started_at: str | None = None) -> MiroFishResult:
    raw = raw or {}
    sig = sig or {}
    md = raw.get("report_markdown") or raw.get("report") or ""
    posts = (sig.get("posts") or [])
    cp = sig.get("probability")
    if cp is None:
        cp = raw.get("crowd_probability")
    r = MiroFishResult(
        market_id=str(market_id or ""), question=question or "",
        question_hash=question_hash(question),
        ok=bool(raw.get("ok")),
        requested_at=requested_at, started_at=started_at, completed_at=_now(),
        report_generated_at=raw.get("report_generated_at"),
        simulation_id=str(raw.get("simulation_id") or ""),
        report_id=str(raw.get("report_id") or ""),
        project_id=str(raw.get("project_id") or ""),
        stage_reached=str(raw.get("stage_reached") or ""),
        crowd_probability=cp, n_posts=int(sig.get("n_posts", len(posts)) or 0),
        report_markdown=md, report_markdown_hash=hashlib.sha256(md.encode("utf-8")).hexdigest()[:16] if md else "",
        error=(raw.get("error") or None),
    )
    r.question_match_score = question_match_score(question, md, posts)
    return r


def validate(result: MiroFishResult, cfg: dict | None = None, now_iso: str | None = None) -> MiroFishResult:
    """Set usable / degraded / freshness_status / warnings per the strict rules."""
    cfg = cfg or config()
    w = result.warnings
    now = _parse_ts(now_iso or _now())

    # technical failure -> failed
    if not result.ok or result.error:
        result.freshness_status = "failed"
        result.usable, result.degraded = False, True
        if result.error:
            w.append(f"backend_error: {str(result.error)[:80]}")
        return result

    # freshness (Phase 3 #5/#6): a report generated BEFORE the request, or older than
    # MAX_AGE, is STALE and rejected. No timestamp + FORCE_FRESH -> trust the unique
    # project name (fresh by construction); no timestamp without FORCE_FRESH -> unknown.
    gen = _parse_ts(result.report_generated_at)
    req = _parse_ts(result.requested_at)
    if gen is not None:
        try:
            result.report_age_seconds = round((now - gen).total_seconds(), 1)
        except Exception:
            result.report_age_seconds = None
        if result.report_age_seconds is not None and result.report_age_seconds < -120.0:
            # generated well in the FUTURE (beyond benign clock skew / sub-second truncation)
            # -> spoofed/invalid timestamp, unverifiable, reject
            result.freshness_status = "stale"
            w.append("report timestamp is in the future (invalid/unverifiable)")
        elif req is not None and gen < req:
            result.freshness_status = "stale"
            w.append("report generated BEFORE this request started (reused/stale)")
        elif result.report_age_seconds is not None and result.report_age_seconds > cfg["MAX_AGE"]:
            result.freshness_status = "stale"
            w.append(f"report age {result.report_age_seconds:.0f}s > max {cfg['MAX_AGE']:.0f}s")
        else:
            result.freshness_status = "fresh"
    else:
        result.freshness_status = "fresh" if cfg["FORCE_FRESH"] else "unknown"
        if not cfg["FORCE_FRESH"]:
            w.append("no report timestamp and FORCE_FRESH off — cannot verify freshness")

    # content / identity checks
    has_report = len(result.report_markdown.strip()) >= cfg["MIN_CHARS"]
    has_posts = result.n_posts >= cfg["MIN_POSTS"]
    if not result.simulation_id:
        w.append("missing simulation_id")
    if not (has_report or has_posts):
        w.append(f"weak: report {len(result.report_markdown)} chars < {cfg['MIN_CHARS']} "
                 f"and posts {result.n_posts} < {cfg['MIN_POSTS']}")
    if cfg["REQUIRE_QUESTION_MATCH"] and result.question_match_score < cfg["MATCH_THRESHOLD"]:
        w.append(f"question_match {result.question_match_score:.2f} < {cfg['MATCH_THRESHOLD']:.2f} "
                 f"(report may be about a different market)")
    if cfg["REQUIRE_PROBABILITY"] and result.crowd_probability is None:
        w.append("probability required but extraction failed")

    # an incomplete simulation is NOT a completed result, regardless of any filler text it
    # carries — a not-yet-finished sim can never be usable (defends the recorded `usable`
    # column and the dashboard, which both key off it). WHITELIST the terminal stages only
    # (mirofish.py ends at report_done / probability_extracted): any other stage — including
    # sim_running, sim_done, or an empty/unknown stage — is incomplete.
    _stage = (result.stage_reached or "").strip().lower()
    _incomplete = _stage not in ("report_done", "probability_extracted")
    if _incomplete:
        w.append(f"incomplete simulation (stage={_stage or 'unknown'}) — not a completed result")

    usable = (
        not _incomplete
        and result.freshness_status == "fresh"
        and bool(result.simulation_id)
        and (has_report or has_posts)
        and not (cfg["REQUIRE_QUESTION_MATCH"] and result.question_match_score < cfg["MATCH_THRESHOLD"])
        and not (cfg["REQUIRE_PROBABILITY"] and result.crowd_probability is None)
    )
    result.usable = bool(usable)
    result.degraded = not result.usable
    return result


def status_label(result: MiroFishResult) -> str:
    if result.freshness_status == "failed":
        return "FAILED"
    if result.usable:
        return "FRESH"
    if result.freshness_status == "stale":
        return "STALE"
    return "WEAK"


# ── mirofish_runs table (Phase 5) ────────────────────────────────────────────────
def _db_path() -> str:
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def init_runs_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            """CREATE TABLE IF NOT EXISTS mirofish_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT, forecast_id TEXT, question TEXT, question_hash TEXT,
                requested_at TEXT, completed_at TEXT, report_generated_at TEXT,
                report_age_seconds REAL, project_id TEXT, simulation_id TEXT, report_id TEXT,
                report_hash TEXT, crowd_probability REAL, n_posts INTEGER, report_chars INTEGER,
                ok INTEGER, usable INTEGER, degraded INTEGER, freshness_status TEXT,
                stage_reached TEXT, min_sims_used REAL, match_threshold_used REAL,
                question_match_score REAL, error TEXT, warnings_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mfruns_mkt ON mirofish_runs(market_id)")
        # forward-migrations (additive + idempotent): older DBs lack stage_reached (so the
        # dashboard can tell a pending sim from a completed one) and the FROZEN decision-time
        # thresholds (so a historical run's used-flag never flips when config later changes).
        for _col, _type in (("stage_reached", "TEXT"),
                            ("min_sims_used", "REAL"),
                            ("match_threshold_used", "REAL")):
            try:
                conn.execute(f"ALTER TABLE mirofish_runs ADD COLUMN {_col} {_type}")
            except Exception:
                pass   # already present
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def record_run(result: MiroFishResult, forecast_id: str | None = None) -> bool:
    import json
    # FREEZE the decision-time contribution thresholds the canonical state machine uses, so a
    # later config change can never flip this run's historical mirofish_used on the dashboard.
    try:
        from harness import mirofish_status as _mfs
        _min_sims, _match_thr = float(_mfs.min_sims()), float(_mfs._match_threshold())
    except Exception:
        # NEVER leave the frozen thresholds null on a new row — that would make state_from_row
        # fall back to live config and let the historical used-flag flip. Replicate the
        # canonical defaults directly from env so a new row is always self-describing.
        try:
            _ms = os.getenv("MIROFISH_MIN_SIMS") or os.getenv("MIROFISH_MIN_POSTS") or "3"
            _min_sims = float(int(float(_ms)))
        except Exception:
            _min_sims = 3.0
        _match_thr = _valid_threshold()
    try:
        conn = sqlite3.connect(_db_path())
        init_runs_db(conn)
        conn.execute(
            """INSERT INTO mirofish_runs (market_id, forecast_id, question, question_hash,
               requested_at, completed_at, report_generated_at, report_age_seconds, project_id,
               simulation_id, report_id, report_hash, crowd_probability, n_posts, report_chars,
               ok, usable, degraded, freshness_status, stage_reached, min_sims_used,
               match_threshold_used, question_match_score, error, warnings_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result.market_id, forecast_id, result.question, result.question_hash,
             result.requested_at, result.completed_at, result.report_generated_at,
             result.report_age_seconds, result.project_id, result.simulation_id, result.report_id,
             result.report_markdown_hash, result.crowd_probability, result.n_posts,
             len(result.report_markdown or ""), int(result.ok), int(result.usable),
             int(result.degraded), result.freshness_status, result.stage_reached,
             _min_sims, _match_thr,
             result.question_match_score, result.error, json.dumps(result.warnings)))
        conn.commit(); conn.close()
        # Live telemetry (best-effort, never affects the run): emit the canonical MiroFish state
        # so the dashboard stream shows REAL mirofish stages. used != alive (Plan 8 honesty).
        try:
            from harness import live_events as _le, mirofish_status as _mfs2
            row = {"usable": int(result.usable), "freshness_status": result.freshness_status,
                   "stage_reached": result.stage_reached, "report_age_seconds": result.report_age_seconds,
                   "question_match_score": result.question_match_score, "n_posts": result.n_posts,
                   "min_sims_used": _min_sims, "match_threshold_used": _match_thr}
            state = _mfs2.state_from_row(row)
            _le.emit("mirofish.state", "mirofish", market_id=result.market_id,
                     question=result.question, stage=result.stage_reached, status=state,
                     message=f"MiroFish {state} ({result.freshness_status})",
                     data={"state": state, "usable": bool(result.usable),
                           "freshness_status": result.freshness_status})
        except Exception:
            pass
        return True
    except Exception:
        return False


def get_runs(market_id: str | None = None, limit: int = 20) -> list[dict]:
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        if market_id:
            rows = conn.execute("SELECT * FROM mirofish_runs WHERE market_id=? ORDER BY id DESC LIMIT ?",
                                (market_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM mirofish_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
