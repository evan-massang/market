# REMAINING PROBLEMS — plan & status

Making the repo a TRUSTWORTHY profitability-measuring engine. Paper-only; no
real-money; no faked profitability; never weaken guards to bet more. The
one-command supervisor is DONE and out of scope here.

Status: ☐ todo · ◑ in progress · ☑ done (commit)

## Priority order (honesty-critical first)

### Methodology / statistics
- ☐ **P1 Gate-1 bet-bias.** `loop.settle_resolved` only resolves forecasts for
  markets with an OPEN paper position (iterates `wallet.get_open_positions()`),
  so forecasts on non-bet markets keep `outcome=NULL` and Gate 1 scores only the
  bet-selected subset → biased. Fix: resolve ALL open `swarm_forecasts` (sweep
  their market_ids vs Gamma), not just bet ones. Gate 1 = all resolved eligible
  opinion forecasts; Gate 2 = paper trades only (already split).
- ☐ **P10 Scoreboard split + small-sample guards.** Forecast Gate vs Trading Gate,
  per-segment sample size, "needs more data / do not trust yet" warnings, never
  let a tiny sample fake a pass (Gate 2 floor already added; extend reporting).

### Correctness bugs
- ☐ **P3 Demo/test contamination.** `swarm_forecasts` holds `TEST` + `bench-test`
  rows (real markets are `0x…` condition IDs). Gates must default to real
  (live/paper_live) only; add an `environment` classification + opt-in flags.
- ☐ **P5 Guard/calibration probability.** Define one canonical `decision_p`
  (calibrated if available, else conservative shrink of raw toward market); guards
  (incl. Guard D) + sizing use `decision_p`, never raw LLM confidence. Store
  raw_swarm_p / challenger_p / calibrated_p / decision_p / market_p separately.

### Database / wallet
- ☐ **P2 db_check reconciliation + repair.** Extend `harness.db_check`: duplicate
  settlements/trade-ids, orphan trades/forecasts, negative cash, impossible
  equity, closed-once, open-not-already-resolved; `ledger_reconciliation_report`;
  `--repair-dry-run` / `--repair` (safe deterministic only) writing an audit event;
  mark legacy pre-fix rows. (Reconciliation already exists; add repair + checks.)

### Guard / calibration / profitability
- ☐ **P6 EV-after-costs penalties.** Extend `profitability.ev_after_costs` with
  spread/liquidity/uncertainty/exit-risk penalties + `MIN_EV_AFTER_COSTS` and the
  penalty multipliers; bet only if it survives. (EV gate exists; add penalties.)
- ☐ **P4 Event portfolio math.** Strengthen `event_portfolio` (outcome-by-outcome
  PnL table, skipped-leg explanations, after-cost EV per leg, fake-arb/liquidity
  rejection). Core engine exists from P3 + the lone-leg audit fix.

### Reliability / network
- ☐ **P8 Retry/backoff.** Shared retry util (timeout + exp backoff + jitter + max
  attempts) for Gamma/GDELT/Wikipedia/MiroFish; MiroFish deadline cap → degraded
  mode; required-data failure skips safely.
- ☐ **P7 CLV snapshots.** Entry + 15m/1h/6h/close price snapshots per trade; CLV by
  theme/mode/strategy. (CLV-at-resolution exists; add timed snapshots.)

### Config / docs
- ☐ **P9 config_check + .env.example.** `harness.config_check` (missing required /
  optional / unknown / secrets-masked); wire or remove `SWARM_SIZE`; add
  challenger/dashboard/timeouts/retry/EV/exposure/CLV vars; docs match code.

### Dashboard
- ☐ **P11 Dashboard endpoints.** `/api/gates/detailed`, `/api/db/reconciliation`,
  `/api/event-portfolios/recent`, `/api/clv/summary`, `/api/config/status` + show
  forecast-vs-trade split, demo exclusion notice, decision_p source.

## Status

- ☑ **P1 Gate-1 bet-bias** (`b672a9f`): `loop._settle_unbet_forecasts` resolves ALL
  real forecasted markets, not just bet ones. +`test_gate_methodology`.
- ☑ **P3 Demo/test exclusion** (`b672a9f`): `harness/environment.py` + scoreboard filter;
  gates default to live/paper_live; `--include-test`/`--include-demo`/`--environment all`.
- ☑ **External-brain interface** (`3173809`, the new direction): `harness/brain/`
  (BrainProvider + EvidencePack/ForecastResult/… + swarm/mock/disabled/manus providers +
  build_brain_pack + non-LLM critic). LLM is now a replaceable component; runs observe-only
  without any LLM. +`test_brain`. Dashboard `/api/brain/status`. `BRAIN_ARCHITECTURE.md`.
- ☑ **P8/Reliability — retry/backoff** (`<this commit>`): `harness/retry.py`
  (timeout-aware backoff + jitter + bounded attempts + give-up), wired into the Gamma
  hot-path fetch (retry transient/5xx, give up on 4xx). +`test_retry`.

### Already existed before this task (verified, mapped to the requests)
Scanner multi-window (`scanner.py`) · persistent cache (`datacache.py`) · structured
evidence packs (`evidence_pack.py` + obs `evidence.pack`) · forecastability classifier
labels (`classifier.py` P4) · event-portfolio engine (`event_portfolio.py`) · EV-after-costs
gate (`profitability.py`) · calibration + decision prob `final_p` (`calibration_apply.py`,
`forecaster_weights.py`) · CLV at resolution (`clv.py`) · performance memory
(`label_perf.py`, `adaptive.py`, `metrics.py`) · dual gates + Gate-2 floor (`scoreboard.py`)
· db_check reconciliation (`db_check.py`) · command-center + system status (`command_center.py`,
dashboard) · per-daemon heartbeats + crash-safety (supervisor). Suite 51 modules.

### Deferred (documented, not yet done — none block paper-only honesty)
- **P2** db_check `--repair`/`--repair-dry-run` + audit-event (reconciliation report exists).
- **P5/P12** canonical `decision_p` storage + Guard-D on final_p (dormant: final_p==p cold).
- **P6/P7** EV-after-costs spread/liquidity/uncertainty/exit penalties + their config knobs.
- **P7/CLV** timed 15m/1h/6h snapshots (CLV-at-resolution exists).
- **P9** `harness.config_check` + remaining `.env.example` vars.
- **P11** extra dashboard endpoints (`/api/db/reconciliation`, `/api/clv/summary`, …).

The honest bottom line is in the final report below / in chat: the two measurement-honesty
fixes (Gate-1 unbias, demo exclusion) and the external-brain decoupling are the load-bearing
new work; the rest of the 20-phase plan largely already existed and is mapped above.
