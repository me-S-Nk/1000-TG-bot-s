import os
import re
from functools import lru_cache
from typing import Optional, Set
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def normalize_database_url(url: Optional[str]) -> str:
    """
    Safely transform database URLs from various cloud providers (e.g. Railway, Render, Heroku)
    into standard SQLAlchemy async driver format (postgresql+asyncpg://).
    """
    if not url:
        return "postgresql+asyncpg://postgres:password@localhost:5432/bot_catalog"

    v = str(url).strip()

    # Convert postgres:// to postgresql+asyncpg://
    if v.startswith("postgres://"):
        v = re.sub(r"^postgres://", "postgresql+asyncpg://", v)
    # Convert postgresql:// (without explicit driver) to postgresql+asyncpg://
    elif v.startswith("postgresql://") and not v.startswith("postgresql+"):
        v = re.sub(r"^postgresql://", "postgresql+asyncpg://", v)

    # Clean up sslmode if incompatible with asyncpg
    # asyncpg expects 'ssl' parameter instead of 'sslmode' in query params
    if "sslmode=" in v:
        v = v.replace("sslmode=require", "ssl=require").replace("sslmode=prefer", "ssl=prefer")

    return v


class Settings(BaseSettings):
    BOT_TOKEN: str = ""
    ADMIN_ID: str | int = ""
    ADMIN_IDS: str | int | None = None
    DATABASE_URL: str = ""
    LOG_LEVEL: str = "INFO"

    # Environment Mode
    ENVIRONMENT: str = "production"

    # Telegram Mini App Configuration
    WEBAPP_URL: str = ""
    WEBAPP_HOST: str = "0.0.0.0"
    WEBAPP_PORT: int = 8080

    @property
    def admin_ids(self) -> Set[int]:
        """Return a set of all configured integer admin Telegram IDs."""
        result: Set[int] = set()
        for source in (self.ADMIN_ID, self.ADMIN_IDS):
            if not source:
                continue
            if isinstance(source, int):
                result.add(source)
            elif isinstance(source, str):
                for part in re.split(r"[,;\s]+", source.strip()):
                    if part and (part.isdigit() or (part.startswith("-") and part[1:].isdigit())):
                        result.add(int(part))
        return result

    def is_admin(self, user_id: Optional[int]) -> bool:
        """Check if given Telegram user_id has administrative rights."""
        if user_id is None:
            return False
        return user_id in self.admin_ids

    def get_db_diagnostic_info(self) -> str:
        """
        Return safe database connection metadata (scheme, host, port, database)
        WITHOUT leaking username, password, or full connection string.
        """
        try:
            parsed = make_url(self.DATABASE_URL)
            return (
                f"scheme={parsed.drivername}, "
                f"host={parsed.host or 'none'}, "
                f"port={parsed.port or 'default'}, "
                f"database={parsed.database or 'none'}"
            )
        except Exception as e:
            return f"unparseable_url (error={e})"

    # Rate limiting configuration (requests per window)
    RATE_LIMIT_START: int = 5          # per 60 seconds
    RATE_LIMIT_SEARCH: int = 10        # per 60 seconds
    RATE_LIMIT_ADMIN: int = 30         # per 60 seconds
    RATE_LIMIT_GENERAL: int = 40       # per 60 seconds
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # Input length limits
    MAX_CATEGORY_NAME_LEN: int = 100
    MAX_CATEGORY_DESC_LEN: int = 500
    MAX_BOT_NAME_LEN: int = 100
    MAX_BOT_DESC_LEN: int = 500
    MAX_USERNAME_LEN: int = 64
    MAX_SEARCH_QUERY_LEN: int = 100

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: Optional[str]) -> str:
        # Check primary value or fall back to standard cloud provider env vars
        raw_url = v or os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or os.getenv("POSTGRES_URL") or os.getenv("POSTGRESQL_URL")
        return normalize_database_url(raw_url)

    @field_validator("WEBAPP_PORT", mode="before")
    @classmethod
    def assemble_webapp_port(cls, v: Optional[object]) -> int:
        # Prioritize Railway standard PORT env var, then WEBAPP_PORT, then fallback to 8080
        raw_port = os.getenv("PORT") or v or os.getenv("WEBAPP_PORT") or 8080
        try:
            return int(raw_port)
        except (ValueError, TypeError):
            return 8080

    @field_validator("WEBAPP_URL", mode="before")
    @classmethod
    def validate_webapp_url(cls, v: Optional[str]) -> str:
        raw = (v or os.getenv("WEBAPP_URL") or "").strip()
        if not raw:
            raise ValueError(
                "WEBAPP_URL environment variable is required and cannot be empty. "
                "In production, provide a valid HTTPS URL (e.g. https://your-app.up.railway.app)."
            )

        env = (os.getenv("ENVIRONMENT") or "production").lower()
        if env not in ("dev", "development", "local", "test"):
            if not raw.startswith("https://"):
                raise ValueError(
                    f"WEBAPP_URL must be a valid HTTPS URL in production (got: '{raw}'). "
                    "Telegram Mini Apps strictly require HTTPS links."
                )
            if "localhost" in raw or "127.0.0.1" in raw:
                raise ValueError(
                    f"WEBAPP_URL cannot point to localhost/127.0.0.1 in production (got: '{raw}'). "
                    "Please set WEBAPP_URL to your public domain (e.g. https://1000bots-production.up.railway.app)."
                )

        return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
