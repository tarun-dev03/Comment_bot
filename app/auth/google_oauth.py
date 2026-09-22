import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import OAuthToken
from app.security import decrypt_text, encrypt_text

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def google_redirect_uri() -> str:
    return f"{get_settings().app_url.rstrip('/')}/auth/google/callback"


def build_google_authorize_url(state: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(settings.google_scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": google_redirect_uri(),
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> str:
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        access = resp.json().get("access_token")
        if not access:
            raise ValueError("No access_token in refresh response")
        return access


async def fetch_google_email(access_token: str) -> str | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("email")


async def save_oauth_token(
    db: AsyncSession,
    user_id: int,
    refresh_token: str,
    google_email: str | None,
) -> None:
    encrypted = encrypt_text(refresh_token)
    result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user_id))
    row = result.scalar_one_or_none()
    if row:
        row.refresh_token_encrypted = encrypted
        row.google_email = google_email
    else:
        db.add(
            OAuthToken(
                user_id=user_id,
                refresh_token_encrypted=encrypted,
                google_email=google_email,
            )
        )
    await db.commit()


async def get_user_refresh_token(db: AsyncSession, user_id: int) -> str | None:
    result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user_id))
    row = result.scalar_one_or_none()
    if not row:
        return None
    try:
        return decrypt_text(row.refresh_token_encrypted)
    except ValueError:
        return None


async def get_access_token_for_user(db: AsyncSession, user_id: int) -> str | None:
    refresh = await get_user_refresh_token(db, user_id)
    if not refresh:
        return None
    try:
        return await refresh_access_token(refresh)
    except Exception:
        logger.exception("Token refresh failed for user %s", user_id)
        return None
