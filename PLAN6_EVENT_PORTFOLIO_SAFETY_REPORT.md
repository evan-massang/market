# Plan 6 — Event Portfolio Safety Report

> Goal: the event portfolio must never create fake arbitrage, fake risk-free
> baskets, partial basket execution, stale/incomplete legsets, or incoherent event
> exposure — and must never weaken Plan 1-5 safety.
>
> Paper-only. No real-money execution. Branch: `fix/event-portfolio-safety`.
> Built on Plans 1-5 (8ea3920 … 29b90e3).

Status: **COMPLETE** — 60/60 test modules pass (incl. the new 24-case Plan-6 module).
Not committed (awaiting review). Two analysis workflows were run: an understanding sweep
(4 analysts) that confirmed the danger map, and an adversarial verification (3 skeptics)
that found **0 holes** and surfaced 2 low-severity caveats — both now hardened.

## Summary

The event engine (`event_portfolio.evaluate_event`) is **pure** — it only
RECOMMENDS a basket and labeled a NO-on-every-ME-leg overround as "arbitrage /
risk-free." The live consumers (`predict_today` / `sameday`), however, open **only
ONE leg** (`my_pos`) per cycle — never the whole basket, never atomically, over an
**incomplete held-sibling legset** read at **stale open-prices**. So a recommended
"arbitrage" basket would result in opening a *single naked NO leg* while claiming
risk-free — a fake-arb / partial-execution hazard. Plan 6 **disables multi-leg /
arbitrage basket EXECUTION by default** (no atomic multi-leg executor exists), lets
only genuine **single-leg edge** opportunities execute through the per-leg Plan 1-5
gate stack, enforces **one-YES coherence** against the open book, softens the
"risk-free" language to recommendation-only, and adds an integrity check for
multiple open YES legs in one event.

## Old Dangerous Behavior

* **Fake risk-free / fake arbitrage:** `is_arbitrage=True` + explanation "guaranteed
  payoff exceeds total cost ⇒ risk-free" was emitted for a NO-on-every-leg basket
  that the consumer would only ever open ONE leg of.
