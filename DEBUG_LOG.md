# DEBUG LOG

Every issue found during the audit: command/where, symptom, root cause, file
responsible, fix. Severity: 🔴 critical (crash / corruption / lying to user) ·
🟠 major (wrong behavior) · 🟡 minor (UX / staleness / fragility).

Status: OPEN → FIXED (commit) / WONTFIX (rationale) / NOT-A-BUG (verified).

---

## Phase 1 — reproduction findings (ground truth)

### D1 🟡 `predict_today once --dry-run` runs a real 235s LLM forecast (flag ignored)
- **Where:** `python -m harness.predict_today once --max 1 --dry-run`
- **Symptom:** expected a fast stub forecast; instead ran a full swarm forecast
  (235s on CPU) + challenger ensemble, then decided NO BET (correlated_exposure).
- **Root cause:** `--dry-run` is a `loop`/`LoopConfig` concept; `predict_today`'s
  arg parser silently accepts unknown flags and never sets `cfg.dry_run`, so the
  real swarm runs. Silent flag-swallowing hides user mistakes.
- **File:** `harness/predict_today.py` (arg parsing in the `once`/`daemon` entry).
- **Fix needed:** either honor `--dry-run` (stub path) in `predict_today`, or
  reject unknown flags with a clear message. (to confirm in Phase 4/8 audit)
- **Status:** OPEN

### D2 🟡 Stray duplicate test files in `harness/` root
- **Where:** `harness/test_challenger.py`, `test_classifier.py`, `test_scoreboard.py`,
  `test_sizing.py`, `test_wallet.py` (canonical tests live in `harness/tests/`).
- **Symptom:** two copies of "the wallet test", etc. Risk of editing the wrong one
  / stale assertions / confusion about which is authoritative.
- **Root cause:** legacy test files predating the `harness/tests/` package.
- **Fix needed:** confirm whether `run_tests.py` discovers them; if stale, remove
  or fold into `harness/tests/`. (to confirm in Phase 0/3 audit)
- **Status:** OPEN

### D3 🟡 `predict_today` FIND stage is slow with no progress output
- **Where:** `predict_today once` — ~60-235s with the terminal showing only the
  stage header, no incremental feedback during the live multi-window Gamma scan.
- **Root cause:** swarm forecast is ~235s/market on CPU (known) + the scan fetches
  several windows serially before any output.
- **Fix needed:** progress feedback / bounded scan; not a correctness bug but a
  "looks hung" fragility. (tuning, low priority)
- **Status:** OPEN

### Verified WORKING (not bugs)
- `harness.doctor` ✅ (11 PASS/1 WARN/0 FAIL, never raised a stack trace).
- `run_tests.py` ✅ 41/41 modules; defaults to no-LLM (only `--llm` opts in) — the
  documented `run_tests.py` does not hang on LLM.
- `harness.scoreboard`, `harness.loop status`, `harness.obs.gate`, `harness.metrics`
  ✅ all run read-only and report honestly (gates FAIL, not faked).
- Full P0-P13 pipeline ✅ ran end-to-end against the live system (forecast →
  challenger ensemble → experiment tag → P8 correlated_exposure guard → NO BET),
  confirming the new code works in production, not just in tests.

---

## Phase 2-17 — deep code audit findings

A 14-subsystem adversarial audit (each finding independently reproduced before fix)
surfaced **41 confirmed defects + 2 refuted**. Fixed in priority batches; each fix
ships a regression test. Severity counts: 1 critical, 18 major, 22 minor.

### BATCH 1 — data integrity / "don't lie about P&L" — FIXED

- **A1 🔴 CRITICAL — settlement double-credit (Gate-2 corruption).** `wallet.settle_market`
  / `close_at_price` guarded `status='open'` only on the SELECT; the per-row UPDATE and
  the wallet credit had no status guard and no cross-process lock, so two daemons
  (sameday + predict_today on the same `polyswarm.db`) could credit the same position
  twice — silently inflating `realized_pnl`, the number Gate 2 reads. Reproduced
  (cash 1019→1058 on re-settle). **FIX:** guarded `UPDATE … WHERE id=? AND status='open'`
  + `rowcount==1` check before crediting the wallet (idempotent — a row transitions and
  credits exactly once), plus `busy_timeout=30s` in `_conn()` so a concurrent daemon
  waits instead of erroring. `harness/wallet.py`. Test: `test_settlement_idempotent` 6/6
  (settle-twice, true two-connection race, close-then-settle, close-twice). FIXED.
