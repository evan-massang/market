"""obs.codeversion — reproducibility fingerprint for a run.

Captures the git SHA / dirty flag (guarded subprocess calls) plus a
content-hash over the harness/core/agents *.py sources, and the model/provider
config from the environment. Every git invocation is guarded — missing git or
a non-repo dir yields git_sha=None / git_dirty=None rather than raising.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from . import config


def _repo_root():
    """Walk up from this file to the first dir containing '.git' (== polyswarm/)."""
    try:
        here = Path(__file__).resolve()
        for parent in [here] + list(here.parents):
            try:
                if (parent / ".git").exists():
                    return parent
            except Exception:
                continue
    except Exception:
        pass
    return None


def _git(repo_root, *args):
    """Run `git -C repo_root <args>`; return stdout (stripped) or None. Never raises."""
    if not repo_root:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root)] + list(args),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        return None
    return None


def _git_sha(repo_root):
    out = _git(repo_root, "rev-parse", "HEAD")
    if out is None:
        return None
    out = out.strip()
    return out or None


def _git_dirty(repo_root):
    out = _git(repo_root, "status", "--porcelain")
    if out is None:
        return None
    return bool(out.strip())


def _excluded(relpath, filename):
    if ".orig" in relpath:
        return True
    if "__pycache__" in relpath:
        return True
    if "/tests/" in relpath:
        return True
    if "obs/tests" in relpath:
        return True
    if filename.startswith("test_"):
        return True
    if relpath.endswith(".db"):
        return True
    return False


def _code_version(pkg_root):
    """sha256 over the sorted (relpath, sha256(file_bytes)) list of source files."""
    try:
        entries = []
        for sub in ("harness", "core", "agents"):
            base = pkg_root / sub
            try:
                if not base.exists():
                    continue
            except Exception:
                continue
            for f in base.rglob("*.py"):
                try:
                    rel = f.relative_to(pkg_root).as_posix()
                except Exception:
                    continue
                if _excluded(rel, f.name):
                    continue
                try:
                    b = f.read_bytes()
                except Exception:
                    continue
                entries.append((rel, hashlib.sha256(b).hexdigest()))
        entries.sort()
        h = hashlib.sha256()
        for rel, digest in entries:
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(digest.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()
    except Exception:
        return None


def reproducibility():
    """Return the reproducibility dict. Never raises."""
    try:
        repo_root = _repo_root()
        return {
            "git_sha": _git_sha(repo_root),
            "git_dirty": _git_dirty(repo_root),
            "code_version": _code_version(config.PKG_ROOT),
            "provider": os.getenv("LLM_PROVIDER"),
            "model_fast": os.getenv("MODEL_FAST"),
            "model_deep": os.getenv("MODEL_DEEP"),
            "debate_rounds": os.getenv("DEBATE_ROUNDS"),
            "seed": None,
            "deterministic": False,
        }
    except Exception:
        return {
            "git_sha": None,
            "git_dirty": None,
            "code_version": None,
            "provider": None,
            "model_fast": None,
            "model_deep": None,
            "debate_rounds": None,
            "seed": None,
            "deterministic": False,
        }
