# External-brain architecture — the LLM is a replaceable component

The reasoning LLM/agent is **one swappable provider**, not the center of the system.
Everything that makes the bot good — scanning, candidate scoring, evidence building,
caching, guards, EV-after-costs, event-portfolio math, calibration, sizing, settlement,
scoring/gates, CLV, performance memory, the dashboard — is **non-LLM core logic** that
talks to the brain through one interface. You can run the whole system **without any
local LLM** (observe-only), or point it at **Manus AI** (or any HTTP brain) later.

## The boundary

```
non-LLM core  ──EvidencePack──▶  BrainProvider  ──ForecastResult──▶  guards/EV/sizing/wallet
(scanner, evidence,                  │
 cache, classifier,                  ├─ swarm     local PolySwarm + challenger (default)
 event portfolio,                    ├─ mock      deterministic, for tests
 EV, calibration,                    ├─ disabled  observe-only (no forecasts)
 sizing, scoring)                    └─ manus     external HTTP (Manus AI or any endpoint)
```

- `harness/brain/base.py` — `BrainProvider` + strict, JSON-able models: `EvidencePack`,
  `ForecastResult` (probability / confidence / reasons / risk_flags / missing_information /
  what_would_change / recommended_action ∈ {bet,observe,skip} / explanation), plus
  `CritiqueResult`, `EventInsight`, `BrainHealth`.
- `harness/brain/providers.py` — the four providers above.
- `harness/brain/pack.py` — `build_brain_pack(market, cfg)` assembles a clean pack from the
  existing non-LLM pipeline (evidence builder + scanner microstructure + classifier).

## Selecting a provider

```bash
BRAIN_PROVIDER=swarm      # default: the existing local swarm, wrapped
BRAIN_PROVIDER=mock       # deterministic; tests
BRAIN_PROVIDER=disabled   # observe-only: scan + score + observe, place NO bets
BRAIN_PROVIDER=manus      # external: needs MANUS_API_BASE (+ optional MANUS_API_KEY)
```

```python
from harness import brain
b = brain.get_provider()                      # honors BRAIN_PROVIDER
result = b.forecast_market(brain.build_brain_pack(market))
# result is a ForecastResult (strict, JSON-able); a malformed/absent brain -> observe-only
```

## Run without a local LLM

`BRAIN_PROVIDER=disabled` (or any unavailable provider) makes every `forecast_market` return
`probability=None, recommended_action="observe"`. The bot still scans, scores candidates,
builds evidence, runs guards, and reconciles the wallet — it simply places no new bets.
The system **never crashes** because the brain is missing: a failed/unconfigured provider
degrades to observe-only by contract.

## Adding Manus AI later (no keys handled here)

`ManusProvider` is provider-agnostic: it POSTs the `EvidencePack` JSON to
`MANUS_API_BASE/forecast` and parses a structured `ForecastResult` (tolerant of a
percentage probability / missing keys / non-dict → observe-only). If `MANUS_API_KEY` is
set it is sent as a Bearer header; the repo does **not** create or manage that key — you
set it in the env separately. Until configured, Manus is observe-only.

## Non-LLM critic (defense in depth)

Every provider inherits a cheap, deterministic `critique_forecast` (no LLM): it flags
no-probability, mechanical markets, thin liquidity, low evidence quality, and edge-too-small,
and escalates severity → observe/skip. A provider may override it with an LLM critic.

## Dashboard

`GET /api/brain/status` shows the configured provider, the available providers, and the
brain's health — so you can see at a glance which brain is in use and whether it's up.

## Guarantees

- **Paper-only.** No provider has a real-money path. Never.
- **No fake profitability.** Gates stay honest (Gate 1 = all resolved forecasts incl.
  no-bet ones; Gate 2 = paper trades only; test/demo excluded).
- **Provider-agnostic.** No local-LLM / Ollama / Manus assumption leaks into core logic.
- **Replaceable.** Swap the brain with one env var; tests run on the mock — no real LLM needed.
