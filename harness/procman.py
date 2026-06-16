"""harness/procman.py — OS-safe background process management (single source of truth).

No third-party deps (psutil is not installed). Spawns a DETACHED background process
(no console window, survives the launching terminal), checks liveness by PID, and
terminates a process TREE gracefully-then-forcefully. Windows uses ctypes for
liveness (os.kill(pid, 0) would TERMINATE on Windows — never use it) and
`taskkill /T` to kill the venv-launcher-stub tree; POSIX uses os.kill + sessions.

This module never decides WHICH pids to touch — the supervisor passes only pids it
recorded in .runtime/pids/. So it can never kill an unrelated process.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

IS_WIN = os.name == "nt"

# Windows creation flags
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def spawn(cmd, cwd, log_path, env=None) -> int:
    """Start ``cmd`` (list) DETACHED in the background. stdout+stderr append to
    ``log_path``; no window; survives the parent terminal closing. Returns the OS pid."""
    full_env = {**os.environ, **(env or {})}
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logf = open(log_path, "ab", buffering=0)
    try:
        if IS_WIN:
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
            p = subprocess.Popen(
                cmd, cwd=cwd, env=full_env, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        else:
            p = subprocess.Popen(
                cmd, cwd=cwd, env=full_env, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return p.pid
    finally:
        # the child holds its own handle to the log; we can close ours
        try:
            logf.close()
        except Exception:
            pass


def is_alive(pid) -> bool:
    """True iff process ``pid`` exists and has not exited. Safe (never signals)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def terminate(pid, graceful_timeout: float = 8.0) -> bool:
    """Stop the process TREE rooted at ``pid``: graceful request first, force-kill the
    whole tree if it doesn't exit within ``graceful_timeout``. Returns True if gone.

    On Windows, `/T` kills children too — important because the venv `python.exe` is a
    launcher stub that spawns the real interpreter as a child."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if not is_alive(pid):
        return True

    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                       capture_output=True, creationflags=_CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + max(0.5, graceful_timeout)
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.25)

    # force the whole tree
    if IS_WIN:
        subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                       capture_output=True, creationflags=_CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    return not is_alive(pid)


# ── pid file helpers (the supervisor's record of what IT started) ───────────────
def write_pid(pid_path: str, pid: int) -> None:
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, "w") as f:
        f.write(str(int(pid)))


def read_pid(pid_path: str):
    try:
        with open(pid_path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def clear_pid(pid_path: str) -> None:
    try:
        os.remove(pid_path)
    except OSError:
        pass
