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
        url = f"{forwarded}://{host}".rstrip("/")
    else:
        url = str(request.base_url).rstrip("/")
    from app.bot.engine import record_public_url
    record_public_url(url)
    return url


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
async def health(request: Request):
    _public_base_url(request)
    return {"ok": True}


@router.get("/api/me")
async def api_me(
    request: Request,
    response: Response,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    _public_base_url(request)
    if user:
        apply_session_cookie(response, user.id, request=request)
        j_res = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
        job = j_res.scalar_one_or_none()
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        has_cookie = c_res.scalar_one_or_none() is not None
        o_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        has_oauth = o_res.scalar_one_or_none() is not None
        return {
            "authenticated": True,
            "email": user.email,
            "user_id": user.id,
            "stream_url": job.stream_url if job else "",
            "mode": job.mode if job else ("cookie" if has_cookie else "oauth"),
            "interval_seconds": job.interval_seconds if job else 30,
            "has_cookie": has_cookie,
            "has_oauth": has_oauth,
        }
    u_res = await db.execute(select(User).order_by(User.id.desc()))
    last_user = u_res.scalars().first()
    return {
        "authenticated": False,
        "suggested_email": last_user.email if last_user else None,
        "stream_url": "",
    }


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    user: User | None = Depends(get_current_user_optional),
):
    _public_base_url(request)
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
    phrase_items: list[dict] = []
    async with async_session_factory() as db:
        if user:
            result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
            oauth = result.scalar_one_or_none()
            c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
            user_cookie_entry = c_res.scalar_one_or_none()
            result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
            job = result.scalar_one_or_none()
            result = await db.execute(
                select(UserPhrase)
                .where(UserPhrase.user_id == user.id)
                .order_by(UserPhrase.id.desc())
            )
            phrase_rows = result.scalars().all()
            phrase_items = [{"id": p.id, "phrase": p.phrase} for p in phrase_rows]
            phrases = [p["phrase"] for p in phrase_items]

    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "oauth": oauth,
            "has_cookie": bool(user_cookie_entry),
            "job": job,
            "phrases": phrases,
            "phrase_items": phrase_items,
            "running": bool(user and is_bot_running(user.id)),
            "settings": get_settings(),
        },
    )
    if user:
        apply_session_cookie(response, user.id, request=request)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User | None = Depends(get_current_user_optional)):
    _public_base_url(request)
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
async def auth_verify(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    if not get_settings().email_login_enabled:
        raise HTTPException(status_code=404, detail="Email sign-in is disabled")
    user = await verify_email_login(db, token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired login link")
    response = RedirectResponse("/", status_code=303)
    apply_session_cookie(response, user.id, request=request)
    return response


@router.post("/auth/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.set_cookie("logged_out", "1", path="/", max_age=60 * 60 * 24 * 30)
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
    # Always refresh and ensure session cookie is attached to keep user signed in!
    apply_session_cookie(response, user_id, request=request)
    return response


@router.post("/api/auth/cookie")
async def save_cookie(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models import BotJob, BotJobStatus, User, UserCookie
    from app.security import encrypt_text
    from app.youtube.live_chat import invalidate_innertube_cache

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
            if not user:
                user = User(email=email_clean)
                db.add(user)
                await db.flush()
        else:
            # Check for user associated with an existing BotJob or latest user
            job_res = await db.execute(select(BotJob).order_by(BotJob.updated_at.desc()))
            recent_job = job_res.scalars().first()
            if recent_job:
                u_res = await db.execute(select(User).where(User.id == recent_job.user_id))
                user = u_res.scalar_one_or_none()

            if not user:
                u_res = await db.execute(select(User).order_by(User.id.desc()))
                user = u_res.scalars().first()

            if not user:
                user = User(email="user@commentbot.local")
                db.add(user)
                await db.flush()

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

    # Invalidate cached session so new cookie takes effect immediately
    invalidate_innertube_cache()

    res_job = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = res_job.scalar_one_or_none()
    if job:
        job.mode = "cookie"
        job.last_error = None
        if job.status == BotJobStatus.ERROR.value:
            job.status = BotJobStatus.STOPPED.value
    await db.commit()

    if is_json or "application/json" in request.headers.get("accept", ""):
        from fastapi.responses import JSONResponse
        res = JSONResponse({"ok": True, "message": "YouTube Cookie updated successfully", "user": user.email})
        apply_session_cookie(res, user.id, request=request)
        return res
    res = RedirectResponse("/", status_code=303)
    apply_session_cookie(res, user.id, request=request)
    return res


class StreamUrlBody(BaseModel):
    stream_url: str


@router.get("/api/settings/stream-url")
async def get_stream_url_setting(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    return {
        "ok": True,
        "stream_url": job.stream_url if job else "",
        "video_id": job.video_id if job else "",
        "mode": job.mode if job else "oauth",
        "interval_seconds": job.interval_seconds if job else 30,
    }


@router.post("/api/settings/stream-url")
async def save_stream_url_setting(
    body: StreamUrlBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    clean_url = body.stream_url.strip()
    video_id = parse_video_id(clean_url) or ""
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if job:
        job.stream_url = clean_url
        if video_id:
            job.video_id = video_id
            if not job.live_chat_id or job.mode == "cookie":
                job.live_chat_id = video_id
    else:
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        has_cookie = c_res.scalar_one_or_none() is not None
        job = BotJob(
            user_id=user.id,
            stream_url=clean_url,
            video_id=video_id,
            live_chat_id=video_id,
            mode="cookie" if has_cookie else "oauth",
            status=BotJobStatus.STOPPED.value,
            interval_seconds=30,
        )
        db.add(job)
    await db.commit()
    return {
        "ok": True,
        "message": "Stream URL saved and kept",
        "stream_url": clean_url,
        "video_id": video_id,
    }


class RunnerSettingsBody(BaseModel):
    interval_seconds: int | None = None
    mode: str | None = None


@router.post("/api/settings/runner")
async def save_runner_settings(
    body: RunnerSettingsBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if not job:
        job = BotJob(
            user_id=user.id,
            stream_url="",
            video_id="",
            live_chat_id="",
            mode="oauth",
            status=BotJobStatus.STOPPED.value,
            interval_seconds=30,
        )
        db.add(job)
    if body.mode in ("oauth", "cookie"):
        job.mode = body.mode
    if body.interval_seconds is not None:
        job.interval_seconds = max(5, min(3600, body.interval_seconds))
    await db.commit()
    return {
        "ok": True,
        "mode": job.mode,
        "interval_seconds": job.interval_seconds,
    }


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
    clean_url = body.stream_url.strip()
    video_id = parse_video_id(clean_url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or video ID")

    mode = body.mode if body.mode in ("oauth", "cookie") else "oauth"
    interval = max(5, min(3600, body.interval_seconds))

    # CRITICAL: Always persist stream_url, video_id, mode, and interval to database FIRST!
    # Even if chat lookup or YouTube API returns an error below, the URL is permanently kept.
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    if job:
        job.stream_url = clean_url
        job.video_id = video_id
        job.mode = mode
        job.interval_seconds = interval
    else:
        job = BotJob(
            user_id=user.id,
            stream_url=clean_url,
            video_id=video_id,
            live_chat_id=video_id,
            mode=mode,
            interval_seconds=interval,
            status=BotJobStatus.STOPPED.value,
        )
        db.add(job)
    await db.commit()

    live_chat_id = video_id
    title = video_id

    if mode == "cookie":
        from app.models import UserCookie
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        if not c_res.scalar_one_or_none():
            # If no cookie exists but Google OAuth is connected, gracefully fall back to OAuth!
            oauth_check = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
            if oauth_check.scalar_one_or_none():
                mode = "oauth"
                job.mode = "oauth"
                await db.commit()
            else:
                raise HTTPException(status_code=400, detail="Connect Google/YouTube first (tap Connect Google above).")

    if mode == "oauth":
        result = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Connect Google/YouTube first (tap Connect Google above).")

        from app.auth.google_oauth import get_access_token_for_user

        access = await get_access_token_for_user(db, user.id)
        if not access:
            raise HTTPException(status_code=400, detail="Could not refresh YouTube token. Tap Reconnect Google.")

        try:
            live_chat_id, title = await fetch_live_chat_id(access, video_id)
        except YouTubeAPIError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    job.live_chat_id = live_chat_id
    job.mode = mode
    job.status = BotJobStatus.RUNNING.value
    job.last_error = None
    job.messages_sent_today = 0
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
        return {"status": "stopped", "running": False, "interval_seconds": 30, "mode": "oauth", "video_id": ""}
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
        "video_id": job.video_id,
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


class PhraseAddBody(BaseModel):
    phrase: str


@router.post("/api/phrases/json")
async def add_phrase_json(
    body: PhraseAddBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    text = body.phrase.strip()[:200]
    if not text:
        raise HTTPException(status_code=400, detail="Phrase cannot be empty")
    new_phrase = UserPhrase(user_id=user.id, phrase=text)
    db.add(new_phrase)
    await db.commit()
    await db.refresh(new_phrase)
    return {"ok": True, "id": new_phrase.id, "phrase": new_phrase.phrase}


class DeletePhraseBody(BaseModel):
    phrase_id: int | None = None
    phrase: str | None = None


@router.post("/api/phrases/delete")
async def delete_phrase(
    body: DeletePhraseBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import delete
    if body.phrase_id:
        await db.execute(delete(UserPhrase).where(UserPhrase.id == body.phrase_id, UserPhrase.user_id == user.id))
    elif body.phrase:
        await db.execute(delete(UserPhrase).where(UserPhrase.phrase == body.phrase.strip(), UserPhrase.user_id == user.id))
    await db.commit()
    return {"ok": True}


@router.get("/api/bot/preview-sample")
async def preview_sample_comment(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.bot.engine import _load_custom_phrases
    from app.bot.messages import MessageGenerator
    phrases = await _load_custom_phrases(db, user.id)
    gen = MessageGenerator(custom_phrases=phrases)
    return {"ok": True, "message": gen.next_message()}
