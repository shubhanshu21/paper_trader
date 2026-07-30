"""
api/routes_terminal.py — Manual Trading Terminal APIs.

Allows users to search all seeded NSE/MCX/BSE instruments, check live LTPs,
and execute manual paper/live trades with real-broker balance and margin checks.
All single-leg manual trades are persisted in the `equity_positions` table
under strategy_name = 'manual_trade'.
"""
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from automate.api.deps import get_brokers
from automate.db.engine import get_session
from automate.db.models import Instrument, EquityPosition

log = logging.getLogger("api.terminal")
router = APIRouter(prefix="/api/terminal", tags=["terminal"])


class ManualTradeRequest(BaseModel):
    instrument_key: str
    direction: str  # 'BUY' | 'SELL'
    quantity: int
    product: str = "CNC"  # 'CNC' | 'MIS' | 'NRML'
    mode: str = "paper"  # 'paper' | 'live'


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/search")
def search_instruments(
    query: str = Query("", description="Search by symbol or name"),
    exchange: str = Query("", description="Filter by exchange (e.g. NSE_EQ, NSE_FO, MCX_FO)"),
    type: str = Query("", description="Filter by instrument type (e.g. EQUITY, OPTSTK, FUTIDX)"),
    limit: int = 50,
):
    """Search for instruments in the seeded MySQL master table."""
    # Safety guards for programmatic calls that receive Query objects
    from fastapi.params import Query as FastAPIQuery
    if isinstance(query, FastAPIQuery) or not isinstance(query, str):
        query = ""
    if isinstance(exchange, FastAPIQuery) or not isinstance(exchange, str):
        exchange = ""
    if isinstance(type, FastAPIQuery) or not isinstance(type, str):
        type = ""

    with get_session() as session:
        stmt = select(Instrument)


        # Filters
        if query:
            words = query.strip().split()
            for word in words:
                if word:
                    q_pattern = f"%{word}%"
                    stmt = stmt.where(
                        (Instrument.symbol.like(q_pattern)) |
                        (Instrument.name.like(q_pattern))
                    )
        if exchange:
            stmt = stmt.where(Instrument.exchange == exchange)
        if type:
            stmt = stmt.where(Instrument.instrument_type == type)


        # Order and limit: prioritize exact prefix match of the first word (e.g. NIFTY over BANKNIFTY)
        from sqlalchemy import case
        order_clauses = []
        if query:
            words = query.strip().split()
            if words:
                first_word = words[0].strip().upper()
                order_clauses.append(
                    case(
                        (Instrument.symbol.like(f"{first_word}%"), 0),
                        else_=1
                    )
                )
        order_clauses.append(Instrument.symbol.asc())
        
        stmt = stmt.order_by(*order_clauses).limit(limit)
        results = session.execute(stmt).scalars().all()
        return [r.to_dict() for r in results]



@router.get("/ltp/{key}")
def get_instrument_ltp(key: str, mode: str = "paper"):
    """
    Fetch real-time LTP quote via the broker.
    Replaces ':' in the key back to '|' for path compatibility.
    """
    instrument_key = key.replace(":", "|")
    brokers = get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker client unavailable.")
    broker = brokers.get(mode)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Invalid broker mode: {mode}")

    try:
        ltp = broker.get_ltp(instrument_key)
        if ltp is None:
            raise HTTPException(status_code=404, detail="LTP quote unavailable.")
        return {"instrument_key": instrument_key, "ltp": ltp}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker quote error: {exc}")


@router.get("/depth/{key}")
def get_market_depth(key: str, mode: str = "paper"):
    """
    5-level bid/offer book + OHLC/volume/circuit-limit snapshot for the
    market depth panel. Returns zeroed-out depth (not a 404/502) when the
    broker has no depth data for this instrument — that's an expected,
    normal state (see BaseBroker.get_market_depth), not an error condition.
    """
    instrument_key = key.replace(":", "|")
    brokers = get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker client unavailable.")
    broker = brokers.get(mode)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Invalid broker mode: {mode}")

    depth = broker.get_market_depth(instrument_key)
    if depth is not None:
        return depth

    zero_level = {"price": 0.0, "qty": 0, "orders": 0}
    return {
        "instrument_key": instrument_key,
        "last_price": 0.0,
        "ohlc": {"open": None, "high": None, "low": None, "close": None},
        "volume": 0,
        "average_price": 0.0,
        "lower_circuit_limit": None,
        "upper_circuit_limit": None,
        "last_trade_time": None,
        "total_buy_quantity": 0,
        "total_sell_quantity": 0,
        "buy": [zero_level] * 5,
        "sell": [zero_level] * 5,
    }


