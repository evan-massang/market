# Plan 10 — Command Log

Every command for Plan 10 (dashboard + supervisor truth). Paper-only.
Branch: `fix/dashboard-supervisor-truth`.

## Phase 0 — prep

```
(Get-Location).Path             -> C:\Users\OMEN\Pictures\Polymarket (session root); repo is .\polyswarm
git rev-parse --abbrev-ref HEAD -> (was fix/scoreboard-accounting-honesty)
git status --short              -> clean except ?? agentdb.rvf / agentdb.rvf.lock (ruflo artifacts)
git log --oneline -6            -> e96fa92 Plan 9 … (Plans 1-9 ALL committed)
git checkout -b fix/dashboard-supervisor-truth   -> Switched to a new branch (from e96fa92)
```
Plans 1-9 committed (e96fa92 = Plan 9). agentdb.rvf* stay untracked. Safe to proceed.

## Phase 1 — inspection (Glob/Grep/Read + 6 parallel read-only Explore agents)

Mapped dashboard routes/HTML badge, health/heartbeat, supervisor/services/procman, launchers,
and existing tests. Surface map in the report. Headline gaps: HTML `freshColor`/`healthTick`
ignore accounting/gate2 (FAKE GREEN); `heartbeat_health` uses MAX(mtime) masking a stale daemon;
missing `/api/services /api/scoreboard /api/mirofish /api/decisions /api/gates /api/version`;
version computed (obs/codeversion) but never surfaced.

## Phases 2-9 — implementation (files written/edited)

```
NEW  harness/status_model.py   canonical states + status() envelope + version_info +
                               classify_service + system_status + read_runtime_json + commit_mismatch
NEW  harness/heartbeat.py      structured atomic write() + honest read() -> canonical status
EDIT harness/predict_today.py  _heartbeat -> structured heartbeat.write (stage/last_error/loop/branch/commit)
EDIT harness/sameday.py        daemon writes a structured heartbeat each cycle
EDIT harness/services.py       Service.hb_json (structured heartbeat path) for the two daemons
EDIT harness/supervisor.py     _collect_status ADDS canonical state/reason/age/stale/paper_only;
                               + system_status(); richer status() print (additive — OK/WARN/FAIL kept)
EDIT harness/health.py         snapshot + generated_at + per-daemon honest reads (daemons[]) + version
EDIT harness/dashboard.py      NEW /api/services /api/scoreboard /api/mirofish /api/decisions /api/gates
                               /api/version /api/truth (canonical envelope); /debug + version; HTML
                               System+Accounting badges, per-daemon badges, Gate-2 status + equity-verified cards
NEW  harness/tests/test_dashboard_supervisor_truth.py   38 tests
```

## Phases 10-11 — tests + regression

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 38/38 passed
.\.venv\Scripts\python.exe -m harness.tests.test_supervisor                  -> 13/13 (additive, unbroken)
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_endpoints         -> 4/4
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```
(network probes mocked in the new tests; temp DB + temp runtime dir; NO service starts.)

## Phase 12 — static verification (Grep)

```
dashboard.py canonical truth refs (audit_accounting/gate2_status/state_from_row/mirofish_used/
  /api/truth/services/gates/version/system_status)  -> 29 occurrences
status_model green set  -> _GREEN = {HEALTHY, SYS_HEALTHY}  (ONLY healthy is green)
unknown/crashed/stale -> var(--green)  -> 0 matches (no fake-green colour mapping)
```

## Phase 12b — adversarial verification (workflow plan10-adversarial-truth)

Round 1 (8 skeptics): accounting-hidden confirmed CLOSED; 7 fake-green vectors found ->
6 fixed + regression-tested, 1 declined as correct design.

```
HIGH  heartbeat pid-check `except: pass` -> fell through to HEALTHY. FIX fail closed -> unknown
      (heartbeat_pid_check_unavailable).
HIGH  /api/mirofish + /api/decisions hardcoded state="ok" -> green w/ used=0 / no data. FIX derive
      state (mirofish: ok only if used>0 else degraded/unknown; decisions empty -> unknown).
HIGH  future heartbeat <=60s tolerated -> HEALTHY. FIX tighten skew to 5s (same-clock).
HIGH  supervisor heartbeat read check_pid=False. FIX check_pid=True (immediate crash detection).
HIGH  heartbeat from a different commit read HEALTHY. FIX commit_mismatch -> degraded
      (heartbeat_commit_mismatch).
