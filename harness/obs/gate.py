"""obs.gate — READ-ONLY dual-gate evaluator (a SEPARATE, write-isolated command).

This is the audit counterpart to harness/scoreboard.py. It computes the SAME two
gates the scoreboard does, but with a hard guarantee that evaluating the gates
performs **no writes** to polyswarm.db NOR to the evidence logs
(events/errors/transcripts/blobs):

  * The DB is opened through a STRICTLY read-only sqlite URI connection
    (``file:...?mode=ro``), so any write — by us or by reused code — raises
    "attempt to write a readonly database". Writing is physically impossible on
    this handle.
  * We DO NOT call obs.emit / hooks.on_gate / evidence.append_gate, because those
    write the DB and the events/ JSONL chain. The gate.eval verdict is written
    directly to a DEDICATED ``logs/gate/<gate_run>.jsonl`` subdir (NOT events/).

GATE 1 (calibration): swarm model Brier < market-price Brier, on >= 50 resolved
        OPINION markets (re-classified from the question, one row per market).
GATE 2 (profitability): the paper bankroll grew after costs
        (realized_pnl > 0 AND equity >= starting_bankroll).

We reuse the scoreboard's pure logic (``theme_of``, ``GATE1_MIN_N``) and the pure
classifier (``tag_market``); the scoreboard's Brier math is inlined per-row (it is
not exposed as a standalone callable), so we replicate that exact formula against
the read-only connection rather than importing-and-running scoreboard.compute()
(which opens read/write connections).

CLI:  python -m harness.obs.gate            -> evaluate + print + write gate log
      python -m harness.obs.gate --json     -> machine-readable verdict
      python -m harness.obs.gate --self-test -> prove read-only + write-isolation
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Pure, side-effect-free reuse — neither import opens a DB at import time.
from harness.classifier import tag_market
from harness.scoreboard import theme_of, GATE1_MIN_N

_GENESIS = "0" * 64


# ── time / hashing helpers ────────────────────────────────────────────────────
def _now_iso():
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if not (ts.endswith("Z") or ts.endswith("+00:00")):
        ts = ts + "+00:00"
    return ts


def _line_sha(text):
    try:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    except Exception:
        return _GENESIS


def _mint_gate_run():
    try:
        from uuid import uuid4
        return "gate_" + uuid4().hex[:10]
    except Exception:
        return "gate_0000000000"


# ── strictly read-only DB connection ──────────────────────────────────────────
def _ro_connect(db_path):
    """Open ``db_path`` through a read-only sqlite URI connection.

    Built with ``Path.as_uri()`` so Windows drive letters / separators produce a
    valid ``file:///C:/.../polyswarm.db`` URI, then ``?mode=ro`` is appended. On
    this handle any write raises sqlite3.OperationalError, which is exactly the
    isolation guarantee this command is built around. Raises if the file is
    missing (mode=ro never creates a database).
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── read queries (replicas of scoreboard's, run on the read-only handle) ───────
def _resolved_opinion_rows_ro(conn):
    """One resolved swarm forecast per market_id with a stored market price,
    restricted to OPINION markets (re-classified from the question). Mirrors
    scoreboard._resolved_opinion_rows including the inline Brier math."""
    try:
        rows = conn.execute(
            "SELECT question, market_id, final_probability, market_odds, outcome, brier_score "
            "FROM swarm_forecasts s WHERE outcome IS NOT NULL AND market_odds IS NOT NULL "
            "AND (s.market_id IS NULL OR s.id = (SELECT MAX(id) FROM swarm_forecasts s2 "
            "WHERE s2.market_id = s.market_id AND s2.outcome IS NOT NULL AND s2.market_odds IS NOT NULL))"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    out = []
    for r in rows:
        if tag_market(r["question"]).label != "opinion":
            continue
        model_b = r["brier_score"]
        if model_b is None:
            model_b = (r["final_probability"] - r["outcome"]) ** 2
        market_b = (r["market_odds"] - r["outcome"]) ** 2
        out.append({
            "question": r["question"], "market_id": r["market_id"],
            "model_brier": model_b, "market_brier": market_b,
            "theme": theme_of(r["question"]),
        })
    return out


def _baseline_opinion_briers_ro(conn):
    """Resolved single-LLM challenger Briers, restricted to OPINION markets.
    Mirrors scoreboard._baseline_opinion_briers."""
    try:
        rows = conn.execute(
            "SELECT question, brier_score FROM baseline_forecasts b WHERE brier_score IS NOT NULL "
            "AND (b.market_id IS NULL OR b.id = (SELECT MAX(id) FROM baseline_forecasts b2 "
            "WHERE b2.market_id = b.market_id AND b2.brier_score IS NOT NULL))"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    return [r["brier_score"] for r in rows if tag_market(r["question"]).label == "opinion"]


def _wallet_state_ro(conn):
    """Replica of wallet.get_state() READ queries on the read-only handle.

    equity = cash + open-stake-at-cost; only realized_pnl counts toward Gate 2.
    Falls back to zeros if the paper tables do not exist."""
    state = {"starting_bankroll": 0.0, "cash": 0.0, "open_exposure": 0.0,
             "equity": 0.0, "realized_pnl": 0.0, "n_open": 0}
    try:
        w = conn.execute(
            "SELECT starting_bankroll, cash, realized_pnl FROM paper_wallet WHERE id=1"
        ).fetchone()
    except sqlite3.OperationalError:
        return state
    if w is None:
        return state
    try:
        exposure = conn.execute(
            "SELECT COALESCE(SUM(stake),0) AS x FROM paper_positions WHERE status='open'"
        ).fetchone()["x"]
        n_open = conn.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        exposure, n_open = 0.0, 0
    cash = w["cash"]
    state.update(
        starting_bankroll=w["starting_bankroll"] or 0.0,
        cash=round((cash or 0.0), 4),
        open_exposure=round((exposure or 0.0), 4),
        equity=round((cash or 0.0) + (exposure or 0.0), 4),
        realized_pnl=round((w["realized_pnl"] or 0.0), 4),
        n_open=n_open,
    )
    return state


# ── dedicated gate log (writes ONLY under logs/gate/) ──────────────────────────
def _gate_dir():
    """logs/gate/ — a SEPARATE subdir, NOT events/. Created on demand. The ONLY
    location this command is permitted to write."""
    d = config.LOGS_DIR() / "gate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_gate_log(verdict, gate_run):
    """Write the gate.eval verdict as one canonical JSON line to
    logs/gate/<gate_run>.jsonl + a .head sidecar. Does NOT touch events/, the DB,
    or obs.emit. Best-effort; returns the path written (or None)."""
    try:
        env = {
            "event": "gate.eval",
            "ts": _now_iso(),
            "level": "INFO",
            "schema_version": config.SCHEMA_VERSION,
            "gate_run": gate_run,
            "n_resolved": verdict["n_resolved"],
            "model_brier_mean": verdict["model_brier_mean"],
            "market_brier_mean": verdict["market_brier_mean"],
            "paper_pnl": verdict["paper_pnl"],
            "gate1_pass": verdict["gate1_pass"],
            "gate2_pass": verdict["gate2_pass"],
            "overall_pass": verdict["overall_pass"],
            "prev_hash": _GENESIS,
        }
        line = json.dumps(env, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path = _gate_dir() / (gate_run + ".jsonl")
        with open(path, "ab") as f:
            f.write((line + "\n").encode("utf-8"))
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        # out-of-band trust anchor for the last (only) line
        head = _gate_dir() / (gate_run + ".head")
        with open(head, "wb") as hf:
            hf.write(_line_sha(line).encode("utf-8"))
        return str(path)
    except Exception:
        return None


# ── public API ─────────────────────────────────────────────────────────────---
def evaluate(db_path=None, write_log=True):
    """Evaluate both gates against a STRICTLY read-only DB connection.

    Returns a dict with the acceptance-criterion fields:
        n_resolved, model_brier_mean, market_brier_mean, paper_pnl,
        gate1_pass, gate2_pass, overall_pass
    plus context (n_required, baseline, per-theme, wallet, gate_run, gate_log,
    db_path, db_mode). Writes the gate.eval verdict to logs/gate/ only when
    write_log is True — never to the DB or events/errors/transcripts/blobs.
    """
    path = str(db_path) if db_path else str(config.resolve_db_path())
    gate_run = _mint_gate_run()

    rows, base, wallet, db_error = [], [], None, None
    try:
        conn = _ro_connect(path)
        try:
            rows = _resolved_opinion_rows_ro(conn)
            base = _baseline_opinion_briers_ro(conn)
            wallet = _wallet_state_ro(conn)
        finally:
            conn.close()
    except Exception as e:  # missing DB, locked beyond ro, etc.
        db_error = repr(e)
    if wallet is None:
        wallet = {"starting_bankroll": 0.0, "cash": 0.0, "open_exposure": 0.0,
                  "equity": 0.0, "realized_pnl": 0.0, "n_open": 0}

    n = len(rows)
    model_b = sum(r["model_brier"] for r in rows) / n if n else None
    market_b = sum(r["market_brier"] for r in rows) / n if n else None
    baseline_b = sum(base) / len(base) if base else None

    themes = {}
    for r in rows:
        t = themes.setdefault(r["theme"], {"n": 0, "_m": 0.0, "_k": 0.0})
        t["n"] += 1
        t["_m"] += r["model_brier"]
        t["_k"] += r["market_brier"]
    themes_out = {
        k: {"n": v["n"],
            "model_brier": (v["_m"] / v["n"]) if v["n"] else None,
            "market_brier": (v["_k"] / v["n"]) if v["n"] else None}
        for k, v in sorted(themes.items())
    }

    # GATE 1 — calibration (identical predicate to scoreboard.compute)
    gate1_pass = bool(
        n >= GATE1_MIN_N and model_b is not None and market_b is not None and model_b < market_b
    )
    # GATE 2 — profitability READINESS (Plan 9: unified, FAIL-CLOSED). Read-only; same DB path.
    # The old predicate (realized>0 AND at-cost equity>=start) could pass on stale/at-cost
    # numbers with too few trades and no baseline/CLV — now it is the accounting-audited gate.
    try:
        from harness import accounting_audit as _acct
        _g2 = _acct.gate2_status(db_path=path)
        gate2_pass = bool(_g2["pass"])
        gate2_status_s, gate2_reasons = _g2["status"], _g2["reasons"]
    except Exception as _e:
        gate2_pass, gate2_status_s, gate2_reasons = False, "unknown", ["gate2_accounting_unverified"]
    overall_pass = bool(gate1_pass and gate2_pass)

    verdict = {
        "n_resolved": n,
        "n_required": GATE1_MIN_N,
        "model_brier_mean": model_b,
        "market_brier_mean": market_b,
        "paper_pnl": wallet["realized_pnl"],
        "gate1_pass": gate1_pass,
        "gate2_pass": gate2_pass,
        "gate2_status": gate2_status_s,
        "gate2_reasons": gate2_reasons,
        "overall_pass": overall_pass,
        # context (not part of the criterion-5 field set, but useful + harmless)
        "baseline_brier_mean": baseline_b,
        "baseline_n": len(base),
        "themes": themes_out,
        "wallet": wallet,
        "db_path": path,
        "db_mode": "ro",
        "db_error": db_error,
        "gate_run": gate_run,
        "ts": _now_iso(),
        "read_only": True,
    }

    verdict["gate_log"] = _write_gate_log(verdict, gate_run) if write_log else None
    return verdict


def _fmt(x):
    return f"{x:.4f}" if isinstance(x, (int, float)) else "  n/a "


def render(verdict=None):
    """Print a human-readable verdict (evaluates if not given)."""
    s = verdict if verdict is not None else evaluate()
    print("=" * 66)
    print(" POLYMARKET HARNESS — GATE EVALUATOR  (READ-ONLY, write-isolated)")
    print("=" * 66)
    print(f" DB (mode=ro): {s['db_path']}")
    if s.get("db_error"):
        print(f" !! DB read error: {s['db_error']}")
    print(f" Resolved opinion markets: n_resolved = {s['n_resolved']}  (gate needs >= {s['n_required']})")
    print()
    if s["themes"]:
        print(f" {'theme':14s} {'n':>4s}  {'model_Brier':>12s}  {'market_Brier':>12s}  {'edge':>8s}")
        print(" " + "-" * 58)
        for theme, t in s["themes"].items():
            mb, kb = t["model_brier"], t["market_brier"]
            edge = (kb - mb) if (mb is not None and kb is not None) else None
            edge_s = f"{edge:+.4f}" if edge is not None else "   n/a"
            print(f" {theme:14s} {t['n']:>4d}  {_fmt(mb):>12s}  {_fmt(kb):>12s}  {edge_s:>8s}")
        print(" " + "-" * 58)
    overall_edge = (s["market_brier_mean"] - s["model_brier_mean"]) \
        if (s["model_brier_mean"] is not None and s["market_brier_mean"] is not None) else None
    print(f" {'OVERALL':14s} {s['n_resolved']:>4d}  {_fmt(s['model_brier_mean']):>12s}  "
          f"{_fmt(s['market_brier_mean']):>12s}  "
          f"{(f'{overall_edge:+.4f}' if overall_edge is not None else '   n/a'):>8s}")
    print()
    w = s["wallet"]
    print(f" GATE 1  (model Brier < market Brier, n>={s['n_required']}):  "
          f"{'PASS' if s['gate1_pass'] else 'FAIL'}"
          + (f"   ({_fmt(s['model_brier_mean'])} vs {_fmt(s['market_brier_mean'])})" if s['n_resolved'] else "   (no resolved markets yet)"))
    print(f" GATE 2  (paper bankroll grew after costs):                 "
          f"{'PASS' if s['gate2_pass'] else 'FAIL'}"
          f"   (start ${w['starting_bankroll']:.2f} -> equity ${w['equity']:.2f}, realized ${s['paper_pnl']:+.2f})")
    print()
    verdict_line = "BOTH GATES PASS — real-money phase may be considered (legality first)" \
        if s["overall_pass"] else "gates not both passed — stay on paper"
    print(f" >>> {verdict_line}")
    if s.get("gate_log"):
        print(f" gate.eval written -> {s['gate_log']}")
    print("=" * 66)


# ── self-test: prove read-only + write-isolation (criterion 5 shape) ───────────
def _dir_listing(d):
    try:
        return {p.name for p in Path(d).iterdir()}
    except Exception:
        return set()


def _sha_size(path):
    p = Path(path)
    if not p.exists():
        return (None, None)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return (h.hexdigest(), p.stat().st_size)


def _self_test():
    """Run evaluate() against a TEMP copy of the live DB with a TEMP OBS_LOGS_DIR,
    and assert: (a) the read-only handle physically refuses writes, (b) the DB
    sha256+size are unchanged across evaluate(), (c) NO new files appear under
    events/errors/transcripts/blobs — only under gate/. Returns a result dict."""
    import shutil

    live_db = config.resolve_db_path()
    results = {"checks": [], "ok": True}

    def _check(name, cond, detail=""):
        results["checks"].append({"name": name, "pass": bool(cond), "detail": detail})
        if not cond:
            results["ok"] = False

    tmp = Path(tempfile.mkdtemp(prefix="gate_selftest_"))
    tmp_db = tmp / "polyswarm.db"
    tmp_logs = tmp / "logs"
    # Pre-create the evidence subdirs so "new file" detection is meaningful.
    for sub in ("events", "errors", "transcripts", "blobs", "gate"):
        (tmp_logs / sub).mkdir(parents=True, exist_ok=True)

    # Copy the live DB so we never read it under load.
    if Path(live_db).exists():
        shutil.copy2(live_db, tmp_db)
        copied = True
    else:
        # Fabricate a minimal DB so the test still proves isolation.
        c = sqlite3.connect(str(tmp_db)); c.execute("CREATE TABLE t(x)"); c.commit(); c.close()
        copied = False

    old_url = os.environ.get("DATABASE_URL")
    old_logs = os.environ.get("OBS_LOGS_DIR")
    os.environ["DATABASE_URL"] = str(tmp_db)
    os.environ["OBS_LOGS_DIR"] = str(tmp_logs)
    try:
        # (a) the read-only handle must refuse writes
        ro = _ro_connect(tmp_db)
        try:
            ro.execute("CREATE TABLE _should_fail (x)")
            ro.commit()
            _check("ro_connection_refuses_write", False, "INSERT/CREATE succeeded on mode=ro")
        except sqlite3.OperationalError as e:
            _check("ro_connection_refuses_write", "readonly" in str(e).lower(), str(e))
        finally:
            ro.close()

        before_sha, before_size = _sha_size(tmp_db)
        snap = {sub: _dir_listing(tmp_logs / sub)
                for sub in ("events", "errors", "transcripts", "blobs", "gate")}

        verdict = evaluate(db_path=str(tmp_db))

        after_sha, after_size = _sha_size(tmp_db)
        snap_after = {sub: _dir_listing(tmp_logs / sub)
                      for sub in ("events", "errors", "transcripts", "blobs", "gate")}

        _check("db_sha_unchanged", before_sha == after_sha,
               f"{before_sha} -> {after_sha}")
        _check("db_size_unchanged", before_size == after_size,
               f"{before_size} -> {after_size}")
        for sub in ("events", "errors", "transcripts", "blobs"):
            new = snap_after[sub] - snap[sub]
            _check(f"no_new_files_in_{sub}", not new, f"new={sorted(new)}")
        gate_new = snap_after["gate"] - snap["gate"]
        _check("gate_log_written", bool(gate_new), f"new={sorted(gate_new)}")
        _check("verdict_has_fields", all(
            k in verdict for k in ("n_resolved", "model_brier_mean", "market_brier_mean",
                                   "paper_pnl", "gate1_pass", "gate2_pass", "overall_pass")
        ))
        results["verdict"] = verdict
        results["copied_live_db"] = copied
        results["tmp"] = str(tmp)
    finally:
        # restore env
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        if old_logs is None:
            os.environ.pop("OBS_LOGS_DIR", None)
        else:
            os.environ["OBS_LOGS_DIR"] = old_logs
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return results


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "--self-test" in argv:
        res = _self_test()
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["ok"] else 1
    verdict = evaluate()
    if "--json" in argv:
        print(json.dumps(verdict, indent=2, default=str))
    else:
        render(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
