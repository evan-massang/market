"""harness/mirofish_status.py — Plan 8: the canonical MiroFish CONTRIBUTION state machine.

`harness.mirofish_validate` already decides whether a single MiroFish result is fresh,
market-matched, and complete (its ``usable`` flag). This module sits on top of it and
answers the HONESTY question Plan 8 cares about: *was a fresh, completed, same-market
MiroFish result actually CONSUMED by the decision?* — the only case in which
``mirofish_used`` may be true.

Canonical states (exactly one per decision):
  not_configured · disabled · backend_unavailable · launch_only_not_used · pending ·
  timed_out · failed · invalid_result · stale_result · market_mismatch ·
  fresh_unused · fresh_used

Key distinctions Plan 8 enforces:
  * a backend being ALIVE is NOT a contribution.
  * launching MiroFish fire-and-forget is ALWAYS ``launch_only_not_used`` (mirofish_used=false).
  * pending / sim_prepared / stale / failed / invalid / wrong-market are NEVER used.
  * a stale result may be shown for DISPLAY only (never for a bet).
  * ``fresh_used`` (mirofish_used=true) requires a fresh+valid result that the decision
    logic actually consumed — set via :func:`mark_used` by the caller AFTER it consumes it.
  * optional MiroFish failure does not block betting (allow_bet stays true) but is recorded
    unused/degraded; required MiroFish failure blocks with a specific no-bet reason.

Pure-ish module (os + the existing validator); no network. Paper-only.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

# The ONLY pipeline stages that mean the MiroFish run actually COMPLETED (see mirofish.py
# STAGE_* ladder: init -> ... -> sim_running -> sim_done -> report_generating -> report_done
# -> probability_extracted). A WHITELIST is used deliberately: anything that is not a terminal
# stage (sim_running, sim_prepared, graph_building, an empty/unknown stage, a future stage the
# backend may add) is treated as INCOMPLETE and can never be a contribution.
_COMPLETE_STAGES = ("report_done", "probability_extracted")

# Tolerance for benign clock skew / sub-second timestamp truncation between the MiroFish
# backend and the harness. A report dated slightly "ahead" is skew; one dated well into the
# future is a spoofed/invalid timestamp and is rejected as unverifiable.
_FUTURE_SKEW_SECONDS = 120.0


def _incomplete_stage(stage) -> bool:
    """True unless ``stage`` is a known terminal/completed stage."""
    return (str(stage or "").strip().lower()) not in _COMPLETE_STAGES


def _parse_ts(s):
    """Parse an ISO timestamp -> aware datetime, or None if absent/unparseable."""
    if not s:
        return None
    try:
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _age_seconds(gen, now_iso=None):
    """Current age of a parsed generation time, in seconds (None if uncomputable)."""
    now = _parse_ts(now_iso) if now_iso else datetime.now(timezone.utc)
    if gen is None or now is None:
        return None
    try:
        return (now - gen).total_seconds()
    except Exception:
        return None

# ── canonical states ──────────────────────────────────────────────────────────
NOT_CONFIGURED = "not_configured"
DISABLED = "disabled"
BACKEND_UNAVAILABLE = "backend_unavailable"
LAUNCH_ONLY_NOT_USED = "launch_only_not_used"
PENDING = "pending"
TIMED_OUT = "timed_out"
FAILED = "failed"
INVALID_RESULT = "invalid_result"
STALE_RESULT = "stale_result"
MARKET_MISMATCH = "market_mismatch"
FRESH_UNUSED = "fresh_unused"
FRESH_USED = "fresh_used"

# states in which a result may be CONSUMED by a decision (only fresh ones)
_USABLE_STATES = {FRESH_UNUSED, FRESH_USED}

# ── per-state "why not used / what is it" reasons (validator output) ───────────
_STATE_REASON = {
    NOT_CONFIGURED: "mirofish_not_configured",
    DISABLED: "mirofish_disabled",
    BACKEND_UNAVAILABLE: "mirofish_backend_unavailable",
    LAUNCH_ONLY_NOT_USED: "mirofish_launch_only_not_used",
    PENDING: "mirofish_pending_not_used",
    TIMED_OUT: "mirofish_timeout_not_used",
    FAILED: "mirofish_failed_not_used",
    INVALID_RESULT: "mirofish_invalid_result_not_used",
    STALE_RESULT: "mirofish_stale_result_not_used",
    MARKET_MISMATCH: "mirofish_market_mismatch_not_used",
    FRESH_UNUSED: "mirofish_fresh_unused",
    FRESH_USED: "mirofish_fresh_used",
}

# ── required-mode no-bet reasons (Plan 8 §9) ──────────────────────────────────
_REQUIRED_NO_BET = {
    BACKEND_UNAVAILABLE: "mirofish_required_unavailable_no_bet",
    NOT_CONFIGURED: "mirofish_required_unavailable_no_bet",
    DISABLED: "mirofish_required_unavailable_no_bet",
    LAUNCH_ONLY_NOT_USED: "mirofish_required_launch_only_no_bet",
    PENDING: "mirofish_required_pending_no_bet",
    TIMED_OUT: "mirofish_required_timeout_no_bet",
    FAILED: "mirofish_required_failed_no_bet",
    INVALID_RESULT: "mirofish_required_invalid_no_bet",
    STALE_RESULT: "mirofish_required_stale_no_bet",
    MARKET_MISMATCH: "mirofish_required_market_mismatch_no_bet",
}
LOW_SIMS_REQUIRED_NO_BET = "mirofish_required_low_sims_no_bet"


# ── config (Plan 8 §3) ─────────────────────────────────────────────────────────
def _flag(name, default):
    v = os.getenv(name)
    return default if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")


def _f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def required_for_bet() -> bool:
    """A usable MiroFish result is REQUIRED to bet. Default false (optional). Also true
    when the legacy MIROFISH_MODE=required is set (backward-compatible bridge)."""
    if _flag("MIROFISH_REQUIRED_FOR_BET", False):
        return True
    return (os.getenv("MIROFISH_MODE", "degraded") or "").strip().lower() == "required"


def max_age_seconds() -> float:
    # accept the Plan-8 name OR the existing MIROFISH_MAX_REPORT_AGE_SECONDS
    if os.getenv("MIROFISH_MAX_AGE_SECONDS") is not None:
        return _f("MIROFISH_MAX_AGE_SECONDS", 900)
    return _f("MIROFISH_MAX_REPORT_AGE_SECONDS", 900)


def min_sims() -> int:
    # project-appropriate: MiroFish here distils a handful of crowd posts, so the floor
    # mirrors MIROFISH_MIN_POSTS rather than a literal 100 (documented in the report).
    if os.getenv("MIROFISH_MIN_SIMS") is not None:
        return int(_f("MIROFISH_MIN_SIMS", 3))
    return int(_f("MIROFISH_MIN_POSTS", 3))


def allow_stale_for_display() -> bool:
    return _flag("MIROFISH_ALLOW_STALE_FOR_DISPLAY", True)


def allow_stale_for_bet() -> bool:
    return _flag("MIROFISH_ALLOW_STALE_FOR_BET", False)


_MATCH_THRESHOLD_DEFAULT = 0.30


def _match_threshold() -> float:
    # The market-match gate may only be made STRICTER than the vetted default — never weaker.
    # A non-positive / >1 / tiny-positive threshold (e.g. 0.0001) would silently disable the
    # gate (`qms < 0.0001` almost never fires), letting a wrong-market report through. So an
    # out-of-range value falls back to the default and any in-range value is floored AT the
    # default: the effective threshold is always in [0.30, 1.0].
    val = _f("MIROFISH_MATCH_THRESHOLD", _MATCH_THRESHOLD_DEFAULT)
    if not (0.0 < val <= 1.0):
        return _MATCH_THRESHOLD_DEFAULT
    return max(val, _MATCH_THRESHOLD_DEFAULT)


# ── canonical result builder ───────────────────────────────────────────────────
def _status(state, *, required, mirofish_used=False, **fields) -> dict:
    allow_use = state in _USABLE_STATES
    # optional: failure never blocks the bet. required: only a usable (fresh) result allows.
    allow_bet = True if not required else allow_use
    out = {
        "ok": state in _USABLE_STATES,
        "state": state,
        "mirofish_used": bool(mirofish_used),
        "allow_decision_use": bool(allow_use),
        "allow_bet": bool(allow_bet),
        "required": bool(required),
        "reason": _STATE_REASON.get(state, state),
        "contribution": "fresh_used" if mirofish_used else "none",
        "market_id": None, "question": None, "requested_at": None, "completed_at": None,
        "age_seconds": None, "max_age_seconds": max_age_seconds(), "sim_status": None,
        "n_sims": None, "summary": None, "score": None, "warnings": [], "raw_path": None,
    }
    out.update(fields)
    return out


def disabled(required: bool | None = None) -> dict:
    req = required_for_bet() if required is None else required
    return _status(DISABLED, required=req)


def not_configured(required: bool | None = None) -> dict:
    req = required_for_bet() if required is None else required
    return _status(NOT_CONFIGURED, required=req)


def backend_unavailable(required: bool | None = None, reason: str = "") -> dict:
    req = required_for_bet() if required is None else required
    return _status(BACKEND_UNAVAILABLE, required=req, warnings=[reason] if reason else [])


def launch_only(market_id=None, question=None, required: bool | None = None) -> dict:
    """The same-day fire-and-forget path: MiroFish was LAUNCHED but its result is NOT read
    into this decision. Always mirofish_used=false / launch_only_not_used."""
    req = required_for_bet() if required is None else required
    return _status(LAUNCH_ONLY_NOT_USED, required=req, market_id=market_id, question=question)


def from_result(result, *, required: bool | None = None, consumed: bool = False) -> dict:
    """Map a validated ``mirofish_validate.MiroFishResult`` to a canonical status dict.

    ``consumed`` is whether the decision logic actually fed this result into a gate/score/
    context; it may be True ONLY for a fresh+usable result (otherwise it is ignored — a
    stale/pending/failed result can never be "used")."""
    req = required_for_bet() if required is None else required
    fs = getattr(result, "freshness_status", "missing")
    stage = (getattr(result, "stage_reached", "") or "").lower()
    err = getattr(result, "error", None)
    usable = bool(getattr(result, "usable", False))
    qms = float(getattr(result, "question_match_score", 0.0) or 0.0)
    n_posts = int(getattr(result, "n_posts", 0) or 0)

    age = getattr(result, "report_age_seconds", None)   # set by validate() ONLY when the
                                                          # timestamp actually parses (verifiable)
    if err and "backend unavailable" in str(err).lower():
        state = BACKEND_UNAVAILABLE
    elif fs == "failed" or err:
        state = FAILED
    elif _incomplete_stage(stage):
        # the run did NOT reach a terminal stage (sim_running/sim_prepared/…/empty) -> PENDING
        # and NEVER used, even if the validator marked it usable on filler text (the leak the
        # audit flagged): a not-yet-finished sim is not a contribution, whatever its length.
        state = PENDING
    elif fs == "stale":
        state = STALE_RESULT
    elif qms < _match_threshold():
        # the report is about a DIFFERENT market. Enforced INDEPENDENTLY of the validator's
        # `usable` flag, so disabling MIROFISH_REQUIRE_QUESTION_MATCH cannot smuggle a
        # wrong-market report into the decision.
        state = MARKET_MISMATCH
    elif usable and n_posts and n_posts < min_sims():
        state = INVALID_RESULT         # too few sims/posts to trust (low_sims)
    elif usable and (age is None or age < -_FUTURE_SKEW_SECONDS):
        # freshness UNVERIFIABLE: the completion timestamp is absent, unparseable (truthy
        # garbage leaves age None), or well in the FUTURE (a spoofed timestamp — beyond benign
        # clock skew). Plan 8 requires a VERIFIABLE generated_at to call a result a contribution.
        state = INVALID_RESULT
    elif usable:
        state = FRESH_USED if consumed else FRESH_UNUSED
    else:
        state = INVALID_RESULT

    used = bool(consumed and state == FRESH_USED)
    return _status(
        state, required=req, mirofish_used=used,
        market_id=getattr(result, "market_id", None) or None,
        question=getattr(result, "question", None) or None,
        requested_at=getattr(result, "requested_at", None),
        completed_at=getattr(result, "completed_at", None),
        age_seconds=getattr(result, "report_age_seconds", None),
        sim_status=stage or None,
        n_sims=n_posts or None,
        score=getattr(result, "crowd_probability", None),
        summary=(getattr(result, "report_markdown", "") or "")[:160] or None,
        warnings=list(getattr(result, "warnings", []) or []),
    )


def state_from_row(row: dict) -> str:
    """Map a persisted mirofish_runs row (dict) to its canonical state AS RECORDED — the
    IMMUTABLE historical fact of whether that run actually contributed to its decision.

    This deliberately uses the FROZEN, recorded ``report_age_seconds`` (computed at decision
    time), NOT a re-computed "now" age. Whether MiroFish was fed to the swarm in a past run is
    a historical fact and must not flip between the decision and a later dashboard view. For
    the time-varying "is this report still fresh right now?" question, use :func:`is_stale_now`
    — a separate display-only signal. A run is ``fresh_used`` ONLY if it was usable, its market
    matched, its pipeline reached a terminal stage, and its recorded age is verifiable
    (present, non-negative). Anything else is failed/pending/stale/market_mismatch/invalid."""
    if not isinstance(row, dict):
        return INVALID_RESULT
    # Use the FROZEN decision-time thresholds recorded with the run when present, so a later
    # config change can never flip this historical verdict; fall back to the current config for
    # legacy rows that predate the frozen columns.
    def _frozen(key, default_fn):
        v = row.get(key)
        try:
            return float(v) if v is not None else default_fn()
        except (TypeError, ValueError):
            return default_fn()
    match_thr = _frozen("match_threshold_used", _match_threshold)
    min_n = _frozen("min_sims_used", min_sims)
    fs = (row.get("freshness_status") or "missing").lower()
    stage = row.get("stage_reached")
    if fs == "failed":
        return FAILED
    if _incomplete_stage(stage):
        return PENDING                 # did not reach a terminal stage -> never a contribution
    if fs == "stale":
        return STALE_RESULT
    try:
        qms = float(row.get("question_match_score") or 0.0)
    except (TypeError, ValueError):
        qms = 0.0
    if qms < match_thr:
        return MARKET_MISMATCH         # different-market report (independent of `usable`)
    if bool(row.get("usable")):
        try:
            n_posts = int(row.get("n_posts") or 0)
        except (TypeError, ValueError):
            n_posts = 0
        if n_posts and n_posts < min_n:
            return INVALID_RESULT       # too few sims/posts — mirror from_result's low_sims gate
        age = row.get("report_age_seconds")     # FROZEN at decision time (verifiable iff set)
        if age is None or age < -_FUTURE_SKEW_SECONDS:
            return INVALID_RESULT       # no/garbage/future timestamp -> freshness unverifiable
        return FRESH_USED
    return INVALID_RESULT


def is_stale_now(row: dict, *, now_iso: str | None = None) -> bool:
    """Is this recorded report stale RIGHT NOW — re-aged from report_generated_at against the
    CURRENT MAX_AGE? Display-only signal, SEPARATE from the immutable mirofish_used (whether it
    was fed to the swarm in that run). True if the timestamp is missing, unparseable, in the
    future, or older than the current MAX_AGE. Lets the dashboard say "used in this run, but the
    report is now stale" without dishonestly flipping the historical contribution flag."""
    if not isinstance(row, dict):
        return True
    gen = _parse_ts(row.get("report_generated_at"))
    if gen is None:
        return True
    age = _age_seconds(gen, now_iso)
    return age is None or age < -_FUTURE_SKEW_SECONDS or age > max_age_seconds()


