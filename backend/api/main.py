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
    uvicorn api.main:app --host 127.0.0.1 --port 8000

For public-facing deployment (with auth enabled):
    PANEL_AUTH_ENABLED=true uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from api import (
    routes_adjustment_preview,
    routes_auth,
    routes_backtest,
    routes_chain_replay,
    routes_custom_strategies,
    routes_daemon,
    routes_dashboard,
    routes_equity,
    routes_health,
    routes_iv_screener,
    routes_leaderboard,
    routes_logs,
    routes_multi_leg,
    routes_notifications,
    routes_oauth,
    routes_oi_scanner,
    routes_performance,
    routes_positions,
    routes_simulator,
    routes_strategies,
    routes_terminal,
    routes_upstox_token,
    routes_wallet,
    routes_watchlist,
    routes_websocket,
    ws_custom_strategy_greeks,
    ws_custom_strategy_positions,
    ws_market_depth,
    ws_notifications,
    ws_positions,
)
from config import LogConfig, PanelAuthConfig
from utils.logger import setup_logger

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
# Background tasks lifespan
# ---------------------------------------------------------------------------
_background_tasks: set = set()


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Start long-running background tasks on startup (replaces the deprecated
    @app.on_event('startup') pattern removed in FastAPI 0.109+)."""
    import asyncio

    from api.bhavcopy_scheduler import bhavcopy_scheduler
    from api.instrument_sync_scheduler import instrument_sync_scheduler
    from api.iv_history_scheduler import iv_history_scheduler
    from api.market_broadcaster import market_price_broadcaster
    from api.strategy_scheduler import strategy_scheduler
    from api.token_refresh_scheduler import token_refresh_scheduler

    # run_daemon.py (the old cron/systemd-style CLI daemon) is retired —
    # it only ever ran hand-written strategies (strategies/registry.py,
    # now empty) via .env's STRATEGY_CONFIGS. All strategy execution now
    # goes through strategy_scheduler (DB-defined custom strategies,
    # built/backtested/deployed from the Strategies page — a single shared
    # loop dispatching to each strategy_type's own engine, see that
    # module's docstring). Daily Upstox token auto-login — the other thing
    # run_daemon.py used to do — moved to its own task
    # (token_refresh_scheduler) so retiring the daemon doesn't silently
    # break login too.
    # Tasks must be referenced for their lifetime — asyncio's event loop only
    # holds a weak reference, so an unreferenced task can be garbage-collected
    # mid-run (see asyncio.create_task docs). _background_tasks keeps them alive
    # and self-cleans via the done_callback.
    tasks = [
        asyncio.create_task(market_price_broadcaster()),
        asyncio.create_task(strategy_scheduler()),
        asyncio.create_task(token_refresh_scheduler()),
        asyncio.create_task(iv_history_scheduler()),
        asyncio.create_task(instrument_sync_scheduler()),
        asyncio.create_task(bhavcopy_scheduler()),
    ]
    _background_tasks.update(tasks)
    for task in tasks:
        task.add_done_callback(_background_tasks.discard)
    yield  # application is running
    # (shutdown cleanup would go here if needed)


# ---------------------------------------------------------------------------
# Rate limiter (shared across all routers via the app state)
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="Automate Control Panel API",
    lifespan=_lifespan,
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
        # Include both ws:// and wss:// so WebSocket connections work over
        # plain HTTP (dev) and HTTPS (production) without separate config.
        "connect-src 'self' ws://127.0.0.1:* ws://localhost:* wss://127.0.0.1:* wss://localhost:*; "
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
# CSRF protection (double-submit cookie) — api/auth.py::validate_csrf already
# implements the check; csrf_protection_middleware below is what actually
# calls it on every request. Applies to every state-changing /api/* request
# except the handful that happen BEFORE a real session cookie exists
# (login/register/mfa verify-login) or that the frontend can't attach a
# custom header to (OAuth's GET browser-redirect endpoints — already exempt
# via the safe-method check, since browsers can't add headers to a
# top-level navigation). The frontend (frontend/src/api.ts::request())
# already sends X-CSRF-Token on every POST/PUT/DELETE/PATCH whenever the
# csrf_token cookie exists — it was just never verified server-side.
#
# _csrf_exempt() is a plain function (not a closure inside the
# PanelAuthConfig.ENABLED branch below) specifically so it's directly unit-
# testable regardless of which config state happened to be active when this
# module was imported — see tests/test_csrf_middleware.py.
# ---------------------------------------------------------------------------
CSRF_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/logout",  # see logout()'s own docstring — a CSRF-forced logout is only a DoS, not data modification
    "/api/auth/mfa/verify-login",
}


def _csrf_exempt(method: str, path: str) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if not path.startswith("/api/"):
        return True
    return path in CSRF_EXEMPT_PATHS


# ---------------------------------------------------------------------------
# Optional auth gate
# When PANEL_AUTH_ENABLED=true: all /api/* routes except /api/auth/* and
# /api/health require a valid session cookie, and CSRF is enforced on every
# state-changing request (see _csrf_exempt above).
# ---------------------------------------------------------------------------
if PanelAuthConfig.ENABLED:
    log.info("Panel auth is ENABLED — all API routes require login.")


    @app.middleware("http")
    async def require_auth_middleware(request: Request, call_next) -> Response:
        path = request.url.path
        # Allow auth routes and health check without authentication
        if path.startswith("/api/auth/") or path in ("/api/health", "/api/market-status"):
            return await call_next(request)
        # Allow frontend static assets (non-/api paths)
        if not path.startswith("/api/") and not path.startswith("/ws/"):
            return await call_next(request)
        # Validate session cookie
        from jose import JWTError

        from api.auth import _token_version_is_current, decode_access_token
        session_cookie = request.cookies.get("__Host-session")
        if not session_cookie:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
            )
        try:
            payload = decode_access_token(session_cookie)
        except JWTError:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Session expired or invalid"},
            )
        # Also check the 'tv' (token_version) claim, same as
        # get_current_user_optional() — otherwise a route with no
        # Depends(get_current_user) of its own stays reachable with a
        # token that "log out everywhere"/account deactivation was meant
        # to revoke, until natural expiry.
        if payload.get("purpose") == "mfa_pending" or not _token_version_is_current(payload):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Session expired or invalid"},
            )
        return await call_next(request)

    @app.middleware("http")
    async def csrf_protection_middleware(request: Request, call_next) -> Response:
        if _csrf_exempt(request.method, request.url.path):
            return await call_next(request)

        from fastapi import HTTPException

        from api.auth import validate_csrf
        try:
            validate_csrf(request, request.cookies.get("csrf_token"))
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
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
app.include_router(routes_oauth.router)
app.include_router(routes_positions.router)
app.include_router(routes_strategies.router)
app.include_router(routes_daemon.router)
app.include_router(routes_backtest.router)
app.include_router(routes_logs.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_leaderboard.router)
app.include_router(routes_iv_screener.router)
app.include_router(routes_oi_scanner.router)
app.include_router(routes_chain_replay.router)
app.include_router(routes_simulator.router)
app.include_router(routes_wallet.router)
app.include_router(routes_wallet.orders_router)
app.include_router(routes_equity.router)
app.include_router(routes_terminal.router)
app.include_router(routes_watchlist.router)
app.include_router(routes_websocket.router)
app.include_router(routes_multi_leg.router)
app.include_router(routes_performance.router)
app.include_router(routes_custom_strategies.router)
app.include_router(routes_adjustment_preview.router)
app.include_router(routes_upstox_token.router)
app.include_router(routes_health.router)
app.include_router(ws_positions.router)
app.include_router(ws_custom_strategy_greeks.router)
app.include_router(ws_custom_strategy_positions.router)
app.include_router(ws_market_depth.router)
app.include_router(routes_notifications.router)
app.include_router(ws_notifications.router)



@app.get("/api/health")
def health():
    auth_status = "enabled" if PanelAuthConfig.ENABLED else "disabled"
    return {"ok": True, "auth": auth_status}


@app.get("/api/market-status")
def market_status():
    """
    Whether NSE F&O is open right now — same real check (weekday, holiday
    calendar, 09:15-15:30 IST session window) every scheduler already uses
    before entering/exiting a position, exposed so the UI can show it too
    instead of leaving "will my strategy trade today?" a mystery. No auth
    required (see require_auth_middleware's exemption below) — this is as
    non-sensitive as /api/health, and the login page can use it too.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from compliance.sebi_rules import assert_market_is_open

    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    try:
        assert_market_is_open()
        return {"open": True, "message": "Market is open", "server_time_ist": now_ist.isoformat()}
    except RuntimeError as exc:
        return {"open": False, "message": str(exc), "server_time_ist": now_ist.isoformat()}


# ---------------------------------------------------------------------------
# Serve built React frontend (production)
# ---------------------------------------------------------------------------
from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"  # backend/api/main.py -> up 3 to the TRUE repo root (frontend/ is a sibling of backend/, not inside it)

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

