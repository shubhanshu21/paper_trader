"""
api/routes_notifications.py — REST access to the Notification table (see
utils/notify.py, the only writer). The frontend Bell icon uses the
live-updating /ws/notifications WebSocket for real-time pushes; these
REST endpoints back the initial page load and the mark-read actions.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from automate.api.auth import get_current_user
from automate.db.engine import get_db
from automate.db.models import Notification

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _visibility_filter(query, user: dict):
    """
    admin: own notifications + system-wide ones (user_id IS NULL, e.g.
    broker login / instrument master download failures — no single owner).
    non-admin: only their own — system-wide alerts about the shared
    broker connection aren't actionable by a viewer account.
    """
    user_id = int(user["sub"])
    if user.get("role") == "admin":
        return query.filter(or_(Notification.user_id == user_id, Notification.user_id.is_(None)))
    return query.filter(Notification.user_id == user_id)


@router.get("")
def list_notifications(limit: int = 50, unread_only: bool = False, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    limit = max(1, min(limit, 200))
    query = _visibility_filter(db.query(Notification), user)
    if unread_only:
        query = query.filter(Notification.read.is_(False))
    rows = query.order_by(Notification.id.desc()).limit(limit).all()
    unread_count = _visibility_filter(db.query(Notification), user).filter(Notification.read.is_(False)).count()
    return {"notifications": [r.to_dict() for r in rows], "unread_count": unread_count}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    row = _visibility_filter(db.query(Notification), user).filter(Notification.id == notification_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read = True
    db.commit()
    return row.to_dict()


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    updated = _visibility_filter(db.query(Notification), user).filter(Notification.read.is_(False)).update(
        {"read": True}, synchronize_session=False
    )
    db.commit()
    return {"marked_read": updated}