DECL  gate2 not dragging System badge: CORRECT — gate2 is readiness not health; surfaced
      separately (/api/truth.gate2_pass + Gates card). Declined.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 42/42 passed (+4)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

Round 2 (re-attack patched code): round-1 fixes held; 5 deeper edges -> 3 fixed + 2 declined:

```
CRIT  heartbeat missing pid field -> skipped check -> HEALTHY. FIX check_pid + no pid -> unknown
      (heartbeat_pid_missing).
HIGH  2-4s future within 5s tolerance -> HEALTHY. FIX tighten to 1s.
CRIT  /api/truth os.path.exists(DB) not usability -> locked/corrupt DB green. FIX _db_usable()
      opens ro + reads sqlite_master; unusable -> error -> unsafe.
DECL  PID-reuse: reused pid can't refresh heartbeat; freshness already catches within max_age.
DECL  git-unavailable hides commit mismatch: PHASE 9 says don't fail on no-git -> unknown.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 45/45 passed (+3)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

Round 3 (re-attack): accounting/gate2 clean again; 5 edges -> 4 fixed + 1 declined:

```
HIGH  JS daemon badge MAX(mtime) fallback green. FIX dim 'unverified', never green.
HIGH  read_runtime_json future_skew 60s. FIX -> 1s (consistent with heartbeat).
HIGH  supervisor mtime fallback "healthy" on hb read-error. FIX -> unknown.
HIGH  version_info cached transient None -> poisons commit-mismatch. FIX cache only success.
DECL  n_posts=0 -> FRESH_USED: Plan 8 semantics, out of scope; usable=1 needs >=500-char report
      (legitimate report-based contribution); dashboard faithfully reflects canonical state.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 48/48 passed (+3)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

Round 4 (re-attack): accounting/mirofish/stale-heartbeat clean again; 4 edges -> 3 fixed + 1 declined:

```
HIGH  sub-second future heartbeat within 1s tolerance. FIX tolerance -> 0 (floored ticks => age>=0).
HIGH  /health hardcoded ok=True ignoring db_ok. FIX ok=db_ok (HTTP 200 kept for liveness gate).
DESIGN gate2 include-but-exempt in /api/truth. FIX remove gate2 from system components; report as
      explicit gate2_pass/gate2_status fields (System=liveness+accounting; gate2 separate/readiness).
DECL  PID-reuse: inherent max_age detection lag; reused pid can't refresh heartbeat; psutil absent.
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 51/51 passed (+3)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

Round 5 (re-attack): stale-heartbeat/accounting/gate2 clean again; 4 edges -> 2 fixed + 2 declined:

```
CRIT  heartbeat last_tick_at OR generated_at -> fresh tick masks stale generated_at. FIX age from
      OLDEST stamp, future from NEWEST (fresh only if ALL stamps fresh).
MED   /api/mirofish green on stale_now used run / dead backend. FIX need fresh_used + live backend.
DECL  stale-PID-identity: PID-identity class (psutil-only); freshness-bounded.
DECL  commit_mismatch(None,known): PHASE 9 don't-fail-on-no-git; transient one-tick (no-cache-None).
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 53/53 passed (+2)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

Round 6 (final re-attack): accounting/mirofish clean again; 4 edges -> 2 fixed + 2 declined:

```
MED   heartbeat omitting generated_at (only last_tick_at). FIX require BOTH timestamps.
HIGH  /api/state scoreboard.compute() unguarded -> DB lock 500s -> stale green cards. FIX _safe + defaults.
DECL  PID-reuse (5th, inherent); commit_mismatch(None) (4th, PHASE 9).
```

```powershell
.\.venv\Scripts\python.exe -m harness.tests.test_dashboard_supervisor_truth  -> 55/55 passed (+2)
.\.venv\Scripts\python.exe run_tests.py --no-llm                             -> 64/64 modules passed
```

FINAL convergence (genuine in-scope fixes): 6 -> 3 -> 4 -> 3 -> 2 -> 2 over 6 rounds. PLAN 10
COMPLETE. Recurring declines (PID-identity/psutil, git-unavailable/PHASE-9, Plan-8 n_posts) are
documented in the report's Remaining Risks. Paused for review; NOT committed.
