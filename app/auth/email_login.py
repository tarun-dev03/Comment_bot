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

    smtp_host = (settings.smtp_host or "").strip()
    if settings.email_dev_mode or not smtp_host:
        logger.info("DEV login link for %s: %s", email, verify_url)
        print(f"\n[DEV] Login link for {email}:\n{verify_url}\n")
        return None

    msg = EmailMessage()
    smtp_from = (settings.smtp_from or settings.smtp_user or "").strip()
    msg["From"] = smtp_from
    msg["To"] = email
    msg["Subject"] = "Sign in to YouTube Comment Bot"
    msg.set_content(
        f"Click the link below to sign in (expires in 15 minutes):\n\n{verify_url}\n"
    )

    username = (settings.smtp_user or "").strip()
    password = (settings.smtp_password or "").replace(" ", "")

    try:
        await _send_smtp(
            msg,
            hostname=smtp_host,
            port=settings.smtp_port,
            username=username,
            password=password,
            use_tls=settings.smtp_use_tls,
        )
        logger.info("Login email sent to %s via %s", email, smtp_host)
    except Exception as e:
        logger.exception("Failed to send login email to %s", email)
        return f"Could not send email: {e}"

    return None


async def _send_smtp(
    msg: EmailMessage,
    *,
    hostname: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
) -> None:
    """Send via STARTTLS (587) or implicit TLS (465)."""
    if port == 465:
        await aiosmtplib.send(
            msg,
            hostname=hostname,
            port=465,
            use_tls=True,
            username=username or None,
            password=password or None,
            timeout=30,
        )
        return

    try:
        await aiosmtplib.send(
            msg,
            hostname=hostname,
            port=port or 587,
            start_tls=True,
            username=username or None,
            password=password or None,
            timeout=30,
        )
    except Exception:
        if hostname == "smtp.gmail.com" and port != 465:
            logger.warning("SMTP on port %s failed; retrying Gmail on 465", port)
            await aiosmtplib.send(
                msg,
                hostname=hostname,
                port=465,
                use_tls=True,
                username=username or None,
                password=password or None,
                timeout=30,
            )
        else:
            raise


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