* **Partial basket execution:** `run_event_portfolio` returns `my_pos` = THIS
  market's leg; the consumer opens just that leg. A single NO leg of a 4-leg hedge
  is not risk-free — its full downside is live. (Workflow quote: "if the consumer
  opens only one leg this cycle … the guaranteed N-1 payoff is destroyed and the
  position is just a single naked NO with full downside.")
* **Assumed (never verified) mutual-exclusivity / exhaustiveness:**
  `run_event_portfolio` forces `mutually_exclusive=True`; the engine also
  auto-detects ME from `len(eligible) >= 2`. Neither verifies the legs are actually
  ME or that the set is exhaustive (`p_norm = model_p/total_p` assumes exhaustive).
* **Incomplete / stale legset:** the legset is the current market + currently-HELD
  siblings (never the full event), and sibling prices are AT-OPEN (stale, not
  refetched). The engine has no staleness gate.
* **Multiple-YES incoherence:** the engine's `n_yes > 1` check only counts its own
  selected positions, not already-HELD YES siblings; the event path skips Guard D
  ("one YES per event").

## New Behavior

* **Event completeness / staleness validation** (`event_safety.validate_event_legset`):
  reports `n_active_markets`, `stale_legs`, `missing_required_legs`,
  `unknown_relationships`, and `exhaustive=False` ALWAYS (a held-sibling legset can
  never be proven exhaustive → can never be risk-free). < 2 active legs / unknown
  relationship / stale leg / missing model_p ⇒ not basket-evaluable.
* **Basket execution disabled by default** (`event_safety.classify_event_execution`
  + `multi_leg_execution_enabled()` default False): an `is_arbitrage` (or multi-leg)
  basket is `event_basket_verified_but_not_executable` → `my_pos` forced to None →
  consumer skips with `event_basket_execution_disabled_no_bet`. No atomic multi-leg
  executor exists, so no basket ever executes.
* **Single-leg edge still works:** a genuine single-leg opportunity
  (`event_single_leg_opportunity`) executes — but only through the existing per-leg
  Plan 1-5 gate stack (EV with m+confidence / risk / bankroll / exposure) which
  validates it is INDEPENDENTLY +EV, then the Plan-4 atomic `wallet.open_position`.
* **One-YES coherence** (`event_safety.check_event_position_coherence`): wired into
  `run_event_portfolio` — a new YES is blocked (`event_already_hold_yes_no_bet`)
  when the open book already holds a YES in the same event; a duplicate market open
  is blocked (`event_incoherent_position_no_bet`).
* **Risk-free label removed:** the engine's prose no longer claims executed
  risk-free; `is_arbitrage` remains as an OBSERVE-ONLY detection flag.
* **DB integrity:** `db_check` adds `event_multiple_open_yes` (WARN if any event has
  > 1 open YES leg).

## Event Entrypoint Map (Phase 1)

| Event path | File/function | Can recommend basket? | Can open position? | Current safety | Gap | Fix |
| ---------- | ------------- | --------------------: | -----------------: | -------------- | --- | --- |
| engine | `event_portfolio.evaluate_event` | YES | **NO (pure)** | accept/reject + exposure cap + worst-case gate | assumes ME/exhaustive; labels risk-free | softened prose; logic kept (recommendation only) |
| consumer build | `predict_today.build_event_legs` | — | no | groups via scanner + shared-slug fallback | legset = current + HELD siblings (incomplete, stale) | unchanged shape; executability now gated downstream |
| consumer run | `predict_today.run_event_portfolio` | — | returns `my_pos` (1 leg) | forced `mutually_exclusive=True` | opened 1 leg of a basket; no coherence | **classify_event_execution** (arb→None) + **coherence check** + stash reason |
| consumer reason | `predict_today.event_leg_reject_reason` | — | no | text reason | generic | surfaces `event_basket_execution_disabled_no_bet` / `event_already_hold_yes_no_bet` |
| predict_today bet path | `predict_one` (is_me_multi) | — | `wallet.open_position(my_pos)` | per-leg EV/risk/bankroll/exposure gates | arb leg could open | my_pos=None ⇒ `_skip` (journaled) |
| sameday bet path | `place_sameday` (event) | — | `wallet.open_position(my_pos)` | per-leg gates + `_sd_skip` | arb leg could open | inherits run_event_portfolio fix; `_sd_skip` journals |
| portfolio_guards | `check_correlation` / `open_positions` | — | no | per-event/theme concentration cap | didn't block 2nd YES specifically | one-YES now enforced in coherence helper |
| wallet | `wallet.open_position` | — | YES (atomic, Plan 4) | guarded/atomic/duplicate-blocked | — | unchanged (the only opener) |

## Event Safety Policy (conservative)

* unknown relationship → no basket (`event_unknown_relationship_no_bet`).
* < 2 active legs → not a basket.
* any stale leg (no price) → no basket (`event_stale_legset_no_bet`).
* any missing model_p → incomplete (`event_incomplete_legset_no_bet`).
* legset is NEVER provably exhaustive → never labeled risk-free.
* `is_arbitrage` / multi-leg basket → execution DISABLED (`event_basket_execution_disabled_no_bet`).
* new YES with a held YES in the event → block (`event_already_hold_yes_no_bet`).
* duplicate market open → block (`event_incoherent_position_no_bet`).
* a single-leg edge leg still passes the full Plan 1-5 per-leg gate stack + atomic wallet.

## Basket Execution Policy

**Multi-leg / arbitrage basket execution is DISABLED BY DEFAULT (Approach B).** There
is NO atomic multi-leg wallet executor (the consumers open one leg at a time), so a
basket can never be opened all-or-none. The engine may RECOMMEND/observe a basket;
the consumer never opens a basket leg. `ENABLE_EVENT_BASKET_EXECUTION` exists as a
documented opt-in but is a no-op until an atomic batch executor is built + verified
(future work). Only single-leg edge opportunities execute, through the existing gates.

## New No-Bet Reasons

`event_incomplete_legset_no_bet`, `event_stale_legset_no_bet`,
`event_unknown_relationship_no_bet`, `event_fake_arbitrage_blocked_no_bet`,
`event_basket_not_executable_no_bet`, `event_basket_execution_disabled_no_bet`,
`event_already_hold_yes_no_bet`, `event_multiple_yes_blocked_no_bet`,
`event_incoherent_position_no_bet`, `event_correlated_exposure_no_bet`.
Strict labels: `event_single_leg_opportunity`, `event_basket_candidate_unverified`,
`event_basket_verified_but_not_executable`, `event_basket_executable`,
`event_basket_blocked`. (In sameday these surface inside `sameday_event_portfolio_no_bet (…)`.)

## Tests Added

`harness/tests/test_event_safety.py` (22): legset validation (complete / missing /
stale / closed-degenerate / unknown-relationship / never-exhaustive); coherence
(already-hold-YES / duplicate / NO-alongside-YES ok / unrelated ok); classification
(arb not executable / single-leg executable / rejected / never-executable-basket-label);
engine-still-recommends-arb (softened prose); run_event_portfolio integration (arb
blocked / single-leg allowed / incoherent-2nd-YES blocked); db_check multiple-YES;
static scans (engine never calls open_position / consumer gates arb / no executed
risk-free claim); + 2 hardening tests (arb blocked even with env flag on; coherence
fail-closed). 24 total.

## Commands Run / Test Results

See `PLAN6_COMMAND_LOG.md`. `test_event_safety` 24/24 (exit 0);
`python run_tests.py --no-llm` → **60/60 modules passed** (exit 0, no FAIL, no skips
beyond the pre-existing LLM-integration self-skip). pytest not used. No live-service /
live-DB / trading commands run.

## Static verification (Phase 12)

| Search | Finding | Safe? | Explanation |
| ------ | ------- | ----- | ----------- |
| `event_portfolio` opens positions | `.open_position(` not present | yes | engine is pure (recommendation only) |
| `is_arbitrage` execution | classify → not executable | yes | `event_basket_execution_disabled_no_bet`, my_pos→None |
| `risk_free`/"⇒ risk-free" executed claim | removed from engine prose | yes | softened to recommendation-only; "NOT risk-free" present |
| multi-YES | `check_event_position_coherence` wired | yes | new YES blocked when a YES is held in the event |
| stale/incomplete legset | `validate_event_legset` | yes | blocks; `exhaustive` always False |
| multi-leg execution | `multi_leg_execution_enabled()` default False | yes | no atomic executor → disabled |
| event no-bets journaled | via `_skip` / `_sd_skip` | yes | exact `event_*_no_bet` codes recorded |

## Remaining Risks (Plan 6 only)

* The single-leg event opportunity executes on the CURRENT market's fresh price; the
  ME-normalization that influenced its selection used STALE sibling prices. This does
  not affect the executed leg's safety — it is independently re-gated by the per-leg
  EV gate on the raw `final_p` (not the normalized prob) — but the *selection* can be
  noisier than predict_today's non-event path. Acceptable (the leg is standalone-+EV-gated).
* No atomic multi-leg executor exists, so genuine +EV multi-leg baskets are left on
  the table (observe-only). This is the safe trade-off until an atomic batch opener is
  built + verified (future work; `ENABLE_EVENT_BASKET_EXECUTION` reserved).

## Proof

* **Incomplete event cannot be called risk-free** — `validate_event_legset.exhaustive`
  is always False (`legset_never_exhaustive_so_never_risk_free`).
* **Stale legset blocks** — `stale_leg_blocks` (`event_stale_legset_no_bet`).
* **Multiple YES blocks** — `already_hold_yes_blocks_new_yes` +
  `run_event_portfolio_blocks_incoherent_second_yes` (`event_already_hold_yes_no_bet`).
* **No partial basket execution** — `run_event_portfolio_blocks_arbitrage_execution`
  (arb → `my_pos None` → `event_basket_execution_disabled_no_bet`); the engine never
  opens (`event_portfolio_engine_never_opens_positions`).
* **Event reject prevents wallet open** — both consumers skip when `my_pos is None`
  (predict_today `_skip`, sameday `_sd_skip`) — journaled.
* **Normal single-leg path still works** — `run_event_portfolio_allows_single_leg_edge`
  + 60/60 suite (predict_today/sameday paths green); the single leg still passes the
  Plan 1-5 gate stack + atomic wallet.
* **Adversarial verification** — 3 skeptics (distinct attack lenses) tried to open an
  arb leg, stack a 2nd YES, bypass Plan 1-5, or find an executed risk-free claim.
  **0 holes found** — each violation is blocked by a citable guard, and
  `evaluate_event` / `classify_event_execution` each have exactly one caller (no bypass
  executor). They raised TWO low-severity caveats, both now HARDENED (below).

## Hardening from adversarial review

* **Arb execution blocked UNCONDITIONALLY** (`event_safety.classify_event_execution`):
  the skeptics noted that flipping `ENABLE_EVENT_BASKET_EXECUTION=true` would have let a
  single arb NO leg open one-at-a-time (no atomic executor exists) — the exact
  fake-risk-free scenario. The `is_arbitrage` block no longer depends on that env flag;
  it is always not-executable. The flag is reserved for a FUTURE atomic batch opener and
  can never enable single-leg arb execution. (`test_arbitrage_blocked_even_if_env_flag_set`)
* **Coherence check is now FAIL-CLOSED** (`predict_today.run_event_portfolio`): the
  one-YES coherence read was wrapped in a fail-open `except: pass`, inconsistent with the
  Plan-1 money gates. If `wallet.get_open_positions()` raises, the leg is now dropped
  (`event_incoherent_position_no_bet`) instead of silently opened.
  (`test_coherence_failure_fails_closed`)

## Phase 14 — acceptance criteria

1. cannot label incomplete baskets risk-free/arbitrage — YES (exhaustive always False; arb execution disabled).
2. stale/incomplete legsets block basket logic — YES (`validate_event_legset`).
3. unknown relationships block — YES (`event_unknown_relationship_no_bet`).
4. multiple YES in ME events blocked by default — YES (coherence helper).
5. existing event exposure checked before adding — YES (coherence reads the open book; per-event exposure cap remains).
6. every basket leg must pass preflight — YES (single legs pass the per-leg Plan 1-5 gate stack; baskets are not executed).
7. multi-leg execution all-or-none OR disabled by default — YES (disabled; no atomic executor).
8. no partial execution treated as success — YES (arb → my_pos None → no-bet).
9. event logic cannot bypass Plan 1-5 gates — YES (single leg goes through the full stack; engine is pure).
10. event reject/observe-only prevents wallet open — YES.
11. all event no-bets journaled with specific reasons — YES.
12. tests prove the above — YES (22 cases).
13. existing tests still pass — YES (60/60).
14. report written — YES (this file).
