"""api/routes_positions.py — view open/closed positions, close one manually."""
import logging

from fastapi import APIRouter, HTTPException

from automate.utils.pnl import compute_strangle_pnl
from automate.utils.position_tracker import get_open_positions, get_closed_positions
from automate.api.deps import get_brokers, get_audit_trail, get_rate_limiter, compute_mtm_economics

log = logging.getLogger("api.positions")
router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("/open")
def list_open_positions():
    brokers = get_brokers()
    positions = get_open_positions()
    for pos in positions:
        econ = compute_mtm_economics(pos, brokers)
        pos["mtm"] = None if econ is None else econ["gross_pnl"]
        pos["net_mtm"] = None if econ is None else econ["net_pnl"]
        pos["charges"] = None if econ is None else econ["charges"]
    return positions


@router.get("/closed")
def list_closed_positions(limit: int = 50):
    positions = get_closed_positions(limit=limit)
    for pos in positions:
        econ = compute_strangle_pnl(
            pos["call_entry_price"], pos["put_entry_price"],
            pos["call_exit_price"] or 0.0, pos["put_exit_price"] or 0.0,
            pos["quantity"],
        )
        pos["gross_pnl"] = econ["gross_pnl"]
        pos["net_pnl"] = econ["net_pnl"]
        pos["charges"] = econ["charges"]
    return positions


@router.post("/{position_id}/close")
def close_position_now(position_id: int):
    from automate.cli.run_position_monitor import close_position_manual

    brokers = get_brokers()
    if brokers is None:
        raise HTTPException(status_code=503, detail="Upstox broker unavailable — check credentials/token.")
    try:
        result = close_position_manual(position_id, brokers, get_audit_trail(), get_rate_limiter(), log)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return result
