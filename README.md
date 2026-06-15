# Polymarket Forecasting & Paper-Trading Harness

An autonomous research harness that tries to answer one question: **can a local multi-agent LLM swarm forecast Polymarket *opinion* markets better than the market price itself — well enough to be profitable after costs — before risking any real money?**

Everything here is **paper-only, $0, keyless, read-only**. No real-money or execution code path exists. The bot reads public market data, forecasts, places *simulated* bets against a paper wallet, settles them on real resolutions, and scores itself. Real-money trading stays out of scope until two gates pass (they have not).

> Hardware reality: this runs on a **16 GB, CPU-only** box (no NVIDIA GPU). Every design choice below — small local models, sequential forecasts, same-day focus — follows from that.

---

## What it does, end to end

Each market goes through a 7-stage pipeline (`harness/predict_today.py`, the AI pipeline):

```
FIND  ──▶ GATHER ──▶ REPORT ──▶ THINK ──▶ GUARDS ──▶ SIZE ──▶ BET ──▶ (later) SETTLE ──▶ SCORE ──▶ GATE
 │          │          │          │          │         │        │              │           │         │
 │          │          │          │          │         │        │              │           │         └ scoreboard.py / obs.gate (read-only)
 │          │          │          │          │         │        │              │           └ Brier: model vs market (calibration.py)
 │          │          │          │          │         │        │              └ resolve on Gamma, book P&L (loop.settle_resolved)
 │          │          │          │          │         │        └ paper fill + slippage + fee (wallet.open_position)
 │          │          │          │          │         └ conviction-scaled fractional Kelly (sizing.size_bet)
 │          │          │          │          └ reliability guards A–D (skip untrustworthy bets)
 │          │          │          └ 5-persona PolySwarm + single-LLM challenger (core/swarm.py)
 │          │          └ MiroFish multi-agent crowd sim builds a report (fed to the LLM, not run as one)
 │          └ GDELT news/tone + Wikipedia facts + WhoIsSharp microstructure signals
 └ same-day OPINION markets the swarm can actually predict (classifier.py)
```

1. **FIND** — pull markets resolving within ~24 h, keep the ones the swarm can forecast. The classifier (`harness/classifier.py`) tags each market `opinion | mechanical | unknown`; sports scores, price levels, weather, tweet-counts and release-dates are **mechanical** and skipped. Genuine news/geopolitical markets ("Iran–US peace deal", "Israel closes airspace") are kept.
2. **GATHER** — build real context: **GDELT** DOC 2.0 news + average-tone + attention-trend (`harness/gdelt.py`, keyless, rate-limited), **Wikipedia** factual grounding for the entities (`harness/wiki.py`, keyless), and **WhoIsSharp**-style microstructure signals (`harness/signals.py`).
3. **REPORT** — run a **MiroFish** multi-agent crowd simulation (a separate Flask service, see below) on the question; distill the crowd's implied probability + sample posts. This report is **fed into the LLM as context** — it is not the forecast itself.
4. **THINK** — the **PolySwarm** swarm (`core/swarm.py`): N personas debate over the gathered context and aggregate to a probability + a `consensus_score`. In parallel, a **single-LLM challenger** produces its own probability (the A/B control).
5. **GUARDS** — refuse bets where the swarm is least trustworthy (see *Reliability guards* below).
6. **SIZE** — **conviction-scaled fractional Kelly** (`harness/sizing.py`): bet bigger only when every reliability signal agrees; always quarter-Kelly floor, capped fraction of bankroll.
7. **BET** — open a paper position (`harness/wallet.py`) with a realistic fill (price + slippage + fee). Mutually-exclusive events get **at most one YES** (one winner) and unlimited NO (fade the losers).
8. **SETTLE / SCORE / GATE** — on resolution, book P&L and compute a **dual Brier** (model vs market). The **gate** (`harness/scoreboard.py`) decides go/no-go: **GATE 1** = model Brier < market Brier on ≥50 resolved opinion markets; **GATE 2** = paper bankroll grew after costs. Both must pass.

---

## Reliability guards (why it skips bets)

Applied in `predict_one()` / `place_sameday()` **before** sizing — observation: a fast/cheap model is confident *and wrong* often, so these refuse the bets it has no business making:

| Guard | Rule | Tunable |
|---|---|---|
| **A — mechanical** | never bet markets classified `mechanical` | `SKIP_MECHANICAL` |
| **B — divergence** | skip if `|swarm_p − challenger_p|` > 0.15 (the swarm number is unreliable) | `MAX_SWARM_CHALLENGER_DIVERGENCE` |
| **C — consensus** | skip if the swarm's internal agreement < 0.50 | `MIN_SWARM_CONSENSUS` |
| **D — event coherence** | bet **multiple legs** of one mutually-exclusive event, but at most **one YES** (one winner); unlimited NO; skip a group whose YES-probs sum > 1.20 | `ONE_YES_PER_EVENT`, `MAX_GROUP_PROB_SUM` |

