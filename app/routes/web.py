import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email_login import request_email_login, verify_email_login
from app.auth.google_oauth import (
    build_google_authorize_url,
    exchange_code_for_tokens,
    fetch_google_email,
    save_oauth_token,
)
from app.auth.oauth_state import create_oauth_state, pop_oauth_state
from app.bot.engine import is_bot_running, start_bot_task, stop_bot_task
from app.config import get_settings
from app.deps import SESSION_COOKIE, get_current_user, get_current_user_optional
from app.db import async_session_factory, get_db
from app.models import BotJob, BotJobStatus, OAuthToken, User, UserCookie, UserPhrase
from app.security import apply_session_cookie
from app.youtube.live_chat import YouTubeAPIError, fetch_live_chat_id, parse_video_id

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _public_base_url(request: Request) -> str:
    """Use the URL the user actually opened (fixes wrong APP_URL → Google 404)."""
    forwarded = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded and host:
        return f"{forwarded}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _google_oauth_configured() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret)


async def _begin_google_oauth(
    request: Request,
    db: AsyncSession,
    user_id: int | None,
) -> RedirectResponse:
    if not _google_oauth_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    state = secrets.token_urlsafe(16)
    redirect_uri = f"{_public_base_url(request)}/auth/google/callback"
    await create_oauth_state(db, state=state, redirect_uri=redirect_uri, user_id=user_id)
    return RedirectResponse(build_google_authorize_url(state, redirect_uri), status_code=303)


async def _user_for_google_login(db: AsyncSession, google_email: str) -> User:
    email = google_email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        user = User(email=email)
        db.add(user)
        await db.flush()
    return user


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
):
    db_error = getattr(request.app.state, "db_error", None)
    if db_error:
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Comment Bot — Database Setup</title></head>
            <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:2rem;max-width:720px;margin:0 auto;line-height:1.6;color:#1e293b;">
            <h1 style="color:#e11d48;margin-bottom:0.5rem;">Database Connection Error</h1>
            <p>The database is currently unreachable. If your free Render PostgreSQL database expired (Render free databases expire after 30 days), you do <b>not</b> need to pay for premium!</p>
            <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:1rem;margin:1rem 0;">
                <b style="color:#991b1b;">Error details:</b>
                <pre style="background:#fff;padding:0.75rem;border-radius:6px;overflow:auto;font-size:0.875rem;margin-top:0.5rem;color:#b91c1c;">{db_error}</pre>
            </div>
            <h3>How to fix this for FREE:</h3>
            <ol style="padding-left:1.25rem;">
                <li style="margin-bottom:0.75rem;">
                    <b>Option 1: Free permanent cloud Postgres (Recommended)</b><br>
                    Create a free Postgres database on <a href="https://neon.tech" target="_blank" style="color:#2563eb;">Neon.tech</a> (free forever, 0.5 GB, 30s setup, no credit card required).<br>
                    Copy the connection string, go to your Render Web Service &rarr; <b>Environment</b> &rarr; set <code>DATABASE_URL</code> to the Neon URL &rarr; Save Changes.
                </li>
                <li style="margin-bottom:0.75rem;">
                    <b>Option 2: Local SQLite</b><br>
                    In your Render Web Service &rarr; <b>Environment</b> &rarr; remove <code>DATABASE_URL</code> or set it to <code>sqlite+aiosqlite:///./comment_bot.db</code> &rarr; Save Changes.
                </li>
            </ol>
            </body></html>""",
            status_code=503,
        )

    oauth = None
    user_cookie_entry = None
    job = None
    phrases: list[str] = []
    async with async_session_factory() as db:
        if user:
            result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
            oauth = result.scalar_one_or_none()
            c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
            user_cookie_entry = c_res.scalar_one_or_none()
            result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
            job = result.scalar_one_or_none()
            result = await db.execute(
                select(UserPhrase.phrase)
                .where(UserPhrase.user_id == user.id)
                .order_by(UserPhrase.id.desc())
            )
            phrases = [row[0] for row in result.all()]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "oauth": oauth,
            "has_cookie": bool(user_cookie_entry),
            "job": job,
            "phrases": phrases,
            "running": user and is_bot_running(user.id),
            "settings": get_settings(),
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "error": None,
        "sent": False,
        "user": None,
        "email_login_enabled": get_settings().email_login_enabled,
    })


@router.post("/auth/email")
async def auth_email(
    request: Request,
    email: EmailStr = Form(...),
    db: AsyncSession = Depends(get_db),
):
    err = await request_email_login(db, str(email))
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": err,
            "sent": err is None,
            "email": email,
            "user": None,
            "email_login_enabled": get_settings().email_login_enabled,
        },
    )


@router.get("/auth/verify")
async def auth_verify(token: str, db: AsyncSession = Depends(get_db)):
    if not get_settings().email_login_enabled:
        raise HTTPException(status_code=404, detail="Email sign-in is disabled")
    user = await verify_email_login(db, token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired login link")
    response = RedirectResponse("/", status_code=303)
    apply_session_cookie(response, user.id)
    return response


@router.post("/auth/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/auth/google/login")
async def google_login_start(request: Request, db: AsyncSession = Depends(get_db)):
    """One-step sign-in: Google account + YouTube scope (no email magic link)."""
    return await _begin_google_oauth(request, db, None)


@router.get("/auth/google/start")
async def google_start(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reconnect or grant YouTube access for an existing session."""
    return await _begin_google_oauth(request, db, user.id)


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return RedirectResponse(f"/login?error={error}", status_code=303)
    if not code or not state:
        return RedirectResponse("/login?error=missing_code", status_code=303)

    pending = await pop_oauth_state(db, state)
    if not pending:
        return RedirectResponse(
            "/login?error=invalid_state",
            status_code=303,
        )

    linked_user_id = pending.user_id
    redirect_uri = pending.redirect_uri

    try:
        token_data = await exchange_code_for_tokens(code, redirect_uri)
    except Exception:
        return RedirectResponse("/login?error=oauth_exchange_failed", status_code=303)

    refresh = token_data.get("refresh_token")
    access = token_data.get("access_token")
    if not refresh:
        return RedirectResponse("/login?error=no_refresh_token", status_code=303)

    google_email = None
    if access:
        google_email = await fetch_google_email(access)

    if linked_user_id is None:
        if not google_email:
            return RedirectResponse("/login?error=no_google_email", status_code=303)
        user = await _user_for_google_login(db, google_email)
        user_id = user.id
    else:
        user_id = linked_user_id
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return RedirectResponse("/login?error=session_expired", status_code=303)

    await save_oauth_token(db, user_id, refresh, google_email)

    home = f"{_public_base_url(request)}/"
    response = RedirectResponse(home, status_code=303)
    if linked_user_id is None:
        apply_session_cookie(response, user_id)
    return response


