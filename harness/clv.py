"""harness/clv.py — B2: closing-line value (CLV) + edge-decay analytics.

Two read-only, best-effort analytics that make the bot's *skill* visible without
ever touching a gate, a threshold, or the bet frequency:

* **CLV (closing-line value)** — for every entry we recorded, how far did the
  market move toward us *after* we bought? A YES bought cheap that drifts up, or
  a NO bought expensive that drifts down, *beat the closing line*. Positive CLV
  is the single most predictive leading indicator of long-run edge, so we track
  it in its own table (``clv_records``) and aggregate it overall + per theme.
* **Edge-decay** — for resolved paper bets, compare the *predicted* edge at entry
  (|model_p - market_p|) against the *realized* return per dollar, bucketed by how
  far ahead of resolution the bet was placed. A predicted edge that stops paying
  off the further out we bet is a *decaying* edge; surfacing the buckets makes
  that decay legible.

Design contract (identical to harness/label_perf.py)
----------------------------------------------------
* Self-contained sqlite table ``clv_records`` created idempotently in the SAME
  polyswarm.db as the rest of the harness. The DB path honors DATABASE_URL
  exactly as core.calibration / label_perf do (so the unit tests' temp DB is
  used) and falls back to ``obs.config.resolve_db_path()``.
* Every public function is best-effort and import-safe: a missing table, a
  missing DB, a malformed row, or thin data degrades to a safe default
  (False / None / {}). NOTHING here may raise into settlement or the bettor.
* These are PURE ANALYTICS. They read paper_positions / clv_records and never
  write to paper_positions, never size a bet, never move a gate or a threshold.
* De-dupe is on READ — the LATEST clv_records row per (market_id, side) wins — so
  a re-recorded CLV never double-counts.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

_TABLE = "clv_records"

# Day-to-resolution buckets for the edge-decay report (ordered, ascending).
_DECAY_BUCKETS = ("<=1d", "1-7d", "7-30d", ">30d", "unknown")


# ── db path / connection ──────────────────────────────────────────────────────
def _db_path() -> str:
    """Resolve the harness DB path (DATABASE_URL first, then the canonical DB)."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        return raw.replace("sqlite+aiosqlite:///./", "").replace("sqlite:///./", "")
    try:
        from harness.obs import config as _cfg
        return str(_cfg.resolve_db_path())
    except Exception:
        return "polyswarm.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _f(x):
    """Coerce to float, mapping anything non-numeric (incl. None) to None."""
    try:
        return None if x is None else float(x)
    except Exception:
        return None


def _clv_for(side, entry, closing):
    """Signed closing-line value for an entry.

    YES: we want the price to RISE after we buy -> clv = closing - entry.
    NO : we want the price to FALL after we buy  -> clv = entry - closing.
    Returns None if side is unknown or either price is missing.
    """
    s = (side or "").strip().upper()
    e, c = _f(entry), _f(closing)
    if e is None or c is None:
        return None
    if s == "YES":
        return c - e
    if s == "NO":
        return e - c
    return None


