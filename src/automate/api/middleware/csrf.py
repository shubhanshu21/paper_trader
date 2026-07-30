"""
api/middleware/csrf.py — CSRF protection middleware.

Provides Cross-Site Request Forgery protection for state-changing operations.
"""
import os
import secrets
from typing import Optional
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF protection middleware."""
    
    def __init__(self, app, csrf_secret: Optional[str] = None):
        super().__init__(app)
        self.csrf_secret = csrf_secret or os.getenv("CSRF_SECRET", secrets.token_hex(32))
        self.csrf_header = os.getenv("CSRF_HEADER", "X-CSRF-Token")
        self.csrf_cookie_name = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
        self.exempt_paths = self._get_exempt_paths()
    
    def _get_exempt_paths(self) -> list:
        """Get paths exempt from CSRF protection."""
        exempt = [
            "/health",
            "/api/health",
            "/api/auth/login",
            "/api/auth/register",
            "/api/auth/logout",
        ]
        return exempt
    
    async def dispatch(self, request: Request, call_next):
        """Process request with CSRF protection."""
        
        # Skip CSRF for GET, HEAD, OPTIONS, TRACE
        if request.method in ["GET", "HEAD", "OPTIONS", "TRACE"]:
            return await call_next(request)
        
        # Skip CSRF for exempt paths
        if any(request.url.path.startswith(path) for path in self.exempt_paths):
            return await call_next(request)
        
        # Skip CSRF for WebSocket upgrades
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        
        # Check CSRF token for state-changing methods
        csrf_token = request.headers.get(self.csrf_header)
        
        if not csrf_token:
            # Try to get from cookie
            csrf_token = request.cookies.get(self.csrf_cookie_name)
        
        if not csrf_token:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": {
                        "code": "CSRF_TOKEN_MISSING",
                        "message": "CSRF token is required for this request"
                    }
                }
            )
        
        # Validate CSRF token
        if not self._validate_csrf_token(csrf_token):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": {
                        "code": "CSRF_TOKEN_INVALID",
                        "message": "Invalid CSRF token"
                    }
                }
            )
        
        return await call_next(request)
    
    def _validate_csrf_token(self, token: str) -> bool:
        """Validate CSRF token."""
        # In production, this would validate against a session or Redis
        # For now, we'll use a simple validation
        try:
            # Token should be at least 32 characters
            if len(token) < 32:
                return False
            
            # Token should be alphanumeric
            if not token.isalnum():
                return False
            
            return True
        except Exception:
            return False
    
    def generate_csrf_token(self) -> str:
        """Generate a new CSRF token."""
        return secrets.token_urlsafe(32)


def get_csrf_token(request: Request) -> str:
    """Get CSRF token from request."""
    middleware = CSRFMiddleware(request.app)
    return middleware.generate_csrf_token()


def setup_csrf_protection(app):
    """Setup CSRF protection for the FastAPI app."""
    csrf_enabled = os.getenv("CSRF_ENABLED", "true").lower() == "true"
    
    if csrf_enabled:
        app.add_middleware(CSRFMiddleware)
