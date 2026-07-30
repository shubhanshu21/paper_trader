"""
api/middleware/rate_limit.py — Production-grade rate limiting.

Provides configurable rate limiting using Redis for distributed
environments with sliding window algorithm.
"""
import os
import time
from typing import Dict, Any
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import redis

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DEFAULT_RATE_LIMIT = os.getenv("DEFAULT_RATE_LIMIT", "200/minute")
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"

# Initialize Redis client for distributed rate limiting
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    print("Warning: Redis not available for rate limiting, using in-memory fallback")

# Initialize limiter
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[DEFAULT_RATE_LIMIT],
    storage_uri=REDIS_URL if REDIS_AVAILABLE else "memory://",
    storage_options={"redis_url": REDIS_URL} if REDIS_AVAILABLE else {}
)


class RateLimitConfig:
    """Rate limit configuration for different endpoints."""
    
    LIMITS = {
        "auth": "5/minute",
        "api": "200/minute",
        "websocket": "100/minute",
        "backtest": "10/minute",
        "orders": "100/minute",
        "strategies": "50/minute",
    }
    
    @classmethod
    def get_limit(cls, endpoint_type: str) -> str:
        """Get rate limit for endpoint type."""
        return cls.LIMITS.get(endpoint_type, cls.LIMITS["api"])


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Custom rate limit exceeded handler."""
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Rate limit exceeded. Please try again later.",
                "details": {
                    "limit": exc.detail,
                    "path": request.url.path,
                    "method": request.method
                }
            }
        },
        headers={"Retry-After": "60"}
    )


class SlidingWindowRateLimiter:
    """Sliding window rate limiter using Redis."""
    
    def __init__(self, redis_client: redis.Redis, window_seconds: int = 60, max_requests: int = 200):
        self.redis = redis_client
        self.window_seconds = window_seconds
        self.max_requests = max_requests
    
    def is_allowed(self, key: str) -> tuple[bool, Dict[str, Any]]:
        """
        Check if request is allowed using sliding window algorithm.
        
        Returns (allowed, info_dict)
        """
        if not REDIS_AVAILABLE:
            return True, {"message": "Rate limiting disabled (Redis unavailable)"}
        
        current_time = time.time()
        window_start = current_time - self.window_seconds
        
        # Remove old entries outside the window
        self.redis.zremrangebyscore(key, 0, window_start)
        
        # Count current requests in window
        current_count = self.redis.zcard(key)
        
        if current_count >= self.max_requests:
            # Get oldest request time for retry calculation
            oldest = self.redis.zrange(key, 0, 0, withscores=True)
            retry_after = int(oldest[0][1] + self.window_seconds - current_time) if oldest else 60
            
            return False, {
                "limit": self.max_requests,
                "remaining": 0,
                "reset": int(oldest[0][1] + self.window_seconds) if oldest else int(current_time + self.window_seconds),
                "retry_after": retry_after
            }
        
        # Add current request
        self.redis.zadd(key, {str(current_time): current_time})
        self.redis.expire(key, self.window_seconds)
        
        return True, {
            "limit": self.max_requests,
            "remaining": self.max_requests - current_count - 1,
            "reset": int(current_time + self.window_seconds)
        }


# Global rate limiter instance
global_rate_limiter = SlidingWindowRateLimiter(
    redis_client if REDIS_AVAILABLE else None,
    window_seconds=60,
    max_requests=200
)


async def check_rate_limit(request: Request, limit: str = DEFAULT_RATE_LIMIT) -> None:
    """
    Check rate limit for a request.
    
    Args:
        request: FastAPI request object
        limit: Rate limit string (e.g., "200/minute")
    """
    if not RATE_LIMIT_ENABLED:
        return
    
    # Parse limit string
    try:
        max_requests, period = limit.split("/")
        max_requests = int(max_requests)
        
        # Convert period to seconds
        period_seconds = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400
        }.get(period, 60)
    except (ValueError, AttributeError):
        max_requests = 200
        period_seconds = 60
    
    # Create rate limiter for this request
    rate_limiter = SlidingWindowRateLimiter(
        redis_client if REDIS_AVAILABLE else None,
        window_seconds=period_seconds,
        max_requests=max_requests
    )
    
    # Get client identifier
    client_id = get_remote_address(request)
    key = f"ratelimit:{client_id}:{request.url.path}"
    
    # Check if allowed
    allowed, info = rate_limiter.is_allowed(key)
    
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Rate limit exceeded",
                "details": info
            },
            headers={"Retry-After": str(info.get("retry_after", 60))}
        )


def setup_rate_limiting(app):
    """Setup rate limiting for the FastAPI app."""
    if RATE_LIMIT_ENABLED:
        app.add_middleware(SlowAPIMiddleware, limiter=limiter)
        app.state.limiter = limiter
