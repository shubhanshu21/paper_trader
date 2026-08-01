"""
api/websocket_manager.py — WebSocket manager for real-time market data streaming.

Handles WebSocket connections for live price updates, order status changes,
and other real-time events using FastAPI WebSocket support with Redis pub/sub.
"""
import json
import logging
import asyncio
from typing import Set, Dict, Any
from fastapi import WebSocket

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

log = logging.getLogger("api.websocket")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts messages with Redis pub/sub."""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        # Active connections: {connection_id: WebSocket}
        self.active_connections: Dict[str, WebSocket] = {}
        # Symbol subscriptions: {symbol: Set[connection_id]}
        self.symbol_subscriptions: Dict[str, Set[str]] = {}
        # Redis pub/sub
        self.redis_url = redis_url
        self.redis_client = None
        self.pubsub = None
        self._listener_task = None
    
    async def initialize_redis(self):
        """Initialize Redis connection and start pub/sub listener."""
        if not REDIS_AVAILABLE:
            log.warning("Redis not available, using in-memory pub/sub")
            return
        
        try:
            self.redis_client = await redis.from_url(self.redis_url, decode_responses=True)
            self.pubsub = self.redis_client.pubsub()
            await self.pubsub.subscribe("market_data")
            
            # Start background listener
            self._listener_task = asyncio.create_task(self._redis_listener())
            log.info("Redis pub/sub initialized")
        except Exception as e:
            log.error(f"Failed to initialize Redis: {e}")
            self.redis_client = None
    
    async def _redis_listener(self):
        """Background task to listen for Redis pub/sub messages."""
        if not self.pubsub:
            return
        
        while True:
            try:
                message = await self.pubsub.get_message(timeout=1.0)
                if message and message["type"] == "message":
                    data = json.loads(message["data"])
                    symbol = data.get("symbol")
                    if symbol:
                        await self.broadcast_to_symbol(symbol, data)
            except Exception as e:
                log.error(f"Redis listener error: {e}")
                await asyncio.sleep(1)
    
    async def connect(self, websocket: WebSocket, connection_id: str):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        self.active_connections[connection_id] = websocket
        log.info(f"WebSocket connected: {connection_id}")
    
    def disconnect(self, connection_id: str):
        """Remove a WebSocket connection and clean up subscriptions."""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
        
        # Remove from all symbol subscriptions
        for symbol in list(self.symbol_subscriptions.keys()):
            if connection_id in self.symbol_subscriptions[symbol]:
                self.symbol_subscriptions[symbol].remove(connection_id)
                if not self.symbol_subscriptions[symbol]:
                    del self.symbol_subscriptions[symbol]
        
        log.info(f"WebSocket disconnected: {connection_id}")
    
    async def subscribe_symbol(self, connection_id: str, symbol: str):
        """Subscribe a connection to a symbol's price updates."""
        if connection_id not in self.active_connections:
            return False
        
        if symbol not in self.symbol_subscriptions:
            self.symbol_subscriptions[symbol] = set()
        
        self.symbol_subscriptions[symbol].add(connection_id)
        log.info(f"Connection {connection_id} subscribed to {symbol}")
        return True
    
    async def unsubscribe_symbol(self, connection_id: str, symbol: str):
        """Unsubscribe a connection from a symbol's price updates."""
        if symbol in self.symbol_subscriptions and connection_id in self.symbol_subscriptions[symbol]:
            self.symbol_subscriptions[symbol].remove(connection_id)
            if not self.symbol_subscriptions[symbol]:
                del self.symbol_subscriptions[symbol]
            log.info(f"Connection {connection_id} unsubscribed from {symbol}")
    
    async def broadcast_to_symbol(self, symbol: str, message: Dict[str, Any]):
        """Broadcast a message to all connections subscribed to a symbol."""
        if symbol not in self.symbol_subscriptions:
            return
        
        message_str = json.dumps(message)
        disconnected = set()

        # Snapshot before iterating — connect()/disconnect() can mutate
        # symbol_subscriptions/active_connections while we're awaiting
        # send_text() below, which would otherwise raise RuntimeError:
        # Set changed size during iteration.
        for connection_id in list(self.symbol_subscriptions[symbol]):
            if connection_id in self.active_connections:
                try:
                    await self.active_connections[connection_id].send_text(message_str)
                except Exception as e:
                    log.error(f"Error sending to {connection_id}: {e}")
                    disconnected.add(connection_id)
        
        # Clean up disconnected connections
        for conn_id in disconnected:
            self.disconnect(conn_id)
    
    async def publish_to_redis(self, symbol: str, message: Dict[str, Any]):
        """Publish message to Redis for distribution across multiple servers."""
        if self.redis_client:
            try:
                await self.redis_client.publish("market_data", json.dumps(message))
            except Exception as e:
                log.error(f"Redis publish error: {e}")
    
    async def broadcast_to_all(self, message: Dict[str, Any]):
        """Broadcast a message to all active connections."""
        message_str = json.dumps(message)
        disconnected = set()

        # Snapshot before iterating — see broadcast_to_symbol() above for why.
        for connection_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.send_text(message_str)
            except Exception as e:
                log.error(f"Error broadcasting to {connection_id}: {e}")
                disconnected.add(connection_id)
        
        # Clean up disconnected connections
        for conn_id in disconnected:
            self.disconnect(conn_id)
    
    def get_connection_count(self) -> int:
        """Return the number of active connections."""
        return len(self.active_connections)
    
    def get_subscriber_count(self, symbol: str) -> int:
        """Return the number of subscribers for a symbol."""
        return len(self.symbol_subscriptions.get(symbol, set()))
    
    async def cleanup(self):
        """Cleanup resources on shutdown."""
        if self._listener_task:
            self._listener_task.cancel()
        if self.pubsub:
            await self.pubsub.close()
        if self.redis_client:
            await self.redis_client.close()


# Global connection manager instance
manager = ConnectionManager()
