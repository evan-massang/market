# Dashboard Live Telemetry + Paper Wallet Cockpit Report

**Branch:** `ui/live-ai-dashboard-terminal` · **Mode:** PAPER-ONLY. Every visual element is
data-backed; no faked events / PnL / links / MiroFish-used / LLM tokens / websocket activity.
Missing data renders honestly (`unknown` / `stale` / `not_running` / `link_unavailable` /
`payout_unknown` / `timer_unknown` / `missing_proof`). No Plan 1–11 gate is weakened.

> Status: **COMPLETE** — 58/58 telemetry tests + 66/66 suite modules green.

## Phase 1 — Inspection table

| Dashboard surface | Current source | Missing live proof | Planned fix |
| ----------------- | -------------- | ------------------ | ----------- |
| Paper wallet | `/api/state` wallet block | no dedicated cockpit; equity-verified not surfaced | NEW `/api/paper-wallet` (Plan 9 accounting truth; verified_equity or `unverified`) |
| Active bets | `/api/state` AB rows | no per-bet payout/profit/loss, link, countdown, AI reason, proof | NEW `/api/paper-bets/open` + `paper_bets.py` |
| Market links | HTML built `event/${slug}` inline | no honest fallback when slug missing | `paper_bets.market_url` → link or `link_unavailable` |
| Countdown timers | none per-bet | no dedicated timer / awaiting-settlement / invalid states | `paper_bets.timer_for` + client-side per-second timer |
| PnL graph | `/api/state` pnl_series | no verification flag, no empty-state | NEW `/api/pnl-curve` (verified per Plan 9; `not_enough_paper_wallet_history`) |
| AI stream | `/ws/llm` (LLM bet-hunt only), `/api/stream` (log tail) | no unified real LLM/MiroFish/swarm/gate/decision/wallet event stream | NEW `live_events.py` ring buffer + SSE `/events/live` + obs-hooks bridge |
| MiroFish stream | `/api/mirofish` snapshot | no live stage/state events | `mirofish.state` emitted from `mirofish_validate.record_run` (Plan 8 canonical state) |
| Gate stream | `/api/gates` snapshot | no live gate-result events | obs `on_gate`/`on_trade_skip`/`on_trade_open` → `gate.result`/`decision.*` events |
| Proof timeline | none | no per-bet proof of the gate stack | NEW `/api/paper-bets/proof` (`missing_proof`, never fake pass) |
| Dashboard truth | `/api/truth` (Plan 10) | reused as-is | top-bar System badge + Proof panel consume it |

## Summary

Rebuilt the dashboard into a dark cyberpunk Polymarket-AI command center, and — critically — made
it **data-backed**: a new cross-process live-event bus feeds an SSE stream from the *real* AI work,
and new endpoints expose the paper wallet, active/settled bets (with clickable Polymarket links,
countdown timers, and exact win/profit/loss), a PnL/equity curve, a per-bet proof timeline, and a
"what is the AI doing now?" view. Nothing animates unless a real event is flowing; missing data is
shown honestly. No Plan 1–11 gate was weakened.

**Files added:** `harness/live_events.py`, `harness/paper_bets.py`,
`harness/tests/test_dashboard_telemetry.py`. **Edited:** `harness/dashboard.py` (10 new endpoints +
SSE + full HTML/CSS/JS rebuild), `harness/obs/hooks.py` (live-events bridge in the `_hook`
decorator — fires before the obs-enabled gate, fully guarded), `harness/mirofish_validate.py` (one
best-effort `mirofish.state` emit).

## Visual Design

