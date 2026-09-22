import logging
from datetime import UTC, datetime

import aiosmtplib
from email.message import EmailMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import EmailLoginToken, User
from app.security import generate_login_token, hash_token, login_token_expiry

logger = logging.getLogger(__name__)


async def request_email_login(db: AsyncSession, email: str) -> str | None:
    """Create login token and send email. Returns error message or None on success."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return "Please enter a valid email address."

    raw_token = generate_login_token()
    token_row = EmailLoginToken(
        email=email,
        token_hash=hash_token(raw_token),
        expires_at=login_token_expiry(),
    )
    db.add(token_row)
    await db.commit()

    settings = get_settings()
    verify_url = f"{settings.app_url.rstrip('/')}/auth/verify?token={raw_token}"

    if settings.email_dev_mode or not settings.smtp_host:
        logger.info("DEV login link for %s: %s", email, verify_url)
        print(f"\n[DEV] Login link for {email}:\n{verify_url}\n")
        return None

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = email
    msg["Subject"] = "Sign in to YouTube Comment Bot"
    msg.set_content(
        f"Click the link below to sign in (expires in 15 minutes):\n\n{verify_url}\n"
    )

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_use_tls,
        )
    except Exception as e:
        logger.exception("Failed to send login email")
        return f"Could not send email: {e}"

    return None


async def verify_email_login(db: AsyncSession, raw_token: str) -> User | None:
    now = datetime.now(UTC)
    token_hash = hash_token(raw_token)
    result = await db.execute(
        select(EmailLoginToken).where(
            EmailLoginToken.token_hash == token_hash,
            EmailLoginToken.used_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < now:
        return None

    row.used_at = now
    result = await db.execute(select(User).where(User.email == row.email))
    user = result.scalar_one_or_none()
    if not user:
        user = User(email=row.email)
        db.add(user)
        await db.flush()

    await db.commit()
    await db.refresh(user)
    return user
