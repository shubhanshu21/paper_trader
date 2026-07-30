"""
api/ws_notifications.py — pushes new Notification rows (see utils/notify.py)
to the connected browser as they're created, plus the current unread
count. Same shape as ws_positions.py/ws_custom_strategy_greeks.py: the
SERVER polls the DB on a short interval, the CLIENT only ever receives
pushes — no client-side polling.

On connect, sends a snapshot of the most recent notifications (so the
Bell icon has something to show immediately); after that, only sends
rows newer than the last one already pushed, so the client can just
prepend/merge instead of re-rendering the whole list every tick.
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from automate.db.engine import SessionLocal
from automate.db.models import Notification

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 4.0
_INITIAL_SNAPSHOT_LIMIT = 30


def _fetch_snapshot() -> dict:
    db = SessionLocal()
    try:
        rows = db.query(Notification).order_by(Notification.id.desc()).limit(_INITIAL_SNAPSHOT_LIMIT).all()
        unread_count = db.query(Notification).filter(Notification.read.is_(False)).count()
        return {
            "notifications": [r.to_dict() for r in rows],
            "unread_count": unread_count,
            "last_id": rows[0].id if rows else 0,
        }
    finally:
        db.close()


def _fetch_new_since(last_id: int) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(Notification).filter(Notification.id > last_id).order_by(Notification.id.asc()).all()
        unread_count = db.query(Notification).filter(Notification.read.is_(False)).count()
        return {
            "notifications": [r.to_dict() for r in rows],
            "unread_count": unread_count,
            "last_id": rows[-1].id if rows else last_id,
        }
    finally:
        db.close()


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        snapshot = await asyncio.to_thread(_fetch_snapshot)
        await websocket.send_json({"type": "snapshot", **snapshot})
        last_id = snapshot["last_id"]

        while True:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            delta = await asyncio.to_thread(_fetch_new_since, last_id)
            last_id = delta["last_id"]
            # Always push, even with an empty notifications list — keeps
            # unread_count in sync after the client marks something read
            # via the REST endpoint.
            await websocket.send_json({"type": "update", **delta})
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/notifications.")
