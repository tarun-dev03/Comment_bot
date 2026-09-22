import logging
import ssl
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _postgres_connect_args(database_url: str) -> dict:
    if not database_url.startswith("postgresql"):
        return {}
    parsed = urlparse(database_url)
    qs = parse_qs(parsed.query)
    if qs.get("sslmode", [""])[0] == "disable":
        return {}
    return {"ssl": ssl.create_default_context()}


settings = get_settings()
_connect_args = _postgres_connect_args(settings.database_url)
engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_connect_args or None,
)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    import app.models  # noqa: F401 — register models with metadata

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")
    except Exception:
        logger.exception("Database initialization failed (check DATABASE_URL / Postgres SSL)")
        raise
