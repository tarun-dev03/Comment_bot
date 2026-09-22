from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    session_secret: str = "change-me-session"
    database_url: str = "sqlite+aiosqlite:///./comment_bot.db"

    google_client_id: str = ""
    google_client_secret: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    smtp_use_tls: bool = True
    email_dev_mode: bool = True
    email_login_enabled: bool = True

    max_messages_per_day: int = 40

    google_scopes: list[str] = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://") :]
        if (
            v.startswith("postgresql://")
            and "+asyncpg" not in v
            and "+psycopg" not in v
        ):
            return "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v

    def use_secure_cookies(self) -> bool:
        return self.app_url.lower().startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