# ── schema ──────────────────────────────────────────────────────────────────--
def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create clv_records (+ indices) idempotently. Never raises."""
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT,
                side TEXT,                 -- YES | NO
                entry_price REAL,
                closing_price REAL,
                clv REAL,
                theme TEXT,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_mkt ON {_TABLE}(market_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_theme ON {_TABLE}(theme)")
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── record ──────────────────────────────────────────────────────────────────--
def record_clv(market_id, side, entry_price, closing_price, theme=None) -> bool:
    """INSERT one closing-line-value row.

    clv is computed from (side, entry_price, closing_price) — positive == we beat
    the closing line. Returns True iff a row was inserted. Best-effort: returns
    False on bad input (unknown side / missing price) or any error.

    Idempotent-ish: an exact re-record (same market_id + side + closing_price) is
    skipped (returns False); read-side de-dupe is the real guarantee.
    """
    try:
        clv = _clv_for(side, entry_price, closing_price)
        if clv is None:
            return False
        s = side.strip().upper()
        ep, cp = _f(entry_price), _f(closing_price)
        conn = sqlite3.connect(_db_path())
        try:
            init_db(conn)
            if market_id is not None:
                dup = conn.execute(
                    f"SELECT 1 FROM {_TABLE} WHERE market_id=? AND side=? AND closing_price=? LIMIT 1",
                    (market_id, s, cp),
                ).fetchone()
                if dup:
                    return False
            conn.execute(
                f"INSERT INTO {_TABLE} (market_id, side, entry_price, closing_price, "
                f"clv, theme, recorded_at) VALUES (?,?,?,?,?,?,?)",
                (market_id, s, ep, cp, clv, theme, datetime.utcnow().isoformat()),
            )
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


# ── aggregation ───────────────────────────────────────────────────────────────
def _resolved_rows(conn: sqlite3.Connection):
    """LATEST clv_records row per (market_id, side) — de-dupe re-recorded CLV.

    Rows with a NULL market_id are each kept (no key to de-dupe on). ``IS`` gives
    NULL-safe equality so a NULL side still de-dupes correctly.
    """
    return conn.execute(
        f"SELECT side, entry_price, closing_price, clv, theme "
        f"FROM {_TABLE} t WHERE t.market_id IS NULL OR t.id = ("
        f"  SELECT MAX(id) FROM {_TABLE} t2 "
        f"  WHERE t2.market_id IS t.market_id AND t2.side IS t.side)"
    ).fetchall()


def _load_rows():
    """De-duped clv rows as a list. [] on any error."""
    try:
        conn = _connect()
    except Exception:
        return []
    try:
        init_db(conn)
        return _resolved_rows(conn)
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clv_of(row):
    """clv from a row, recomputing from prices if the stored value is absent."""
    v = _f(row["clv"])
    if v is None:
        v = _clv_for(row["side"], row["entry_price"], row["closing_price"])
    return v


def _summarize_clv(clvs: list[float]) -> dict | None:
    """{n, mean_clv, pct_positive} over a list of clv values; None if empty."""
    n = len(clvs)
    if n == 0:
        return None
    pos = sum(1 for v in clvs if v > 0)
    return {
        "n": n,
        "mean_clv": sum(clvs) / n,
        "pct_positive": pos / n,
    }


def mean_clv(min_n: int = 5) -> dict | None:
    """Overall CLV summary: ``{n, mean_clv, pct_positive}``.

    Positive mean_clv / high pct_positive == we are systematically beating the
    closing line (the leading edge indicator). Returns None below ``min_n`` (or on
    error / no data) so a caller treats thin data as "not yet meaningful".
    """
    try:
        clvs = [v for v in (_clv_of(r) for r in _load_rows()) if v is not None]
        if len(clvs) < min_n:
            return None
        return _summarize_clv(clvs)
    except Exception:
        return None


def clv_by_theme(min_n: int = 5) -> dict:
    """Per-theme CLV: ``{theme: {n, mean_clv, pct_positive}}``.

    Only themes with n >= ``min_n`` are returned. A NULL theme is bucketed under
    'other'. Returns {} on error / no qualifying theme.
    """
    try:
        buckets: dict[str, list] = {}
        for r in _load_rows():
            v = _clv_of(r)
            if v is None:
                continue
            theme = r["theme"] or "other"
            buckets.setdefault(theme, []).append(v)
        out: dict = {}
        for theme, clvs in buckets.items():
            if len(clvs) >= min_n:
                out[theme] = _summarize_clv(clvs)
        return out
    except Exception:
        return {}


# ── edge decay ────────────────────────────────────────────────────────────────
def _parse_ts(s):
    """Best-effort parse of an ISO-ish timestamp to a datetime. None on failure.

    Tolerates a trailing 'Z', a space separator (sqlite CURRENT_TIMESTAMP), and a
    date-only string. Returns a naive datetime (we only ever take differences).
    """
    if not s:
        return None
    txt = str(s).strip()
    if txt.endswith("Z"):
        txt = txt[:-1]
    txt = txt.replace("T", " ")
    # drop a timezone offset like +00:00 if present (we compare same-zone deltas)
    for sep in ("+",):
        if sep in txt[11:]:
            txt = txt[:11] + txt[11:].split(sep)[0]
    txt = txt.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(txt)
    except Exception:
        return None


def _days_to_resolution(opened_at, end_date):
    """Days between entry (opened_at) and resolution (end_date). None if unknown."""
    o, e = _parse_ts(opened_at), _parse_ts(end_date)
    if o is None or e is None:
        return None
    try:
        return (e - o).total_seconds() / 86400.0
    except Exception:
        return None


def _decay_bucket(days) -> str:
    if days is None:
        return "unknown"
    d = max(0.0, days)
    if d <= 1.0:
        return "<=1d"
    if d <= 7.0:
        return "1-7d"
    if d <= 30.0:
        return "7-30d"
    return ">30d"


def _predicted_edge(row) -> float | None:
    """Predicted edge at entry = |model_p - market_p|, falling back to |edge|."""
    mp, kp = _f(row["model_p"]), _f(row["market_p"])
    if mp is not None and kp is not None:
        return abs(mp - kp)
    return abs(_f(row["edge"])) if _f(row["edge"]) is not None else None


def edge_decay_report() -> dict:
    """Rolling realized-edge stats for resolved paper bets, bucketed by lead time.

    For each settled position with a realized P&L and a positive stake, compare the
    PREDICTED edge at entry (|model_p - market_p|) against the REALIZED return per
    dollar (realized_pnl / stake), grouped by how far ahead of resolution the bet
    was placed (days from opened_at to end_date; a single 'unknown' bucket when a
    timestamp is missing).

    Returns ``{bucket: {n, mean_predicted_edge, mean_realized_return,
    edge_capture, pct_profitable}}`` for every non-empty bucket, ordered shortest
    lead-time first. ``edge_capture`` = mean_realized_return / mean_predicted_edge
    (None when the denominator is ~0). Read-only; degrades to {} on thin data or
    any error — NEVER raises.
    """
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        # If paper_positions doesn't exist (fresh DB), bail to {} rather than raise.
        try:
            rows = conn.execute(
                "SELECT model_p, market_p, edge, stake, realized_pnl, opened_at, end_date "
                # include cashed-out trades for the realized-return analytic (audit #8/#19)
                "FROM paper_positions WHERE status IN ('settled','closed') AND realized_pnl IS NOT NULL"
            ).fetchall()
        except Exception:
            return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    acc: dict[str, dict] = {}
    for r in rows:
        stake = _f(r["stake"])
        pnl = _f(r["realized_pnl"])
        pe = _predicted_edge(r)
        if stake is None or stake <= 0 or pnl is None:
            continue
        ret = pnl / stake
        bucket = _decay_bucket(_days_to_resolution(r["opened_at"], r["end_date"]))
        a = acc.setdefault(bucket, {"n": 0, "pe_sum": 0.0, "pe_n": 0,
                                    "ret_sum": 0.0, "wins": 0})
        a["n"] += 1
        a["ret_sum"] += ret
        if ret > 0:
            a["wins"] += 1
        if pe is not None:
            a["pe_sum"] += pe
            a["pe_n"] += 1

    out: dict = {}
    for bucket in _DECAY_BUCKETS:
        a = acc.get(bucket)
        if not a or a["n"] == 0:
            continue
        n = a["n"]
        mean_pe = (a["pe_sum"] / a["pe_n"]) if a["pe_n"] else None
        mean_ret = a["ret_sum"] / n
        capture = (mean_ret / mean_pe) if (mean_pe is not None and abs(mean_pe) > 1e-9) else None
        out[bucket] = {
            "n": n,
            "mean_predicted_edge": mean_pe,
            "mean_realized_return": mean_ret,
            "edge_capture": capture,
            "pct_profitable": a["wins"] / n,
        }
    return out


# ── timed CLV snapshots (entry vs 15m / 1h / 6h) ──────────────────────────────--
# The closing-line CLV above needs a resolution; these intermediate snapshots tell us
# MUCH sooner whether the scanner is finding good entries (positive drift after we buy)
# or entering late (price moves against us). Captured per open position as each bucket
# becomes due, using a caller-supplied current price map (no network in this module).
_SNAP_TABLE = "clv_snapshots"
SNAP_BUCKETS = {"15m": 15.0, "1h": 60.0, "6h": 360.0}


def init_snapshots(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(_db_path())
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_SNAP_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT, side TEXT, bucket TEXT,
                entry_price REAL, snap_price REAL, clv REAL, theme TEXT,
                opened_at TEXT, recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_SNAP_TABLE}_mkt ON {_SNAP_TABLE}(market_id)")
        conn.commit()
    except Exception:
        pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _minutes_since(opened_at, now=None) -> float | None:
    a = _parse_ts(opened_at)
    if a is None:
        return None
    now = now or datetime.utcnow()
    try:
        if a.tzinfo is not None:
            a = a.replace(tzinfo=None)
        return (now - a).total_seconds() / 60.0
    except Exception:
        return None


def record_snapshot(market_id, side, bucket, entry_price, snap_price, theme=None,
                    opened_at=None) -> bool:
    """Record one timed CLV snapshot (idempotent per market_id+side+bucket)."""
    try:
        clv = _clv_for(side, entry_price, snap_price)
        if clv is None or bucket not in SNAP_BUCKETS:
            return False
        s = side.strip().upper()
        conn = sqlite3.connect(_db_path())
        try:
            init_snapshots(conn)
            dup = conn.execute(
                f"SELECT 1 FROM {_SNAP_TABLE} WHERE market_id=? AND side=? AND bucket=? LIMIT 1",
                (market_id, s, bucket)).fetchone()
            if dup:
                return False
            conn.execute(
                f"INSERT INTO {_SNAP_TABLE} (market_id, side, bucket, entry_price, snap_price, "
                f"clv, theme, opened_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (market_id, s, bucket, _f(entry_price), _f(snap_price), clv, theme,
                 opened_at, datetime.utcnow().isoformat()))
            conn.commit()
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


def snapshot_open_positions(price_map: dict, now=None) -> int:
    """For every OPEN paper position, record any DUE 15m/1h/6h snapshot not yet taken,
    using price_map[market_id] (current YES price). Returns the count recorded.
    Best-effort / no network: the caller fetches the prices."""
    if not price_map:
        return 0
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    except Exception:
        return 0
    try:
        rows = conn.execute(
            "SELECT market_id, side, fill_price, opened_at, question FROM paper_positions "
            "WHERE status='open'").fetchall()
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    recorded = 0
    for r in rows:
        mid = r["market_id"]
        cur = price_map.get(mid)
        if cur is None:
            continue
        mins = _minutes_since(r["opened_at"], now)
        if mins is None:
            continue
        side = (r["side"] or "YES")
        # current price on the side we hold (price_map stores the YES price)
        try:
            cur_side = float(cur) if side.upper() == "YES" else (1.0 - float(cur))
        except Exception:
            continue
        try:
            from harness import scoreboard
            theme = scoreboard.theme_of(r["question"] or "")
        except Exception:
            theme = None
        for bucket, mins_due in SNAP_BUCKETS.items():
            if mins >= mins_due:
                if record_snapshot(mid, side, bucket, r["fill_price"], cur_side,
                                   theme=theme, opened_at=r["opened_at"]):
                    recorded += 1
    return recorded


def clv_snapshot_summary(min_n: int = 1) -> dict:
    """Mean CLV per bucket (15m/1h/6h) — positive = price drifted toward us after entry
    (good entries); negative = we entered late / wrong."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    except Exception:
        return {}
    try:
        rows = conn.execute(f"SELECT bucket, clv FROM {_SNAP_TABLE} WHERE clv IS NOT NULL").fetchall()
    except Exception:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    acc: dict = {}
    for r in rows:
        acc.setdefault(r["bucket"], []).append(r["clv"])
    out = {}
    for bucket, vals in acc.items():
        if len(vals) >= min_n:
            out[bucket] = {"n": len(vals), "mean_clv": round(sum(vals) / len(vals), 6),
                           "pct_positive": round(sum(1 for v in vals if v > 0) / len(vals), 4)}
    return out
