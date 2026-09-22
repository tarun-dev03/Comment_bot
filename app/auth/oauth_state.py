from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Integer, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OAuthLoginState


def oauth_state_expiry(minutes: int = 15) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


async def create_oauth_state(
    db: AsyncSession,
    *,
    state: str,
    redirect_uri: str,
    user_id: int | None,
) -> None:
    await db.execute(delete(OAuthLoginState).where(OAuthLoginState.expires_at < datetime.now(UTC)))
    db.add(
        OAuthLoginState(
            state=state,
            redirect_uri=redirect_uri,
            user_id=user_id,
            expires_at=oauth_state_expiry(),
        )
    )
    await db.commit()


async def pop_oauth_state(db: AsyncSession, state: str) -> OAuthLoginState | None:
    result = await db.execute(select(OAuthLoginState).where(OAuthLoginState.state == state))
    row = result.scalar_one_or_none()
    if not row:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    await db.execute(delete(OAuthLoginState).where(OAuthLoginState.state == state))
    await db.commit()
    if expires < datetime.now(UTC):
        return None
    return row
