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
from sqlalchemy import or_

from api.auth import get_current_user_ws
from db.engine import SessionLocal
from db.models import Notification

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 4.0
_INITIAL_SNAPSHOT_LIMIT = 30


def _visibility_filter(query, user: dict):
    """Same rule as routes_notifications.py: admins also see system-wide (user_id IS NULL) alerts."""
    user_id = int(user["sub"])
    if user.get("role") == "admin":
        return query.filter(or_(Notification.user_id == user_id, Notification.user_id.is_(None)))
    return query.filter(Notification.user_id == user_id)


def _fetch_snapshot(user: dict) -> dict:
    db = SessionLocal()
    try:
        rows = _visibility_filter(db.query(Notification), user).order_by(Notification.id.desc()).limit(_INITIAL_SNAPSHOT_LIMIT).all()
        unread_count = _visibility_filter(db.query(Notification), user).filter(Notification.read.is_(False)).count()
        return {
            "notifications": [r.to_dict() for r in rows],
            "unread_count": unread_count,
            "last_id": rows[0].id if rows else 0,
        }
    finally:
        db.close()


def _fetch_new_since(last_id: int, user: dict) -> dict:
    db = SessionLocal()
    try:
        rows = _visibility_filter(db.query(Notification), user).filter(Notification.id > last_id).order_by(Notification.id.asc()).all()
        unread_count = _visibility_filter(db.query(Notification), user).filter(Notification.read.is_(False)).count()
        return {
            "notifications": [r.to_dict() for r in rows],
            "unread_count": unread_count,
            "last_id": max((r.id for r in rows), default=last_id),
        }
    finally:
        db.close()


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket):
    await websocket.accept()
    user = get_current_user_ws(websocket)
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return
    try:
        snapshot = await asyncio.to_thread(_fetch_snapshot, user)
        await websocket.send_json({"type": "snapshot", **snapshot})
        last_id = snapshot["last_id"]

        while True:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            delta = await asyncio.to_thread(_fetch_new_since, last_id, user)
            last_id = delta["last_id"]
            # Always push, even with an empty notifications list — keeps
            # unread_count in sync after the client marks something read
            # via the REST endpoint.
            await websocket.send_json({"type": "update", **delta})
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/notifications.")
