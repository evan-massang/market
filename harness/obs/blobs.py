"""obs.blobs — content-addressed, scrubbed blob storage.

Large payloads (raw API responses, full prompts/completions) are stored once
under blobs_dir()/<sha256> and referenced from the event log by hash. String
content is scrubbed for secrets BEFORE hashing so the stored bytes and the hash
are both clean. Writes are atomic (tmp file + os.replace) and idempotent.

Guarded: store_blob returns (None, None) on any failure; read_blob returns None.
"""

import hashlib
import os
import tempfile

from . import config
from . import redact


def store_blob(content):
    """Persist `content` (str or bytes) content-addressed; return (sha256_hex, blob_ref).

    - str content is scrubbed via redact.scrub_str first.
    - bytes content is decoded (utf-8, errors='replace') and scrubbed; if the
      scrub changed anything (a secret was present) the scrubbed TEXT is stored
      so the secret is masked, otherwise the original bytes are stored as-is
      (truly-binary content is preserved verbatim).
    - sha256 is computed over the (possibly scrubbed) bytes.
    - written atomically; no-op if the file already exists.
    - blob_ref == 'blobs/<sha256>'.
    Returns (None, None) on any failure.
    """
    try:
        if isinstance(content, bytes):
            try:
                decoded = content.decode("utf-8", errors="replace")
                scrubbed = redact.scrub_str(decoded)
                # Only re-encode when a secret was actually masked; this keeps
                # truly-binary blobs byte-for-byte intact while still ensuring a
                # secret value embedded in bytes never lands on disk in clear.
                data = scrubbed.encode("utf-8") if scrubbed != decoded else content
            except Exception:
                data = content
        else:
            s = content if isinstance(content, str) else str(content)
            s = redact.scrub_str(s)
            data = s.encode("utf-8")

        sha = hashlib.sha256(data).hexdigest()
        d = config.blobs_dir()
        path = d / sha

        if not path.exists():
            fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, str(path))
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise

        return sha, "blobs/" + sha
    except Exception:
        return None, None


def read_blob(sha256):
    """Return the blob's text (utf-8, lossy decode) or None if missing/failed."""
    try:
        if not sha256:
            return None
        path = config.blobs_dir() / sha256
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None
    except Exception:
        return None
