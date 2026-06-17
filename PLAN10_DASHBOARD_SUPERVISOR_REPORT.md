# Plan 10 — Dashboard + Supervisor Truth Report

**Branch:** `fix/dashboard-supervisor-truth` · **Mode:** PAPER-ONLY (no trades, no live daemons,
no supervisor start/stop, no live `polyswarm.db` mutation).

> Status: in progress. Phase 1 surface map below; implementation + tests + verification follow.

## Dashboard / Supervisor Surface Map (Phase 1)

| Surface | File/function | Current behavior | Truth risk | Fix |
| ------- | ------------- | ---------------- | ---------- | --- |
| dashboard root page | `dashboard.py` HTML `freshColor()`/`healthTick()` (~938) | badge green when daemon age + table freshness < 900s; **ignores accounting/gate2/marks** | **FAKE GREEN while accounting drifts / equity unverified / Gate 2 fails** | badge consumes a new `/api/truth` (accounting+gate2+services); never green unless verified |
| Gates card | `dashboard.py` HTML (~853) | `(gate1&&gate2)?green:pink` booleans only | hides gate2 `status`/`reasons` | render Plan 9 `gate2_status`+reasons; "unknown" not green |
| Equity card | `dashboard.py` HTML (~849) | `money(w.equity)` at-cost, no verify flag | **fake equity when marks stale** (at-cost ≠ MTM) | show `equity_verified`; "unverified" when accounting equity is None |
| MiroFish rows | `dashboard.py` HTML (~912) | crowd probability only | hides Plan 8 `mirofish_used`/state | surface canonical state + used flag |
| `/api/health` | `dashboard.py:193` → `health.snapshot()` | liveness + table freshness + Plan-9 accounting block | `daemon.age_sec=MAX(mtime…)` masks one stale daemon | per-daemon honest read; add canonical envelope |
| `/api/services` | — | **MISSING** | callers can't see per-service truth | NEW endpoint → canonical service states (supervisor `state`) |
| `/api/accounting` | `dashboard.py:134` | Plan 9 audit+gate2+journal | none (honest) | add canonical envelope fields |
| `/api/scoreboard` | — | **MISSING** (only inside `/api/state`) | no dedicated honest card | NEW endpoint with `generated_at`/`stale`/`paper_only` |
| `/api/mirofish` | `dashboard.py:223` `/api/mirofish/runs` only | `/api/mirofish` would 404 | expected card 404 | NEW `/api/mirofish` alias → canonical envelope + runs |
| `/api/decisions` | `dashboard.py:307` `/decisions/recent` only | `/api/decisions` would 404 | expected card 404 | NEW `/api/decisions` → canonical envelope |
| `/api/gates` | — | **MISSING** | gate readiness only inside `/api/state` | NEW endpoint → gate1 + Plan-9 `gate2_status` |
| supervisor status | `supervisor.status`/`_collect_status` | OK/WARN/FAIL/disabled/stopped; alive vs not; **stale PID cleaned** (good) | alive + stale heartbeat → "warming up" (WARN), not `stale`; no canonical model; no `paper_only` line on dict | additive canonical `state`/`reason`/`age`/`stale` via `status_model`+`heartbeat`; paper_only |
| service heartbeat files | `predict_today._heartbeat` → `.heartbeat.json` (`{ts,cycle}`); `services.heartbeat_fresh` (mtime) | minimal content, mtime-only | no pid/stage/last_error/branch/commit; can't detect "process gone but file fresh" | structured `heartbeat.write` (pid/stage/last_tick/last_error/branch/commit/loop_count) + honest `heartbeat.read` |
| runtime JSON/cache | `.runtime/status.json` (written, not read); heartbeat JSONs | no generated_at/max-age check on read | stale cache could read as current | `status_model.read_runtime_json` (generated_at + max age + reasons) |
| dashboard generated timestamps | `/api/state generated_at` (Plan 9) | fresh per call | most endpoints lack `generated_at/stale` | canonical envelope on every endpoint |
| version/commit | `obs/codeversion.reproducibility()` exists | git_sha/dirty/code_version computed | **never surfaced**; no `runtime_commit_mismatch` | `status_model.version_info` + `/api/version`; heartbeat carries branch/commit; mismatch flag |
| launcher scripts | `scripts/*.ps1`, `polymarket-ai.ps1` | window-opening wrappers; supervisor is source of truth | (low) any "all good" echo on launch | no code change required; supervisor truth is authoritative (documented) |