@router.get("/positions")
def list_manual_positions():
    """List manual open and closed positions (strategy_name = 'manual_trade')."""
    with get_session() as session:
        open_pos = session.execute(
            select(EquityPosition).where(
                (EquityPosition.strategy_name == "manual_trade") &
                (EquityPosition.status == "OPEN")
            ).order_by(EquityPosition.entry_date.desc())
        ).scalars().all()

        closed_pos = session.execute(
            select(EquityPosition).where(
                (EquityPosition.strategy_name == "manual_trade") &
                (EquityPosition.status == "CLOSED")
            ).order_by(EquityPosition.exit_date.desc()).limit(100)
        ).scalars().all()

        return {
            "open": [p.to_dict() for p in open_pos],
            "closed": [p.to_dict() for p in closed_pos],
        }


@router.post("/trade")
def execute_manual_trade(req: ManualTradeRequest):
    """
    Place a manual buy/sell order on equities/options/futures/commodities.
    Maintains a FIFO/averaging position logic inside the `equity_positions` table.
    """
    # 1. Lookup instrument to get details (symbol name, tick size, lot size)
    with get_session() as session:
        inst = session.get(Instrument, req.instrument_key)
        if not inst:
            raise HTTPException(status_code=404, detail=f"Instrument {req.instrument_key} not found in master list.")

    # 2. Get broker client
    brokers = get_brokers()
    if not brokers:
        raise HTTPException(status_code=503, detail="Broker client unavailable.")
    broker = brokers.get(req.mode)
    if not broker:
        raise HTTPException(status_code=400, detail=f"Invalid broker mode: {req.mode}")

    # 3. Check quote
    ltp = broker.get_ltp(req.instrument_key)
    if ltp is None:
        raise HTTPException(status_code=503, detail="Cannot execute trade: LTP price quote unavailable.")

    log.info(
        "Manual trade request: %s | %s %d units | mode=%s LTP=%.2f",
        req.instrument_key, req.direction, req.quantity, req.mode, ltp
    )

    # 4. Check database for existing open manual position for this contract key
    with get_session() as session:
        pos = session.execute(
            select(EquityPosition).where(
                (EquityPosition.strategy_name == "manual_trade") &
                (EquityPosition.symbol == req.instrument_key) &
                (EquityPosition.mode == req.mode) &
                (EquityPosition.status == "OPEN")
            )
        ).scalar_one_or_none()

        action = "OPEN"
        # Opposite trade closes or reduces position
        if pos:
            open_direction = pos.direction
            # Same direction: add/average
            if open_direction == req.direction:
                action = "AVERAGE"
            else:
                action = "CLOSE" if req.quantity >= pos.quantity else "REDUCE"

        # 5. Place order via broker (runs balance/margin check in PaperBroker)
        try:
            if req.direction == "BUY":
                order_id = broker.place_buy_order(
                    instrument_token=req.instrument_key,
                    quantity=req.quantity,
                    product=req.product,
                    tag="MANUAL_BUY",
                )
            else:
                order_id = broker.place_sell_order(
                    instrument_token=req.instrument_key,
                    quantity=req.quantity,
                    product=req.product,
                    tag="MANUAL_SELL",
                )
        except RuntimeError as exc:
            # Shortfall exceptions raised by PaperBroker
            raise HTTPException(status_code=402, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Broker order submission failed: {exc}")

        if not order_id and not req.mode == "paper":
            raise HTTPException(status_code=502, detail="Broker rejected order placement (empty order ID).")

        order_id = order_id or f"PAPER-{req.direction[:1]}M-{req.instrument_key.split('|')[-1]}"

        # 6. Apply to DB position structure
        today = date.today().isoformat()
        if action == "OPEN":
            # Create new open position
            new_pos = EquityPosition(
                strategy_name="manual_trade",
                mode=req.mode,
                symbol=req.instrument_key,  # Store full token as symbol
                direction=req.direction,
                product=req.product,
                entry_date=today,
                entry_price=ltp,
                quantity=req.quantity,
                entry_order_id=order_id,
                status="OPEN",
            )
            session.add(new_pos)
            log.info("Opened new manual position for %s, ID=%s", req.instrument_key, order_id)
        elif action == "AVERAGE":
            # Increase quantity and average price
            total_cost = (float(pos.entry_price) * pos.quantity) + (ltp * req.quantity)
            total_qty = pos.quantity + req.quantity
            pos.entry_price = round(total_cost / total_qty, 4)
            pos.quantity = total_qty
            log.info("Averaged manual position for %s to qty=%d", req.instrument_key, total_qty)
        elif action == "CLOSE":
            # Full square off
            exit_price = ltp
            entry_price = float(pos.entry_price)
            gross_pnl = (exit_price - entry_price) * pos.quantity
            if pos.direction == "SHORT" or pos.direction == "SELL":
                gross_pnl = -gross_pnl

            # Round transaction charges
            charges = round((exit_price * pos.quantity) * 0.0003, 2)
            net_pnl = gross_pnl - charges

            pos.status = "CLOSED"
            pos.exit_date = today
            pos.exit_price = exit_price
            pos.exit_reason = "MANUAL_EXIT"
            pos.exit_order_id = order_id
            pos.gross_pnl = gross_pnl
            pos.net_pnl = net_pnl
            pos.charges = charges
            log.info("Closed manual position for %s, net P&L = %.2f", req.instrument_key, net_pnl)
        elif action == "REDUCE":
            # Partial exit: reduce qty but compute intermediate P&L
            # (In standard FIFO, we reduce quantity and realize P&L for the exited portion)
            exit_price = ltp
            entry_price = float(pos.entry_price)
            portion_qty = req.quantity
            portion_pnl = (exit_price - entry_price) * portion_qty
            if pos.direction == "SHORT" or pos.direction == "SELL":
                portion_pnl = -portion_pnl

            # Create a separate closed position row for the closed portion to log P&L correctly
            closed_portion = EquityPosition(
                strategy_name="manual_trade",
                mode=req.mode,
                symbol=req.instrument_key,
                direction=pos.direction,
                product=pos.product,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                quantity=portion_qty,
                entry_order_id=pos.entry_order_id,
                status="CLOSED",
                exit_date=today,
                exit_price=exit_price,
                exit_reason="MANUAL_PARTIAL",
                exit_order_id=order_id,
                gross_pnl=portion_pnl,
                net_pnl=portion_pnl - round((exit_price * portion_qty) * 0.0003, 2),
                charges=round((exit_price * portion_qty) * 0.0003, 2),
            )
            session.add(closed_portion)

            # Reduce remaining open position qty
            pos.quantity -= portion_qty
            log.info("Reduced manual position for %s by qty=%d", req.instrument_key, portion_qty)

    return {"status": "success", "action": action, "order_id": order_id}


@router.post("/positions/{position_id}/close")
def close_manual_position(position_id: int):
    """Directly exit / square off an active manual position by its database ID."""
    with get_session() as session:
        pos = session.get(EquityPosition, position_id)
        if pos is None:
            raise HTTPException(status_code=404, detail="Manual position not found.")
        if pos.status != "OPEN":
            raise HTTPException(status_code=400, detail="Position is already closed.")

        # Resolve order direction to execute order
        exit_dir = "SELL" if pos.direction == "BUY" or pos.direction == "LONG" else "BUY"

        # Place the opposite order via terminal trade logic
        req = ManualTradeRequest(
            instrument_key=pos.symbol,
            direction=exit_dir,
            quantity=pos.quantity,
            product=pos.product,
            mode=pos.mode
        )

    return execute_manual_trade(req)