Near-black radial background; glassmorphism cards with magenta/green glow borders; monospace
terminal panels. **Top bar:** Polymarket AI, branch·commit, dirty flag, PAPER-ONLY badge, System
truth badge, DB status, SSE state + last-event age, clock. **Hero row:** Paper Wallet (huge equity,
realized/unrealized/total, accounting), P&L/Readiness (drawdown, CLV, Gate 2), Safety gates.
**Charts:** equity/cash curve (canvas, verified vs unverified). **Center:** AI Swarm Map — canvas
nodes (LLM×3, swarm, challenger, evidence, MiroFish, gates, wallet, accounting, journal) that glow
magenta/green/amber/gray **only** from real event recency; edges pulse when a real event flows.
**Right:** Live AI Stream (SSE) with LLM/MiroFish/Swarm/Gates/Decisions/Wallet/Errors tabs. **Active
Paper Bets** cards, **Recent Settled** table, **Markets Evaluated** (bet/no-bet), **Proof panel**,
**Execution Log** terminal (filterable, marks `[replay]`). Colours map only `ok/healthy/pass/…` to
green — `unknown`/`stale` never green.

## Live Telemetry

Event types: `agent.started/finished/parse_failed/token`, `swarm.started/vote/degraded`,
`forecast.final`, `challenger.vote`, `mirofish.stage/state`, `gate.result`, `candidate.ranked`,
`decision.bet/no_bet`, `wallet.update`, `position.opened/settled`, `pnl.tick`, `evidence.pack`,
`heartbeat`, `log`, `error`. Sources: predict_today, sameday, swarm, agent, challenger, mirofish,
gate, wallet, accounting, profit_intel, dashboard, system. Every event carries `ts`, `source`,
`paper_only=true`; replayed events carry `replay=true`. The bus is a **separate SQLite ring buffer**
(`LIVE_EVENTS_DB`, never `polyswarm.db`) so daemon-emitted events reach the dashboard cross-process.
Instrumentation is the `obs.hooks` bridge (one decorator change) plus the MiroFish emit — all
best-effort: a telemetry failure can never change a forecast/gate/bet (proven by
`broadcast_failure_does_not_affect_decision_path`).

## WebSocket / SSE / API

SSE `/events/live` (EventSource, auto-reconnect, pings, ~2h cap) + REST `/api/live/recent` &
`/api/live/status`. New data endpoints: `/api/paper-wallet`, `/api/paper-bets/open`,
`/api/paper-bets/settled`, `/api/paper-bets/proof`, `/api/pnl-curve`, `/api/equity-curve`,
`/api/pnl`, `/api/ai-now`, `/api/candidates/recent`. All read-only, `_safe`-guarded, `paper_only`.

## Paper Wallet

`/api/paper-wallet` uses Plan 9 `audit_accounting`: starting/cash/realized/unrealized/total,
open exposure, open & settled counts, max drawdown, accounting status + reasons. `verified_equity`
is the audited equity **only when accounting verifies marks**; otherwise `None` + `equity_verified:
false` (the UI shows `unverified`, never a fabricated number).

## Active Paper Bets

`/api/paper-bets/open` per bet: question, `event_slug`→`polymarket.com/event/<slug>` link (or
`link_unavailable`), side, entry (fill) price, shares, stake, fee, **possible payout/profit-if-win +
max loss** (from the verified wallet share model: payout = shares, profit = shares−stake−fee,
max_loss = stake+fee, for both YES and NO), unrealized (or `unknown` without a live mark),
opened/end time, `seconds_until_end` + `timer_status`, AI reason + `gates_passed`, forecast &
challenger probability, MiroFish state (Plan 8), evidence quality. No "guaranteed/safe profit"
wording.

## Settled Paper Bets

`/api/paper-bets/settled`: question, link or `link_unavailable`, side, entry, final price/outcome,
stake, payout, realized PnL, settled time, AI reason, `clv_final`.

## PnL / Equity Graph

`/api/pnl-curve` from `equity_snapshots` (Plan 9 truth): cash/equity/realized/unrealized/total +
drawdown per point, each flagged `verified` from the live accounting status. `< 2` points →
`not_enough_paper_wallet_history`; accounting not-ok → `state: unverified` (points marked
unverified). No fabricated points.

## Proof Panel

Shows SSE state, last event id/age, client count, ai_pipeline & sameday heartbeat states, last
MiroFish state, last decision, branch/commit/dirty, paper-only, DB usability — concrete evidence the
system is actually running, not just a pretty UI.

## AI Stream

