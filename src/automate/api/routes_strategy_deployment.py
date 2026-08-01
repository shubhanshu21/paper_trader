"""
api/routes_strategy_deployment.py — Strategy deployment workflow.

Handles deployment of custom strategies to paper trading and live modes,
with performance tracking and automated options handling.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automate.api.auth import get_current_user
from automate.db.engine import get_db
from automate.db.models import CustomStrategy, CustomStrategyPosition
from automate.utils.logger import get_logger

log = get_logger(__name__)


def _current_user_id(user: dict) -> int:
    return int(user["sub"])


def _get_owned_strategy(db: Session, strategy_id: int, user_id: int) -> CustomStrategy:
    strategy = db.query(CustomStrategy).filter(
        CustomStrategy.id == strategy_id, CustomStrategy.user_id == user_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


def _square_off_open_positions(strategy: CustomStrategy, db: Session) -> int:
    """
    Immediately close every OPEN leg for this strategy at market, via the
    same paper/live broker its status implies. Used by pause/stop — a
    paused/stopped strategy must not be left holding a naked basket that
    nothing is monitoring anymore (the scheduler only manages
    PAPER_TRADING/LIVE rows). Returns the number of legs closed.
    """
    legs = db.query(CustomStrategyPosition).filter(
        CustomStrategyPosition.strategy_id == strategy.id,
        CustomStrategyPosition.status == "OPEN",
    ).all()
    if not legs:
        return 0

    from automate.api.custom_strategy_scheduler import _get_brokers, _mode_for_status
    brokers = _get_brokers()
    if brokers is None:
        log.error("Cannot square off strategy %s — broker not ready.", strategy.id)
        return 0
    broker = brokers[_mode_for_status(strategy.status)]

    closed = 0
    for leg in legs:
        opposite = broker.place_buy_order if leg.transaction_type == "SELL" else broker.place_sell_order
        try:
            exit_order_id = opposite(
                instrument_token=leg.instrument_key, quantity=leg.quantity, order_type="MARKET",
                tag=f"CUSTOM_STOP_{strategy.id}_{leg.leg_index}"[:20],
                user_id=strategy.user_id,
            )
        except Exception as exc:
            log.critical("Failed to square off leg %s for strategy %s on pause/stop: %s", leg.instrument_key, strategy.id, exc)
            continue
        leg.status = "CLOSED"
        leg.exit_price = broker.get_ltp(leg.instrument_key)
        leg.exit_order_id = exit_order_id
        leg.exit_reason = "MANUAL_STOP"
        leg.closed_at = datetime.now()
        closed += 1
    db.commit()
    return closed

router = APIRouter(prefix="/api/strategy-deployment", tags=["strategy-deployment"])


class DeploymentRequest(BaseModel):
    """Request model for deploying a strategy."""
    strategy_id: int
    mode: str  # 'paper' | 'live'


class DeploymentResponse(BaseModel):
    """Response model for deployment."""
    strategy_id: int
    status: str
    message: str
    deployed_at: str


@router.post("/deploy")
def deploy_strategy(request: DeploymentRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """
    Deploy a custom strategy to paper trading or live mode.

    This integrates the custom strategy with the existing strategy execution system.
    """
    strategy = _get_owned_strategy(db, request.strategy_id, _current_user_id(user))

    # Check if strategy can be deployed
    if strategy.status not in ["BACKTESTING", "PAUSED"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Strategy must be in BACKTESTING or PAUSED status to deploy. Current status: {strategy.status}"
        )
    
    if request.mode == "paper":
        new_status = "PAPER_TRADING"
    elif request.mode == "live":
        new_status = "LIVE"
    else:
        raise HTTPException(status_code=400, detail="Invalid mode. Must be 'paper' or 'live'")
    
    # Check if backtest has been run
    if strategy.backtest_return_pct is None:
        raise HTTPException(
            status_code=400, 
            detail="Strategy must be backtested before deployment"
        )
    
    # For live deployment, check paper trading performance
    if request.mode == "live" and strategy.paper_return_pct is None:
        raise HTTPException(
            status_code=400, 
            detail="Strategy must be paper traded before live deployment"
        )
    
    # Update strategy status
    strategy.status = new_status
    strategy.deployed_at = datetime.now()
    db.commit()
    db.refresh(strategy)

    # No separate "registration" step needed — api/custom_strategy_scheduler.py's
    # background task (started once at API boot, see main.py) polls
    # CustomStrategy rows by status every tick during market hours, so a
    # row landing on PAPER_TRADING/LIVE here is picked up automatically.

    return DeploymentResponse(
        strategy_id=strategy.id,
        status=strategy.status,
        message=f"Strategy deployed to {request.mode} mode successfully",
        deployed_at=strategy.deployed_at.isoformat()
    )


@router.post("/pause/{strategy_id}")
def pause_strategy(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Pause a running strategy."""
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    if strategy.status not in ["PAPER_TRADING", "LIVE"]:
        raise HTTPException(
            status_code=400, 
            detail="Can only pause strategies in PAPER_TRADING or LIVE status"
        )
    
    closed = _square_off_open_positions(strategy, db)
    strategy.pre_pause_status = strategy.status
    strategy.status = "PAUSED"
    db.commit()
    db.refresh(strategy)

    return {
        "strategy_id": strategy_id,
        "status": "PAUSED",
        "message": f"Strategy paused successfully ({closed} open leg(s) squared off)." if closed else "Strategy paused successfully."
    }


