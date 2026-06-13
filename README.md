# Polymarket Forecasting + Paper-Trading Harness

An autonomous, **paper-only**, **$0** system that forecasts Polymarket *opinion* markets,
finds **+EV bets**, sizes them with fractional Kelly, and tracks everything on a live
dashboard. No real money, no execution, no API keys required — it runs a local LLM
(Ollama/Qwen) and free public data.

Built on a (patched) copy of [PolySwarm](https://github.com/defidaddydavid/polyswarm)
(MIT) as the multi-agent forecaster, with a net-new `harness/` layer on top.

> ⚠️ **Paper trading only.** Nothing here places real orders or holds funds. Real-money
> prediction-market betting is geofenced/restricted in many places — out of scope.

## What it does

```
Gamma (read-only) ──▶ classify (opinion vs mechanical) ──▶ forecast ──▶ size (¼-Kelly)
                                                                │
                                                          paper wallet ──▶ dashboard
```

- **`harness/gamma.py`** — read-only Polymarket Gamma market data (no wallet/keys).
- **`harness/classifier.py`** — tags markets `opinion` vs `mechanical`; only opinion is traded.
- **`harness/backtest.py` + `strategy.py`** — the profitability engine (see below).
- **`harness/sizing.py` / `wallet.py`** — fractional-Kelly sizing + simulated fills (slippage, caps, guardrails).
- **`harness/loop.py` / `strategy_bet.py`** — the autonomous betting loop.
- **`harness/scoreboard.py`** — dual-Brier + paper-P&L gates.
- **`harness/dashboard.py`** — live dark web UI (P&L chart, positions + countdowns, decision transcript).
- **`harness/gdelt.py` / `signals.py` / `mirofish*.py`** — optional upstream context/signals.

## The key finding (why this is the interesting part)

Backtested on **394 real resolved opinion markets** (`harness/backtest.py`):

| strategy | ROI | note |
|---|---|---|
| **Favorite-longshot edge** (fade overpriced longshots, back underpriced favorites) | **+18% / 96% win** | slippage-robust (stays + at 0–2¢) |
| Naive "buy cheap YES" (an overconfident LLM's instinct) | **−100%** | wipes the bankroll |

Prediction-market crowds **overbet longshots** — so the edge is a pure, no-LLM price rule.
Reproduce it:
```bash
python -m harness.backtest calib        # the favorite-longshot bias, bucket by bucket
python -m harness.backtest strategies   # +18% edge vs −100% naive
```

## Quickstart

```bash
cd polyswarm
uv venv --python 3.11 && uv pip install -r requirements.txt
# Ollama running locally with a qwen2.5 model; config in .env (see .env.example)
export PYTHONUTF8=1
python -m harness.strategy_bet      # place a +EV paper portfolio
python -m harness.dashboard         # live dashboard at http://localhost:8800
python -m harness.loop settle       # realize P&L as markets resolve
```

Full operator's manual: **[HARNESS.md](HARNESS.md)**.

## Hard rules
Paper money only. Forecasters parallel, signals are features, benchmarking is offline.
Evidence over vibes — every claim of profitability is backtested, not asserted.

---
*Forecaster core: PolySwarm (MIT, © defidaddydavid). Harness layer + favorite-longshot
strategy: this repo.*