def mark_used(status: dict, contribution: str = "context_only") -> dict:
    """Flip a fresh_unused status to fresh_used AFTER the decision actually consumed it.
    A no-op (returns unchanged) unless the result is fresh+usable — a stale/pending/failed
    result can NEVER become used."""
    if not isinstance(status, dict) or not status.get("allow_decision_use"):
        return status
    status = dict(status)
    status["state"] = FRESH_USED
    status["mirofish_used"] = True
    status["reason"] = _STATE_REASON[FRESH_USED]
    status["contribution"] = contribution
    return status


def required_no_bet_reason(status: dict, *, prefix: str = "") -> str | None:
    """When MiroFish is REQUIRED and not used, the specific no-bet reason; else None.
    ``prefix`` (e.g. 'sameday_') namespaces it for the same-day daemon."""
    if not isinstance(status, dict) or not status.get("required"):
        return None
    state = status.get("state")
    if state in _USABLE_STATES and status.get("mirofish_used"):
        return None
    if state == FRESH_UNUSED:
        # required but a usable result was not consumed -> treat as launch-only/unused
        base = "mirofish_required_unavailable_no_bet"
    else:
        base = _REQUIRED_NO_BET.get(state, "mirofish_required_unavailable_no_bet")
    return f"{prefix}{base}" if prefix else base
