"""core/probability_parser.py — Plan 7: ONE strict LLM-probability parser.

Converts an LLM reply into a TRADABLE YES probability ONLY when it is unambiguous
and in range. It NEVER turns a year/date/count/money amount, a bare out-of-range
number, malformed JSON, or prose without probability wording into a confident
probability, and it NEVER clamps an out-of-range value into 0/1.

Contract:
  * a tradable probability is finite and STRICTLY within [0.01, 0.99] (exact 0/1
    are rejected for trading — never clamped).
  * a percent is applied ONLY when the model makes it explicit: a ``%`` sign, the
    word "percent", a ``unit: "percent"`` field, or a ``probability_percent`` field.
    A bare number > 1 (e.g. 65, 2026, 1.2) is REJECTED, never assumed to be percent.
  * JSON is preferred; prose is accepted only when a number is clearly attached to
    probability wording.
  * conflicting probability values → ambiguous → reject.
  * on ANY failure: ``ok=False`` with a specific reason — the caller must treat that
    as NO forecast (no bet), never a default 0.5.

Pure module (json/math/re only); no I/O, no cycle.
"""
from __future__ import annotations

import json
import math
import re

# ── result reason codes ───────────────────────────────────────────────────────
OK = "ok"
PARSE_ERROR = "llm_probability_parse_error"
OUT_OF_RANGE = "llm_probability_out_of_range"
AMBIGUOUS = "llm_probability_ambiguous"
MISSING = "llm_probability_missing"

# ── no-bet reason codes (for callers / journaling; Plan 7 §8) ─────────────────
PARSE_ERROR_NO_BET = "llm_probability_parse_error_no_bet"
OUT_OF_RANGE_NO_BET = "llm_probability_out_of_range_no_bet"
AMBIGUOUS_NO_BET = "llm_probability_ambiguous_no_bet"
MISSING_NO_BET = "llm_probability_missing_no_bet"
FALLBACK_DISPLAY_ONLY_NO_BET = "llm_probability_fallback_display_only_no_bet"
CHALLENGER_PARSE_FAILED_NO_BET = "challenger_parse_failed_no_bet"
SWARM_PARSE_FAILED_NO_BET = "swarm_parse_failed_no_bet"

_TRADE_MIN, _TRADE_MAX = 0.01, 0.99
_PROB_FIELDS = ("probability", "p_yes", "yes_probability", "prob_yes", "yes_prob")
# re.ASCII so ``\d`` matches ONLY ASCII 0-9 — a fullwidth/Unicode digit (e.g. "６５")
# is NOT captured and falls through to reject, rather than being mis-scaled.
_LONE_NUM = re.compile(r"^[+-]?\d+(?:\.\d+)?\s*%?$", re.ASCII)
# a number clearly attached to probability wording (both orders)
_AFTER = re.compile(r"probab\w*[^0-9%+\-]{0,14}([+-]?\d+(?:\.\d+)?)\s*(%?)", re.IGNORECASE | re.ASCII)
_BEFORE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(%?)[^0-9]{0,18}probab", re.IGNORECASE | re.ASCII)


def _result(ok, probability, reason, method, raw, confidence=None, warnings=None):
    return {"ok": bool(ok), "probability": probability, "confidence": confidence,
            "reason": reason, "method": method, "raw": (raw or "")[:200],
            "warnings": list(warnings or [])}


def _to_decimal(value, *, is_percent: bool, allow_percent: bool) -> tuple[float | None, str]:
    """Convert ONE candidate to a tradable decimal in [0.01,0.99], or (None, reason).
    NEVER clamps. ``is_percent`` (a %/percent/unit signal) divides by 100 (0..100)."""
    if isinstance(value, bool):
        return None, OUT_OF_RANGE
    if isinstance(value, str):
        s = value.strip().lower()
        if not _LONE_NUM.match(s):
            return None, MISSING                # not a lone number -> not a probability
        if s.endswith("%"):
            is_percent = True
            s = s[:-1].strip()
        try:
            num = float(s)
        except ValueError:
            return None, MISSING
    elif isinstance(value, (int, float)):
        num = float(value)
    else:
        return None, MISSING
    if not math.isfinite(num):
        return None, OUT_OF_RANGE
    if is_percent:
        if not allow_percent:
            return None, MISSING
        if num < 0.0 or num > 100.0:
            return None, OUT_OF_RANGE           # 120%, -5% -> rejected, NOT clamped
        dec = num / 100.0
    else:
        # a decimal WITHOUT an explicit percent signal must already be a (0,1) decimal.
        # a bare value >= 1 or <= 0 (65, 2026, 1.2, -0.1) is NOT assumed percent -> reject.
        if num <= 0.0 or num >= 1.0:
            return None, OUT_OF_RANGE
        dec = num
    if not (_TRADE_MIN <= dec <= _TRADE_MAX):    # exact 0/1 and sub-0.01 rejected
        return None, OUT_OF_RANGE
    return round(dec, 6), OK


