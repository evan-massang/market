# Build & Verification Report

Paper-only / $0 / keyless throughout. Hardware: 16 GB, CPU-only, local Ollama.

## 1. What was built

An autonomous Polymarket **opinion-market** forecasting + **paper**-trading harness, built on a cloned PolySwarm engine, plus a full observability/audit layer. The goal is evidentiary: prove (or disprove) that a local multi-agent LLM swarm beats the market price — on Brier *and* on paper P&L after costs — across ≥50 resolved opinion markets, **before** any real money.

### Pipeline (per market)
`FIND → GATHER → REPORT(MiroFish) → THINK(swarm + single-LLM challenger) → GUARDS → SIZE → BET → SETTLE → SCORE → GATE`

- **FIND/classify** — opinion vs mechanical; tweet-counts / release-dates / scorelines / price-levels are mechanical and skipped.
- **GATHER** — GDELT news+tone (keyless), Wikipedia facts (keyless), WhoIsSharp microstructure signals.
- **REPORT** — MiroFish multi-agent crowd sim → crowd probability, fed to the LLM as context.
- **THINK** — N-persona PolySwarm + a single-LLM challenger (A/B control), with a `consensus_score`.
- **SIZE/BET** — conviction-scaled fractional Kelly; realistic paper fills (slippage + fee).
- **SETTLE/SCORE/GATE** — dual Brier (model vs market) + the two go/no-go gates.

## 2. Reliability guards & sizing (the bet-quality work)

The bot was placing its biggest bets exactly where the swarm is least trustworthy. Added, before sizing:

- **A** skip `mechanical`; **B** skip `|swarm − challenger| > 0.15` (the main one); **C** skip `consensus < 0.50`; **D** mutually-exclusive event coherence — multiple legs allowed but at most one YES (one winner), unlimited NO, skip incoherent groups (YES-probs sum > 1.20).
- **Conviction-scaled sizing** — a 0–1 conviction score (agreement + consensus + edge + data) scales Kelly (0.25→0.50) and the cap (2%→10%); only a big edge the swarm and challenger agree on becomes a large bet.
- **Model** — settled on `qwen2.5:3b` (1.5× faster than 7B, consistent enough to clear the guards; `gemma2:2b` benchmarked faster but too noisy — its swarm/challenger divergence ran ~0.79, so the guard skipped nearly every bet).
- **Inputs** — fixed the GDELT query builder (was "US permanent" for an Iran/US market → now "Iran US peace"), added a Wikipedia source, broadened-query fallback.

## 3. Observability / audit layer (`harness/obs/`)

Three layers, additive, guarded no-op (a logging failure can never crash a daemon):

1. **Event log** — append-only, hash-chained JSONL (`logs/events/<run_id>.jsonl`) + `.head` last-line trust anchor; 18 event types; correlation IDs on every line; full prompts/payloads content-addressed in `logs/blobs/`; secrets scrubbed.
2. **Human transcript** — deterministic Markdown rendered purely from the event log.
3. **Frozen evidence** — append-only `obs_*` SQLite tables with `AFTER UPDATE/DELETE → RAISE(ABORT)` triggers; forecasts frozen with a `record_hash` *before* the outcome is knowable; resolutions/scores appended separately.

Plus `explain <market_id>` / `replay <forecast_id>` (full trail reconstruction, joins across runs + blobs) and a **read-only** gate evaluator (opens the DB `mode=ro`, writes only to `logs/gate/`).

### Acceptance criteria — 7/7 PASS

| # | Criterion | Result |
|---|---|---|
| C1 | every event has correct correlation IDs | PASS |
| C2 | `explain`/`replay` reconstruct a complete trail | PASS |
| C3 | a forecast cannot be altered by a later resolution | PASS |
| C4 | hash chain detects tamper / insert / delete (+ last-line via `.head`) | PASS |
| C5 | gate evaluator performs no writes to DB or logs | PASS |
| C6 | no secret string appears under `logs/` | PASS |
| C7 | transcript is regenerable byte-identically from the event log | PASS |

No test was weakened; `size_bet` and `_call_llm` were verified byte-identical (obs is observation-only).

## 4. How it was verified

Built and verified with multi-agent workflows in phases (recon+design → foundation → wiring → consumers+tests), each phase adversarially verified by an independent agent. A real size-5 forecast confirmed end-to-end: `forecast.final` frozen with a matching `record_hash`, a 228-event chain verifying clean, and correlation IDs chaining `forecast.final → sizing → trade`. The live daemons now run with observability on; their production logs verify clean.

## 5. Status, gates, and honest caveats

- **P0–P5 built and tested. The two gates are NOT met** — they need ≥50 resolved opinion markets, which accrue over weeks. Realized paper P&L so far is small and unproven; the system exists to *measure*, not assume.
- Profitability is an open empirical question — bigger/faster models and more guards improve the *odds*, not the certainty. No claim is made that it beats the market yet.
- A pre-existing swarm bug (`KeyError 'herding_score'`) crashes forecasts at swarm size 2 (needs ≥3 agents); the daemons use size 5 and are unaffected — and obs correctly logs such crashes as `error` events.
- Real-money trading remains out of scope and unimplemented.
