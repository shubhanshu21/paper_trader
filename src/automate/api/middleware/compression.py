"""
api/middleware/compression.py — API response compression middleware.

Provides gzip compression for API responses to reduce bandwidth
and improve performance.
"""
import gzip
import os
from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class CompressionMiddleware(BaseHTTPMiddleware):
    """Gzip compression middleware for API responses."""
    
    def __init__(
        self,
        app: ASGIApp,
        minimum_size: int = 500,  # Only compress responses larger than this
        compress_level: int = 6,  # Compression level (1-9)
    ):
        super().__init__(app)
        self.minimum_size = minimum_size
        self.compress_level = compress_level
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with compression."""
        response = await call_next(request)
        
        # Skip compression for already compressed responses
        if "content-encoding" in response.headers:
            return response
        
        # Skip compression for small responses
        if not response.body or len(response.body) < self.minimum_size:
            return response
        
        # Check if client accepts gzip
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response
        
        # Compress response body
        compressed_body = gzip.compress(response.body, compresslevel=self.compress_level)
        
        # Update response headers
        response.headers["content-encoding"] = "gzip"
        response.headers["content-length"] = str(len(compressed_body))
        response.body = compressed_body
        
        return response


def setup_compression(app):
    """Setup compression middleware for the FastAPI app."""
    compression_enabled = os.getenv("ENABLE_COMPRESSION", "true").lower() == "true"
    
    if compression_enabled:
        compress_level = int(os.getenv("COMPRESSION_LEVEL", "6"))
        app.add_middleware(
            CompressionMiddleware,
            minimum_size=500,
            compress_level=compress_level
        )