**Conviction sizing** (`CONVICTION_*` constants): a 0–1 conviction score (swarm/challenger agreement + consensus + edge size + data gathered) scales both the Kelly fraction (0.25→0.50) and the per-bet cap (2%→10%). Kelly still sizes by edge, so only a *big edge the swarm and challenger agree on* becomes a large bet.

---

## Observability / audit trail (`harness/obs/`)

Because the gate verdict is computed from these records, the records **are the evidence**. Three layers, all additive (a logging failure can never crash a daemon):

1. **Event log** — append-only, **hash-chained** JSONL at `polyswarm/logs/events/<run_id>.jsonl` (+ a `.head` sidecar so even a last-line edit is detected). 18 event types, every line carries correlation IDs (`run_id`, `market_id`, `forecast_id`, `agent_id`, `llm_call_id`). Full LLM prompts/completions and raw API payloads are content-addressed in `logs/blobs/`; secrets are scrubbed everywhere.
2. **Human transcript** — `obs.transcript.build(run_id)` renders a deterministic Markdown narrative **from the event log only**.
3. **Frozen evidence** — append-only `obs_*` tables in `polyswarm.db` with `AFTER UPDATE/DELETE → RAISE(ABORT)` triggers. A forecast is frozen with a `record_hash` **before its outcome is knowable**; resolutions/scores are appended separately and can never alter the frozen row.

Tools:
```bash
python -m harness.obs.explain explain <market_id>     # full decision trail for a market
python -m harness.obs.explain replay  <forecast_id>   # reconstruct one forecast end to end
python -m harness.obs.gate                            # read-only gate evaluator (opens DB mode=ro)
python -m harness.obs.tests.test_hash_chain           # any of the 7 acceptance tests
```

All **7 acceptance criteria pass**: correlation IDs, explain/replay, frozen-forecast immutability, tamper-evident hash chain, read-only gate, zero secrets on disk, deterministic transcript.

---

## How to run it

Ollama must be running. **Run the preflight health check first** — it is read-only (~12 probes), never writes, and never trades:

```powershell
# from polyswarm\  (PowerShell)
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m harness.doctor          # add --json for machine-readable output
```

Expected output on a healthy box (a `WARN` is tolerated — it never fails the run; only a `FAIL` makes doctor exit non-zero):

```text
harness.doctor — read-only health check
------------------------------------------------------------
[PASS] python     3.11.9 (>= 3.11)
[PASS] deps       all 7 importable (httpx, fastapi, uvicorn, dotenv, pydantic, sqlalchemy, sqlite3)
[PASS] env        MODEL_FAST=qwen2.5:3b, OLLAMA_BASE_URL set
[PASS] db         15 tables, all required present (polyswarm.db)
[PASS] ollama     up, 9 models, 'qwen2.5:3b' present
[PASS] gamma      reachable, 1 market returned
[PASS] wikipedia  reachable, summary 'extract' present
[PASS] mirofish   backend up (:5001, external mode)
[PASS] dashboard  serving (http://localhost:8800)
[PASS] heartbeat  .heartbeat.json touched 407s ago
[PASS] obs_chain  chain intact: run_68b1c7b3d5 (204 lines)
[WARN] gdelt      HTTP 429 from GDELT doc API (rate-limited / throttled)
------------------------------------------------------------
OK: 11 pass, 1 warn, 0 fail  (of 12 checks)
```

**The five daemons** are Windows PowerShell launchers that live in `C:\Users\OMEN\Pictures\Polymarket\` — one level **above** the `polyswarm/` package (they are *not* in the git repo root). Launch them in numeric order:

```powershell
# from C:\Users\OMEN\Pictures\Polymarket\
.\1_mirofish_backend.ps1    # MiroFish crowd-sim Flask service    (:5001)
.\2_mirofish_frontend.ps1   # MiroFish web UI                     (:3000)
.\3_sameday_daemon.ps1      # same-day swarm daemon            -> sameday_live.log
.\4_dashboard.ps1           # live monitor    (http://localhost:8800)
.\5_ai_pipeline.ps1         # precise AI pipeline daemon (find->gather->MiroFish->LLM->bet) -> ai_night.log
```

**Manual / one-off** — run from `polyswarm/` with the venv interpreter and UTF-8 on:

```powershell
# from polyswarm\  (PowerShell)
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m harness.predict_today once --max 1 --size 5 --rounds 1 --min-edge 0.03
.\.venv\Scripts\python.exe -m harness.predict_today daemon --with-mirofish --size 5 --rounds 1 --mf-wait 360
.\.venv\Scripts\python.exe -m harness.scoreboard      # the two gates (read-only, no network)
.\.venv\Scripts\python.exe -m harness.loop status     # paper wallet + open positions
.\.venv\Scripts\python.exe -m harness.doctor          # the preflight health check (above)
.\.venv\Scripts\python.exe run_tests.py               # full acceptance + unit test suite
```

The same commands on a POSIX shell (e.g. Git Bash): `PYTHONUTF8=1 ./.venv/Scripts/python.exe -m harness.predict_today once …` — identical module paths.

`predict_today once` runs the full find→gather→think→bet chain on the soonest same-day markets (minutes per forecast on CPU). An abridged healthy run:

```text
  PRECISE same-day AI pipeline — find -> gather -> think -> bet (today only, <24h)

