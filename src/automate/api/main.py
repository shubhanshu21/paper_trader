"""
api/main.py — FastAPI control-panel backend.

Security additions vs. original:
- Security headers middleware (X-Content-Type-Options, X-Frame-Options, CSP,
  Permissions-Policy).
- Optional auth gate: when PANEL_AUTH_ENABLED=true, all /api/* routes (except
  /api/auth/* and /api/health) require a valid '__Host-session' JWT cookie.
- Rate limiting on /api/auth/login and /api/auth/register (via slowapi).
- CORS restricted to localhost origins only (unchanged).

Run (from the repo root, inside .venv):
    uvicorn automate.api.main:app --host 127.0.0.1 --port 8000

For public-facing deployment (with auth enabled):
    PANEL_AUTH_ENABLED=true uvicorn automate.api.main:app --host 0.0.0.0 --port 8000
"""
import logging
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from automate.api import (
    routes_positions, routes_strategies, routes_daemon,
    routes_backtest, routes_logs, routes_dashboard,
    routes_leaderboard, routes_wallet, ws_positions,
    routes_auth, routes_equity, routes_terminal,
    routes_watchlist, routes_orders, routes_websocket,
    routes_advanced_orders, routes_multi_leg, routes_performance,
    routes_custom_strategies, routes_strategy_deployment, routes_health,
    ws_custom_strategy_greeks, routes_notifications, ws_notifications,
    ws_custom_strategy_positions, ws_market_depth,
)
from automate.config import LogConfig, PanelAuthConfig
from automate.utils.logger import setup_logger

# Every module in this app calls utils.logger.get_logger(__name__), which is
# just logging.getLogger(name) — it attaches no handlers itself and relies
# entirely on propagation to the root logger. Nothing configured the root
# logger before this, so every log.info()/log.debug() call across the whole
# API process (scheduler startup messages, "token refreshed automatically",
# etc.) was silently dropped — only WARNING+ ever appeared, via Python's
# handler-of-last-resort. Console-only (no log_file) since this process's
# stdout is already captured to logs/api.log by whatever runs uvicorn; a
# second TimedRotatingFileHandler on the same path would double-write it.
setup_logger(name="", level=LogConfig.LEVEL)

log = logging.getLogger("api")

# ---------------------------------------------------------------------------
# Rate limiter (shared across all routers via the app state)
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="Automate Control Panel API",
    # Disable automatic OpenAPI/docs exposure in production if auth is enabled
    docs_url=None if PanelAuthConfig.ENABLED else "/docs",
    redoc_url=None if PanelAuthConfig.ENABLED else "/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# CORS — localhost-only origins (unchanged from original design)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ],
    allow_credentials=True,   # required for cookie-based auth
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token", "Authorization"],
)

# ---------------------------------------------------------------------------
# Security headers middleware
# Adds baseline hardening headers to every response.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    # Prevent MIME-type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"
    # CSP — allow self for scripts/styles; restrict everything else
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "   # React inline styles require this
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
        "frame-ancestors 'none'; "
        "object-src 'none';"
    )
    # Disable unused browser features
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # Don't cache sensitive API responses
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

# ---------------------------------------------------------------------------
# Optional auth gate
# When PANEL_AUTH_ENABLED=true: all /api/* routes except /api/auth/* and
# /api/health require a valid session cookie.
# ---------------------------------------------------------------------------
if PanelAuthConfig.ENABLED:
    log.info("Panel auth is ENABLED — all API routes require login.")


    @app.middleware("http")
    async def require_auth_middleware(request: Request, call_next) -> Response:
        path = request.url.path
        # Allow auth routes and health check without authentication
        if path.startswith("/api/auth/") or path == "/api/health":
            return await call_next(request)
        # Allow frontend static assets (non-/api paths)
        if not path.startswith("/api/") and not path.startswith("/ws/"):
            return await call_next(request)
        # Validate session cookie
        from jose import JWTError
        from automate.api.auth import decode_access_token
        session_cookie = request.cookies.get("__Host-session")
        if not session_cookie:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
        try:
            decode_access_token(session_cookie)
        except JWTError:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Session expired or invalid"},
            )
        return await call_next(request)
else:
    log.info(
        "Panel auth is DISABLED (PANEL_AUTH_ENABLED not set). "
        "Set PANEL_AUTH_ENABLED=true to require login."
    )

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(routes_auth.router)
app.include_router(routes_positions.router)
app.include_router(routes_strategies.router)
app.include_router(routes_daemon.router)
app.include_router(routes_backtest.router)
app.include_router(routes_logs.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_leaderboard.router)
app.include_router(routes_wallet.router)
app.include_router(routes_wallet.orders_router)
app.include_router(routes_equity.router)
app.include_router(routes_terminal.router)
app.include_router(routes_watchlist.router)
app.include_router(routes_orders.router)
app.include_router(routes_websocket.router)
app.include_router(routes_advanced_orders.router)
app.include_router(routes_multi_leg.router)
app.include_router(routes_performance.router)
app.include_router(routes_custom_strategies.router)
app.include_router(routes_strategy_deployment.router)
app.include_router(routes_health.router)
app.include_router(ws_positions.router)
app.include_router(ws_custom_strategy_greeks.router)
app.include_router(ws_custom_strategy_positions.router)
app.include_router(ws_market_depth.router)
app.include_router(routes_notifications.router)
app.include_router(ws_notifications.router)



@app.on_event("startup")
async def _start_background_tasks():
    import asyncio
    from automate.api.market_broadcaster import market_price_broadcaster
    from automate.api.custom_strategy_scheduler import custom_strategy_scheduler
    from automate.api.token_refresh_scheduler import token_refresh_scheduler
    from automate.api.advanced_orders_scheduler import advanced_orders_scheduler

    # run_daemon.py (the old cron/systemd-style CLI daemon) is retired —
    # it only ever ran hand-written strategies (strategies/registry.py,
    # now empty) via .env's STRATEGY_CONFIGS. All strategy execution now
    # goes through custom_strategy_scheduler (DB-defined custom strategies,
    # built/backtested/deployed from the Strategies page). Daily Upstox
    # token auto-login — the other thing run_daemon.py used to do — moved
    # to its own task (token_refresh_scheduler) so retiring the daemon
    # doesn't silently break login too.
    asyncio.create_task(market_price_broadcaster())
    asyncio.create_task(custom_strategy_scheduler())
    asyncio.create_task(token_refresh_scheduler())
    asyncio.create_task(advanced_orders_scheduler())


@app.get("/api/health")
def health():
    auth_status = "enabled" if PanelAuthConfig.ENABLED else "disabled"
    return {"ok": True, "auth": auth_status}


# ---------------------------------------------------------------------------
# Serve built React frontend (production)
# ---------------------------------------------------------------------------
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")

    # Catch 404s for client-side React Router paths (e.g., reloading on /terminal or /equity)
    @app.exception_handler(StarletteHTTPException)
    async def spa_route_fallback(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            path = request.url.path
            # Do not hijack API or WebSocket routes
            if not path.startswith("/api/") and not path.startswith("/ws/"):
                index_file = _FRONTEND_DIST / "index.html"
                if index_file.exists():
                    return HTMLResponse(content=index_file.read_text(), status_code=200)
        
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)