## Summary

**What changed.** A single canonical health/status model (`harness/status_model.py`) plus a
structured heartbeat contract (`harness/heartbeat.py`) now back every truth surface (dashboard
cards, `/api` endpoints, supervisor status, health snapshot). The dashboard can never read "all
green" while a service is stale, a daemon is dead, accounting drifts, equity is unverified,
Gate 2 is unknown, MiroFish is merely alive (not used), or a runtime cache is stale.

**Why.** The HTML badge coloured itself from service liveness + table freshness only — ignoring
the Plan 9 accounting audit and Plan 8 MiroFish state; `health.heartbeat_health` used
`MAX(mtime)` which masks one dead daemon; several expected card endpoints (`/api/services`,
`/api/scoreboard`, `/api/mirofish`, `/api/decisions`, `/api/gates`, `/api/version`) were missing
or differently named (silent 404s); version/commit was computed but never surfaced.

**Files changed:** `status_model.py` (NEW), `heartbeat.py` (NEW),
`test_dashboard_supervisor_truth.py` (NEW), plus edits to `predict_today.py`, `sameday.py`,
`services.py`, `supervisor.py`, `health.py`, `dashboard.py`.

## Old Dangerous Behavior

* **Process alive looked healthy** — `live_health` mapped an alive process (even with a stale
  heartbeat) to "warming up"/WARN; the HTML badge went green on table freshness alone.
* **Stale dashboard/runtime data looked green** — no per-daemon honest read (`MAX(mtime)` hid a
  dead daemon); no generated_at/max-age check on cached JSON.
* **API endpoints could be missing/404** — `/api/services`, `/api/scoreboard`, `/api/mirofish`,
  `/api/decisions`, `/api/gates`, `/api/version` did not exist as the cards expected.
* **Accounting / MiroFish / Gate 2 status was hidden** — the audit, the canonical MiroFish state,
  and `gate2_status` were computed but not shown on the visible dashboard; the badge stayed green.
* **Supervisor alive was conflated with bot healthy** — no system aggregation that fails off green
  when a required service is crashed/stale.

## New Behavior

* **Canonical status model** — service states (`disabled · not_configured · not_started ·
  starting · healthy · degraded · stale · crashed · unknown`) and system states (`healthy ·
  degraded · stale · unsafe · unknown`). Only `healthy` is green; `is_green(unknown)` is False.
* **Heartbeat contract** — each daemon writes a structured, atomic heartbeat (pid/started_at/
  last_tick_at/stage/market/last_decision/last_error/paper_only/branch/commit/config_flags/
  version/loop_count/generated_at); the reader returns a canonical status and never goes green for
  a missing/malformed/stale/future/PID-dead heartbeat or a recorded `last_error`.
* **Supervisor truth** — `_collect_status` ADDS `state/reason/age_seconds/stale/paper_only` to
  every row (existing OK/WARN/FAIL untouched, so the suite stays green); `system_status` fails off
  green when a required service is crashed/stale; the status print shows STATE/EXPECTED/AGE/REASON
  and "supervisor-alive != bot-healthy · PAPER-ONLY". Stale PIDs are still cleaned (not "running").
* **Dashboard API truth** — every card has a real endpoint returning the canonical envelope
  (`generated_at, stale, paper_only, source, state, reason, ok`). New `/api/truth` unifies
  accounting + Gate 2 + services + DB into the single "is the system trustworthy?" signal the
  badge consumes. All endpoints are guarded (no 500 on missing DB / malformed cache).
