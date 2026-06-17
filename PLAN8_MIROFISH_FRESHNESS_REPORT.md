# Plan 8 — MiroFish Freshness + Contribution Honesty

**Branch:** `fix/mirofish-freshness-honesty`  ·  **Mode:** PAPER-ONLY (no trades placed, no live daemons run)
**Status:** implementation + tests complete; adversarial verification below.

## Goal

> The bot must never claim MiroFish contributed to a forecast, gate, score, dashboard, or
> no-bet/bet decision unless a fresh, completed, market-matched MiroFish result was actually
> read and used.

Concretely, none of these may ever happen: use stale MiroFish output; use a report from a
different market; treat an incomplete/`sim_prepared`/pending sim as complete; claim
`mirofish_used=true` when MiroFish was only launched fire-and-forget; show MiroFish
green/healthy when it is unavailable/stale/failed/pending/not-used; silently ignore failure
when MiroFish is required; let failure become fake confidence; let stale data reach
sizing/wallet; or hide MiroFish status from the journal/obs/report.

## What was wrong (before)

`harness/mirofish_validate.py` already decided whether a *single* result was fresh,
market-matched, and complete (its `usable` flag), and `predict_today` fed a `usable` report
to the swarm. But there was **no canonical notion of CONTRIBUTION**:

1. `mirofish_used` had no single source of truth — different layers could disagree.
2. A backend merely being **alive** could read as "healthy/green" — liveness was conflated
   with contribution.
3. The same-day path launches MiroFish **fire-and-forget** and never reads the result, yet
   nothing structurally guaranteed that path reports `mirofish_used=false`.
4. **Leak:** a `sim_prepared` (not-yet-run) payload with **no timestamp** is "fresh by
   construction" under `FORCE_FRESH`; with filler text ≥ `MIN_CHARS` it could pass
   `validate()` as `usable=true` — a pending sim masquerading as a completed contribution.
5. A `usable` result with **no `report_generated_at`** had freshness that could not be
   *verified* — yet `FORCE_FRESH` trusted it.
6. "Required MiroFish" had only a coarse unusable reason, not a specific, honest no-bet code.

## The fix — a canonical CONTRIBUTION state machine

New module **`harness/mirofish_status.py`** sits on top of the existing validator and answers
the one honesty question: *was a fresh, completed, same-market, verifiable MiroFish result
actually CONSUMED by this decision?* — the only case in which `mirofish_used` may be true.

**12 canonical states** (exactly one per decision):
`not_configured · disabled · backend_unavailable · launch_only_not_used · pending ·
timed_out · failed · invalid_result · stale_result · market_mismatch · fresh_unused ·
fresh_used`. Only `{fresh_unused, fresh_used}` are *usable*; only `fresh_used` is *used*.

Each status dict carries: `state`, `mirofish_used`, `allow_decision_use`, `allow_bet`,
`required`, `reason`, `contribution`, and the result metadata (market, timestamps, age,
sim_status, n_sims, score, warnings) for the journal/obs/dashboard.

Key API:
- `from_result(result, *, required, consumed)` — maps a validated `MiroFishResult` to a state.
  Priority: `backend_unavailable` → `failed` → **`pending` (any prepared/running stage,
  ALWAYS, regardless of `usable`)** → `stale` → `market_mismatch` → low-sims `invalid` →
  **unverifiable-timestamp `invalid`** → `fresh_used/fresh_unused` → `invalid`.
- `mark_used(status, contribution)` — flips `fresh_unused`→`fresh_used`; **no-op on every
  non-usable state** (stale/pending/failed/… can never become used).
- `required_no_bet_reason(status, prefix)` — the specific canonical no-bet code when MiroFish
  is required and not used (e.g. `mirofish_required_stale_no_bet`, `…_pending_no_bet`,
  `…_market_mismatch_no_bet`, `…_launch_only_no_bet`).
- `state_from_row(row)` — maps a persisted `mirofish_runs` row to a state for HONEST display,
  using the **same** criterion as `from_result` (usable **and** verifiable timestamp →
  `fresh_used`; else stale/failed/mismatch/invalid — never green).
- Config: `required_for_bet()` (new `MIROFISH_REQUIRED_FOR_BET` OR legacy `MIROFISH_MODE=required`),
  `max_age_seconds()`, `min_sims()`, `allow_stale_for_display()/_for_bet()`.

### Wiring

