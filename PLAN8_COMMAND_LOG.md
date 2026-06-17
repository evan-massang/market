# Plan 8 — Command Log

Every command for Plan 8 (MiroFish freshness + contribution honesty). Paper-only.
Branch: `fix/mirofish-freshness-honesty`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/llm-probability-parser-hardening)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -8            -> d6003da Plan7 … 8ea3920 Plan1 (ALL committed); f124b49 = earlier MiroFish-honesty work
git checkout -b fix/mirofish-freshness-honesty  -> Switched to a new branch
```
Plans 1-7 committed -> safe to proceed. agentdb.rvf* untouched.

## Phase 1 — locate MiroFish paths (Glob/Grep/Read + understanding workflow)

MiroFish modules: harness/mirofish.py (forecast_market, STAGE_SIM_PREPARED), mirofish_validate.py
(EXISTING validator: MiroFishResult/config/validate/mirofish_runs), mirofish_signal.py, mirofish_quick.py
(same-day fire-and-forget), mirofish_resume.py, mirofish_check.py (CLI). Integration: predict_today
(_mirofish_report ~375, _p_mirofish_gate ~433, REPORT step ~905), sameday (_ai_scout launch-only,
mirofish_used=False), dashboard.py (api_mirofish_report ~359), services.py (mirofish_backend),
transcript.py. A parallel understanding workflow (map-mirofish) deep-read these; findings in the report.

## Phases 2-9 — implementation (files written/edited)

```
NEW  harness/mirofish_status.py        canonical CONTRIBUTION state machine (12 states)
EDIT harness/predict_today.py          REPORT step: feed swarm ⟺ allow_decision_use ⟺ mirofish_used;
                                        _p_mirofish_gate: required-mode -> canonical no-bet reason
EDIT harness/sameday.py                _ai_scout health: mirofish_state=launch_only_not_used;
                                        place_sameday: required-mode launch_only gate (sameday_ prefix)
EDIT harness/dashboard.py              /api/mirofish/runs: used/fed_to_swarm derived from canonical state
EDIT harness/config_check.py           6 Plan-8 flags registered as KNOWN
EDIT .env.example                      Plan-8 MiroFish freshness/contribution block (6 flags)
NEW  harness/tests/test_mirofish_honesty.py   34 Plan-8 honesty tests
EDIT harness/tests/test_mirofish_validate.py  required-mode assertion -> canonical reason substring
```

Hardening during build (audit/adversarial-driven):
- `from_result`: a pending/sim_prepared/running/queued/started stage is ALWAYS `pending` and
  NEVER used, **regardless of validator `usable`** (closes the no-timestamp + filler sim_prepared leak).
- `from_result`: a `usable` result with NO `report_generated_at` (fresh "by construction" only)
  is `invalid_result` — freshness UNVERIFIABLE -> not used.
- predict_today: the swarm-context append, `mirofish_used`, and `_mf_usable` are all gated on the
  SAME canonical `allow_decision_use` (no fed-but-not-used / used-but-not-fed split).
- dashboard + `state_from_row`: `used` derives from `state == FRESH_USED` (requires verifiable
  timestamp), not the raw `usable` column -> a usable-no-ts row never shows green/used.

## Phases 10-11 — tests + regression

```powershell
$env:PYTHONUTF8 = "1"   # required: console is cp1252; reports print → and 🐟/🤖