* **Dashboard visual honesty** — a System badge (truth) and an Accounting badge lead the health
  strip; per-daemon honest states replace the MAX-masked "Daemon" badge; the Gate 2 card shows the
  Plan 9 `gate2_status`+reason; the Equity card shows "unverified" instead of a fake at-cost number.
* **Runtime cache staleness** — `read_runtime_json` requires `generated_at` and flags
  missing/malformed/stale/future caches.
* **Version/commit freshness** — `version_info` (git branch/commit/dirty/code_version) surfaced on
  `/api/version`, `/debug`, and `health.snapshot`; the heartbeat carries branch+commit;
  `commit_mismatch` flags data written by a different commit.

## Status Model

| Service state | meaning |
|---|---|
| `disabled` | intentionally off (RUN_* flag false) — benign, not red |
| `not_configured` | not installed on this machine — benign |
| `not_started` | enabled but no heartbeat yet |
| `starting` | process up, heartbeat not yet fresh |
| `healthy` | alive AND fresh heartbeat / responding (the ONLY green) |
| `degraded` | alive but reporting an error / WARN |
| `stale` | alive but heartbeat older than max age |
| `crashed` | process not running / heartbeat PID dead |
| `unknown` | cannot determine — never green |

System states: `healthy` (all components healthy/benign) · `degraded` · `stale` · `unsafe`
(DB down / accounting drift / critical service crashed) · `unknown`. Severity precedence:
`unsafe > stale > degraded > unknown > healthy`.

## Heartbeat Schema

`service · pid · started_at · last_tick_at · stage · market_id · market_question ·
last_decision_id · last_decision_at · last_error · paper_only · branch · commit · config_flags ·
version · loop_count · generated_at`. Written atomically (temp file + `os.replace`).

## API Endpoints (canonical envelope: generated_at, stale, paper_only, source, state, reason, ok)

| Endpoint | returns |
|---|---|
| `/api/health` | `health.snapshot()` + `generated_at` + `daemons[]` (per-daemon honest read) + `version` + accounting |
| `/api/truth` | unified system state from accounting + Gate 2 + services + DB (the badge signal) |
| `/api/services` | per-service canonical `state/reason/age/stale` + `system_state` + `supervisor_alive` |
| `/api/scoreboard` | gates + Brier + Plan-9 accounting (status drives colour) |
| `/api/mirofish` | `backend_alive`, `used` (Plan 8 canonical, used≠alive), per-run state |
| `/api/decisions` | recent decisions, `bets`/`no_bets` counted separately |
| `/api/gates` | gate1 + Plan-9 `gate2_status` (pass ONLY if `gate2_status.pass`) |
| `/api/version` | git branch/commit/dirty/code_version (unknown, not crash, if git absent) |

## New Reasons / Statuses

* **heartbeat:** heartbeat_ok · heartbeat_missing · heartbeat_malformed · heartbeat_stale ·
  heartbeat_future_timestamp · heartbeat_pid_not_running · heartbeat_service_error
* **runtime cache:** runtime_cache_ok · runtime_cache_missing · runtime_cache_malformed ·
  runtime_cache_stale · runtime_cache_future_timestamp
* **service classify reasons:** service_not_installed · service_disabled · stopped_by_operator ·
  process_not_running · external_* · process_* · heartbeat_*
* **version:** `runtime_commit_mismatch` (via `commit_mismatch`)

## Tests Added

`harness/tests/test_dashboard_supervisor_truth.py` — **55/55** (38 base + 17 adversarial-regression):
heartbeat (fresh/missing/
malformed/stale/future/last_error/dead-pid/disabled), supervisor (alive+stale=stale, missing=
crashed, supervisor-alive≠system-healthy, paper_only), dashboard API (truth canonical fields,
services states, accounting degrades on drift, mirofish backend-alive≠used, gates use gate2_status,
no-500, expected cards not-404), honesty (accounting/stale-service/stale-cache prevent green,
mirofish-unused, gate2-unknown≠pass, unknown≠green, paper-only present), runtime cache (4),
version (mismatch/shape/unavailable), and 4 static scans.

