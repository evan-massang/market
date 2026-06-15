"""obs.redact — strip secrets from strings/objects before they hit disk.

Reads LIVE os.environ on every call (so late-loaded keys are still masked).
Never raises: on any internal failure the input is returned unchanged.
"""

import os
import re

REDACTED = "***REDACTED***"

# Key names that designate secrets. _API_KEY is subsumed by _KEY but listed for clarity.
_SECRET_NAME_RE = re.compile(r".*(_API_KEY|_KEY|_SECRET|_TOKEN)$", re.IGNORECASE)

# Always treat these specific names as secret-valued, even if they don't match the regex.
_EXPLICIT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CHALLENGER_API_KEY",
)

# Don't mask trivially short values (avoids redacting common noise like "1", "true").
_MIN_SECRET_LEN = 6


def _name_matches_regex(name):
    try:
        return bool(_SECRET_NAME_RE.match(name or ""))
    except Exception:
        return False


def secret_values():
    """Return the set of secret VALUE strings drawn from the live environment.

    A value is collected when its KEY matches the secret-name regex OR is one
    of the explicit names, the value is non-empty, and len(value) >= 6.
    """
    out = set()
    try:
        for k, v in os.environ.items():
            try:
                if not v or len(v) < _MIN_SECRET_LEN:
                    continue
                if _name_matches_regex(k) or k in _EXPLICIT_NAMES:
                    out.add(v)
            except Exception:
                continue
    except Exception:
        pass
    return out


def scrub_str(s):
    """Replace each live secret value occurrence in `s` with REDACTED."""
    try:
        if not isinstance(s, str) or not s:
            return s
        out = s
        for val in secret_values():
            if val and val in out:
                out = out.replace(val, REDACTED)
        return out
    except Exception:
        return s


def scrub_obj(o):
    """Recursively scrub dict/list/tuple/str.

    In dicts, additionally DROP any key whose NAME matches the secret-name regex.
    Other scalar types are returned as-is.
    """
    try:
        if isinstance(o, str):
            return scrub_str(o)
        if isinstance(o, dict):
            res = {}
            for k, v in o.items():
                if isinstance(k, str) and _name_matches_regex(k):
                    continue  # drop secret-named key entirely
                res[k] = scrub_obj(v)
            return res
        if isinstance(o, list):
            return [scrub_obj(x) for x in o]
        if isinstance(o, tuple):
            return tuple(scrub_obj(x) for x in o)
        return o
    except Exception:
        return o