.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 34/34 passed
.\.venv\Scripts\python.exe harness/tests/test_mirofish_validate.py   -> 12/12 passed
.\.venv\Scripts\python.exe harness/tests/test_sameday_parity.py      -> 21/21 passed
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```
(62 modules = 61 prior + new test_mirofish_honesty; no regressions.)

## Phase 12 — static verification (Grep over the whole tree)

```
mirofish_used\s*[:=]\s*True  (whole repo)   -> NO matches (no hardcoded fake contribution)
mirofish_used assignments (harness/*.py):
  mirofish_status.py:260   mark_used        -> the ONLY place set True (canonical machine)
  predict_today.py:931     m["_mirofish_used"] = _mf_st["mirofish_used"]   (derived)
  dashboard.py:214         r["mirofish_used"] = (r["state"] == FRESH_USED) (derived)
  sameday.py:180,227       "mirofish_used": False                          (fire-and-forget)
core/  mirofish references                  -> NONE (no path bypasses harness state machine)
mirofish_status.py liveness/ping/green      -> none; docstring: "a backend being ALIVE is NOT a contribution"
```

## Phase 12b — adversarial verification (workflow plan8-mirofish-adversarial)

Round 1 (8 skeptics vs original code): 2 attacks confirmed closed (launch_only, failed),
**6 exploitable holes found** (2 critical, 2 high, 2 medium). All fixed + regression-tested:

```
CRIT  required bypass: REQUIRED_FOR_BET=true + USE_MIROFISH=false -> gate allowed (off early-out
      before required check). FIX predict_today _p_mirofish_gate: required check FIRST (fail closed).
CRIT  unverifiable ts: verifiability used truthiness of report_generated_at; a garbage string is
      truthy -> fresh_used. FIX from_result/state_from_row key off report_age_seconds is None.
HIGH  wrong-market: REQUIRE_QUESTION_MATCH=false -> validator usable=true low qms slips in. FIX
      from_result enforces qms<threshold -> market_mismatch INDEPENDENTLY of `usable`.
HIGH  sim_prepared persisted: stage_reached not stored -> dashboard shows pending as green. FIX
      validate() forces usable=False for incomplete stages; persist stage_reached (+ALTER);
      state_from_row returns pending for incomplete stages.
MED   retroactive MAX_AGE: state_from_row trusted cached freshness. FIX re-validate age vs current
      MAX_AGE at display time.
```

```powershell
# after the fixes:
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 41/41 passed (+7 adversarial)
.\.venv\Scripts\python.exe harness/tests/test_mirofish_validate.py   -> 12/12 (flipped the unsafe
                                                                        "required+off -> allow" assert)
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 2 (re-attack patched code): round-1 fixes held; **4 deeper holes** (2 crit, 2 high) fixed:

```
CRIT  sim_running stage: _INCOMPLETE_STAGES blacklisted "running" but pipeline emits
      "sim_running". FIX whitelist terminal stages (report_done, probability_extracted);
      anything else (incl. empty/future) = incomplete -> pending. (validate + status + dash)
CRIT  future timestamp: negative age missed by age>MAX_AGE and age is None checks. FIX reject
      age < -120s (future beyond skew) in validate/from_result/state_from_row/is_stale_now.
HIGH  negative MATCH_THRESHOLD: qms<-1 never fires. FIX out-of-range -> safe default 0.30.
HIGH  used-flag flip: round-1 re-aging flipped mirofish_used between decision and display. FIX
      state_from_row IMMUTABLE (frozen recorded age); add separate is_stale_now() + dashboard
      stale_now field ("used in this run; report now stale").
SKEW  added _FUTURE_SKEW_SECONDS=120 so benign clock skew / sub-second truncation isn't a spoof
      (also prevents wrongly rejecting valid reports in production).
```

```powershell
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 46/46 passed (+5 r2)
.\.venv\Scripts\python.exe harness/tests/test_mirofish_validate.py   -> 12/12 passed
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 3 (re-attack again): 2 deeper holes (both high), each a tightening of a round-2 fix:

```
HIGH  tiny threshold: MIROFISH_MATCH_THRESHOLD=0.0001 is "in range" but disables the gate.
      FIX floor any in-range value AT the default 0.30 (gate only gets stricter, never weaker).
HIGH  low-sims dash flip: state_from_row didn't mirror from_result's n_posts<min_sims gate.
      FIX state_from_row rejects low recorded n_posts -> invalid (no fresh_used flip).
```

```powershell
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 48/48 passed (+2 r3)
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 4 (re-attack): 4 reports -> 2 genuine fixes + 2 non-issues:

```
CRIT  fail-OPEN gate: _p_mirofish_gate `except: return True,"ok"` allowed a bet if config
      raised while required. FIX fail CLOSED when required_for_bet() -> mirofish_config_unavailable_no_bet.
HIGH  config flip: state_from_row re-read min_sims/threshold from env. FIX freeze them per-run
      (min_sims_used, match_threshold_used columns); state_from_row uses frozen values.
FALSE no-timestamp->used: already closed (from_result age is None -> INVALID). Proven by test.
OOS   wrong-market >30% overlap: gate enforced + unique-project mitigation; market_id echo / HMAC
      is a backend change outside this plan. Documented as residual.
```

```powershell
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 50/50 passed (+2 r4)
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 5 (re-attack): 2 reports -> 1 genuine fix + 1 out-of-scope:

```
MED   frozen-threshold fallback: record_run stored NULL on freeze exception -> state_from_row
      fell back to live config (re-opened round-4 flip). FIX env-derived defaults, never NULL.
OOS   backend forges report_generated_at on a stale cached project: backend-trust class
      (needs name-echo/HMAC); harness enforces all timestamp defenses + FORCE_FRESH. Documented.
```

```powershell
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 51/51 passed (+1 r5)
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 6 (re-attack): 2 reports -> 1 genuine fix + 1 non-bug:

```
CRIT  double-exception fail-open: inner `except: pass` in the round-4 handler fell through to
      return True if required_for_bet() also raised. FIX bulletproof inline env read, fail
      closed on ANY uncertainty (no nested re-raise path).
NOBUG _mf_usable=True returns ok before required check: _mf_usable==allow_decision_use==genuine
      fresh USED result -> allowing in required mode is CORRECT; reorder would break legit case
      and not stop the "fake complete" (backend-trust) result. Declined.
```

```powershell
.\.venv\Scripts\python.exe harness/tests/test_mirofish_honesty.py    -> 52/52 passed (+1 r6)
.\.venv\Scripts\python.exe run_tests.py --no-llm                     -> 62/62 modules passed
```

Round 7 (final re-attack): **0 exploitable** — all 8 attacks (incl. all five mandated
categories + required-bypass + no-timestamp + _mf_usable ordering) confirmed not exploitable.

Convergence (genuine, in-scope): 6 -> 4 -> 2 -> 2 -> 1 -> 1 -> 0 over rounds 1-7; mandated
categories closed every round. PLAN 8 COMPLETE.

## Phase 13-14 — final state

```
harness/tests/test_mirofish_honesty.py  -> 52/52 passed
run_tests.py --no-llm                    -> 62/62 modules passed
adversarial workflow x7                  -> exploitable 6,4,2,2,1,1,0
```
Paused for user review. NOT committed (assistant never commits). agentdb.rvf* stay untracked.

Files touched by the adversarial fixes:
  harness/mirofish_status.py — _parse_ts/_age_seconds, _COMPLETE_STAGES whitelist +
    _incomplete_stage, _FUTURE_SKEW_SECONDS, _match_threshold fallback, from_result (independent
    qms + age verifiability incl. future), state_from_row IMMUTABLE + is_stale_now.
  harness/mirofish_validate.py — incomplete-stage usable=False (whitelist), future-ts -> stale,
    _valid_threshold fallback, stage_reached column (CREATE + ALTER + INSERT).
  harness/predict_today.py — _p_mirofish_gate required-first ordering.
  harness/dashboard.py — used from immutable state + separate stale_now.
  harness/tests/test_mirofish_honesty.py (34->46 tests), test_mirofish_validate.py (assert flip).
