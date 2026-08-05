"""api/routes_wallet.py — virtual paper-trading wallet, funds statement, order book."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import get_current_user
from utils.costs import DEFAULT_RATES
from utils.orders import get_order_book
from utils.wallet import (
    get_charge_rates,
    get_ledger,
    get_wallet_summary,
    set_charge_rates,
    set_starting_capital,
)
from utils.wallet_adjustments import add_adjustment

router = APIRouter(prefix="/api/wallet", tags=["wallet"])
orders_router = APIRouter(prefix="/api/orders", tags=["orders"])


class AdjustmentRequest(BaseModel):
    amount: float
    note: str = ""


class CapitalRequest(BaseModel):
    starting_capital: float


class ChargeRatesRequest(BaseModel):
    """Any field left out (None) is reset to the codebase default for that component."""
    brokerage_per_order: float | None = None
    exchange_charge_pct: float | None = None
    gst_pct: float | None = None
    stt_pct: float | None = None
    sebi_charge_pct: float | None = None
    stamp_duty_pct: float | None = None


@router.get("")
def wallet_summary(user: dict = Depends(get_current_user)):
    return get_wallet_summary(int(user["sub"]))


@router.get("/ledger")
def wallet_ledger(user: dict = Depends(get_current_user)):
    return get_ledger(int(user["sub"]))


@router.post("/adjust")
def wallet_adjust(req: AdjustmentRequest, user: dict = Depends(get_current_user)):
    user_id = int(user["sub"])
    try:
        add_adjustment(user_id, req.amount, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_wallet_summary(user_id)


@router.post("/capital")
def wallet_set_capital(req: CapitalRequest, user: dict = Depends(get_current_user)):
    user_id = int(user["sub"])
    try:
        set_starting_capital(user_id, req.starting_capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_wallet_summary(user_id)


@router.get("/charge-rates")
def wallet_get_charge_rates(user: dict = Depends(get_current_user)):
    """
    This account's effective F&O transaction-cost rates (any per-user
    override layered on the codebase defaults) plus the defaults
    themselves, so the Profile page can show what "reset to default" would
    revert each field to.
    """
    return {"rates": get_charge_rates(int(user["sub"])), "defaults": DEFAULT_RATES}


@router.post("/charge-rates")
def wallet_set_charge_rates(req: ChargeRatesRequest, user: dict = Depends(get_current_user)):
    """
    Persist per-user overrides for the transaction-cost formula (see
    utils/costs.py) — lets the app be kept in sync with NSE/SEBI/govt rate
    changes from the Profile page, without a code deploy. A field omitted
    from the request body resets that component back to the codebase
    default (see ChargeRatesRequest).
    """
    user_id = int(user["sub"])
    set_charge_rates(user_id, req.model_dump())
    return {"rates": get_charge_rates(user_id), "defaults": DEFAULT_RATES}


@router.post("/reset")
def wallet_reset(user: dict = Depends(get_current_user)):
    """
    Wipes THIS ACCOUNT's own paper-trading state (positions, equity
    positions, custom-strategy legs, wallet balance/adjustments) — never
    another user's data, and never shared reference data (BacktestRun
    history, the bhavcopy market-data cache), which the reset used
    to also delete globally regardless of who asked for it.
    """
    from db.engine import get_session
    from db.models import (
        CustomStrategy,
        CustomStrategyPosition,
        EquityPosition,
        Position,
        WalletSettings,
    )
    from utils.wallet_adjustments import clear_adjustments

    user_id = int(user["sub"])

    # 1. Clear this account's manual balance adjustments file
    clear_adjustments(user_id)

    # 2. Delete only this account's own rows
    with get_session() as session:
        deleted_positions = session.query(Position).filter_by(user_id=user_id).delete(synchronize_session=False)
        deleted_equity = session.query(EquityPosition).filter_by(user_id=user_id).delete(synchronize_session=False)

        own_strategy_ids = [s.id for s in session.query(CustomStrategy.id).filter_by(user_id=user_id).all()]
        deleted_custom_legs = 0
        if own_strategy_ids:
            deleted_custom_legs = session.query(CustomStrategyPosition).filter(
                CustomStrategyPosition.strategy_id.in_(own_strategy_ids)
            ).delete(synchronize_session=False)
            # Positions are gone, so any "already entered this cycle" marker
            # left on the strategy is now stale — without this, the
            # scheduler's cycle-gate (custom_strategy_scheduler.py's
            # _get_last_entered_expiry check) sees the old expiry still
            # recorded and skips re-entry for the rest of that expiry's
            # cycle, even though there's nothing actually open anymore.
            session.query(CustomStrategy).filter(
                CustomStrategy.id.in_(own_strategy_ids)
            ).update({CustomStrategy.last_entry_date: None}, synchronize_session=False)

        # Reset this account's own starting capital baseline to 0
        row = session.query(WalletSettings).filter_by(user_id=user_id).first()
        if row:
            row.starting_capital = 0
        else:
            session.add(WalletSettings(user_id=user_id, starting_capital=0))

    return {
        "deleted_positions": deleted_positions,
        "deleted_equity": deleted_equity,
        "deleted_custom_strategy_legs": deleted_custom_legs,
        **get_wallet_summary(user_id)
    }


@orders_router.get("")
def order_book(mode: str | None = None, limit: int = 200, user: dict = Depends(get_current_user)):
    return get_order_book(mode=mode, limit=limit, user_id=int(user["sub"]))
