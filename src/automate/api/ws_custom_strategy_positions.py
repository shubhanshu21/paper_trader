"""
api/ws_custom_strategy_positions.py — pushes live LTP/P&L for every open
custom-strategy leg the logged-in user owns, over a WebSocket, same shape
as ws_custom_strategy_greeks.py: the SERVER polls broker LTPs on a timer,
the CLIENT just receives pushes — no client-side setInterval/polling
(the Positions page used to poll this on a 30s client-side timer, which
doesn't match the WebSocket-first convention the rest of the app uses).
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from automate.api.auth import get_current_user_ws
from automate.api.live_positions import compute_open_positions

log = logging.getLogger("api.ws")
router = APIRouter()

_POLL_INTERVAL_SEC = 3.0  # matches ws_positions.py's cadence for the legacy positions feed


@router.websocket("/ws/custom-strategy-positions")
async def custom_strategy_positions_ws(websocket: WebSocket):
    await websocket.accept()
    user = get_current_user_ws(websocket)
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return
    user_id = int(user["sub"])
    try:
        while True:
            rows = await asyncio.to_thread(compute_open_positions, user_id)
            await websocket.send_json({"type": "positions", "rows": rows})
            await asyncio.sleep(_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.debug("Client disconnected from /ws/custom-strategy-positions.")
