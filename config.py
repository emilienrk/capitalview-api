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
        self.community_encryption_key: str = os.environ.get("COMMUNITY_ENCRYPTION_KEY", "")
        
        env = os.getenv("ENV", "production")
        self.environment: str = env
        if not self.secret_key:
            print("CRITICAL: SECRET_KEY environment variable is required")
            sys.exit(1)

        if env == "production":
            if not self.database_url:
                print("CRITICAL: DATABASE_URL environment variable is required")
                sys.exit(1)
            if not self.encryption_key:
                print("WARNING: ENCRYPTION_KEY environment variable is not set")
        
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
        self.trusted_proxy_count: int = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
        # Guards /health/deep, which tells a caller what is broken and how stale
        # the data is. Unset outside production leaves it open for local use;
        # unset in production hides the route entirely rather than exposing it.
        self.health_token: str = os.getenv("HEALTH_TOKEN", "")
        # Build provenance, injected as build args by the CI and frozen as ENV
        # in the image. Both None in a working tree, as the shared /version
        # contract expects.
        self.git_sha: str | None = os.getenv("GIT_SHA") or None
        self.build_time: str | None = os.getenv("BUILD_TIME") or None

        # ── MCP (agent access to the API) ─────────────────────
        self.mcp_enabled: bool = os.getenv("MCP_ENABLED", "true").lower() == "true"
        self.mcp_path: str = os.getenv("MCP_PATH", "/mcp")
        # Advertised to MCP clients and shown in the settings UI
        self.mcp_public_url: str = os.getenv("MCP_PUBLIC_URL", f"http://localhost:8000{self.mcp_path}")
        # Optional Host allow-list re-enabling the SDK's DNS-rebinding protection
        self.mcp_allowed_hosts: list[str] = [
            host.strip()
            for host in os.getenv("MCP_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        ]

        # ── Market Data ───────────────────────────────────────
        self.yahoo_user_agent: str = os.getenv(
            "YAHOO_USER_AGENT", 
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        self.yahoo_api_url: str = os.getenv("YF_API_URL", "https://query1.finance.yahoo.com/v1/finance/search")
        self.market_data_timeout: int = int(os.getenv("MARKET_DATA_TIMEOUT", "10"))
        
        self.cmc_api_url: str = os.getenv("CMC_API_URL", "https://pro-api.coinmarketcap.com")
        self.cmc_api_key: str = os.getenv("CMC_API_KEY", "")

        # ── CoinGecko (crypto historical prices) ─────────────
        self.coingecko_api_url: str = os.getenv("CG_API_URL", "https://api.coingecko.com/api/v3")
        self.coingecko_api_key: str = os.getenv("CG_API_KEY", "")

        # ── Enable Banking ─────────────────────────────────────
        # Fixed, query-parameter-free path (spec §C3): the portal refuses to
        # register a redirect URL carrying one. Declared verbatim in each
        # user's own Enable Banking application.
        self.banking_callback_url: str = os.getenv(
            "BANKING_CALLBACK_URL", "http://localhost:8000/banking/callback"
        )
        # Where the bank callback sends the browser back to. A setting of its
        # own, not cors_origins[0]: that list is an allow-list whose order is
        # incidental and which may legitimately be empty.
        self.frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:5173").strip().rstrip("/")


@lru_cache()
def get_settings() -> Settings:
    return Settings()