- **A2 🟡 close_at_price fee + open affordability.** `close_at_price` omitted the fee
  (overstating closed P&L vs `settle_market`); `open_position` checked affordability
  against `stake` but debited `stake+fee` (negative cash possible). Dormant at the default
  `fee_frac=0.0` but now consistent. **FIX:** `realized = sell_value - stake - fee`;
  affordability vs `stake*(1+fee_frac)`. Tests included above. FIXED.
- **A3 🟠 wallet↔ledger drift unreconciled (Gate 2 trusts a possibly-wrong number) +
  missing `db_check`.** **FIX:** new `harness/db_check.py` (`python -m harness.db_check`,
  read-only): PRAGMA integrity_check, table presence, **reconciles wallet cash/realized/
  equity against the positions ledger**, flags negative cash / bad status / settled-vs-
  closed split / unresolved-but-bet forecasts. On the LIVE db it immediately surfaces the
  real drift (wallet realized −42.25 vs ledger −39.43; equity invariant off $40.44) —
  honest + debuggable. Test: `test_db_check` 4/4 (clean reconciles OK, drift WARNs,
  missing table FAILs, run is read-only). FIXED.

Suite: 41 → 43 modules, all green.

### BATCH 2 — forecast parser robustness — FIXED

- **B1 🟠 one malformed agent reply discarded the WHOLE forecast.** `core/agent.py:235`
  `float(data["probability"])` raised KeyError/ValueError/TypeError on missing-key /
  `"60%"` / null / `"about 0.6"` / a non-dict `_parse_json` return, and `core/swarm.py`
  looped agents with NO per-agent guard, so one bad reply (of 5/12 slow CPU forecasts)
  aborted `forecast()` and threw away every other agent's completed estimate. **FIX:**
  new `_coerce_prob` (numbers clamp as before; `"60%"`/`"60 percent"`/prose →
  fraction; null/garbage → default), wrap `_parse_json` so pure prose falls through to
  the coercer, and a non-dict/`None` result raises a CLEAN per-agent `ValueError`.
  `core/swarm.forecast` now wraps each `agent.estimate()` in try/except → **skips just
  that agent** (logs `obs.on_error`), and returns a neutral degraded forecast if EVERY
  agent fails (instead of crashing the 26-method aggregation on an empty set).
  `core/agent.py`, `core/swarm.py`. FIXED.
- **B2 🟡 challenger prose "60 percent" → 0.99.** `challenger.single_llm_forecast`'s
  no-JSON fallback `float(re.search(r'[01]?\.?\d+', raw))` read "60" as a raw prob and
  clamped to max-confidence 0.99. **FIX:** reuse `_coerce_prob` (→ 0.6); an unparseable
  reply returns `None` (reject) instead of a fake 0.99. `harness/challenger.py`. FIXED.

Test: `test_parser_robust` 5/5 (numeric clamp, %/prose, unparseable→default, agent
estimate never raises a dirty error, challenger prose ≠ 0.99). `test_swarm_sizes`
still 5/5. Suite 44 modules.

### BATCH 3 — P&L-metric consistency + classifier accuracy — FIXED

- **C1 🟠 cashed-out ('closed') trades excluded from every P&L analytic but counted
  in Gate 2 → two conflicting realized numbers, hidden losers.** `metrics._settled_rows`,
  `adaptive.theme_pnl`, `clv.edge_decay_report`, `command_center.losing_trades` all
  filtered `status='settled'`, but `close_at_price` writes `'closed'` and the wallet
  realized_pnl (Gate 2) includes those. **FIX:** P&L analytics now use
  `status IN ('settled','closed')`. Live: `paper_metrics` realized went −31.94 →
  **−39.43** (now equals the positions-ledger sum), losing-trades view 10 → 25
  (cashed-out losers visible); the residual gap to the wallet's −42.25 is exactly the
  drift `db_check` flags. Outcome/Brier metrics keep `settled` only (a cash-out has no
  on-chain outcome). FIXED.
