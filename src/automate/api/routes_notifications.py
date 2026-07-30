"""
api/routes_notifications.py — REST access to the Notification table (see
utils/notify.py, the only writer). The frontend Bell icon uses the
live-updating /ws/notifications WebSocket for real-time pushes; these
REST endpoints back the initial page load and the mark-read actions.
"""
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automate.db.engine import get_db
from automate.db.models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(limit: int = 50, unread_only: bool = False, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    query = db.query(Notification)
    if unread_only:
        query = query.filter(Notification.read.is_(False))
    rows = query.order_by(Notification.id.desc()).limit(limit).all()
    unread_count = db.query(Notification).filter(Notification.read.is_(False)).count()
    return {"notifications": [r.to_dict() for r in rows], "unread_count": unread_count}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, db: Session = Depends(get_db)):
    row = db.query(Notification).filter(Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read = True
    db.commit()
    return row.to_dict()


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db)):
    updated = db.query(Notification).filter(Notification.read.is_(False)).update({"read": True})
    db.commit()
    return {"marked_read": updated}
