# STARTUP AUDIT — what actually has to run

Phase 0 of the one-command orchestrator. The real services, their real commands
(from the `1_…`–`5_` launchers), ports, health checks, and dependencies. Paper-only.

## Services (real commands)

| service | manage? | command (real) | cwd | port | health | required |
|---|---|---|---|---|---|---|
| `ollama` | **external — check only** | (not started) | — | 11434 | HTTP `GET :11434` + model present | yes (provider) |
| `mirofish_backend` | start | `MiroFish/backend/.venv/Scripts/python.exe run.py` | `MiroFish/backend` | 5001 | `health.mirofish_health()` (:5001) | only if `RUN_MIROFISH` |
| `mirofish_frontend` | start | `npm run dev` | `MiroFish/frontend` | 3000 | TCP :3000 | optional (`RUN_MIROFISH_FRONTEND`) |
| `dashboard` | start | `python -m harness.dashboard` | `polyswarm` | 8800 | HTTP `GET :8800/health` | `RUN_DASHBOARD` |
| `sameday_daemon` | start | `python -u -m harness.sameday daemon` | `polyswarm` | — | heartbeat: `sameday_live.log` mtime | `RUN_SAMEDAY_DAEMON` |
| `ai_pipeline` | start | `python -u -m harness.predict_today daemon --with-mirofish --size 5 --rounds 1 --min-edge 0.03 --interval 30 --mf-wait 360` | `polyswarm` | — | heartbeat: `.heartbeat.json` mtime | `RUN_AI_PIPELINE` |

### Notes vs README / the original launchers
- The polyswarm **swarm/forecasting agent is not a separate process** — it runs *inside*
  `ai_pipeline` (`predict_today`) and inside `sameday_daemon`. So there is **no distinct
  `polyswarm_daemon`**; `RUN_POLYSWARM_DAEMON` is treated as a synonym/no-op (the swarm is
  already covered by `ai_pipeline`). This corrects the assumption in the task's example list.
- `ollama` and `mirofish_backend` use their OWN python/venv and may already be running
  (started outside the repo). The supervisor **checks** them and only starts a managed one
  if it isn't already healthy — it never starts a second Ollama.
- Dependency order: `ollama` (check) → `mirofish_backend` (if enabled) → `dashboard` →
  `sameday_daemon` → `ai_pipeline` (depends on `mirofish_backend` when `RUN_MIROFISH`).
- All of this is PAPER-ONLY; no real-money path is involved in any service.

## Runtime layout the supervisor uses
```
polyswarm/.runtime/
  pids/<service>.pid        one PID per managed service (+ supervisor.pid for the watcher)
  logs/<service>.log        stdout+stderr per service (rotated 10MB x5)
  heartbeats/<service>.json per-service heartbeat (pid, started_at, last_seen, restarts, health)
  status.json               last full status snapshot written by start/watch
  stop.flag                 presence tells the watcher to stop everything
```
