"""obs.eventlog — append-only, hash-chained JSONL event log.

Each run gets one file events_dir()/<run_id>.jsonl. Every line is a canonical
JSON envelope whose `prev_hash` is the sha256 of the previous line's exact
text, forming a tamper-evident chain. ERROR/WARN lines are mirrored into
errors_dir()/<run_id>.jsonl.

emit() is best-effort and NEVER raises: the entire body is wrapped so a running
daemon can never crash because of logging. JSONL is the canonical record.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

try:
    import msvcrt  # Windows file locking
except Exception:  # pragma: no cover - non-Windows / unavailable
    msvcrt = None

from . import config
from . import redact
from .ids import current

_GENESIS = "0" * 64


# Canonical registry of every event name emit() may write — the single source of
# truth for "what events exist". Each name has exactly one payload builder in
# obs.hooks. emit() itself does NOT validate against this set (logging must never
# reject a call); the registry exists so tooling (explain/replay/transcript) and
# tests share one authoritative, APPEND-ONLY list. Never remove or rename an
# entry — only append. Listed in canonical pipeline order.
CANONICAL_EVENTS = (
    "run.start",
    "data.fetch",
    "classify.decision",
    "forecast.start",
    "llm.call",
    "agent.estimate",
    "debate.round",
    "blend.compute",
    "forecast.final",
    "evidence.pack",
    "sizing.decision",
    "trade.open",
    "trade.skip",
    "event.portfolio",
    "resolution.observed",
    "trade.settle",
    "score.brier",
    "gate.eval",
    "run.end",
    "error",
)


def line_sha(text):
    """sha256 hex of the exact line text (utf-8), WITHOUT any trailing newline."""
    try:
        if text is None:
            text = ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception:
        return _GENESIS


def _now_iso():
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    # Aware UTC isoformat already ends in '+00:00'; guarantee a tz marker regardless.
    if not (ts.endswith("Z") or ts.endswith("+00:00")):
        ts = ts + "+00:00"
    return ts


def _lock(f):
    """Best-effort exclusive lock on byte 0 of the file (cross-process on Windows)."""
    if msvcrt is None:
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    except Exception:
        pass


def _unlock(f):
    if msvcrt is None:
        return
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass


def _last_nonempty_line(text):
    last = ""
    for ln in text.split("\n"):
        if ln.strip():
            last = ln
    return last


def _append_raw(path, line):
    """Append `line` + '\n' to `path` under a best-effort lock (no chaining)."""
    try:
        with open(path, "ab+") as f:
            _lock(f)
            try:
                f.seek(0, os.SEEK_END)
                f.write((line + "\n").encode("utf-8"))
                f.flush()
            finally:
                _unlock(f)
    except Exception:
        pass


def _head_path(run_id):
    return config.events_dir() / (str(run_id) + ".head")


def _write_head(run_id, sha):
    """Atomically write the trust-anchor head sidecar for `run_id`.

    The `.head` file holds line_sha(last line). It is a TRUST ANCHOR: the hash
    chain alone cannot detect tampering of the *final* line (no subsequent line
    commits a hash of it), so the head pins it out-of-band. Written via
    tmp + os.replace so a reader never sees a partial head. Callers update it
    under the SAME append lock that guards the .jsonl, keeping the two in sync.
    """
    try:
        d = config.events_dir()
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".head-tmp-")
        try:
            with os.fdopen(fd, "wb") as hf:
                hf.write((sha or "").encode("utf-8"))
                hf.flush()
                try:
                    os.fsync(hf.fileno())
                except Exception:
                    pass
            os.replace(tmp, str(_head_path(run_id)))
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def emit(event, level="INFO", **fields):
    """Append one hash-chained event envelope. Returns the envelope, or None.

    No-op (returns None) when OBS is disabled. Best-effort: any failure is
    swallowed and None is returned.
    """
    if not config.enabled():
        return None
    try:
        ids = current()
        env = {
            "event": event,
            "ts": _now_iso(),
            "level": level,
            "schema_version": config.SCHEMA_VERSION,
        }
        # correlation ids, then scrubbed caller fields
        env.update(ids)
        try:
            env.update(redact.scrub_obj(dict(fields)))
        except Exception:
            pass

        run_id = ids.get("run_id") or "norun"
        path = config.events_dir() / (str(run_id) + ".jsonl")

        line = None
        with open(path, "ab+") as f:
            _lock(f)
            try:
                f.seek(0)
                raw = f.read()
                text = raw.decode("utf-8", errors="replace") if raw else ""
                last = _last_nonempty_line(text)
                env["prev_hash"] = line_sha(last) if last else _GENESIS
                line = json.dumps(
                    env, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                f.seek(0, os.SEEK_END)
                f.write((line + "\n").encode("utf-8"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
                # Update the trust-anchor head (sha of the just-written last
                # line) under the SAME append lock so chain + head stay in sync.
                _write_head(run_id, line_sha(line))
            finally:
                _unlock(f)

        if level in ("ERROR", "WARN") and line is not None:
            epath = config.errors_dir() / (str(run_id) + ".jsonl")
            _append_raw(epath, line)

        return env
    except Exception:
        return None


def verify_chain(run_id):
    """Verify the hash chain of events_dir()/<run_id>.jsonl.

    Returns {'ok': bool, 'first_bad_index': int|None, 'reason': str, 'n': int}.
    line[0].prev_hash must be the genesis ('0'*64); each subsequent line's
    prev_hash must equal line_sha(previous line). first_bad_index is the index
    of the first line whose link is broken (covers tamper/insert/delete).
    """
    result = {"ok": True, "first_bad_index": None, "reason": "ok", "n": 0}
    try:
        path = config.events_dir() / (str(run_id) + ".jsonl")
        if not path.exists():
            result.update(ok=False, reason="no such run log")
            return result

        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="replace") if raw else ""
        lines = [ln for ln in text.split("\n") if ln.strip()]
        result["n"] = len(lines)
        if not lines:
            result.update(ok=False, reason="empty log")
            return result

        try:
            first = json.loads(lines[0])
        except Exception:
            result.update(ok=False, first_bad_index=0, reason="line 0 is not valid JSON")
            return result
        if first.get("prev_hash") != _GENESIS:
            result.update(
                ok=False, first_bad_index=0, reason="genesis prev_hash mismatch"
            )
            return result

        for i in range(len(lines) - 1):
            expected = line_sha(lines[i])
            try:
                nxt = json.loads(lines[i + 1])
            except Exception:
                result.update(
                    ok=False,
                    first_bad_index=i + 1,
                    reason="line %d is not valid JSON" % (i + 1),
                )
                return result
            if nxt.get("prev_hash") != expected:
                result.update(
                    ok=False,
                    first_bad_index=i + 1,
                    reason="prev_hash mismatch at index %d" % (i + 1),
                )
                return result

        # Last-line trust anchor. The link chain above cannot detect tampering
        # of the final line (nothing downstream commits a hash of it), so we
        # consult the out-of-band .head sidecar written under the append lock.
        # If present, line_sha(actual last line) must equal the stored head.
        try:
            head_path = config.events_dir() / (str(run_id) + ".head")
            if head_path.exists():
                with open(head_path, "rb") as hf:
                    stored_head = hf.read().decode("utf-8", errors="replace").strip()
                if stored_head and stored_head != line_sha(lines[-1]):
                    result.update(
                        ok=False,
                        first_bad_index=len(lines) - 1,
                        reason="last-line tamper",
                    )
                    return result
        except Exception:
            pass

        return result
    except Exception as e:
        return {
            "ok": False,
            "first_bad_index": None,
            "reason": "verify error: %r" % (e,),
            "n": result.get("n", 0),
        }
