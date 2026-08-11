"""
api/routes_websocket.py — WebSocket endpoints for real-time market data.

Provides WebSocket endpoints for live price streaming, order status updates,
and other real-time trading events.
"""
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from api.auth import get_current_user_ws
from api.websocket_manager import manager

log = logging.getLogger("api.websocket")
router = APIRouter()


@router.websocket("/ws/market")
async def websocket_market_data(
    websocket: WebSocket,
    symbols: str = Query(..., description="Comma-separated list of symbols to subscribe")
):
    """
    WebSocket endpoint for real-time market data streaming.

    Subscribe to live price updates for specified symbols.
    Messages are broadcast when prices change.
    """
    await websocket.accept()
    if get_current_user_ws(websocket) is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return

    connection_id = str(uuid.uuid4())
    manager.active_connections[connection_id] = websocket
    log.info(f"WebSocket connected: {connection_id}")
    
    # Parse symbols and subscribe. Deliberately NOT .upper()'d — instrument_key
    # is a structured 'EXCHANGE|identifier' string, not a plain ticker, and
    # Upstox's own index keys are case-sensitive ('NSE_INDEX|Nifty 50', not
    # 'NIFTY 50') — uppercasing silently broke every index/anything with a
    # non-ISIN identifier, since the mangled key never matches a real quote.
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    for symbol in symbol_list:
        await manager.subscribe_symbol(connection_id, symbol)
    
    try:
        # Send initial connection confirmation
        await websocket.send_json({
            "type": "connected",
            "connection_id": connection_id,
            "subscribed_symbols": symbol_list
        })
        
        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "subscribe":
                new_symbols = data.get("symbols", [])
                for symbol in new_symbols:
                    await manager.subscribe_symbol(connection_id, symbol)

            elif data.get("type") == "unsubscribe":
                symbols_to_remove = data.get("symbols", [])
                for symbol in symbols_to_remove:
                    await manager.unsubscribe_symbol(connection_id, symbol)
            
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        manager.disconnect(connection_id)
        log.info(f"WebSocket disconnected: {connection_id}")


@router.websocket("/ws/orders")
async def websocket_orders(websocket: WebSocket):
    """
    WebSocket endpoint for real-time order status updates.
    
    Subscribe to order status changes, fills, and execution updates.
    """
    await websocket.accept()
    if get_current_user_ws(websocket) is None:
        await websocket.send_json({"type": "error", "detail": "Not authenticated"})
        await websocket.close()
        return

    connection_id = str(uuid.uuid4())
    manager.active_connections[connection_id] = websocket
    log.info(f"WebSocket connected: {connection_id}")

    try:
        await websocket.send_json({
            "type": "connected",
            "connection_id": connection_id,
            "channel": "orders"
        })
        
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        manager.disconnect(connection_id)
        log.info(f"Orders WebSocket disconnected: {connection_id}")


@router.get("/ws/stats")
def websocket_stats():
    """Get WebSocket connection statistics."""
    return {
        "active_connections": manager.get_connection_count(),
        "symbol_subscriptions": {
            symbol: manager.get_subscriber_count(symbol)
            for symbol in manager.symbol_subscriptions
        }
    }
