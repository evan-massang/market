# AUDIT REPORT — Polymarket forecasting + paper-trading harness

Audit goal: prove what works, find what's broken, fix bugs, add tests, make the
system stable enough to run continuously **without lying to the user**. Paper-only;
no real-money execution; LLM-key path out of scope (handled separately).

Status legend: ✅ verified working · ⚠️ works with caveat · ❌ broken · 🔧 fixed in this audit

---

## PHASE 0 — Repo map

### Top-level layout
- `polyswarm/` — the system (cloned PolySwarm engine + the `harness/` package).
- Parent `Polymarket/` — PowerShell launchers `1_…`–`5_…`, sibling repos
  (`MiroFish/`, `WhoIsSharp/`, `PolyBench/`, `polymarket-agents/`), and some
  one-off scripts (`selfcheck.py`, `validate_forecast.py`, `bench_models.py`,
  `crowd_local.py`).

### Inside `polyswarm/`
| Area | Files |
|---|---|
| Engine | `core/` (swarm.py, agent.py, calibration.py, calibration_curve.py, regime.py, + 18 aggregation methods), `agents/personas.py`, `api/routes.py`, `main.py` |
| Harness (net-new) | `harness/` — 55 modules (see below) |
| Observability | `harness/obs/` (config, ids, redact, blobs, codeversion, eventlog, evidence, hooks, transcript, explain, gate) + `harness/obs/tests/` (7 acceptance tests) |
| Tests | `harness/tests/` — 34 modules; `harness/obs/tests/` — 7 modules; runner `run_tests.py` |
| Config | `.env`, `.env.example`, `requirements.txt`, `Dockerfile`, `docker-compose.yml` |
| Database | `polyswarm.db` (SQLite; 19 tables), DB path via `DATABASE_URL` |
| Docs | `README.md`, `REPORT.md`, `HARNESS.md` (legacy), `OBS_BUILD_PLAN.md` |

### Entrypoints (`python -m harness.<x>`)
`doctor`, `scoreboard`, `metrics`, `loop` (run/status/daemon), `predict_today`
(once/daemon), `sameday` (daemon), `dashboard`, `scanner`, `event_portfolio`,
`market_quality`, `signals`, `gamma`, `gdelt`, `wiki`, `transcript`, `backtest`,
`benchmark`, `polybench`, `place_bet`, `history`, `health`, `mirofish*`, plus
`harness.obs.explain` / `harness.obs.gate` and `run_tests.py`.

### Daemons
- `harness.sameday daemon` — same-day favorite-longshot + AI scout (shell 3).
- `harness.predict_today daemon` — precise AI pipeline find→gather→MiroFish→LLM→bet (shell 5).
- `harness.loop daemon` — generic find/settle loop.

### Dashboard
- `harness.dashboard` (FastAPI, :8800) — `/`, `/api/state`, `/api/health`,
  `/api/stream`, `/api/mirofish_graph`, `/api/mirofish_report`, `/ws/llm`, and
  (P11) `/api/command_center`, `/api/explain/{market_id}`.

### Paper-trading / forecasting / obs files
- Paper wallet: `harness/wallet.py` (paper_wallet + paper_positions, $1-payout share model).
- Forecasting: `core/swarm.py`, `core/agent.py`, `agents/personas.py`,
  `harness/challenger.py`, `harness/predict_today.py`, `harness/loop.py`.
- Risk/guards/sizing: `sizing.py`, `profitability.py`, `adaptive.py`,
  `market_quality.py`, `portfolio_guards.py`, `risk_guards.py`, `bankroll.py`,
  `event_portfolio.py`.
- Settlement/scoring: `loop.settle_resolved`, `scoreboard.py`, `metrics.py`,
  `core/calibration.py`, `clv.py`, `label_perf.py`, `forecaster_weights.py`.
- Observability: `harness/obs/*`.

### Map-level findings
- ⚠️ **Stray duplicate test files in `harness/` root**: `test_challenger.py`,
  `test_classifier.py`, `test_scoreboard.py`, `test_sizing.py`, `test_wallet.py`
  exist alongside the canonical `harness/tests/`. Risk of confusion / stale copies.
  (To verify: are they discovered by `run_tests.py`? are they stale duplicates?)
- ⚠️ `verify_p05.py` — a one-off P5 verification script left in `polyswarm/`.
- ⚠️ `HARNESS.md` — flagged legacy; may contain stale instructions (to diff vs README).

---

## PHASE 1 — Reproduce the documented commands (ground truth)

Run with `PYTHONUTF8=1 ./.venv/Scripts/python.exe …` (git-bash) — identical module paths to the README's PowerShell form.

| Command | Result |
|---|---|
| `python -m harness.doctor` | ✅ 11 PASS / 1 WARN (gdelt transient timeout) / 0 FAIL |
| `python run_tests.py` (default = no-LLM) | ✅ 41/41 modules pass |
| `python -m harness.scoreboard` | ✅ runs; honest GATE1/GATE2 **FAIL** + P7 analytics |
| `python -m harness.loop status` | ✅ shows paper wallet + open positions |
| `python -m harness.obs.gate` | ✅ read-only gate eval; writes only to `logs/gate/` |
| `python -m harness.metrics` | ✅ honest report (log loss 0.758 > coin-flip 0.693) |
| `python -m harness.predict_today once --max 1 --dry-run` | ⚠️ **slow** — >60s in FIND stage with no progress output (live multi-window Gamma scan); needs a faster/--max-bounded scan or progress feedback. (to confirm: hang vs slow) |
| live daemons (shells 3+5) + dashboard (:8800) + MiroFish (:5001) | ✅ all up (doctor confirms heartbeat 221s, dashboard serving) |

**README accuracy: largely correct.** Every documented `harness.*` command that
was tested works. `run_tests.py` defaults to no-LLM (only `--llm` opts in), so the
documented `run_tests.py` does not hang on LLM calls.

---

## PHASE 2-17 — see DEBUG_LOG.md for per-bug detail; this section is updated as fixes land.

_(Deep code audit in progress — confirmed bugs, fixes, and added tests recorded below as each category is closed.)_

---

## Honest status (not a profitability claim)
Both go/no-go gates are **NOT met** and the system reports so plainly:
GATE1 FAIL (0 resolved opinion markets, needs ≥50), GATE2 FAIL (equity below
start), model log loss 0.758 > coin-flip 0.693. The system is paper-only and
exists to *measure* whether the edge is real before any real money.
