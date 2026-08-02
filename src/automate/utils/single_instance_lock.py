"""
utils/single_instance_lock.py — OS-level guard against two instances of
a background task (e.g. custom_strategy_scheduler) running at once on
the same machine — the only way this app is ever deployed (a single
systemd service, no --workers, no horizontal scaling; see
automate-api.service's ExecStart). A DB-row-based lock would need its
own staleness/heartbeat logic to detect a crashed holder; flock()
already gives that for free — the OS releases it automatically the
instant the holding process exits, crashes, or is kill -9'd, no
heartbeat needed.

This exists because custom_strategy_scheduler.py places REAL broker
orders. If it were ever accidentally run twice — a bad deploy leaving an
old process behind, `uvicorn --workers 2`, a second `python -m uvicorn`
started by hand on the same box — both instances would independently
see "nothing entered this cycle yet" and each place the SAME real
order, doubling every live position. See
utils/position_reconciliation.py for the companion safety net that
catches drift AFTER the fact; this prevents the most common cause of it
BEFORE it happens.
"""
import fcntl
import os
from pathlib import Path
from typing import Dict, IO

from automate.utils.logger import get_logger

log = get_logger(__name__)

_LOCK_DIR = Path("logs")
# Keeps each acquired lock's file handle open for the whole process
# lifetime — closing it (or letting it get garbage collected) would
# release the flock early. Never read, only holds references.
_held_locks: Dict[str, IO] = {}


def acquire_singleton_lock(name: str) -> bool:
    """
    Try to become the sole holder of `name`'s lock for this process's
    lifetime. Returns True if acquired (this process now owns it —
    released automatically on process exit/crash, no manual cleanup
    needed even after an unclean shutdown), False if another live
    process already holds it.
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _LOCK_DIR / f"{name}.lock"
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.write(str(os.getpid()))
    fh.flush()
    _held_locks[name] = fh
    return True
