"""CapitalView API - Main entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from sqlmodel import Session, select

from config import get_settings
from database import get_session, get_engine
from models import User
from routes import bank_router, cashflow_router, stocks_router, crypto_router, users_router


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

app = FastAPI(
    title=settings.app_name,
    description="Personal wealth management and investment tracking API",
    version="0.1.0",
    lifespan=lifespan,
)

# Routes
app.include_router(bank_router, prefix="/api")
app.include_router(cashflow_router, prefix="/api")
app.include_router(stocks_router, prefix="/api")
app.include_router(crypto_router, prefix="/api")
app.include_router(users_router, prefix="/api")


@app.get("/")
def root():
    """Health check endpoint."""
    return {"status": "ok", "app": settings.app_name}


@app.get("/health/db")
def health_db(session: Session = Depends(get_session)):
    """Check database connection."""
    try:
        session.exec(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception as e:
        return {"status": "error", "database": str(e)}