| Layer | Change |
|---|---|
| `predict_today.py` REPORT step | Builds the canonical status from the validated result; **feeds the swarm ⟺ `allow_decision_use` ⟺ `mirofish_used`** — one criterion. Sets `m["_mirofish"]/_mirofish_used/_mirofish_state/_mirofish_reason/_mirofish_contribution/_mirofish_required` and prints the state. A stale/weak/pending/wrong-market/failed/unverifiable report is **never appended**. |
| `predict_today.py` `_p_mirofish_gate` | Required mode (new flag OR legacy) → returns the specific canonical `mirofish_required_*_no_bet` reason; the gate runs **before** sizing/wallet. Optional mode never blocks on MiroFish (degraded mode still evidence-gates). |
| `sameday.py` `_ai_scout` | Health dict marks `mirofish_state=launch_only_not_used` + `mirofish_used=False` (fire-and-forget is structurally not-used). |
| `sameday.py` `place_sameday` | Required-mode gate after swarm-health: `launch_only(required=True)` → `sameday_mirofish_required_launch_only_no_bet`, journaled, `continue` (no wallet). |
| `dashboard.py` `/api/mirofish/runs` | Per-run `state` + `mirofish_used`/`fed_to_swarm` derived from the canonical state (not the raw `usable` column); top-level `used` count + a note: *"mirofish_used is true ONLY for a fresh, market-matched run actually fed to the swarm. Backend liveness is NOT a contribution."* |
| `config_check.py` + `.env.example` | 6 Plan-8 flags registered/documented. |

### Config flags (all default to the SAFE value)

| Flag | Default | Meaning |
|---|---|---|
| `MIROFISH_REQUIRED_FOR_BET` | `false` | require a fresh *used* result to bet |
| `MIROFISH_MAX_AGE_SECONDS` | `900` | max result age before stale |
| `MIROFISH_WAIT_TIMEOUT_SECONDS` | `0` | 0 = fire-and-forget (never wait) |
| `MIROFISH_MIN_SIMS` | `3` | min crowd posts/sims for a usable result |
| `MIROFISH_ALLOW_STALE_FOR_DISPLAY` | `true` | stale may be shown as historical |
| `MIROFISH_ALLOW_STALE_FOR_BET` | `false` | stale may NEVER bet |

## Tests

`harness/tests/test_mirofish_honesty.py` — **52/52 passed** (34 base + 18 adversarial-regression
across rounds 1–6). Coverage:

- **State machine (14):** fresh-consumed→used; fresh-unconsumed→mark_used; stale→display-only +
  mark_used no-op; wrong-market→mismatch; **pending/sim_prepared→never used (the leak)**;
  failed; backend_unavailable; **missing-timestamp→unverifiable→not used**; low-sims→invalid;
  disabled; not_configured; launch_only; **exhaustive "only fresh+consumed yields used"**;
  mark_used no-op on every unusable state.
- **Required policy (7):** unavailable/stale/pending/market_mismatch each block with their
  specific reason; fresh_used allows; launch_only blocks with `sameday_` prefix; optional
  failure never blocks.
- **predict_today gate (4):** optional-unusable doesn't block; required-stale and
  required-pending block *before the wallet* with the right reason; usable passes.
- **Dashboard honesty (5):** usable+ts→used; **usable-no-ts→not used (backend-alive ≠ used)**;
  stale→stale; failed→failed; live endpoint counts exactly ONE of {fresh, stale, usable-no-ts}
  as used.
- **Backend-liveness (1):** running/queued/started/sim_prepared → pending, never used.
- **Static scans (3):** no hardcoded `mirofish_used=True`; `used` only from canonical state;
  sameday required-gate wired.

**Regression:** `run_tests.py --no-llm` → **62/62 modules passed** (61 prior + the new module;
no regressions). `test_mirofish_validate` (12/12) and `test_sameday_parity` (21/21) green.

> Note: the console is cp1252; reports print `→`/`🐟`/`🤖`, so tests must run with
> `PYTHONUTF8=1` (the suite runner sets UTF-8 itself).

## Static verification

- Whole-repo grep `mirofish_used\s*[:=]\s*True` → **no matches**. The only place the flag is set
  true is `mirofish_status.mark_used` (the canonical machine, gated on `allow_decision_use`).
- `predict_today` and `dashboard` **derive** `mirofish_used` from the canonical state; `sameday`
  only ever sets `False`.
- `core/` has **zero** MiroFish references — no path bypasses the harness state machine.
- `mirofish_status.py` has no liveness/ping logic; its docstring states *"a backend being ALIVE
  is NOT a contribution."*

## Adversarial verification

