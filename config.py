"""Application settings."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url: str = os.getenv("DATABASE_URL")
    secret_key: str = os.getenv("SECRET_KEY")
    algorithm: str = os.getenv("ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
    refresh_token_expire_days: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
    encryption_key: str = os.getenv("ENCRYPTION_KEY")
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    app_name: str = os.getenv("APP_NAME", "CapitalView API")
    environment: str = os.getenv("ENV", "production")


def get_settings() -> Settings:
    return Settings()