def _confidence(obj) -> float | None:
    """Lenient: a finite confidence in [0,1] if present, else None (degraded). An
    invalid confidence does NOT invalidate a valid probability (documented choice)."""
    if not isinstance(obj, dict) or "confidence" not in obj:
        return None
    c = obj["confidence"]
    if isinstance(c, bool):
        return None
    try:
        cf = float(c)
    except (TypeError, ValueError):
        return None
    return round(cf, 6) if (math.isfinite(cf) and 0.0 <= cf <= 1.0) else None


def _try_json(s: str):
    t = s
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    try:
        return json.loads(t), True
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0)), True
            except (json.JSONDecodeError, ValueError):
                pass
    return None, False


def _from_json_obj(obj, raw, allow_percent):
    if not isinstance(obj, dict):
        return None                              # bare number/list -> fall through to prose
    conf = _confidence(obj)
    # most-explicit field wins
    if "probability_percent" in obj:
        dec, why = _to_decimal(obj["probability_percent"], is_percent=True, allow_percent=allow_percent)
        return _result(why == OK, dec, why, "json:probability_percent", raw, conf)
    unit_percent = str(obj.get("unit", "")).strip().lower() in ("percent", "pct", "%")
    found = []
    for fld in _PROB_FIELDS:
        if fld in obj:
            dec, why = _to_decimal(obj[fld], is_percent=unit_percent, allow_percent=allow_percent)
            found.append((fld, dec, why))
    if not found:
        return _result(False, None, MISSING, "json:no_prob_field", raw, conf)
    valids = {d for (_f, d, w) in found if w == OK}
    if not valids:
        why = next((w for (_f, d, w) in found if w != OK), OUT_OF_RANGE)
        return _result(False, None, why, "json:invalid_prob_field", raw, conf)
    if len(valids) > 1:
        return _result(False, None, AMBIGUOUS, "json:conflicting_fields", raw, conf)
    return _result(True, valids.pop(), OK, "json:prob_field", raw, conf)


def _from_prose(text, raw, allow_percent):
    if not re.search(r"probab", text, re.IGNORECASE):
        return _result(False, None, MISSING, "prose:no_probability_wording", raw)
    valids = set()
    saw_out_of_range = False
    for m in list(_AFTER.finditer(text)) + list(_BEFORE.finditer(text)):
        token = m.group(1) + ("%" if m.group(2) else "")
        dec, why = _to_decimal(token, is_percent=bool(m.group(2)), allow_percent=allow_percent)
        if why == OK:
            valids.add(dec)
        elif why == OUT_OF_RANGE:
            saw_out_of_range = True
    if not valids:
        # a probability-attached number existed but was out of range (e.g. "probability 120%")
        # -> report OUT_OF_RANGE (rejected, NEVER clamped); otherwise nothing usable was found.
        if saw_out_of_range:
            return _result(False, None, OUT_OF_RANGE, "prose:out_of_range", raw)
        return _result(False, None, MISSING, "prose:no_valid_probability", raw)
    if len(valids) > 1:
        return _result(False, None, AMBIGUOUS, "prose:conflicting", raw)
    return _result(True, valids.pop(), OK, "prose:probability", raw)


def parse_probability_response(text, *, source: str, allow_percent: bool = True,
                               require_json: bool = False) -> dict:
    """Strictly parse an LLM reply into a tradable YES probability. See module docstring.
    Returns {ok, probability, confidence, reason, method, raw, warnings}."""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    s = raw.strip()
    if not s:
        return _result(False, None, PARSE_ERROR, "empty", raw)
    obj, json_ok = _try_json(s)
    if json_ok:
        r = _from_json_obj(obj, raw, allow_percent)
        if r is not None:
            return r                              # JSON dict was authoritative (ok or not)
    if require_json:
        return _result(False, None, PARSE_ERROR, "no_json", raw)
    return _from_prose(s, raw, allow_percent)