[1/4] FIND — scanning same-day markets the AI can predict…
      Picked 1 market(s) resolving today:
        - [opinion] 7.4h - Will <event> happen by <date>?
==============================================================================
MARKET: Will <event> happen by <date>?
  resolves in 7.4h (today) · market YES 38% · class=opinion · 0x1a2b3c4d5e6f78…
[2/4] GATHER — pulling GDELT news/sentiment + microstructure signals…
      gathered 2143 chars of real context in 6s
[3/4] THINK — 5-persona swarm forecasting WITH that data (slow on CPU)…
      swarm: 44.0% YES vs market 38% · regime=… · consensus=0.71 · 53s
      single-LLM challenger (A/B): 41.0% YES
[4/4] BET — sizing on the swarm's edge vs the market…
      conviction 0.62 → 0.41x Kelly, cap 7%
      edge +6.0% → kelly stake (within cap)
      BET PLACED: YES $12.40 @ 0.391 — resolves in 7.4h
==============================================================================
DONE — 1 data-driven bet(s) placed. wallet: cash $… · equity $… · realized $… · N open
```

(Numbers vary per market; a market that fails a reliability guard prints `DECISION: NO BET — <reason>` instead. One bad market never aborts the pass — the per-market body is crash-wrapped, the failure is logged via the obs error hook, and the loop continues to the next market.)

`scoreboard` prints the two go/no-go gates from `polyswarm.db` (read-only, no network):

```text
==================================================================
 POLYMARKET HARNESS — DUAL-GATE SCOREBOARD  (paper, out-of-sample)
==================================================================
 Resolved opinion markets: n = 0  (gate needs >= 50)
 …
 GATE 1  (model Brier < market Brier, n>=50):  FAIL   (no resolved markets yet)
 GATE 2  (paper bankroll grew after costs):                 FAIL   (start $1000.00 -> equity $978.80, realized $-61.63)

 >>> gates not both passed — stay on paper
==================================================================
```

**Model**: `MODEL_FAST` in `polyswarm/.env` (currently `qwen2.5:3b` — 1.5× faster than 7B, consistent enough to clear the guards; `gemma2:2b` is faster but too noisy). 14B+ does not fit 16 GB.

---

## Module map (`polyswarm/harness/`)

| Module | Role |
|---|---|
| `predict_today.py` | the AI pipeline (find→gather→MiroFish→swarm→guards→size→bet) + daemon |
| `sameday.py` | continuous same-day daemon; settles, cashes out >24 h bets, scouts + bets |
| `loop.py` | original autonomous loop; `_build_enrichment` (GATHER) + `settle_resolved` |
| `gamma.py` | read-only Polymarket Gamma client (markets, prices, resolution) |
| `gdelt.py` / `wiki.py` / `signals.py` | GATHER sources (news+tone / facts / microstructure) |
| `classifier.py` | opinion vs mechanical tagging |
| `sizing.py` | fractional-Kelly position sizing (+ conviction scaling) |
| `wallet.py` | paper wallet + positions (fills, slippage, settle, cash-out) |
| `challenger.py` | single-LLM A/B baseline |
| `mirofish*.py` | MiroFish crowd-sim client + signal distillation |
| `scoreboard.py` | dual-Brier + the two gates |
| `doctor.py` | read-only preflight health check (~12 PASS/WARN/FAIL probes); never writes/trades |
| `journal.py` | dashboard time-series (equity, decisions) |
| `dashboard.py` | FastAPI live monitor (`:8800`) — equity/P&L, gates, agent feed, MiroFish graph |
| `obs/` | observability/audit layer (see above) |
| `core/` | PolySwarm engine: `swarm.py`, `agent.py`, `aggregator.py`, advanced methods |

---

## Status & constraints

- **P0–P5 built and unit-tested.** Gates **not yet met** — they need ≥50 resolved opinion markets, which accrue over weeks of running. Realized paper P&L is small and unproven; the point is to *measure*, not to assume.
- **Same-day only** operating mode (resolutions accrue in days, not the multi-year 2028 races).
- **Keyless / $0**: local Ollama for the swarm; GDELT + Wikipedia are keyless; no wallet, no private keys, no real trades.
- MiroFish needs `ZEP_API_KEY` (Zep Cloud) to boot — kept in `MiroFish/.env`, **not committed**.

See `REPORT.md` for the build/verification report and `polyswarm/OBS_BUILD_PLAN.md` for the observability design.
