from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory, get_db
from app.models import User
from app.security import load_session_cookie

SESSION_COOKIE = "session"


async def get_current_user_optional(request: Request) -> User | None:
    if getattr(request.app.state, "db_error", None):
        return None
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    user_id = load_session_cookie(cookie)
    if not user_id:
        return None
    try:
        async with async_session_factory() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            return result.scalar_one_or_none()
    except Exception:
        return None


async def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
