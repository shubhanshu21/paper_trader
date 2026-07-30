"""api/routes_wallet.py — virtual paper-trading wallet, funds statement, order book."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from automate.utils.orders import get_order_book
from automate.utils.wallet import get_ledger, get_wallet_summary, set_starting_capital
from automate.utils.wallet_adjustments import add_adjustment

router = APIRouter(prefix="/api/wallet", tags=["wallet"])
orders_router = APIRouter(prefix="/api/orders", tags=["orders"])


class AdjustmentRequest(BaseModel):
    amount: float
    note: str = ""


class CapitalRequest(BaseModel):
    starting_capital: float


@router.get("")
def wallet_summary():
    return get_wallet_summary()


@router.get("/ledger")
def wallet_ledger():
    return get_ledger()


@router.post("/adjust")
def wallet_adjust(req: AdjustmentRequest):
    try:
        add_adjustment(req.amount, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return get_wallet_summary()


@router.post("/capital")
def wallet_set_capital(req: CapitalRequest):
    try:
        set_starting_capital(req.starting_capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return get_wallet_summary()


@router.post("/reset")
def wallet_reset():
    """Wipes the database clean regarding everything except bhavcopy data."""
    from automate.db.engine import get_session
    from automate.db.models import Position, BacktestRun, Candle, EquityPosition, WalletSettings
    from automate.utils.wallet_adjustments import clear_adjustments

    # 1. Clear manual balance adjustments file
    clear_adjustments()

    # 2. Truncate/delete all database tables except fno_bhavcopy
    with get_session() as session:
        deleted_positions = session.query(Position).delete(synchronize_session=False)
        deleted_equity = session.query(EquityPosition).delete(synchronize_session=False)
        deleted_backtests = session.query(BacktestRun).delete(synchronize_session=False)
        deleted_candles = session.query(Candle).delete(synchronize_session=False)

        # Reset starting capital baseline to 0
        row = session.get(WalletSettings, 1)  # _SETTINGS_ROW_ID = 1
        if row:
            row.starting_capital = 0
        else:
            session.add(WalletSettings(id=1, starting_capital=0))

    return {
        "deleted_positions": deleted_positions,
        "deleted_equity": deleted_equity,
        "deleted_backtests": deleted_backtests,
        "deleted_candles": deleted_candles,
        **get_wallet_summary()
    }


@orders_router.get("")
def order_book(mode: Optional[str] = None, limit: int = 200):
    return get_order_book(mode=mode, limit=limit)