- **C2 🟠 approval-rating opinion markets mislabeled 'mechanical' (skipped).** "X's
  approval be above 50%" fired the +3 numeric-threshold (mechanical) but the
  approval-poll signal only matched the exact substring "approval rating". **FIX:** added
  threshold-scoped `approval` + `approve of` opinion signals (weight 4) so approval-rating
  markets classify opinion — while a MECHANICAL "FDA/SEC approval" (no %) is deliberately
  NOT caught. `harness/classifier.py`. FIXED.
- **C3 🟡 bare 'candidate' (weight 1) labeled non-political markets opinion** (e.g.
  "vaccine candidate succeed in Phase 3"). **FIX:** `candidate_kw` now requires political
  context; real election markets already fire `elections_kw`. FIXED.

Tests: +`test_metrics::paper_metrics_includes_cashed_out_closed`; classifier 15/15,
adaptive/clv/command_center green. Suite 44 modules.

### BATCH 4 — daemon / CLI safety + sameday guard symmetry — FIXED

- **D1/#11 🟠 predict_today CLI crashed / swallowed --dry-run.** Hand-rolled parser
  `int(argv[i+1])` IndexError-crashed on a trailing flag, ignored unknown flags, and
  silently swallowed `--dry-run` (ran a real 235s forecast). **FIX:** rewrote
  `predict_today.main` with **argparse** (rejects bad input with SystemExit 2 + usage),
  threaded `--dry-run` into `LoopConfig.dry_run`, and short-circuited the slow MiroFish +
  challenger calls in `predict_one` under dry-run. Added a Phase-5 startup config summary
  (provider/model/swarm/min_edge/mirofish/dry_run/**PAPER**). FIXED.
- **#12 🟠 sameday CLI silent no-op / ignored --interval.** Unknown command was a clean
  exit-0 no-op; `daemon --interval N` ignored N. **FIX:** argparse with choices + wired
  `--interval` into `daemon()`. FIXED.
- **#4 🟠 sameday did NOT enforce the P4B observe-only guard** predict_today enforces —
  a losing label was frozen in one daemon but still bet by the live sameday daemon.
  **FIX:** after the forecast (still logged for scoring), `_observe_only_for(q)` →
  withhold the bet, mirroring predict_today. FIXED.
- **#5 🟠 sameday skips were invisible** (silent wallet-reject; guard skips never reached
  the journal). **FIX:** new `_sd_skip` helper (print + obs `trade.skip` + `journal`)
  used for the observe-only + wallet-reject paths, so the dashboard decisions transcript
  now shows why the live daemon declined. FIXED.

Test: +`test_cli_args` 3/3 (bad-arg rejection, `--dry-run` reaches cfg). Suite 45 modules.

### BATCH 5 — dashboard endpoints + crash-safety — FIXED

- **#14 🟠 requested endpoints missing.** Added `/health` (alias of `/api/health`),
  `/decisions/recent` (journal decisions), `/errors` (recent obs error events with
  market_id/stage/exception), `/debug` (effective config + guard tunables via
  `provenance.config_snapshot` + health + `db_check` summary; secret-free, PAPER-labelled).
  `harness/dashboard.py`. FIXED.
- **#15 🟠 `/api/state` 500'd the whole dashboard under DB write contention** —
  `paper.get_open_positions()/get_closed_positions()` were the only unguarded reads
  (every sibling read already catches `OperationalError`). **FIX:** a `_safe()` wrapper
  → `[]` on a transient lock, so a settling daemon can't blank the dashboard. FIXED.

Test: +`test_dashboard_endpoints` 4/4 (all endpoints 200 on an EMPTY db; /api/state
positions = [] not 500; /debug secret-free + PAPER). Suite 46 modules.

_(remaining batches appended below as each is closed)_
