"""
api/routes_price_alerts.py — CRUD for "notify me when NIFTY crosses X"
price alerts (db.models.PriceAlert). Evaluation against live LTP happens
separately in api/price_alert_scheduler.py — this module only manages
the alert rows themselves.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from api.auth import get_current_user
from db.engine import SessionLocal
from db.models import PriceAlert

router = APIRouter(prefix="/api/price-alerts", tags=["price-alerts"])

_VALID_CONDITIONS = {"ABOVE", "BELOW", "CROSSES_ABOVE", "CROSSES_BELOW"}


class CreateAlertRequest(BaseModel):
    symbol: str
    condition: str
    target_price: float
    note: str | None = None

    @field_validator("condition")
    @classmethod
    def _valid_condition(cls, v: str) -> str:
        v = v.upper()
        if v not in _VALID_CONDITIONS:
            raise ValueError(f"condition must be one of {sorted(_VALID_CONDITIONS)}")
        return v

    @field_validator("target_price")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("target_price must be positive")
        return v


@router.post("")
def create_alert(req: CreateAlertRequest, user: dict = Depends(get_current_user)):
    db = SessionLocal()
    try:
        alert = PriceAlert(
            user_id=int(user["sub"]), symbol=req.symbol.upper(), condition=req.condition,
            target_price=req.target_price, note=req.note, status="ACTIVE",
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        return alert.to_dict()
    finally:
        db.close()


@router.get("")
def list_alerts(status: str | None = None, user: dict = Depends(get_current_user)):
    """The caller's own alerts, newest first. `status` optionally filters to ACTIVE/TRIGGERED/CANCELLED; omitted returns all."""
    db = SessionLocal()
    try:
        q = db.query(PriceAlert).filter(PriceAlert.user_id == int(user["sub"]))
        if status:
            q = q.filter(PriceAlert.status == status.upper())
        alerts = q.order_by(PriceAlert.created_at.desc()).all()
        return {"alerts": [a.to_dict() for a in alerts]}
    finally:
        db.close()


@router.delete("/{alert_id}")
def delete_alert(alert_id: int, user: dict = Depends(get_current_user)):
    """Removes an alert outright — the historical record of a TRIGGERED alert actually firing already lives in the Notification it sent, not in this row."""
    db = SessionLocal()
    try:
        alert = db.query(PriceAlert).filter(
            PriceAlert.id == alert_id, PriceAlert.user_id == int(user["sub"]),
        ).first()
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        db.delete(alert)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()
