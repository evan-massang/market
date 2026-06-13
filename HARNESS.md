# Polymarket Forecasting + Paper-Trading Harness — Operator's Manual

Autonomous, **paper-only**, **$0** harness built on a (patched) PolySwarm. Forecasts
**opinion** Polymarket markets with a 12-agent local-LLM swarm, sizes simulated bets
with fractional Kelly, and scores two gates. **No real money, no keys, no execution**
until BOTH gates pass and legality is cleared.

All commands run from `polyswarm/` with the venv. On Windows always export UTF-8 first:

```bash
cd C:/Users/OMEN/Pictures/Polymarket/polyswarm
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8        # Git Bash; PowerShell: $env:PYTHONUTF8=1
PY=./.venv/Scripts/python.exe
```

The model is **qwen2.5:7b** via Ollama (set in `polyswarm/.env`). State lives in
`polyswarm/polyswarm.db` (forecasts, paper wallet, positions, challenger, benchmark).

## ✅ Already set up for you
- **Scheduled task `PolymarketHarness`** runs `harness_pass.bat` every **3 hours**
  (settle → forecast near-term opinion markets), logging to `polyswarm/harness_cron.log`.
  Manage it: `Get-ScheduledTask PolymarketHarness` · `Disable-ScheduledTask PolymarketHarness`
  · `Start-ScheduledTask PolymarketHarness` (run now). It only bets markets resolving
  within **180 days** (`--max-days`), so the gate actually accrues — the top opinion
  markets are multi-year 2028 races that would never resolve.
- **3 seed paper positions** already open ($1,000 bankroll).
- **qwen2.5:14b** pulled locally as the P5 bigger model (no key needed; slow on CPU).
- **P2.5 $0 fallback** built (`harness.history`) so you don't need the OneDrive download.

## Daily operation

```bash
# one pass: fetch -> classify (opinion only) -> forecast -> size -> paper-fill -> log
$PY -m harness.loop run --max-markets 5 --size 6 --rounds 1 --challenger

# settle any markets that have resolved (updates wallet P&L + Brier)
$PY -m harness.loop settle

# scheduled, hands-off: settle -> run -> sleep 3h, repeat  (Ctrl-C to stop)
$PY -m harness.loop daemon --interval 10800 --challenger

# read state and the two gates
$PY -m harness.loop status
$PY -m harness.scoreboard

# LIVE DASHBOARD — dark web UI (P&L chart, positions, A/B, gates, decision transcript)
$PY -m harness.dashboard          # then open http://localhost:8800  (auto-refreshes every 5s)
```

Useful flags: `--dry-run` (stub forecast, test the pipeline fast), `--no-gdelt`,
`--no-signals`, `--max-markets N`, `--size N` (0 = all 12 agents, slower),
`--min-edge 0.02`, `--bankroll 1000`, `--challenger` (also run the single-LLM A/B).

### Schedule it on Windows (Task Scheduler)
Run a pass every 3 hours without keeping a terminal open:
```powershell
$cmd = 'cd C:\Users\OMEN\Pictures\Polymarket\polyswarm; $env:PYTHONUTF8=1; ' +
       '.\.venv\Scripts\python.exe -m harness.loop settle; ' +
       '.\.venv\Scripts\python.exe -m harness.loop run --max-markets 5 --size 6 --rounds 1 --challenger'
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -Command `"$cmd`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
             -RepetitionInterval (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName 'PolymarketHarness' -Action $action -Trigger $trigger
```

## The two gates (real money only when BOTH pass, out-of-sample, n>=50)
- **GATE 1 (calibration):** swarm Brier < market-price Brier on >=50 resolved opinion markets.
- **GATE 2 (profitability):** paper bankroll grew over the same sample after costs.
`harness/scoreboard.py` reports both, per theme, with n, plus the single-LLM A/B
(does the 12-agent swarm beat one plain LLM call?). **Reaching n>=50 takes weeks of
live accrual** — markets resolve over time; that is expected, not a bug.

## P2.5 — historical market-calibration read
**$0, no download needed** (the bar the swarm must beat):
```bash
$PY -m harness.history --n 200 --max-scored 80     # closed opinion markets + CLOB pre-resolution price
```

### Optional: the richer PolyBench dataset (manual)
1. Download the SQLite DB (browser only — OneDrive blocks curl/wget):
   https://1drv.ms/u/c/4d62feca782041b1/IQDR4BGbCmakS7Cid8OzCcHxAR-iaHdX0WPtmmdWQ_ab2Tg?e=cHQ2MZ
2. Save it as `C:/Users/OMEN/Pictures/Polymarket/PolyBench/database/polymarket_analysis.db`
3. Confirm the schema, then read:
   ```bash
   $PY -m harness.polybench inspect      # verify table/column names
   $PY -m harness.polybench read         # opinion coverage + market-price Brier (the BAR)
   ```
   (If `inspect` shows different column names, tell me and I'll adjust the adapter.)

## P5 — bigger-model benchmark (free hosted endpoint)
Get a free key (e.g. Groq at console.groq.com, or Google AI Studio), then:
```bash
export LLM_PROVIDER=openai
export OPENAI_BASE_URL=https://api.groq.com/openai/v1     # Groq example
export OPENAI_API_KEY=<your_free_key>
$PY -m harness.benchmark --model llama-3.3-70b-versatile --limit 50
```
Re-forecasts the SAME logged, resolved opinion markets through the bigger model and
prints bigger-model Brier vs our-swarm Brier vs market Brier, with a verdict
(approach dead / method works-scale-up / 7B already wins). Run it only after some
opinion markets have resolved.

## Hard rules
- Paper money only until BOTH gates pass; paper->real is a reviewed change, never a flag.
- Forecasters parallel, never chained. Signals are features. Benchmarking is offline.
- One news source, one order-flow reader, one sizing engine.
- Settle Polymarket **legality/geofencing** where you live before any real execution.

See `polyswarm/harness/` for the modules and `~/.claude/.../memory/` notes.
```