An 8-skeptic workflow (`plan8-mirofish-adversarial`) each tried to BREAK Plan 8 — make a
stale / pending / launch-only / failed / wrong-market / unverifiable output influence a bet or
appear used, plus required-mode bypass and cross-layer consistency. **Round 1 confirmed 2
attacks closed (launch-only, failed/backend-down) and found 6 real holes** (2 critical, 2
high, 2 medium). All 6 were fixed and given a dedicated regression test; round 2 re-attacked
the patched code.

| # | Sev | Attack | Hole | Fix | Test |
|---|---|---|---|---|---|
| 1 | **critical** | required-mode bypass | `MIROFISH_REQUIRED_FOR_BET=true` + `USE_MIROFISH=false` → `_p_mirofish_gate` early-returned `ok` before the required check → bet with **no MiroFish at all** | gate reordered: the required check runs BEFORE the off/disabled early-out → fails closed (`mirofish_required_unavailable_no_bet`) | `pt_required_blocks_when_mirofish_off`, `test_off_and_usable_pass` |
| 2 | **critical** | unverifiable timestamp | the verifiability check used *truthiness* of `report_generated_at`; a garbage non-parseable string is truthy → bypassed → `fresh_used` | `from_result` + `state_from_row` key off `report_age_seconds is None` (set only when the timestamp actually parses) → garbage ts → `invalid_result` | `unverifiable_malformed_timestamp_not_used`, `dash_garbage_timestamp_not_used` |
| 3 | **high** | wrong-market | setting `MIROFISH_REQUIRE_QUESTION_MATCH=false` makes the validator mark a low-qms report `usable=true`; the mismatch check was `not usable and qms<thr`, so `usable=true` slipped through | `from_result` enforces `qms < threshold → market_mismatch` **independently** of the validator's `usable` flag | `wrong_market_independent_of_question_match_flag` |
| 4 | **high** | sim_prepared persisted | `stage_reached` wasn't stored, so the dashboard's `state_from_row` showed a pending sim as green | validator forces `usable=false` for incomplete stages (root); `stage_reached` now persisted (+ idempotent `ALTER`); `state_from_row` returns `pending` for incomplete stages | `validate_rejects_incomplete_stage`, `dash_sim_prepared_row_pending` |
| 5 | medium | retroactive MAX_AGE | `state_from_row` trusted the cached `freshness_status`; a report fresh-when-recorded showed green even after it aged past (or `MAX_AGE` was reduced below) its age | `state_from_row` RE-VALIDATES age from `report_generated_at` vs the CURRENT `MAX_AGE` at display time → now-stale shows `stale` | `dash_reaged_past_maxage_is_stale` |
| 6 | high | (= #4) stage inconsistency | duplicate of #4 (stage not persisted → dashboard `used` count wrong) | same as #4 | same as #4 |

**Round 2** re-attacked the patched code: round-1 fixes all held (stale, launch-only, failed,
required-gate-exception confirmed closed), but the skeptics found **4 deeper edges** (2
critical, 2 high). All fixed + regression-tested:

| # | Sev | Attack | Hole | Fix | Test |
|---|---|---|---|---|---|
| 7 | **critical** | `sim_running` stage | `_INCOMPLETE_STAGES` blacklisted `"running"` but the pipeline emits `"sim_running"` (name mismatch) → a still-running sim read as complete | replaced the blacklist with a **completion WHITELIST** (`report_done`, `probability_extracted`); any other/empty/future stage is incomplete → `pending` (validator + state machine + dashboard) | `sim_running_never_used`, `dash_sim_running_row_pending` |
| 8 | **critical** | future-dated timestamp | a `report_generated_at` in the future → negative age; the stale check (`age>MAX_AGE`) and the unverifiable check (`age is None`) both missed it → `fresh_used` | reject `age < -120s` (future, beyond benign clock skew) in `validate` + `from_result` + `state_from_row` + `is_stale_now` | `future_timestamp_not_used`, `dash_future_timestamp_not_used` |
| 9 | high | negative MATCH_THRESHOLD | `MIROFISH_MATCH_THRESHOLD=-1` → `qms < -1` always False → market-match gate silently off | an out-of-range threshold falls back to the safe default `0.30` (clamping to 0 wouldn't help — `qms<0` never fires) | `negative_match_threshold_cannot_bypass` |
| 10 | high | used-flag flip | the round-1 dashboard re-aging made `mirofish_used` flip true→false between decision and display | `state_from_row` is now **immutable** (uses the frozen recorded age); current freshness is a SEPARATE `is_stale_now`/`stale_now` signal — "used in this run, report now stale" | `dash_used_is_immutable_but_stale_now_flagged` |

A clock-skew tolerance (`_FUTURE_SKEW_SECONDS = 120`) was added so benign sub-minute skew /
timestamp truncation between the MiroFish backend and the harness isn't mistaken for a future
spoof — this also prevents valid reports from being wrongly rejected in production.

**Round 3** re-attacked again: only **2 deeper edges** remained (both high, no critical), each
a tightening of a round-2 fix:

| # | Sev | Attack | Hole | Fix | Test |
|---|---|---|---|---|---|
| 11 | high | tiny positive threshold | `MIROFISH_MATCH_THRESHOLD=0.0001` is "in range" but still disables the gate (`qms<0.0001` ~never fires) | the gate may only be made STRICTER: any in-range value is FLOORED at the default `0.30` (effective ∈ [0.30, 1.0]) | `tiny_match_threshold_floored_at_default` |
| 12 | high | low-sims dashboard flip | `from_result` rejects `n_posts < min_sims` → invalid, but `state_from_row` didn't mirror it → low-sims run showed `fresh_used` | `state_from_row` now mirrors the low-sims gate (rejects on the recorded `n_posts`) | `dash_low_sims_not_used` |

**Convergence:** exploitable findings fell 6 → 4 → 2 across rounds 1–3, with round-3 issues
being refinements (no new attack classes; stale/pending/launch-only/failed stayed closed every
round).

**Round 4** raised 4 reports — triaged to **2 genuine fixes + 2 non-issues**:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 13 | **critical** | fail-OPEN exception handler in `_p_mirofish_gate` — if `mirofish_validate` import/`config()` raised, the gate returned `ok`, allowing a bet with MiroFish required | **FIXED** — the handler now fails CLOSED when `required_for_bet()` (env-only, robust): returns `mirofish_config_unavailable_no_bet`. Optional mode still proceeds. Test `pt_required_fails_closed_when_config_raises` |
| 14 | high | config-change flip — `state_from_row` re-read `min_sims()`/`_match_threshold()` from env, so changing them after a decision flipped a historical run's used-flag | **FIXED** — the decision-time thresholds are now FROZEN into the run row (`min_sims_used`, `match_threshold_used`) and `state_from_row` uses them (current config only for legacy rows). Test `dash_uses_frozen_thresholds_no_flip`. (Note: the flip was in the *safe, under-claiming* direction, but the documented immutability guarantee is now honored.) |
| — | high | "FORCE_FRESH no-timestamp → used" | **NOT A BUG (false positive)** — a no-timestamp result has `report_age_seconds is None`, so `from_result` already returns `invalid_result` (not used); proven by `missing_timestamp_unverifiable_not_used`. The HMAC/backend-signing ask is a different threat model (compromised backend channel), out of Plan 8 scope. |
| — | high | "wrong-market via >30% keyword overlap" | **OUT OF SCOPE / inherent** — the market-match gate IS enforced (floored 0.30, independent of the validator flag) and doubly mitigated by FORCE_FRESH's unique project-per-market. Perfectly separating near-identical markets needs the backend to echo `market_id` (a backend change outside this harness plan). Documented as a residual limitation. |

**Convergence (genuine findings):** 6 → 4 → 2 → 2, and every mandated category
(stale/pending/launch-only/failed/wrong-market) stayed closed across all rounds.

**Residual limitations (documented, not bugs):** (a) market-identity rests on question-keyword
overlap ≥ 0.30 (no `market_id` echo from the backend) — mitigated by the unique-project-per-
market design; (b) MiroFish backend responses are trusted (no HMAC) — a compromised backend is
outside Plan 8's threat model, which governs how the *harness* claims contribution.

**Round 5** raised 2 reports — **1 genuine fix + 1 out-of-scope**:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 15 | medium | frozen-threshold fallback flip — if `record_run`'s freeze hit an exception it stored NULL, and `state_from_row` then fell back to live config (re-opening the round-4 flip on new rows) | **FIXED** — the freeze now falls back to env-derived canonical defaults (never NULL on a new row). Test `record_run_always_freezes_thresholds` |
| — | high | backend returns a stale cached project with a FORGED recent `report_generated_at` | **OUT OF SCOPE (backend-trust)** — defeating a backend that *lies* about timestamps needs the backend to echo+verify the unique project name or sign responses; the harness already enforces every timestamp-based defense (`gen ≥ requested_at`, age ≤ MAX_AGE, future-rejection, verifiable-ts) and FORCE_FRESH requests a unique project per run. Same class as round-4 #2. |

All seven other round-5 attacks (incl. stale, pending, launch-only, failed, wrong-market,
required-bypass, no-timestamp) were confirmed **not exploitable**.

**Convergence (genuine, in-scope):** 6 → 4 → 2 → 2 → 1; the round-5 item was a flaw in the
round-4 mitigation itself, now closed.

**Round 6** raised 2 reports (both labelled critical) — **1 genuine fix + 1 non-bug**:

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 16 | critical | double-exception fail-open — if `config()` AND `required_for_bet()` both raised, the inner `except: pass` fell through to `return True,"ok"` | **FIXED** — the except handler now reads the requirement via a BULLETPROOF inline env check and fails closed on ANY uncertainty (no nested call that can re-raise into a fail-open). Test `pt_required_fails_closed_on_double_exception` |
| — | critical | "`_mf_usable=True` returns ok before the required check → bets in required mode" | **NOT A BUG** — `_mf_usable = allow_decision_use`, true only for a fresh+matched+verifiable+complete+consumed result, and in the flow `_mf_usable=True ⟺ mirofish_used=True`; allowing a bet on a *genuine fresh used* result is the intended required-mode behavior. The proposed reorder would BREAK legitimate required+usable betting AND would not stop the "fake complete" result it describes (a forged FRESH_USED still yields `required_no_bet_reason=None`). The "fake-but-complete backend result" is the backend-trust class. |

**Convergence (genuine, in-scope):** 6 → 4 → 2 → 2 → 1 → 1. Findings in rounds 4–6 were all
edges in the *incremental mitigations themselves* (fail-open gate, config-flip, frozen-NULL,
double-exception) — not new attack classes against the core design. The double-exception fix is
now structurally fail-closed (the only `True` return requires env reads to succeed AND be
definitively optional).

**Round 7 (final): 0 exploitable.** All eight attacks — stale, pending, launch-only,
backend-unavailable/failed, wrong-market, FORCE_FRESH no-timestamp, required-bypass, and the
`_mf_usable` gate-ordering concern — were confirmed **not exploitable**. The genuine in-scope
finding count converged cleanly: **6 → 4 → 2 → 2 → 1 → 1 → 0** across seven adversarial rounds.
Every fix after round 1 closed an edge in a prior mitigation, not a new attack class, and the
five mandated categories were closed in every round.

## Acceptance criteria

| Plan-8 requirement | Status |
|---|---|
| Never use STALE MiroFish output | ✅ `stale_result`; never fed; required→no-bet; dashboard re-ages |
| Never use a report from a DIFFERENT market | ✅ `market_mismatch`, enforced independently of the validator flag |
| Never treat incomplete/`sim_prepared`/pending as complete | ✅ `pending` at both layers (validator + state machine + dashboard) |
| Never claim `mirofish_used=true` for a launch-only sim | ✅ `launch_only_not_used`; same-day is structurally not-used |
| Never show MiroFish green when unavailable/stale/failed/pending/not-used | ✅ dashboard derives `used` from the canonical state, re-validated at display time |
| Never silently ignore failure when MiroFish is REQUIRED | ✅ required + any non-used state (incl. MiroFish off) → specific `mirofish_required_*_no_bet` |
| Never let failure become fake confidence | ✅ failed/unavailable → not used, not fed, blocks if required |
| Never let stale data reach sizing/wallet | ✅ gate runs before sizing/wallet; stale never `allow_decision_use` |
| Never hide MiroFish status from journal/obs/report | ✅ canonical `state/used/reason/contribution` on `m[...]`, printed, journaled, on the dashboard |
| `mirofish_used=true` ONLY for fresh+completed+same-market+verifiable+consumed | ✅ exhaustively tested; only `mark_used` sets it, gated on `allow_decision_use` |

**Verdict:** ✅ **PLAN 8 COMPLETE.** All acceptance criteria met; **52/52** dedicated honesty
tests + **62/62** suite modules green; **7 adversarial rounds** drove the genuine in-scope
exploitable count from 6 to **0**, with the five mandated categories closed in every round.
Stale, pending, launch-only, failed, and wrong-market MiroFish outputs are proven unable to
influence a bet or appear as used. Residual limitations are backend-trust only (a compromised
local backend forging timestamps / returning a wrong-market report) — outside Plan 8's harness-
honesty scope, mitigated by FORCE_FRESH's unique-project-per-run and every timestamp-based
defense the harness can apply.

## Files changed

```
NEW  harness/mirofish_status.py
NEW  harness/tests/test_mirofish_honesty.py
EDIT harness/predict_today.py
EDIT harness/sameday.py
EDIT harness/dashboard.py
EDIT harness/config_check.py
EDIT harness/tests/test_mirofish_validate.py
EDIT .env.example
DOC  PLAN8_COMMAND_LOG.md, PLAN8_MIROFISH_FRESHNESS_REPORT.md
```
`agentdb.rvf*` remain untracked (ruflo artifacts — never staged).
