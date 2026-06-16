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

_(Implemented phases are marked ☑ with their commit as work lands. Honest note:
this is a large program; I am doing the honesty-critical correctness phases first
and will record exactly what is done vs deferred in the final report.)_
