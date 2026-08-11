"""
api/routes_kill_switch.py — the accessible control surface for the
SEBI-mandated global kill switch (compliance.sebi_rules.get_global_kill_
switch()). One shared instance for the whole process — every strategy
engine's order-placement chokepoint checks it before placing a NEW order
(never before closing/unwinding an existing one, see each engine's own
_place()/close_leg() split) — so activating it here actually halts every
strategy at once, not just one engine.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import get_current_user, require_admin
from compliance.sebi_rules import get_global_kill_switch

router = APIRouter(prefix="/api/kill-switch", tags=["kill-switch"])


class ActivateRequest(BaseModel):
    reason: str = "Manual trigger"


@router.get("/status")
def status(user: dict = Depends(get_current_user)):
    return get_global_kill_switch().status()


@router.post("/activate")
def activate(req: ActivateRequest, user: dict = Depends(require_admin)):
    """Halts ALL new order flow across every strategy engine, immediately — closing/unwinding an already-open position is NOT affected."""
    get_global_kill_switch().activate(reason=f"{req.reason} (by user {user.get('sub')})")
    return get_global_kill_switch().status()


@router.post("/reset")
def reset(user: dict = Depends(require_admin)):
    """Manual re-enable only — never happens automatically."""
    get_global_kill_switch().reset()
    return get_global_kill_switch().status()
