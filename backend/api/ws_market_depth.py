"""
api/ws_market_depth.py — pushes the 5-level bid/offer book for ONE
instrument over a WebSocket while a client has it open (MarketDepthModal.tsx),
same shape as ws_custom_strategy_greeks.py: the SERVER polls the broker on
a timer, the CLIENT only ever receives pushes — replaces what used to be
a 2s client-side REST poll (routes_terminal.py's GET /depth/{key} is kept
for one-off/manual inspection only, same "REST for one-off, WS for live"
split every other live surface in this app already uses).

Deliberately its OWN per-connection poll loop (not routed through the
broadcast-to-many ConnectionManager /ws/market already uses for LTP) —
depth is a heavier payload only the one viewer with the modal open needs,
broadcasting it to every symbol subscriber would be wasteful.
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import get_current_user_ws
from api.deps import get_brokers

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 2.0

_ZERO_LEVEL = {"price": 0.0, "qty": 0, "orders": 0}


def _zeroed_depth(instrument_key: str) -> dict:
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
        "buy": [_ZERO_LEVEL] * 5,
        "sell": [_ZERO_LEVEL] * 5,
    }


def _fetch_depth(instrument_key: str, mode: str) -> dict:
    brokers = get_brokers()
    if not brokers:
        return _zeroed_depth(instrument_key)
    broker = brokers.get(mode)
    if not broker:
        return _zeroed_depth(instrument_key)
    depth = broker.get_market_depth(instrument_key)
    return depth if depth is not None else _zeroed_depth(instrument_key)


@router.websocket("/ws/market-depth/{key}")
async def market_depth_ws(websocket: WebSocket, key: str, mode: str = "paper"):
    await websocket.accept()
    user = get_current_user_ws(websocket)
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return
    instrument_key = key.replace(":", "|")
    try:
        while True:
            depth = await asyncio.to_thread(_fetch_depth, instrument_key, mode)
            await websocket.send_json({"type": "depth", **depth})
            await asyncio.sleep(_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/market-depth/%s.", key)
