"""
api/ws_custom_strategy_greeks.py — pushes live Black-76 Greeks for one
custom strategy's open legs over a WebSocket, same shape as ws_positions.py:
the SERVER polls broker LTPs on a timer, the CLIENT just receives pushes —
no client-side setInterval/polling.
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import get_current_user_ws
from api.live_greeks import compute_live_greeks

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 5.0


@router.websocket("/ws/custom-strategy-greeks/{strategy_id}")
async def custom_strategy_greeks_ws(websocket: WebSocket, strategy_id: int):
    await websocket.accept()
    user = get_current_user_ws(websocket)
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return
    owner_user_id = int(user["sub"])
    try:
        while True:
            payload = await asyncio.to_thread(compute_live_greeks, strategy_id, owner_user_id)
            if payload is None:
                await websocket.send_json({"type": "error", "detail": "Strategy not found."})
                break
            await websocket.send_json({"type": "greeks", **payload})
            await asyncio.sleep(_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/custom-strategy-greeks/%s.", strategy_id)
