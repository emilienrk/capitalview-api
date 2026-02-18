"""CapitalView API - Main entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlmodel import Session, select
from starlette.middleware.base import BaseHTTPMiddleware

from config import get_settings
from database import get_session, get_engine
from models import User
from routes import (
    auth_router,
    bank_router,
    cashflow_router,
    stocks_router,
    crypto_router,
    dashboard_router,
    notes_router,
    settings_router,
)


def rate_limit_key_func(request: Request):
    """
    Key function for rate limiting.
    Skip rate limiting for OPTIONS requests (CORS preflight).
    """
    if request.method == "OPTIONS":
        return None
    return get_remote_address(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    engine = get_engine()
    try:
        with Session(engine) as session:
            session.exec(select(1))
        print("✅ Database connection successful!")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
    
    yield
    print("👋 Shutting down...")


settings = get_settings()

limiter = Limiter(key_func=rate_limit_key_func)

app = FastAPI(
    title=settings.app_name,
    description="Personal wealth management and investment tracking API",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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


app.include_router(auth_router)
app.include_router(bank_router)
app.include_router(cashflow_router)
app.include_router(stocks_router)
app.include_router(crypto_router)
app.include_router(dashboard_router)
app.include_router(notes_router)
app.include_router(settings_router)


@app.get("/")
def root():
    """Health check endpoint."""
    return {"status": "ok", "app": settings.app_name}


@app.get("/health")
def health():
    """Simple health check for container monitoring."""
    return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}


@app.get("/health/db")
def health_db(session: Session = Depends(get_session)):
    """Check database connection."""
    try:
        session.exec(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception:
        return {"status": "error", "database": "unavailable"}