@router.post("/resume/{strategy_id}")
def resume_strategy(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Resume a paused strategy."""
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    if strategy.status != "PAUSED":
        raise HTTPException(
            status_code=400,
            detail="Can only resume strategies in PAUSED status"
        )

    # Resume to the mode it was actually running in before it was paused
    # (recorded by pause_strategy()); legacy rows paused before this field
    # existed have no recorded mode, so fall back to paper trading.
    strategy.status = strategy.pre_pause_status or "PAPER_TRADING"
    strategy.pre_pause_status = None
    db.commit()
    db.refresh(strategy)

    # TODO: Signal the daemon to resume strategy execution

    return {
        "strategy_id": strategy_id,
        "status": strategy.status,
        "message": "Strategy resumed successfully"
    }


@router.post("/stop/{strategy_id}")
def stop_strategy(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Stop a strategy completely."""
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    if strategy.status not in ["PAPER_TRADING", "LIVE", "PAUSED"]:
        raise HTTPException(
            status_code=400, 
            detail="Strategy is not running"
        )
    
    closed = _square_off_open_positions(strategy, db)
    strategy.status = "STOPPED"
    db.commit()
    db.refresh(strategy)

    return {
        "strategy_id": strategy_id,
        "status": "STOPPED",
        "message": f"Strategy stopped successfully ({closed} open leg(s) squared off)." if closed else "Strategy stopped successfully."
    }


@router.get("/status/{strategy_id}")
def get_deployment_status(strategy_id: int, db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """Get current deployment status of a strategy."""
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    return {
        "strategy_id": strategy.id,
        "name": strategy.name,
        "status": strategy.status,
        "deployed_at": strategy.deployed_at.isoformat() if strategy.deployed_at else None,
        "backtest_return_pct": strategy.backtest_return_pct,
        "paper_return_pct": strategy.paper_return_pct,
        "live_return_pct": strategy.live_return_pct,
        "auto_roll": strategy.auto_roll,
        "auto_adjust": strategy.auto_adjust,
    }


@router.get("/active")
def list_active_deployments(db: Session = Depends(get_db), user: dict = Depends(get_current_user)):
    """List all actively deployed strategies (paper or live) owned by the caller."""
    strategies = db.query(CustomStrategy).filter(
        CustomStrategy.status.in_(["PAPER_TRADING", "LIVE", "PAUSED"]),
        CustomStrategy.user_id == _current_user_id(user),
    ).all()
    
    return {
        "strategies": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "instrument_type": s.instrument_type,
                "symbols": s.symbols,
                "deployed_at": s.deployed_at.isoformat() if s.deployed_at else None,
                "paper_return_pct": s.paper_return_pct,
                "live_return_pct": s.live_return_pct,
            }
            for s in strategies
        ]
    }


@router.patch("/performance/{strategy_id}")
def update_deployment_performance(
    strategy_id: int,
    paper_return_pct: Optional[float] = None,
    live_return_pct: Optional[float] = None,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Update performance metrics for a deployed strategy.

    This endpoint is called by the strategy execution system to update
    performance metrics as the strategy runs.
    """
    strategy = _get_owned_strategy(db, strategy_id, _current_user_id(user))

    if paper_return_pct is not None:
        strategy.paper_return_pct = paper_return_pct
    
    if live_return_pct is not None:
        strategy.live_return_pct = live_return_pct
    
    db.commit()
    db.refresh(strategy)
    
    return strategy.to_dict()
