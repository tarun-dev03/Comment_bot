import asyncio
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory, get_db  # noqa: F401
from app.models import User
from app.security import decrypt_text, encrypt_text, hash_token, load_session_cookie

SESSION_COOKIE = "session"


async def _load_user_by_id(user_id: int) -> User | None:
    for attempt in range(2):
        try:
            async with async_session_factory() as db:
                result = await db.execute(select(User).where(User.id == user_id))
                return result.scalar_one_or_none()
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
    return None


async def get_current_user_optional(request: Request) -> User | None:
    """Resolve ONLY the strictly signed-in user from their verified session cookie.
    Never falls back to another user in the database.
    """
    if getattr(request.app.state, "db_error", None):
        return None
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    user_id = load_session_cookie(cookie)
    if not user_id:
        return None
    return await _load_user_by_id(user_id)


async def get_current_user(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    verify_expected_user_header(request, user)
    return user


def verify_expected_user_header(request: Request, user: User) -> None:
    """Guard against stale browser tabs acting on a switched session account."""
    raw = request.headers.get("x-user-id")
    if not raw:
        return
    try:
        expected_id = int(raw.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid X-User-Id header") from exc
    if expected_id != user.id:
        raise HTTPException(
            status_code=409,
            detail="Account mismatch: your browser session switched to another account. Refreshing page.",
        )


def extract_sync_token(request: Request, body_token: str | None = None) -> str | None:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if tok:
            return tok
    hdr = (request.headers.get("x-sync-token") or "").strip()
    if hdr:
        return hdr
    if body_token and body_token.strip():
        return body_token.strip()
    return None


async def get_user_by_sync_token(db: AsyncSession, raw_token: str) -> User | None:
    token_hash = hash_token(raw_token.strip())
    result = await db.execute(select(User).where(User.sync_token_hash == token_hash))
    return result.scalar_one_or_none()


async def ensure_user_sync_token(db: AsyncSession, user: User, regenerate: bool = False) -> str:
    if not regenerate and user.sync_token_encrypted and user.sync_token_hash:
        try:
            existing = decrypt_text(user.sync_token_encrypted)
            if existing and hash_token(existing) == user.sync_token_hash:
                return existing
        except ValueError:
            pass

    raw = secrets.token_urlsafe(32)
    user.sync_token_hash = hash_token(raw)
    user.sync_token_encrypted = encrypt_text(raw)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return raw


async def resolve_api_user_optional(
    request: Request,
    db: AsyncSession,
    body_token: str | None = None,
) -> tuple[User | None, str]:
    """Authenticate an API request via Extension Sync Token or verified session cookie.
    Returns (user, source) where source is 'token', 'session', or 'none'.
    If a sync token is supplied but invalid, raises HTTP 401 instead of falling back.
    """
    raw_token = extract_sync_token(request, body_token=body_token)
    if raw_token:
        token_user = await get_user_by_sync_token(db, raw_token)
        if not token_user:
            raise HTTPException(status_code=401, detail="Invalid or expired Extension Sync Key")
        return token_user, "token"

    session_user = await get_current_user_optional(request)
    if session_user:
        verify_expected_user_header(request, session_user)
        return session_user, "session"

    return None, "none"
