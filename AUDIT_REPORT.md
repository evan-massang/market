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

## PHASE 2-17 — deep code audit + fixes

A 14-subsystem **adversarial** audit (each finding independently reproduced before any
fix) surfaced **41 confirmed defects + 2 refuted** (1 critical, 18 major, 22 minor).
Fixed in 8 priority batches; **every fix ships a regression test.** Per-bug detail with
reproductions and file:line is in `DEBUG_LOG.md`. Summary:

### 1. What was broken (highlights)
- 🔴 **Settlement double-credit** — `settle_market`/`close_at_price` had no `status='open'`
  guard on the UPDATE/wallet-credit and no cross-process lock; two daemons could credit a
  position twice, **silently inflating the realized P&L Gate 2 reads**.
- 🟠 **Parser fragility** — one malformed LLM reply (`"60%"`/null/prose) crashed the whole
  swarm forecast; the challenger turned `"60 percent"` into 0.99.
- 🟠 **P&L inconsistency** — cashed-out (`closed`) trades were excluded from every analytic
  but counted in Gate 2 → two conflicting realized numbers, hidden losers.
- 🟠 **Classifier** — approval-rating opinion markets mislabeled mechanical (skipped);
  non-political "candidate" markets mislabeled opinion.
- 🟠 **Daemon CLI** — hand-rolled parsers IndexError-crashed / silently swallowed
  `--dry-run` & unknown commands; **sameday didn't enforce observe-only** and its skips
  were invisible (never reached the journal/dashboard).
- 🟠 **Event-portfolio** — a forced-ME event with one eligible leg fabricated a guaranteed
  win (defeating the worst-case risk gate).
- 🟠 **Dashboard** — `/api/state` 500'd under DB write contention; `/health` `/debug`
  `/errors` `/decisions/recent` missing.
- 🟠 **DB-path** — bare `sqlite:///` mis-parsed by 5 old modules (split-brain / crash).
- 🟡 **Gate 2** could flash PASS on one trade; `MODEL_FAST` env leak; dead `is_stale`
  freshness branch; wallet↔ledger drift unreconciled.

### 2. What was fixed (8 batches, commits `6861ce5`…`129d6f2`)
B1 settlement idempotency (guarded UPDATE + rowcount + busy_timeout) + fee math + new
`db_check` reconciliation · B2 parser robustness (`_coerce_prob`, per-agent skip, neutral
degraded forecast) · B3 P&L metrics include `closed` + classifier approval/candidate ·
B4 argparse + `--dry-run` honored + sameday observe-only + visible skips · B5 dashboard
`/health` `/debug` `/errors` `/decisions/recent` + `/api/state` crash-safety · B6
event-portfolio lone-leg fallback · B7 `sqlite:///` db-path + dedicated **loss-cause
analyzer** · B8 Gate-2 sample floor + env-leak/is_stale/docstring.

### 3. Tests added (suite 41 → 47 modules, all no-network)
`test_settlement_idempotent` (6), `test_db_check` (4), `test_parser_robust` (5),
`test_cli_args` (3), `test_dashboard_endpoints` (4), `test_loss_analysis` (6), plus new
cases in `test_metrics`/`test_event_portfolio`/`test_scoreboard`. **`run_tests.py` → 47/47.**

### 4. What still needs work
See the "Remaining" list in `DEBUG_LOG.md`: Gate-1 bet-bias (#10), EV-gate-vs-arb (#7) +
arb-liquidity, Guard-D-on-final_p (#28, dormant), gamma retry/backoff, MiroFish deadline
cap, `.env.example`/`SWARM_SIZE` hygiene, attention-metric label (no behavior). None block
paper-only operation. **The live `polyswarm.db` has a real wallet↔ledger drift** (wallet
realized −42.25 vs ledger −39.43; equity invariant off ~$40) from earlier external row
deletion — now **surfaced** by `db_check` (was silently trusted). It does not affect the
gate verdict (both already FAIL) but should be reconciled before reading Gate 2.

---

## PHASE 18 — final status

5. **How to run** (from `polyswarm/`, `PYTHONUTF8=1 ./.venv/Scripts/python.exe …`):
   - `-m harness.doctor` preflight · `-m harness.db_check` DB integrity/reconciliation
   - `-m harness.predict_today daemon --with-mirofish` (AI pipeline) · `-m harness.sameday daemon`
   - `-m harness.dashboard` (:8800) · `-m harness.scoreboard` / `-m harness.metrics` (gates)
   - `-m harness.loss_analysis` (loss-cause) · `python run_tests.py` (47/47)
6. **How to verify it works:** `python run_tests.py` → 47/47; `harness.doctor` → 11 PASS/1
   WARN/0 FAIL; `harness.db_check` → integrity OK (reconcile WARNs on the live drift, by
   design); `harness.scoreboard`/`harness.metrics` report honest FAILs.
7. **Known limitations:** swarm forecast ~235s/market on CPU; gates need weeks of resolved
   opinion markets; the live wallet running-total has drifted (visible via `db_check`).
8. **Safe to run paper-only?** **Yes.** No real-money execution exists; every change here is
   a tightening or read-only; `wallet.py` has no signing/order path (asserted by
   `test_acceptance` C18). The settlement double-credit (the one integrity risk) is fixed.
9. **Profitability proven?** **No — and not claimed.** GATE1 FAIL (0 resolved opinion
   markets, needs ≥50), GATE2 FAIL (equity below start), model **log loss 0.758 >
   coin-flip 0.693**. The system measures whether the edge is real before any real money.
10. **Next highest-impact:** reconcile the live wallet ledger (a `db_check --fix` or fresh
    bankroll); fix Gate-1 bet-bias (#10) so calibration is scored on the full forecast set;
    then accrue ≥50 resolved opinion markets to actually read the gates.

---

## Honest status (not a profitability claim)
Both go/no-go gates are **NOT met** and the system reports so plainly:
GATE1 FAIL (0 resolved opinion markets, needs ≥50), GATE2 FAIL (equity below
start), model log loss 0.758 > coin-flip 0.693. The system is paper-only and
exists to *measure* whether the edge is real before any real money.