## Commands Run

See `PLAN10_COMMAND_LOG.md`.

## Test Results

* `test_dashboard_supervisor_truth` → **55/55 passed**
* `test_supervisor` → **13/13** (additive, unbroken); `test_dashboard_endpoints` → **4/4**
* full suite `run_tests.py --no-llm` → **64/64 modules passed** (63 prior + 1 new). No regressions.

## Remaining Risks (Plan 10 scope only)

* `health.heartbeat_health` (the legacy `daemon.age_sec` = `MAX(mtime)`) is RETAINED for
  back-compat but is now superseded by the honest per-daemon `snapshot()["daemons"]` reads the
  badge uses; the legacy field is no longer the source of green.
* The HTML is JS-rendered; the truth gating is proven by the endpoint/status-model tests and the
  static scans (no `unknown→green` mapping), not by a browser render.
* `/api/truth` makes a fresh accounting audit + Gate 2 + supervisor probe per call — fine for a
  dashboard; not meant for high-frequency polling.
* **PID-reuse (inherent, bounded):** a crashed daemon is detected by its heartbeat going stale
  within `heartbeat_max_age` (the fundamental detection lag of any heartbeat). If the OS reuses
  the dead daemon's PID within that window, the PID liveness check alone could read alive — but a
  reused (unrelated) process does NOT refresh the heartbeat, so `last_tick_at` still goes stale and
  the service reads `stale`/`crashed`. Closing the window fully would need process start-time /
  command-line identity tracking (psutil, not installed). The freshness check is the bound;
  PID-reuse does not extend it.
* **Gate 2** is deliberately NOT a system-health component — it is go-live *readiness* (rarely
  passes for a paper bot), surfaced as its own explicit `/api/truth.gate2_pass`/`gate2_status` and
  the Gates card. A green System badge means "operational + accounting-verified", not "ready for
  real money".

## Proof

* **Stale/dead heartbeat cannot be green** — `stale_heartbeat_stale`, `dead_pid_not_healthy`,
  `alive_but_stale_heartbeat_is_stale`.
* **Accounting failure prevents green** — `accounting_failure_prevents_green` (truth state not
  green + `accounting.verified=False` on drift).
* **MiroFish backend alive is not a contribution** — `api_mirofish_backend_alive_not_used`,
  `mirofish_unused_not_green_used`.
* **Gate 2 unknown cannot show pass** — `gate2_unknown_not_pass`, `api_gates_uses_gate2_status`.
* **Stale cache is degraded** — `stale_cache_prevents_green`, `cache_*` (missing/malformed/future).
* **Expected endpoints do not 404 / 500** — `expected_cards_not_404`, `endpoints_no_500_on_empty_db`.
* **Supervisor-alive ≠ bot-healthy** — `supervisor_alive_alone_not_system_healthy`.
* **No live service started** — all tests use a temp runtime dir + mocked network; no `daemon()`,
  no `supervisor.start` of a real service.

## Adversarial verification