LLM (`on_llm_call`→agent.started, `on_agent_estimate`→agent.finished), swarm
(`on_forecast_start`→swarm.started, `on_forecast_final`→forecast.final), challenger (via votes),
MiroFish (`record_run`→mirofish.state), gates (`on_gate`→gate.result), decisions
(`on_trade_open`→decision.bet, `on_trade_skip`→decision.no_bet), wallet/settle
(`on_trade_settle`→position.settled), errors (`on_error`). No token streaming is faked (provider has
none) — agents emit started/finished only.

## Bet Proof Timeline

`/api/paper-bets/proof?position_id=…`: candidate_ranked, evidence_collected, llm_agents_voted,
swarm_consensus, challenger_checked, mirofish_checked, ev/risk/bankroll/exposure gates,
wallet_atomic_open, position_recorded, dashboard_event. Each step is `passed`/`blocked`/
`missing_proof`/`not_applicable`. A missing step is **never** a fake pass (proven by
`proof_missing_step_shows_missing_proof`); an opened position proves the wallet-atomic + money gates
(it could not exist in the wallet otherwise — Plan 3).

## Honesty Rules

No fake events (every event is a real obs/MiroFish callback or replay-marked). No fake green
(`unknown`/`stale` never map to the green var; Gate 2 PASS only from `gate2.pass`; equity verified
only from Plan 9; MiroFish used only from Plan 8 canonical state). No fake PnL (all numbers fetched
from the audited endpoints; no hardcoded figures). No fake links (`event_slug` only, else
`link_unavailable`). No fake payout (invalid/missing → `payout_unknown`). Paper-only is visible on
every surface and envelope. Missing data → `unknown`/`stale`/`link_unavailable`/`payout_unknown`/
`timer_unknown`/`missing_proof`.

## Tests

`harness/tests/test_dashboard_telemetry.py` — **58/58**: event model + live endpoints (6), paper
wallet (5), active bets (8), settled (4), timers (4), PnL graph (4), proof timeline (4), UI/static
(15), instrumentation (8). Full suite `run_tests.py --no-llm` → **66/66 modules**. No live daemons,
no live APIs, no live-DB mutation; temp DB + temp event store only.

| Search | Finding | Safe? | Explanation |
| ------ | ------- | :---: | ----------- |
| `private_key`/`LIVE_TRADING`/`real money`/`wallet.open_position(`/`safe_bet(` in new files | 0 | ✅ | telemetry/UI is read-only; never trades |
| `mirofish_used = True` / `gate2_pass = True` hardcoded | 0 | ✅ | MiroFish used = Plan 8 canonical; Gate 2 = Plan 9 truth |
| fake PnL literals (`127.16`, `+14.39`, `SIMULATED`) | 0 | ✅ | all numbers from audited endpoints |
| honest vocab (`link_unavailable`/`payout_unknown`/`timer_unknown`/`missing_proof`/`unverified`) | 45 | ✅ | missing data shown honestly |
| 10 new endpoints registered | 10 | ✅ | all paper-only, `_safe`-guarded |

## Remaining Risks

UI/telemetry only: (1) per-bet `current_price`/unrealized/CLV show `unknown` unless a live mark is
supplied (no network in the read path) — honest, not faked; (2) live events require the AI daemons
to be running to populate — with no daemon the stream honestly shows idle/stale; (3) the cockpit was
verified via TestClient + the test suite, not a running browser (no live daemon started per the
constraints). No real-money path exists; this is paper-only observation tooling.

## Final Verdict

**PASS.** Dashboard visually upgraded to the dark cyberpunk trading-terminal direction and adapted
to Polymarket AI; SSE live stream of real LLM/MiroFish/swarm/gate/decision/wallet events (no faked
motion); proof panel, paper wallet, active paper bets with clickable links + countdown timers +
payout/profit/loss, recent settled bets, and a PnL/equity graph all present and data-backed; equity
uses Plan 9 accounting truth, MiroFish uses Plan 8 canonical state, Gate 2 uses Plan 9; the
dashboard still cannot fake green; paper-only is visible everywhere; 58/58 + 66/66 tests pass.
