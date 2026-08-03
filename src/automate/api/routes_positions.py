"""api/routes_positions.py — view open/closed positions, close one manually."""
import logging

from fastapi import APIRouter, Depends, HTTPException

from automate.api.auth import get_current_user
from automate.api.deps import compute_mtm_economics, get_audit_trail, get_brokers, get_rate_limiter
from automate.utils.pnl import compute_strangle_pnl
from automate.utils.position_tracker import get_closed_positions, get_open_positions, get_position
from automate.utils.wallet import get_charge_rates

log = logging.getLogger("api.positions")
router = APIRouter(prefix="/api/positions", tags=["positions"])


def _scope_user_id(user: dict) -> int | None:
    """Admins also see system-wide (no-owner) rows from the legacy CLI daemon — see Position.user_id's docstring."""
    return None if user.get("role") == "admin" else int(user["sub"])


@router.get("/open")
def list_open_positions(user: dict = Depends(get_current_user)):
    brokers = get_brokers()
    positions = get_open_positions(user_id=_scope_user_id(user))
    # Admins viewing the system-wide (no-owner) feed get their OWN rates
    # applied to every row — a minor approximation for that one cross-account view.
    rates = get_charge_rates(int(user["sub"]))
    for pos in positions:
        econ = compute_mtm_economics(pos, brokers, rates)
        pos["mtm"] = None if econ is None else econ["gross_pnl"]
        pos["net_mtm"] = None if econ is None else econ["net_pnl"]
        pos["charges"] = None if econ is None else econ["charges"]
        pos["call_ltp"] = None if econ is None else econ["call_ltp"]
        pos["put_ltp"] = None if econ is None else econ["put_ltp"]
    return positions


@router.get("/closed")
def list_closed_positions(limit: int = 50, user: dict = Depends(get_current_user)):
    positions = get_closed_positions(limit=limit, user_id=_scope_user_id(user))
    rates = get_charge_rates(int(user["sub"]))
    for pos in positions:
        econ = compute_strangle_pnl(
            pos["call_entry_price"], pos["put_entry_price"],
            pos["call_exit_price"] or 0.0, pos["put_exit_price"] or 0.0,
            pos["quantity"],
            rates,
        )
        pos["gross_pnl"] = econ["gross_pnl"]
        pos["net_pnl"] = econ["net_pnl"]
        pos["charges"] = econ["charges"]
    return positions


@router.post("/{position_id}/close")
def close_position_now(position_id: int, user: dict = Depends(get_current_user)):
    from automate.cli.run_position_monitor import close_position_manual

    pos = get_position(position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    # Same 404-not-403 non-enumerable-ownership pattern used for custom
    # strategies — admins may also close system-wide (no-owner) legacy rows.
    owner_id = pos.get("user_id")
    if owner_id is not None and owner_id != int(user["sub"]) and user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Position not found")

    brokers = get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Upstox broker unavailable — check credentials/token.")
    try:
        result = close_position_manual(position_id, brokers, get_audit_trail(), get_rate_limiter(), log)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result