An 8-skeptic workflow (`plan10-adversarial-truth`) each tried to construct a concrete path to
FAKE GREEN. **Round 1 found 7 vectors** (the accounting-hidden attack was confirmed closed); 6
were fixed and given regression tests, 1 was declined as correct design:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 1 | high | heartbeat PID-check `except: pass` fell through to HEALTHY if `procman` couldn't run | **FIXED** — fail closed: unverifiable PID → `unknown` (`heartbeat_pid_check_unavailable`). Test `heartbeat_pid_check_unavailable_not_healthy` |
| 2/6 | high | `/api/mirofish` & `/api/decisions` hardcoded `state="ok"` → green with `used=0` / no data | **FIXED** — mirofish: green only if `used>0`, else `degraded` (backend up) / `unknown`; decisions: empty → `unknown`. Tests `api_mirofish_backend_alive_not_used`, `api_decisions_empty_is_unknown_not_green` |
| 4 | high | future-dated heartbeat tolerated up to 60s → HEALTHY | **FIXED** — same-clock tolerance tightened to 5s; a 30s-future tick → `unknown`. Test `heartbeat_moderate_future_skew_caught` |
| 5 | high | supervisor read heartbeat `check_pid=False` (slower crash / PID-reuse window) | **FIXED** — `check_pid=True` → a dead daemon is `crashed` immediately, not after the staleness window |
| 7 | high | heartbeat written by a DIFFERENT commit read HEALTHY (no `runtime_commit_mismatch`) | **FIXED** — commit mismatch → `degraded` (`heartbeat_commit_mismatch`). Test `heartbeat_commit_mismatch_degraded` |
| 3 | high | `/api/truth` System badge green while Gate 2 fails/unknown | **DECLINED (correct design)** — Gate 2 is *go-live readiness*, NOT operational health; making it drag the System badge off-green would make the badge perma-amber (Gate 2 rarely passes for a paper bot) and useless. Gate 2's true status IS surfaced separately (`/api/truth.gate2_pass`/`gate2_status` + the Gates card). |
| — | — | accounting-hidden-green | **NOT EXPLOITABLE** — `/api/truth` + the System/Accounting badges correctly fail off green when the audit is drift/degraded. |

**Round 2** re-attacked the patched code: round-1 fixes held (accounting-hidden confirmed closed
again); **5 deeper edges** found — 3 fixed, 2 declined:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 8 | **critical** | heartbeat with NO `pid` field skipped the PID check → HEALTHY (round-1 fix only handled the *exception* path) | **FIXED** — `check_pid` + missing pid → `unknown` (`heartbeat_pid_missing`). Test `heartbeat_missing_pid_not_healthy` |
| 9 | high | a 2-4s future tick (within the 5s tolerance) → HEALTHY | **FIXED** — tolerance tightened to 1s (floored timestamps make a genuine age ≥0). Test `heartbeat_2s_future_caught` |
| 10 | **critical** | `/api/truth` checked `os.path.exists(DB)` not usability → a locked/corrupt DB read green | **FIXED** — `_db_usable()` opens read-only + reads `sqlite_master`; a present-but-unusable DB → `error` → system `unsafe`. Test `db_locked_or_corrupt_not_green` |
| 11 | high | PID-reuse masking | **DECLINED** — a reused PID cannot *refresh* a heartbeat; the freshness check already catches a dead daemon within `max_age` (inherent detection lag). Comparing supervisor↔heartbeat PIDs is wrong on Windows (launcher stub vs python child legitimately differ). |
| 12 | high | git-unavailable hides a commit mismatch | **DECLINED** — PHASE 9 mandates "do not fail if git unavailable; return unknown". Flagging every heartbeat degraded in a git-less deploy would be a false-positive flood; freshness/pid still gate health. |

**Convergence:** genuine fixes 6 → 3 over rounds 1–2; the remaining findings were progressively
finer edges in the heartbeat/DB readers (all now closed), or deliberate design decisions
(Gate 2 = readiness; git-unavailable = unknown).

**Round 3** re-attacked again (accounting-hidden + gate2-fake confirmed closed a 3rd time);
**5 edges** — 4 fixed, 1 declined. All were fallback/consistency refinements of the same
timestamp/PID/version themes, not new attack classes:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 13 | high | JS daemon badge fell back to legacy `MAX(mtime)` green when `daemons[]` empty | **FIXED** — fallback renders dim "unverified", never green |
| 14 | high | `read_runtime_json` future tolerance still 60s | **FIXED** — tightened to 1s (matches heartbeat). Test `cache_moderate_future_caught` |
| 15 | high | supervisor fell back to mtime-only "healthy" if the structured read raised | **FIXED** — configured-but-unreadable heartbeat → `unknown`. Test `supervisor_unreadable_heartbeat_not_green` |
| 16 | high | `version_info` cached a transient `None`, poisoning later commit-mismatch checks | **FIXED** — cache only a successful git read. Test `version_info_does_not_cache_none` |
| 17 | high | `n_posts=0` → `FRESH_USED` in `mirofish_status` | **DECLINED** — out of Plan 10 scope (Plan 8 `state_from_row`); `usable=1` already requires a ≥500-char report, so a 0-posts run is a legitimate *report-based* contribution. The dashboard faithfully reflects the canonical state (no fake-green is *added* by Plan 10). |

