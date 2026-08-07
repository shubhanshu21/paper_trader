"""
api/routes_daemon.py — start/stop/status for run_daemon.py, managed as a
plain OS process (same way a human would from the terminal) rather than
folding the daemon's own loop into the API process. This keeps the CLI
(`python3 -m cli.run_daemon`) and the API as two independent,
equally-valid ways to control the exact same process.
"""
import os
import signal
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.auth import require_admin

router = APIRouter(prefix="/api/daemon", tags=["daemon"])

# backend/api/routes_daemon.py -> up 2 levels to backend/, same anchor
# backtest/__main__.py uses — needed so PID_FILE/logs resolve the same
# absolute path regardless of the API process's own cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
PID_FILE = REPO_ROOT / "logs" / "daemon.pid"
LOG_FILE = REPO_ROOT / "logs" / "daemon.log"
# Marks "a human deliberately stopped this" so ensure_daemon_running() (API
# startup + the periodic supervisor in main.py) doesn't resurrect it — a
# stopped daemon should stay stopped until /start is called again, not get
# silently respawned within minutes because that looks identical to a crash.
STOPPED_FLAG = REPO_ROOT / "logs" / "daemon.stopped"


def _running_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return None
    try:
        os.kill(pid, 0)  # signal 0: existence check only, doesn't actually kill
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # exists, owned by another user — still "running" from our point of view
    return pid


@router.get("/status")
def daemon_status():
    pid = _running_pid()
    return {"running": pid is not None, "pid": pid}


def _spawn_daemon() -> None:
    """Shared by the /start endpoint and the API's own startup hook (main.py) —
    spawns run_daemon.py exactly as a human clicking "start" would: a detached
    OS subprocess, not a thread/task inside this process, so the daemon keeps
    running across API restarts/redeploys."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOPPED_FLAG.unlink(missing_ok=True)  # this spawn is an explicit (re)start
    with open(LOG_FILE, "a") as log_fh:
        subprocess.Popen(
            [sys.executable, "-m", "cli.run_daemon"],
            cwd=str(REPO_ROOT),
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detached — stopping/crashing the API must not affect the daemon
        )


def ensure_daemon_running() -> bool:
    """Start the daemon iff it isn't already running AND it wasn't
    deliberately stopped. Returns True if a new process was spawned. Safe to
    call on every API startup and periodically (main.py's _daemon_supervisor)
    — restarting automate-api or re-checking on a timer must not resurrect a
    daemon someone intentionally stopped via /stop, and must not spawn
    duplicates when one's already running."""
    if STOPPED_FLAG.exists():
        return False
    if _running_pid() is not None:
        return False
    _spawn_daemon()
    return True


@router.post("/start")
def daemon_start(user: dict = Depends(require_admin)):
    pid = _running_pid()
    if pid is not None:
        raise HTTPException(status_code=409, detail=f"Daemon already running (pid={pid}).")
    _spawn_daemon()
    return {"started": True}


@router.post("/stop")
def daemon_stop(user: dict = Depends(require_admin)):
    pid = _running_pid()
    if pid is None:
        raise HTTPException(status_code=409, detail="Daemon is not running.")
    STOPPED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOPPED_FLAG.touch()  # tells ensure_daemon_running() (startup + supervisor) to leave it stopped
    os.kill(pid, signal.SIGTERM)  # run_daemon.py turns this into a clean KeyboardInterrupt-style shutdown
    return {"stopping": True, "pid": pid}
