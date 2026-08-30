"""CapitalView API - Main entry point."""

import logging
import tomllib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Request, Response

_pyproject = Path(__file__).parent / "pyproject.toml"
with _pyproject.open("rb") as _f:
    __version__: str = tomllib.load(_f)["project"]["version"]
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sqlmodel import Session, select
from starlette.middleware.base import BaseHTTPMiddleware

from config import get_settings
from database import get_session, get_engine
from logging_config import request_id_var, setup_logging
from mcp_server import build_mcp_route, build_mcp_server
from models import User
from routes import (
    auth_router,
    api_tokens_router,
    bank_router,
    banking_router,
    cashflow_router,
    stocks_router,
    crypto_router,
    dashboard_router,
    notes_router,
    settings_router,
    notifications_router,
    asset_router,
    community_router,
    market_router,
    projection_router,
    imports_router,
    analytics_router,
)




@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    engine = get_engine()
    try:
        with Session(engine) as session:
            session.exec(select(1))
        logger.info("startup: database connection ok")
    except Exception as e:
        logger.error("startup: database connection failed: %s", e)

    # Start APScheduler for nightly price updates and banking consent health checks
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from services.market import update_all_prices_daily
    from services.banking.health import check_all_consents_daily

    scheduler = AsyncIOScheduler()
    scheduler.add_job(update_all_prices_daily, "cron", hour=23, minute=30, id="daily_price_update")
    scheduler.add_job(check_all_consents_daily, "cron", hour=2, minute=0, id="daily_bank_consent_check")
    scheduler.start()
    logger.info("startup: scheduler started (prices 23:30, bank consent 02:00)")

    if mcp_server is None:
        yield
    else:
        # The MCP app never gets its own lifespan run, so the host app
        # owns the session manager's task group. Skipping this makes the first
        # MCP request fail with "Task group is not initialized".
        async with mcp_server.session_manager.run():
            logger.info("startup: MCP server listening at %s", settings.mcp_path)
            yield

    scheduler.shutdown(wait=False)
    logger.info("shutdown complete")


settings = get_settings()

# Before anything else logs: uvicorn has already applied its own config by the
# time it imports this module, and this replaces it.
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

mcp_server = build_mcp_server() if settings.mcp_enabled else None

app = FastAPI(
    title=settings.app_name,
    description="Personal wealth management and investment tracking API",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return response


app.add_middleware(SecurityHeadersMiddleware)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Carry an id through every log line of one request.

    Honours an inbound X-Request-ID so a trace started by the frontend or a
    proxy keeps its identity, and echoes it back for the caller to quote.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response: Response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response


app.add_middleware(RequestIdMiddleware)


app.include_router(auth_router)
app.include_router(api_tokens_router)
app.include_router(bank_router)
app.include_router(banking_router)
app.include_router(cashflow_router)
app.include_router(stocks_router)
app.include_router(crypto_router)
app.include_router(dashboard_router)
app.include_router(notes_router)
app.include_router(settings_router)
app.include_router(notifications_router)
app.include_router(asset_router)
app.include_router(community_router)
app.include_router(market_router)
app.include_router(projection_router)
app.include_router(imports_router)
app.include_router(analytics_router)

if mcp_server is not None:
    # Its own ASGI app rather than a FastAPI route: the MCP endpoint speaks
    # JSON-RPC, and its bearer auth is unrelated to the session cookies the REST
    # routes use.
    app.router.routes.append(build_mcp_route(mcp_server))


@app.get("/")
def root():
    """Health check endpoint."""
    return {"status": "ok", "app": settings.app_name}


@app.get("/health")
def health():
    """Simple health check for container monitoring."""
    return {"status": "ok", "app": settings.app_name, "version": __version__}


@app.get("/health/db")
def health_db(session: Session = Depends(get_session)):
    """Check database connection.

    Answers 503 when the database is unreachable: an external monitor watches
    the HTTP status, and a 200 carrying {"status": "error"} reads as green.
    """
    try:
        session.exec(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception:
        return JSONResponse(
            status_code=503, content={"status": "error", "database": "unavailable"}
        )
