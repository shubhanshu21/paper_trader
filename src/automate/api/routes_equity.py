"""
api/routes_equity.py — Equity position management endpoints.

Provides read access to open/closed equity positions and a manual close
endpoint. Uses the same security model as routes_positions.py (ORM queries,
no string SQL, parameterized access).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from automate.api.auth import get_current_user
from automate.config import EquityMACrossoverConfig
from automate.db.engine import get_session
from automate.db.models import EquityPosition

log = logging.getLogger("api.equity")
router = APIRouter(prefix="/api/equity", tags=["equity"])


def _scope_user_id(user: dict):
    """Admins also see system-wide (no-owner) rows — see EquityPosition.user_id's docstring."""
    return None if user.get("role") == "admin" else int(user["sub"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _position_with_live_price(pos_dict: dict, brokers: Optional[dict]) -> dict:
    """
    Attach mark-to-market data to an open equity position.

    current_price/unrealized_pnl are written to the DB by the /ws/positions
    background poller (ws_positions.py, every 3s) — this just serves the
    last value that poller wrote, which is what the caller wants for the
    very first page load before a WebSocket connection is established.
    If that poller hasn't run yet (e.g. right after server startup) these
    fields will be None until the first poll cycle completes.
    """
    return pos_dict


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _attach_display_symbols(positions: list[dict]) -> list[dict]:
    """
    EquityPosition.symbol stores the instrument_key (needed for broker LTP
    lookups), not a human-readable ticker — see utils/instrument_display.py.
    Batch-resolve once for the whole page instead of a query per row.
    """
    from automate.utils.instrument_display import resolve_display_symbols
    display_symbols = resolve_display_symbols(p["symbol"] for p in positions)
    for p in positions:
        p["display_symbol"] = display_symbols.get(p["symbol"], p["symbol"])
    return positions


@router.get("/positions/open")
def list_open_equity_positions(user: dict = Depends(get_current_user)):
    """List all currently open equity positions owned by the caller."""
    scoped_user_id = _scope_user_id(user)
    with get_session() as session:
        stmt = select(EquityPosition).where(EquityPosition.status == "OPEN")
        if scoped_user_id is not None:
            stmt = stmt.where(EquityPosition.user_id == scoped_user_id)
        rows = session.execute(stmt.order_by(EquityPosition.entry_date.desc())).scalars().all()
        positions = [r.to_dict() for r in rows]

    positions = _attach_display_symbols(positions)

    # Attach live MTM if brokers are available
    try:
        from automate.api.deps import get_brokers
        brokers = get_brokers()
        positions = [_position_with_live_price(p, brokers) for p in positions]
    except Exception:
        pass  # Degrade gracefully — price data is optional

    return positions


@router.get("/positions/closed")
def list_closed_equity_positions(limit: int = 100, user: dict = Depends(get_current_user)):
    """List the most recent closed equity positions owned by the caller (default last 100)."""
    scoped_user_id = _scope_user_id(user)
    with get_session() as session:
        stmt = select(EquityPosition).where(EquityPosition.status == "CLOSED")
        if scoped_user_id is not None:
            stmt = stmt.where(EquityPosition.user_id == scoped_user_id)
        rows = session.execute(stmt.order_by(EquityPosition.exit_date.desc()).limit(limit)).scalars().all()
        return _attach_display_symbols([r.to_dict() for r in rows])


@router.get("/watchlist")
def equity_watchlist():
    """Return the configured equity symbol watchlist and strategy params."""
    return {
        "symbols": EquityMACrossoverConfig.SYMBOLS,
        "strategy": "equity_ma_crossover",
        "mode": EquityMACrossoverConfig.MODE,
        "product": EquityMACrossoverConfig.PRODUCT,
        "short_window": EquityMACrossoverConfig.SHORT_WINDOW,
        "long_window": EquityMACrossoverConfig.LONG_WINDOW,
        "num_shares": EquityMACrossoverConfig.NUM_SHARES,
    }


@router.post("/positions/{position_id}/close")
def close_equity_position(position_id: int, user: dict = Depends(get_current_user)):
    """
    Manually close an open equity position.

    In paper mode: marks the position as closed at the last known price (or
    at entry price if no live price is available) — no real orders placed.
    In live mode: places a sell order via Upstox (requires valid access token).
    """
    with get_session() as session:
        pos = session.get(EquityPosition, position_id)
        if pos is None:
            raise HTTPException(status_code=404, detail=f"Equity position {position_id} not found")
        # Same 404-not-403 non-enumerable-ownership pattern used elsewhere —
        # admins may also close system-wide (no-owner) legacy rows.
        if pos.user_id is not None and pos.user_id != int(user["sub"]) and user.get("role") != "admin":
            raise HTTPException(status_code=404, detail=f"Equity position {position_id} not found")
        if pos.status != "OPEN":
            raise HTTPException(status_code=409, detail="Position is already closed")

        if pos.mode == "paper":
            # Paper close: use entry price as exit price (no live data required)
            # TODO: fetch live LTP for a more realistic paper P&L calculation
            exit_price = float(pos.entry_price)
            gross_pnl = (exit_price - float(pos.entry_price)) * pos.quantity
            if pos.direction == "SHORT":
                gross_pnl = -gross_pnl

            from datetime import date
            pos.status = "CLOSED"
            pos.exit_date = date.today().isoformat()
            pos.exit_price = exit_price
            pos.exit_reason = "MANUAL"
            pos.gross_pnl = gross_pnl
            pos.net_pnl = gross_pnl  # simplified — no charge model for equity yet
            pos.charges = 0.0

            result = pos.to_dict()
        else:
            # Live close — requires Upstox broker
            raise HTTPException(
                status_code=501,
                detail=(
                    "Live equity close via API not yet implemented. "
                    "Use the Upstox app or CLI to close live equity positions."
                ),
            )

    log.info(
        "Equity position %s closed manually: symbol=%s mode=%s exit_price=%s",
        position_id, pos.symbol, pos.mode, pos.exit_price,
    )
    return result
