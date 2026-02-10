"""Application settings."""

import os
import sys
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self):
        # ── Required settings (fail fast if missing) ──────────
        self.database_url: str = os.environ.get("DATABASE_URL", "")
        self.secret_key: str = os.environ.get("SECRET_KEY", "")
        self.encryption_key: str = os.environ.get("ENCRYPTION_KEY", "")
        
        env = os.getenv("ENV", "production")
        self.environment: str = env
        
        if env == "production":
            if not self.secret_key:
                print("❌ CRITICAL: SECRET_KEY environment variable is required")
                sys.exit(1)
            if not self.database_url:
                print("❌ CRITICAL: DATABASE_URL environment variable is required")
                sys.exit(1)
            if not self.encryption_key:
                print("⚠️  WARNING: ENCRYPTION_KEY environment variable is not set")
        
        # ── Configurable settings ─────────────────────────────
        self.algorithm: str = os.getenv("ALGORITHM", "HS256")
        self.access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
        self.refresh_token_expire_days: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
        self.cors_origins: list[str] = [
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        ]
        self.debug: bool = os.getenv("DEBUG", "false").lower() == "true" and env != "production"
        self.app_name: str = os.getenv("APP_NAME", "CapitalView API")

        # ── Market Data ───────────────────────────────────────
        self.yahoo_user_agent: str = os.getenv(
            "YAHOO_USER_AGENT", 
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        self.yahoo_api_url: str = os.getenv("YF_API_URL", "https://query1.finance.yahoo.com/v1/finance/search")
        self.market_data_timeout: int = int(os.getenv("MARKET_DATA_TIMEOUT", "10"))
        
        self.cmc_api_url: str = os.getenv("CMC_API_URL", "https://pro-api.coinmarketcap.com")
        self.cmc_api_key: str = os.getenv("CMC_API_KEY", "")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
