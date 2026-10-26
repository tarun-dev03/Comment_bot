from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory, get_db
from app.models import BotJob, OAuthToken, User, UserCookie
from app.security import load_session_cookie

SESSION_COOKIE = "session"


async def get_current_user_optional(request: Request) -> User | None:
    if getattr(request.app.state, "db_error", None):
        return None
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        user_id = load_session_cookie(cookie)
        if user_id:
            for attempt in range(2):
                try:
                    async with async_session_factory() as db:
                        result = await db.execute(select(User).where(User.id == user_id))
                        u = result.scalar_one_or_none()
                        if u:
                            return u
                except Exception:
                    if attempt == 0:
                        import asyncio
                        await asyncio.sleep(0.2)
                        continue

    # If the user explicitly logged out, don't auto-restore session
    if request.cookies.get("logged_out") == "1":
        return None

    # Persistent Session Fallback:
    # Ensure credentials and bot state are NEVER forgotten across browser restarts, reboots or new tabs
    try:
        async with async_session_factory() as db:
            # 1. Prefer user with active UserCookie (synced credentials)
            c_res = await db.execute(select(UserCookie).order_by(UserCookie.updated_at.desc()))
            recent_cookie = c_res.scalars().first()
            if recent_cookie:
                u_res = await db.execute(select(User).where(User.id == recent_cookie.user_id))
                u = u_res.scalar_one_or_none()
                if u:
                    return u

            # 2. Prefer user with OAuth token
            o_res = await db.execute(select(OAuthToken).order_by(OAuthToken.updated_at.desc()))
            recent_oauth = o_res.scalars().first()
            if recent_oauth:
                u_res = await db.execute(select(User).where(User.id == recent_oauth.user_id))
                u = u_res.scalar_one_or_none()
                if u:
                    return u

            # 3. Prefer user with active or recent BotJob
            j_res = await db.execute(select(BotJob).order_by(BotJob.updated_at.desc()))
            recent_job = j_res.scalars().first()
            if recent_job:
                u_res = await db.execute(select(User).where(User.id == recent_job.user_id))
                u = u_res.scalar_one_or_none()
                if u:
                    return u

            # 4. Any registered user in the database
            u_res = await db.execute(select(User).order_by(User.id.desc()))
            u = u_res.scalars().first()
            if u:
                return u

            # 5. If database is empty, auto-create default owner so app works seamlessly
            default_user = User(email="user@commentbot.local")
            db.add(default_user)
            await db.commit()
            await db.refresh(default_user)
            return default_user
    except Exception:
        return None


async def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
