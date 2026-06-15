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

Four processes (Windows PowerShell launchers at the repo root); Ollama must be running:

```powershell
.\1_mirofish_backend.ps1    # MiroFish crowd-sim Flask service  (:5001)
.\2_mirofish_frontend.ps1   # MiroFish web UI                   (:3000)
.\3_sameday_daemon.ps1      # favorite-longshot + AI same-day daemon
.\5_ai_pipeline.ps1         # the precise AI pipeline daemon (find→gather→MiroFish→LLM→bet)
.\4_dashboard.ps1           # live monitor                      (http://localhost:8800)
```

Manual / one-off:
```bash
# from polyswarm/ , with PYTHONUTF8=1 and ./.venv/Scripts/python.exe
python -m harness.predict_today once --max 1 --size 5 --rounds 1 --min-edge 0.03
python -m harness.predict_today daemon --with-mirofish --size 5 --rounds 1 --mf-wait 360
python -m harness.scoreboard          # read the two gates
python -m harness.loop status
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
