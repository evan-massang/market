"""harness.config_check — verify .env / environment matches what the code actually reads.

`python -m harness.config_check` (add --json). It NEVER prints a secret value (keys are
masked). It reports: required-missing (FAIL), optional-missing (informational), unknown
vars in .env that the code does not read (WARN — possibly dead/typo), and known-but-unread
advertised vars (e.g. SWARM_SIZE). Read-only; never writes.
"""
from __future__ import annotations

import os
import re
import sys

# Known config: name -> (required, reads?, note). `reads=False` means it is documented
# but NOT consumed by the code (advertised-but-dead — flagged). Keep in sync with code.
KNOWN = {
    # provider / model (optional — sane defaults; LLM keys handled separately)
    "LLM_PROVIDER": (False, True, "reasoning provider (default ollama)"),
    "MODEL_FAST": (False, True, "local model id (default qwen2.5:3b)"),
    "OLLAMA_BASE_URL": (False, True, "ollama endpoint"),
    "OLLAMA_TIMEOUT": (False, True, "ollama read timeout (s)"),
    "BRAIN_PROVIDER": (False, True, "swarm|mock|disabled|manus"),
    "MANUS_API_BASE": (False, True, "external brain endpoint"),
    "MANUS_API_KEY": (False, True, "external brain key (masked)"),
    "MANUS_TIMEOUT": (False, True, "external brain timeout"),
    "CHALLENGER_API_KEY": (False, True, "hosted challenger key (masked)"),
    "CHALLENGER_BASE_URL": (False, True, "hosted challenger endpoint"),
    "CHALLENGER_MODEL": (False, True, "hosted challenger model"),
    "CHALLENGER_MODELS": (False, True, "challenger ensemble roster"),
    # database / obs
    "DATABASE_URL": (False, True, "sqlite path (default polyswarm.db)"),
    "OBS_ENABLED": (False, True, "observability on/off"),
    "OBS_LOGS_DIR": (False, True, "obs log dir"),
    "HARNESS_HEARTBEAT": (False, True, "ai_pipeline heartbeat file"),
    # dashboard / mirofish ports
    "DASH_PORT": (False, True, "dashboard port (default 8800)"),
    "DASHBOARD_PORT": (False, True, "dashboard port (supervisor)"),
    "DASH_STREAM_LOG": (False, True, "dashboard live-feed log"),
    "MIROFISH_BASE": (False, True, "mirofish backend url"),
    "MIROFISH_BACKEND_PORT": (False, True, "mirofish backend port"),
    # supervisor
    "SUPERVISOR_ENABLED": (False, True, "supervisor toggle"),
    "RUN_MIROFISH": (False, True, "start mirofish backend"),
    "RUN_MIROFISH_FRONTEND": (False, True, "start mirofish UI"),
    "RUN_DASHBOARD": (False, True, "start dashboard"),
    "RUN_SAMEDAY_DAEMON": (False, True, "start sameday daemon"),
    "RUN_AI_PIPELINE": (False, True, "start AI pipeline"),
    "SUPERVISOR_RESTART_CRASHED": (False, True, "auto-restart crashed services"),
    "SUPERVISOR_MAX_RESTARTS": (False, True, "restart cap"),
    "SUPERVISOR_RESTART_WINDOW_SECONDS": (False, True, "restart window"),
    "SUPERVISOR_RUNTIME_DIR": (False, True, "runtime state dir override"),
    # reliability / EV
    "RETRY_MAX_ATTEMPTS": (False, True, "retry attempts"),
    "RETRY_BASE_SECONDS": (False, True, "retry base backoff"),
    "RETRY_MAX_BACKOFF_SECONDS": (False, True, "retry backoff cap"),
    "MIN_EV_AFTER_COSTS": (False, True, "min after-cost EV to bet"),
    "SPREAD_PENALTY_MULTIPLIER": (False, True, "EV spread penalty"),
    "LIQUIDITY_PENALTY_MULTIPLIER": (False, True, "EV liquidity penalty"),
    "UNCERTAINTY_PENALTY_MULTIPLIER": (False, True, "EV uncertainty penalty"),
    "EXIT_RISK_PENALTY_MULTIPLIER": (False, True, "EV exit-risk penalty"),
    "DEBATE_ROUNDS": (False, True, "swarm debate rounds"),
    # advertised-but-NOT-read (the swarm size comes from the --size CLI flag, not env)
    "SWARM_SIZE": (False, False, "advertised in docs but NOT read by code — use --size"),
}

_SECRET = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)$", re.I)


def _mask(name: str, value: str) -> str:
    if value is None:
        return "(unset)"
    if _SECRET.search(name):
        return "***set (masked)***" if value else "(empty)"
    return value


def _read_env_file(path=".env") -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def run(env_file: str = ".env") -> dict:
    rows = []
    env_vars = _read_env_file(env_file)
    seen = set()

    for name, (required, reads, note) in KNOWN.items():
        seen.add(name)
        present = (name in os.environ) or (name in env_vars)
        val = os.environ.get(name, env_vars.get(name))
        if not reads:
            rows.append((name, "WARN", f"{note}"))
        elif required and not present:
            rows.append((name, "FAIL", f"REQUIRED and missing — {note}"))
        elif present:
            rows.append((name, "OK", f"{_mask(name, val)}"))
        else:
            rows.append((name, "INFO", f"optional, unset — {note}"))

    # unknown vars present in .env that the code does not read (possibly dead / typo)
    for name in env_vars:
        if name not in seen:
            rows.append((name, "WARN", "in .env but not read by code (dead var or typo?)"))

    n_fail = sum(1 for _, s, _ in rows if s == "FAIL")
    n_warn = sum(1 for _, s, _ in rows if s == "WARN")
    return {"rows": rows, "fail": n_fail, "warn": n_warn}


def render(res: dict) -> None:
    print("harness.config_check — config vs code (secrets masked)")
    print("-" * 64)
    for name, status, detail in res["rows"]:
        if status == "INFO":
            continue   # keep the default view focused; --json shows everything
        print(f"[{status:<4}] {name:<32} {detail}")
    print("-" * 64)
    print(f"{'CONFIG OK' if res['fail'] == 0 else 'CONFIG NOT OK'} — "
          f"{res['fail']} fail, {res['warn']} warn")


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    res = run()
    if "--json" in argv:
        import json
        print(json.dumps(res, indent=2))
    else:
        render(res)
    return 1 if res["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
