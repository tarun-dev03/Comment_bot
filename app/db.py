import logging
import ssl
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _postgres_connect_args(database_url: str) -> dict:
    """Render Postgres uses TLS with a self-signed cert; do not verify it."""
    if not database_url.startswith("postgresql"):
        return {}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {"ssl": ctx}


settings = get_settings()
_connect_args = _postgres_connect_args(settings.database_url)
_engine_kwargs: dict = {"echo": False}
if _connect_args:
    _engine_kwargs["connect_args"] = _connect_args
engine = create_async_engine(settings.database_url, **_engine_kwargs)
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
