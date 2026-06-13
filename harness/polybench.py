"""
P2.5 — PolyBench OFFLINE historical read (preliminary gate read on resolved markets).

PolyBench ships only pipeline code; the dataset is a separate SQLite DB you
download from the OneDrive link in PolyBench/README.md (browser-only) and save to:
    C:/Users/OMEN/Pictures/Polymarket/PolyBench/database/polymarket_analysis.db
(or pass --db PATH). We read it DIRECTLY with stdlib sqlite3 — we do NOT import
PolyBench's config.py (it sys.exits without a paid OpenRouter key).

What this gives instantly over thousands of markets ($0, no LLM):
  * coverage — how many resolved markets are OPINION vs MECHANICAL (our classifier)
  * the BAR — the market-PRICE Brier on resolved OPINION markets (what our swarm
    must beat for Gate 1), using a pre-resolution price snapshot when available
  * their LLM baselines, if a predictions table is present

Because the exact PolyBench schema can drift, this module INTROSPECTS the DB and
adapts. Run `inspect` first to confirm the columns, then `read`.

CLI:  python -m harness.polybench inspect [--db PATH]
      python -m harness.polybench read    [--db PATH] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3

from harness.classifier import tag_market

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "PolyBench", "database", "polymarket_analysis.db")


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(
            f"[polybench] DB not found at {db_path}\n"
            "  -> download the SQLite DB from the OneDrive link in PolyBench/README.md\n"
            "     and save it there (or pass --db PATH).")
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    return c


def _tables(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]


def _cols(conn, table) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _first(cols: list[str], *cands: str) -> str | None:
    low = {c.lower(): c for c in cols}
    for cand in cands:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def inspect(db_path: str) -> None:
    conn = _connect(db_path)
    print(f"[polybench] {db_path}\n")
    for t in _tables(conn):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  table {t:24s} rows={n:>8d}  cols={_cols(conn, t)}")
    print("\n  -- sample resolved market (best-effort join) --")
    try:
        for row in _resolved_rows(conn, limit=1, verbose=True):
            print(json.dumps({k: (str(v)[:90] if v is not None else None) for k, v in row.items()}, indent=2))
    except Exception as e:
        print(f"  (could not auto-join: {type(e).__name__}: {e} — check column names above)")
    conn.close()


def _to_outcome(winning, outcomes_json) -> float | None:
    """Map a winning_outcome value to 1.0 (YES) / 0.0 (NO) / None."""
    if winning is None:
        return None
    s = str(winning).strip().lower()
    if s in ("yes", "1", "1.0", "true"):
        return 1.0
    if s in ("no", "0", "0.0", "false"):
        return 0.0
    # winning may be an index or the literal outcome string — match against outcomes
    try:
        outs = json.loads(outcomes_json) if isinstance(outcomes_json, str) else (outcomes_json or [])
        outs = [str(o).strip().lower() for o in outs]
        if s in outs:
            return 1.0 if outs.index(s) == 0 else 0.0
        if s.isdigit() and int(s) < len(outs):
            return 1.0 if int(s) == 0 else 0.0
    except Exception:
        pass
    return None


def _yes_price_from(prices_json) -> float | None:
    """Pull a YES price (first element) from a JSON-string/list of prices."""
    try:
        vals = json.loads(prices_json) if isinstance(prices_json, str) else prices_json
        if isinstance(vals, list) and vals:
            return float(vals[0])
        return float(vals)
    except Exception:
        return None


def _resolved_rows(conn, limit: int | None = None, verbose: bool = False):
    """Yield resolved markets joined to their winning outcome, schema-adaptively."""
    tables = _tables(conn)
    mcols = _cols(conn, "markets") if "markets" in tables else []
    if not mcols:
        raise RuntimeError("no 'markets' table")
    q_col = _first(mcols, "question", "title")
    desc_col = _first(mcols, "description", "rules")
    id_col = _first(mcols, "id", "market_id", "condition_id")
    outs_col = _first(mcols, "outcomes")
    mprice_col = _first(mcols, "outcome_prices", "outcomePrices", "market_prices")

    # resolutions table (separate) or an inline winner column
    res_table = "resolutions" if "resolutions" in tables else None
    win_col = None
    if res_table:
        rcols = _cols(conn, res_table)
        win_col = _first(rcols, "winning_outcome", "winner", "resolved_outcome", "outcome")
        rmid_col = _first(rcols, "market_id", "id")
        sql = (f"SELECT m.{id_col} AS mid, m.{q_col} AS question, "
               f"{'m.'+desc_col+' AS description, ' if desc_col else ''}"
               f"{'m.'+outs_col+' AS outcomes, ' if outs_col else ''}"
               f"{'m.'+mprice_col+' AS mprices, ' if mprice_col else ''}"
               f"r.{win_col} AS winning "
               f"FROM markets m JOIN {res_table} r ON r.{rmid_col} = m.{id_col} "
               f"WHERE r.{win_col} IS NOT NULL")
    else:
        win_col = _first(mcols, "winning_outcome", "resolved_outcome", "outcome")
        if not win_col:
            raise RuntimeError("no resolutions table and no winner column on markets")
        sql = (f"SELECT m.{id_col} AS mid, m.{q_col} AS question, "
               f"{'m.'+desc_col+' AS description, ' if desc_col else ''}"
               f"{'m.'+outs_col+' AS outcomes, ' if outs_col else ''}"
               f"{'m.'+mprice_col+' AS mprices, ' if mprice_col else ''}"
               f"m.{win_col} AS winning FROM markets m WHERE m.{win_col} IS NOT NULL")
    if limit:
        sql += f" LIMIT {int(limit)}"
    for r in conn.execute(sql):
        yield dict(r)


def read(db_path: str, limit: int | None = None) -> dict:
    conn = _connect(db_path)
    rows = list(_resolved_rows(conn, limit=limit))
    conn.close()
    total = len(rows)
    opinion, mechanical = 0, 0
    market_briers, scored = [], 0
    for r in rows:
        label = tag_market({"question": r.get("question"), "description": r.get("description")}).label
        if label == "opinion":
            opinion += 1
        elif label == "mechanical":
            mechanical += 1
        else:
            continue
        if label != "opinion":
            continue
        outcome = _to_outcome(r.get("winning"), r.get("outcomes"))
        price = _yes_price_from(r.get("mprices"))
        if outcome is not None and price is not None and 0.0 <= price <= 1.0:
            market_briers.append((price - outcome) ** 2)
            scored += 1
    market_brier = sum(market_briers) / len(market_briers) if market_briers else None

    print("=" * 62)
    print(" P2.5 — POLYBENCH OFFLINE READ (resolved markets)")
    print("=" * 62)
    print(f"   resolved markets read : {total}")
    print(f"   classified OPINION    : {opinion}")
    print(f"   classified MECHANICAL : {mechanical}")
    print(f"   opinion w/ price+outcome (scored): {scored}")
    if market_brier is not None:
        print(f"\n   MARKET-PRICE Brier on resolved OPINION markets: {market_brier:.4f}")
        print("   ^ THIS IS THE BAR — our swarm Brier must come in below it for Gate 1.")
        print("   (Note: price must be a PRE-resolution snapshot, not the settled 0/1 —")
        print("    run `inspect` to confirm which price column was used.)")
    else:
        print("\n   could not compute market Brier — check the price column via `inspect`.")
    print("=" * 62)
    return {"total": total, "opinion": opinion, "mechanical": mechanical,
            "scored": scored, "market_brier": market_brier}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness.polybench")
    ap.add_argument("command", choices=["inspect", "read"])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    if a.command == "inspect":
        inspect(a.db)
    else:
        read(a.db, limit=a.limit)


if __name__ == "__main__":
    main()
