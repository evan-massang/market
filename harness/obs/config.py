"""obs.config — paths, db resolution, and the OBS_ENABLED kill-switch.

All public entry points are guarded; importing this module never raises.

PKG_ROOT is the polyswarm/ package dir (the one that contains both
'harness' and 'core'). The DB and logs are anchored to PKG_ROOT so that
observability always targets the SAME polyswarm/polyswarm.db the daemons
use — regardless of the current working directory.
"""

import os
from pathlib import Path

SCHEMA_VERSION = 1


def _resolve_pkg_root():
    """Walk: harness/obs/config.py -> parents[2] == polyswarm/.

    Verify it looks like the package root (contains 'harness' and 'core').
    Falls back to the computed path even if verification fails so that an
    unusual layout cannot make the import raise.
    """
    try:
        p = Path(__file__).resolve().parents[2]
        try:
            if (p / "harness").is_dir() and (p / "core").is_dir():
                return p
        except Exception:
            pass
        return p
    except Exception:
        # Last-ditch fallback: never raise at import time.
        return Path(os.getcwd())


PKG_ROOT = _resolve_pkg_root()


def resolve_db_path():
    """Return the Path to the calibration/evidence sqlite DB.

    Honors env DATABASE_URL (stripping the 'sqlite+aiosqlite:///./' prefix the
    rest of the harness uses). Relative paths are anchored to PKG_ROOT — NOT
    the current working directory — so this resolves to the canonical
    polyswarm/polyswarm.db rather than a cwd-relative or stray copy.
    """
    try:
        raw = os.getenv("DATABASE_URL")
        if raw:
            cleaned = raw.replace("sqlite+aiosqlite:///./", "")
            # Some configs use a bare sqlite:/// prefix; strip a leading one too.
            cleaned = cleaned.replace("sqlite:///./", "")
            p = Path(cleaned)
            if not p.is_absolute():
                p = PKG_ROOT / p
            return p
        return PKG_ROOT / "polyswarm.db"
    except Exception:
        return PKG_ROOT / "polyswarm.db"


def LOGS_DIR():
    """Root logs dir: env OBS_LOGS_DIR else PKG_ROOT/'logs'."""
    try:
        d = os.getenv("OBS_LOGS_DIR")
        return Path(d) if d else (PKG_ROOT / "logs")
    except Exception:
        return PKG_ROOT / "logs"


def _subdir(name):
    """Return LOGS_DIR()/name, creating it (parents+exist_ok) on demand."""
    d = LOGS_DIR() / name
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def events_dir():
    return _subdir("events")


def transcripts_dir():
    return _subdir("transcripts")


def blobs_dir():
    return _subdir("blobs")


def errors_dir():
    return _subdir("errors")


def enabled():
    """OBS master switch. Default '1' (on); '0' => caller should no-op."""
    try:
        return os.getenv("OBS_ENABLED", "1") != "0"
    except Exception:
        return False
