"""
api/ws_positions.py — WebSocket endpoint pushing open-position (with MTM)
updates to every connected browser, so the dashboard reflects reality
within a few seconds without the client having to poll. Internally this is
still a poll (position/LTP data isn't event-driven anywhere in this
codebase) — the point of the WebSocket is that the CLIENT doesn't poll;
the server does the polling once and fans it out to however many browser
tabs are open.

Covers two independent position tables:
- Position (options short-strangle strategies) — MTM computed in-memory only.
- EquityPosition (manual trades + equity_ma_crossover) — live current_price/
  unrealized_pnl are also written back to the DB on every poll, since other
  read paths (routes_equity.py) serve from the DB and previously had no way
  to show a live price at all (no current_price/unrealized_pnl columns existed).
"""
import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from automate.api.auth import get_current_user_ws
from automate.api.deps import compute_mtm_economics, get_brokers
from automate.db.engine import get_session
from automate.db.models import EquityPosition
from automate.utils.position_tracker import get_open_positions

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 3.0


def _scope_user_id(user: dict):
    """Admins also see system-wide (no-owner) rows from the legacy CLI daemon — see Position.user_id's docstring."""
    return None if user.get("role") == "admin" else int(user["sub"])


def _snapshot_options(brokers, user: dict) -> list:
    from automate.utils.wallet import get_charge_rates

    positions = get_open_positions(user_id=_scope_user_id(user))
    # Admins viewing the system-wide (no-owner) feed get their OWN rates
    # applied to every row — a minor approximation for that one cross-account
    # view, same as elsewhere (see routes_positions.py).
    rates = get_charge_rates(int(user["sub"]))
    for pos in positions:
        econ = compute_mtm_economics(pos, brokers, rates)
        pos["mtm"] = None if econ is None else econ["gross_pnl"]
        pos["net_mtm"] = None if econ is None else econ["net_pnl"]
        pos["charges"] = None if econ is None else econ["charges"]
        pos["call_ltp"] = None if econ is None else econ["call_ltp"]
        pos["put_ltp"] = None if econ is None else econ["put_ltp"]
    return positions


def _snapshot_equity(brokers, user: dict) -> list:
    """
    Fetch live LTP for every OPEN equity position and persist current_price/
    unrealized_pnl back to the row, so any other read path (REST, future
    reloads) also sees the last-known live price, not just this socket.
    Batches LTP fetches per broker mode (one HTTP call for all paper-mode
    symbols, one for all live-mode symbols) instead of one call per position
    — same rationale as market_broadcaster.py's get_ltp_batch usage.
    """
    from automate.utils.instrument_display import resolve_display_symbols

    scoped_user_id = _scope_user_id(user)
    with get_session() as session:
        stmt = select(EquityPosition).where(EquityPosition.status == "OPEN")
        if scoped_user_id is not None:
            stmt = stmt.where(EquityPosition.user_id == scoped_user_id)
        rows = session.execute(stmt).scalars().all()

        ltp_by_mode: dict[str, dict[str, float]] = {}
        if brokers:
            by_mode: dict[str, list[str]] = {}
            for pos in rows:
                by_mode.setdefault(pos.mode, []).append(pos.symbol)
            for mode, symbols in by_mode.items():
                broker = brokers.get(mode)
                if broker is None:
                    continue
                try:
                    ltp_by_mode[mode] = broker.get_ltp_batch(symbols)
                except Exception as exc:
                    log.debug("Equity batch LTP fetch failed for mode=%s: %s", mode, exc)

        display_symbols = resolve_display_symbols(pos.symbol for pos in rows)

        result = []
        for pos in rows:
            ltp = ltp_by_mode.get(pos.mode, {}).get(pos.symbol)

            if ltp is not None:
                sign = -1 if pos.direction in ("SHORT", "SELL") else 1
                pos.current_price = ltp
                pos.unrealized_pnl = (ltp - float(pos.entry_price)) * pos.quantity * sign
                pos.price_updated_at = datetime.now(UTC)

            pos_dict = pos.to_dict()
            pos_dict["display_symbol"] = display_symbols.get(pos.symbol, pos.symbol)
            result.append(pos_dict)

        session.commit()
        return result


def _snapshot(user: dict) -> dict:
    brokers = get_brokers()
    return {
        "options": _snapshot_options(brokers, user),
        "equity": _snapshot_equity(brokers, user),
    }


@router.websocket("/ws/positions")
async def positions_ws(websocket: WebSocket):
    await websocket.accept()
    user = get_current_user_ws(websocket)
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return
    try:
        while True:
            snapshot = await asyncio.to_thread(_snapshot, user)
            await websocket.send_json({"type": "positions", **snapshot})
            await asyncio.sleep(_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/positions.")
