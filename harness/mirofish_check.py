"""harness.mirofish_check — inspect MiroFish health, recent runs, and freshness.

    python -m harness.mirofish_check                 backend + recent runs + stale warnings
    python -m harness.mirofish_check --market <id>   all MiroFish runs for one market
    python -m harness.mirofish_check --purge-stale --dry-run   list stale rows that would be ignored

Read-only by default; --purge-stale only REPORTS (it never deletes — stale rows are kept,
marked unusable). Paper-only.
"""
from __future__ import annotations

import os
import sys

from harness import mirofish_validate as mfv


def _backend_status() -> dict:
    base = os.getenv("MIROFISH_BASE", "http://localhost:5001").rstrip("/")
    out = {"base": base, "reachable": False, "reports": None}
    try:
        import httpx
        r = httpx.get(base + "/api/report/list", timeout=6.0)
        out["reachable"] = r.status_code < 500
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        out["reports"] = len(items) if isinstance(items, list) else None
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:60]}"
    return out


def run(market_id: str | None = None) -> dict:
    cfg = mfv.config()
    runs = mfv.get_runs(market_id, limit=25)
    usable = sum(1 for r in runs if r.get("usable"))
    stale = [r for r in runs if r.get("freshness_status") == "stale"]
    return {"backend": _backend_status(), "config": cfg, "n_runs": len(runs),
            "usable": usable, "unusable": len(runs) - usable, "stale": len(stale), "runs": runs}


def render(res: dict, market_id=None) -> None:
    b = res["backend"]
    print("harness.mirofish_check")
    print("-" * 70)
    print(f"  backend     {b['base']}  reachable={b['reachable']}  reports={b.get('reports')}"
          + (f"  ({b['error']})" if b.get("error") else ""))
    c = res["config"]
    print(f"  policy      MODE={c['MODE']}  FORCE_FRESH={c['FORCE_FRESH']}  MAX_AGE={c['MAX_AGE']:.0f}s  "
          f"MIN_POSTS={c['MIN_POSTS']}  MIN_CHARS={c['MIN_CHARS']}  REQUIRE_MATCH={c['REQUIRE_QUESTION_MATCH']}")
    print(f"  runs        {res['n_runs']} recorded · usable {res['usable']} · unusable {res['unusable']} "
          f"· stale {res['stale']}")
    if market_id:
        print(f"  (filtered to market {market_id})")
    print("-" * 70)
    for r in res["runs"][:15]:
        lbl = "FRESH" if r.get("usable") else (r.get("freshness_status") or "?").upper()
        print(f"  [{lbl:<6}] {(r.get('market_id') or '')[:20]:<20} posts={r.get('n_posts')} "
              f"match={r.get('question_match_score')} age={r.get('report_age_seconds')}s "
              f"sim={(r.get('simulation_id') or '-')[:16]} {('' if r.get('usable') else (r.get('warnings_json') or ''))[:60]}")
    print("-" * 70)
    print("  HONEST: a STALE or unusable run is NEVER fed to the swarm; in MODE=required it blocks the bet.")


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    market = None
    if "--market" in argv:
        i = argv.index("--market")
        market = argv[i + 1] if i + 1 < len(argv) else None

    if "--purge-stale" in argv:
        res = run(market)
        stale = [r for r in res["runs"] if r.get("freshness_status") == "stale" or not r.get("usable")]
        print(f"mirofish_check --purge-stale (DRY-RUN — nothing deleted; {len(stale)} unusable/stale run(s)):")
        for r in stale[:30]:
            print(f"  would IGNORE: id={r.get('id')} market={(r.get('market_id') or '')[:20]} "
                  f"status={r.get('freshness_status')} usable={r.get('usable')}")
        print("  (stale rows are KEPT and marked unusable — never silently reused.)")
        return 0

    res = run(market)
    if "--json" in argv:
        import json
        print(json.dumps(res, indent=2, default=str))
    else:
        render(res, market)
    return 0


if __name__ == "__main__":
    sys.exit(main())
