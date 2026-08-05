"""api/routes_logs.py — tail the daemon's own log file for the dashboard's Logs view."""
from collections import deque
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/logs", tags=["logs"])

REPO_ROOT = Path(__file__).resolve().parent.parent  # backend/ — where logs/ actually lives
LOG_FILE = REPO_ROOT / "logs" / "daemon.log"


@router.get("/tail")
def tail_log(lines: int = 200):
    if not LOG_FILE.exists():
        return {"lines": []}
    with LOG_FILE.open(errors="replace") as f:
        return {"lines": list(deque(f, maxlen=lines))}
