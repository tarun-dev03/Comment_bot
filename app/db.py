import logging
import ssl
from collections.abc import AsyncGenerator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _postgres_connect_args(database_url: str) -> dict:
    """Render / Neon / Supabase Postgres uses TLS; do not verify self-signed certs.
    Disable statement cache so asyncpg works with transaction poolers (Neon, PgBouncer, Supabase).
    """
    if not database_url.startswith("postgresql"):
        return {}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {"ssl": ctx, "statement_cache_size": 0}


settings = get_settings()
_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"timeout": 30} if _is_sqlite else _postgres_connect_args(settings.database_url)
_engine_kwargs: dict = {"echo": False}
if _connect_args:
    _engine_kwargs["connect_args"] = _connect_args
if not _is_sqlite:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 300

engine = create_async_engine(settings.database_url, **_engine_kwargs)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


def _missing_columns_and_indexes(sync_conn) -> tuple[set[str], set[str]]:
    inspector = inspect(sync_conn)
    bot_job_cols = {c["name"] for c in inspector.get_columns("bot_jobs")} if inspector.has_table("bot_jobs") else set()
    user_cols = {c["name"] for c in inspector.get_columns("users")} if inspector.has_table("users") else set()
    return bot_job_cols, user_cols


async def init_db() -> None:
    import app.models  # noqa: F401 — register models with metadata

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            bot_job_cols, user_cols = await conn.run_sync(_missing_columns_and_indexes)

            if bot_job_cols:
                if "interval_seconds" not in bot_job_cols:
                    await conn.execute(text("ALTER TABLE bot_jobs ADD COLUMN interval_seconds INTEGER DEFAULT 30"))
                if "mode" not in bot_job_cols:
                    await conn.execute(text("ALTER TABLE bot_jobs ADD COLUMN mode VARCHAR(32) DEFAULT 'oauth'"))

            if user_cols:
                if "sync_token_hash" not in user_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN sync_token_hash VARCHAR(64)"))
                if "sync_token_encrypted" not in user_cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN sync_token_encrypted TEXT"))
                await conn.execute(
                    text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_sync_token_hash ON users (sync_token_hash)")
                )
        logger.info("Database initialized")
    except Exception:
        logger.exception("Database initialization failed (check DATABASE_URL / Postgres SSL)")
        raise
