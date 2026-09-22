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
from app.bot.engine import is_bot_running, start_bot_task, stop_bot_task
from app.config import get_settings
from app.deps import SESSION_COOKIE, get_current_user, get_current_user_optional
from app.db import get_db
from app.models import BotJob, BotJobStatus, OAuthToken, User, UserPhrase
from app.security import apply_session_cookie
from app.youtube.live_chat import YouTubeAPIError, fetch_live_chat_id, parse_video_id

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_oauth_states: dict[str, int | None] = {}  # None = Sign in with Google (no email step)


def _google_oauth_configured() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret)


def _begin_google_oauth(user_id: int | None) -> RedirectResponse:
    if not _google_oauth_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    state = secrets.token_urlsafe(16)
    _oauth_states[state] = user_id
    return RedirectResponse(build_google_authorize_url(state), status_code=303)


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
    db: AsyncSession = Depends(get_db),
):
    oauth = None
    job = None
    phrases: list[str] = []
    if user:
        result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        oauth = result.scalar_one_or_none()
        result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
        job = result.scalar_one_or_none()
        result = await db.execute(
            select(UserPhrase.phrase).where(UserPhrase.user_id == user.id).order_by(UserPhrase.id.desc())
        )
        phrases = [row[0] for row in result.all()]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "oauth": oauth,
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
async def google_login_start():
    """One-step sign-in: Google account + YouTube scope (no email magic link)."""
    return _begin_google_oauth(None)


@router.get("/auth/google/start")
async def google_start(user: User = Depends(get_current_user)):
    """Reconnect or grant YouTube access for an existing session."""
    return _begin_google_oauth(user.id)


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return RedirectResponse(f"/?error={error}", status_code=303)
    if not code or not state or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback")
    linked_user_id = _oauth_states.pop(state)

    try:
        token_data = await exchange_code_for_tokens(code)
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

    response = RedirectResponse("/", status_code=303)
    if linked_user_id is None:
        apply_session_cookie(response, user_id)
    return response


class StartBotBody(BaseModel):
    stream_url: str


@router.post("/api/bots/start")
async def api_start_bot(
    body: StartBotBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Connect Google/YouTube first")

    video_id = parse_video_id(body.stream_url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or video ID")

    from app.auth.google_oauth import get_access_token_for_user

    access = await get_access_token_for_user(db, user.id)
    if not access:
        raise HTTPException(status_code=400, detail="Could not refresh YouTube token")

    try:
        live_chat_id, title = await fetch_live_chat_id(access, video_id)
    except YouTubeAPIError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if job:
        job.stream_url = body.stream_url.strip()
        job.video_id = video_id
        job.live_chat_id = live_chat_id
        job.status = BotJobStatus.RUNNING.value
        job.last_error = None
    else:
        job = BotJob(
            user_id=user.id,
            stream_url=body.stream_url.strip(),
            video_id=video_id,
            live_chat_id=live_chat_id,
            status=BotJobStatus.RUNNING.value,
        )
        db.add(job)
    await db.commit()

    await start_bot_task(user.id)
    return {"ok": True, "video_title": title, "status": "running"}


@router.post("/api/bots/stop")
async def api_stop_bot(user: User = Depends(get_current_user)):
    await stop_bot_task(user.id)
    return {"ok": True, "status": "stopped"}


@router.get("/api/bots/status")
async def api_bot_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if not job:
        return {"status": "stopped", "running": False}
    return {
        "status": job.status,
        "running": is_bot_running(user.id),
        "messages_sent": job.messages_sent,
        "messages_sent_today": job.messages_sent_today,
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