@router.post("/api/auth/cookie")
async def save_cookie(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models import BotJob, BotJobStatus, User, UserCookie
    from app.security import encrypt_text

    cookie_val = None
    is_json = False
    data = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        is_json = True
        try:
            data = await request.json()
            cookie_val = data.get("cookie", "")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")
    else:
        form = await request.form()
        cookie_val = form.get("cookie", "")

    user = await get_current_user_optional(request)
    if not user:
        if is_json and data.get("email"):
            email_clean = data["email"].strip().lower()
            u_res = await db.execute(select(User).where(User.email == email_clean))
            user = u_res.scalar_one_or_none()
        elif is_json:
            u_res = await db.execute(select(User).order_by(User.id.asc()))
            users = u_res.scalars().all()
            if len(users) == 1:
                user = users[0]

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Not signed in. Please sign in to the web dashboard or provide your account email in the sync payload.",
        )

    text_val = (cookie_val or "").strip()
    if not text_val:
        raise HTTPException(status_code=400, detail="Cookie string cannot be empty")

    encrypted = encrypt_text(text_val)
    res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
    existing = res.scalar_one_or_none()
    if existing:
        existing.cookie_encrypted = encrypted
    else:
        db.add(UserCookie(user_id=user.id, cookie_encrypted=encrypted))

    res_job = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = res_job.scalar_one_or_none()
    if job:
        job.mode = "cookie"
        job.last_error = None
        if job.status == BotJobStatus.ERROR.value:
            job.status = BotJobStatus.STOPPED.value
    await db.commit()

    if is_json or "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "message": "YouTube Cookie updated successfully", "user": user.email}
    return RedirectResponse("/", status_code=303)


class StartBotBody(BaseModel):
    stream_url: str
    interval_seconds: int = 30
    mode: str = "oauth"


@router.post("/api/bots/start")
async def api_start_bot(
    body: StartBotBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    video_id = parse_video_id(body.stream_url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or video ID")

    mode = body.mode if body.mode in ("oauth", "cookie") else "oauth"

    live_chat_id = video_id
    title = video_id

    if mode == "cookie":
        from app.models import UserCookie
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        if not c_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Save your YouTube Cookie in dashboard settings first.")
    else:
        result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Connect Google/YouTube first (or switch to YouTube Cookie mode)")

        from app.auth.google_oauth import get_access_token_for_user

        access = await get_access_token_for_user(db, user.id)
        if not access:
            raise HTTPException(status_code=400, detail="Could not refresh YouTube token")

        try:
            live_chat_id, title = await fetch_live_chat_id(access, video_id)
        except YouTubeAPIError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    interval = max(5, min(3600, body.interval_seconds))

    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if job:
        job.stream_url = body.stream_url.strip()
        job.video_id = video_id
        job.live_chat_id = live_chat_id
        job.mode = mode
        job.interval_seconds = interval
        job.status = BotJobStatus.RUNNING.value
        job.last_error = None
        job.messages_sent_today = 0
    else:
        job = BotJob(
            user_id=user.id,
            stream_url=body.stream_url.strip(),
            video_id=video_id,
            live_chat_id=live_chat_id,
            mode=mode,
            interval_seconds=interval,
            status=BotJobStatus.RUNNING.value,
            last_error=None,
            messages_sent_today=0,
        )
        db.add(job)
    await db.commit()

    await start_bot_task(user.id)
    return {"ok": True, "video_title": title, "mode": mode, "status": "running"}


@router.post("/api/bots/stop")
async def api_stop_bot(user: User = Depends(get_current_user)):
    await stop_bot_task(user.id)
    return {"ok": True, "status": "stopped"}


@router.get("/api/bots/status")
async def api_bot_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if not job:
        return {"status": "stopped", "running": False, "interval_seconds": 30, "mode": "oauth"}
    return {
        "status": job.status,
        "running": is_bot_running(user.id),
        "messages_sent": job.messages_sent,
        "messages_sent_today": job.messages_sent_today,
        "interval_seconds": job.interval_seconds or 30,
        "mode": job.mode or "oauth",
        "last_message_at": job.last_message_at.isoformat() if job.last_message_at else None,
        "last_error": job.last_error,
        "stream_url": job.stream_url,
    }


@router.post("/api/phrases")
async def add_phrase(
    phrase: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    text = phrase.strip()[:200]
    if not text:
        raise HTTPException(status_code=400, detail="Phrase cannot be empty")
    db.add(UserPhrase(user_id=user.id, phrase=text))
    await db.commit()
    return RedirectResponse("/", status_code=303)