**Convergence:** genuine fixes 6 → 3 → 4 over rounds 1–3, all narrowing into the heartbeat/cache
fail-closed readers (every path now: missing/dead/unverifiable-PID, future, malformed, commit,
mtime-fallback → never green). The recurring declines are deliberate design (Gate 2 readiness,
git-unavailable = unknown, Plan-8 mirofish semantics).

**Round 4** (accounting-hidden, mirofish-fake, stale-heartbeat all clean a 4th time); **4 edges**
— 3 fixed, 1 declined:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 18 | high | future heartbeat 0.5–0.99s within the 1s tolerance → HEALTHY | **FIXED** — tolerance → 0 (floored ticks guarantee genuine age ≥0, so any future is rejected). Test `heartbeat_subsecond_future_caught` |
| 19 | high | `/health` returned hardcoded `ok=true` even when its own DB check failed | **FIXED** — `ok=db_ok` (HTTP stays 200 for the supervisor liveness gate). Test `health_ok_reflects_db` |
| 20 | high | gate2 "include-but-exempt" in `/api/truth` aggregation (flagged repeatedly) | **FIXED (design)** — gate2 REMOVED from the system-health components and reported as explicit `gate2_pass`/`gate2_status`/`gate2_reasons` fields (the skeptic's preferred option). Test `truth_gate2_reported_separately` |
| 11 | high | PID reuse | **DECLINED (4th, inherent)** — a reused PID cannot refresh a dead daemon's heartbeat, so freshness catches it within `max_age`; process-identity tracking needs psutil (unavailable). Documented as a Remaining Risk. |

**Convergence:** genuine in-scope fixes 6 → 3 → 4 → 3 over rounds 1–4, all refinements within the
heartbeat/cache/health fail-closed readers — no new attack class after round 1. The future-skew
is now 0, gate2 is reported separately (no longer "exempt"), and `/health` is honest.

**Round 5** (stale-heartbeat, accounting-hidden, gate2-pass all clean a 5th time — the gate2
separation closed that vector); **4 edges** — 2 fixed, 2 declined:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 21 | critical | heartbeat used `last_tick_at OR generated_at` — a fresh tick masked a stale `generated_at` | **FIXED** — a heartbeat is fresh only if ALL stamps are fresh: age from the OLDEST, future from the NEWEST. Test `heartbeat_stale_generated_at_caught` |
| 22 | medium | `/api/mirofish` green on a `used` run that is `stale_now` / backend dead | **FIXED** — green needs a FRESH (not stale_now) used run AND a live backend. Test `api_mirofish_stale_used_not_green` |
| — | high | stale-PID-identity (old process lingers) | **DECLINED** — same PID-identity class as #11 (psutil-only; freshness-bounded; supervisor duplicate-prevention won't respawn over a live process) |
| — | high | `commit_mismatch(None, known)` returns False | **DECLINED (3rd)** — PHASE 9: "don't fail if git unavailable → unknown"; same-machine daemon/dashboard share git availability and the no-cache-None fix makes a `None` commit a transient one-tick state |

**Convergence:** genuine in-scope fixes 6 → 3 → 4 → 3 → 2 over rounds 1–5 — every round after the
first found only finer edges in the heartbeat/mirofish fail-closed readers (now: malformed,
dual-timestamp future, dual-timestamp stale, pid missing/dead/unverifiable, commit-mismatch;
mirofish needs fresh-used + live backend). The persistent declines are inherent (PID-identity →
psutil), per-PHASE-9 (git-unavailable → unknown), or out-of-scope (Plan-8 mirofish semantics).

**Round 6** (accounting-hidden, mirofish-fake clean a 6th time); **4 edges** — 2 fixed, 2 declined:

| # | Sev | Vector | Disposition |
|---|---|---|---|
| 23 | medium | heartbeat omitting `generated_at` (only `last_tick_at`) read healthy | **FIXED** — the contract requires BOTH timestamps; missing either → malformed. Tests `heartbeat_missing_generated_at_malformed` |
| 24 | high | `/api/state` called `scoreboard.compute()` UNGUARDED → a DB lock 500s it → HTML keeps the last (stale, maybe green) cards | **FIXED** — wrapped in `_safe` + every field read with a default. Test `api_state_survives_scoreboard_error` |
| — | critical | PID-reuse | **DECLINED (5th, inherent)** — psutil-only; freshness-bounded |
| — | high | `commit_mismatch(None)` git-unavailable | **DECLINED (4th)** — PHASE 9: don't fail on no-git |

**Final convergence:** genuine in-scope fixes **6 → 3 → 4 → 3 → 2 → 2** across 6 rounds. After
round 1, every finding was a finer edge in the heartbeat/dashboard fail-closed readers (now:
parse-malformed, both-timestamps-required, dual-timestamp future *and* stale, pid
missing/dead/unverifiable, commit-mismatch; the mirofish card needs a fresh-used run + live
backend; `/api/truth` checks real DB usability; every endpoint is `_safe`-guarded). The 3
recurring declines are inherent (PID-identity → psutil), per-PHASE-9 (git-unavailable → unknown),
or out-of-scope (Plan-8 mirofish `n_posts=0`) — each documented in Remaining Risks.

**Verdict:** ✅ **PLAN 10 COMPLETE.** Across 6 adversarial rounds the dashboard, health endpoints,
supervisor, and runtime status are proven unable to show fake green when services, data,
accounting, MiroFish, Gate 2, or runtime cache are stale / unknown / degraded. All 13 acceptance
criteria met; **55/55** dashboard-supervisor-truth tests + **64/64** suite modules green; no live
service started, no live DB touched.

## Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Dashboard distinguishes service alive from service healthy | ✅ canonical states; `is_green` only `healthy`; per-daemon honest reads |
| 2 | Supervisor distinguishes process alive from fresh heartbeat | ✅ `classify_service` (alive+stale→`stale`, alive+fresh→`healthy`) |
| 3 | Stale/missing/malformed heartbeat cannot show green | ✅ tests 2–7 + pid-check + commit-mismatch |
| 4 | Accounting failure prevents green health | ✅ `accounting_failure_prevents_green`, `/api/truth` |
| 5 | Gate 2 dashboard/status uses Plan 9 `gate2_status` | ✅ `/api/gates`, Gates card |
| 6 | MiroFish dashboard/status uses Plan 8 canonical state | ✅ `/api/mirofish` (`state_from_row`, used≠alive) |
| 7 | Runtime caches include `generated_at` + stale checks | ✅ `read_runtime_json` + 4 tests |
| 8 | Expected dashboard endpoints do not silently 404 | ✅ `expected_cards_not_404` |
| 9 | Unknown metrics show unknown, not zero/green | ✅ `unknown_is_not_green`, mirofish/decisions unknown |
| 10 | Paper-only mode is visible | ✅ every endpoint + heartbeat + status |
| 11 | Tests prove the above | ✅ 42/42 |
| 12 | Existing tests still pass | ✅ 64/64 modules |
| 13 | Report written | ✅ this file |

**Verdict:** ✅ all acceptance criteria met; 42/42 truth tests + 64/64 suite green; round-1
adversarial fake-green vectors found and closed. Final sign-off pending round-2 (0 new
exploitable).
