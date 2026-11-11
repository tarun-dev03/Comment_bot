import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.email_login import request_email_login, verify_email_login
from app.auth.google_oauth import (
    build_google_authorize_url,
    exchange_code_for_tokens,
    fetch_google_email,
    get_access_token_for_user,
    get_user_refresh_token,
    save_oauth_token,
)
from app.auth.oauth_state import create_oauth_state, pop_oauth_state
from app.bot.engine import _load_custom_phrases, is_bot_running, record_public_url, start_bot_task, stop_bot_task
from app.bot.messages import MessageGenerator, get_conversation_db_stats
from app.config import get_settings
from app.db import async_session_factory, get_db
from app.deps import (
    SESSION_COOKIE,
    ensure_user_sync_token,
    get_current_user,
    get_current_user_optional,
    resolve_api_user_optional,
)
from app.models import BotJob, BotJobStatus, OAuthToken, User, UserCookie, UserPhrase
from app.security import apply_session_cookie, encrypt_text
from app.youtube.live_chat import (
    YouTubeAPIError,
    fetch_live_chat_id,
    invalidate_innertube_cache,
    parse_video_id,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _public_base_url(request: Request) -> str:
    """Use the URL the user actually opened (fixes wrong APP_URL -> Google 404)."""
    forwarded = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded and host:
        url = f"{forwarded}://{host}".rstrip("/")
    else:
        url = str(request.base_url).rstrip("/")
    record_public_url(url)
    return url


def _google_oauth_configured() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret)


async def _begin_google_oauth(
    request: Request,
    db: AsyncSession,
    user_id: int | None,
    login_hint: str | None = None,
) -> RedirectResponse:
    if not _google_oauth_configured():
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    state = secrets.token_urlsafe(16)
    redirect_uri = f"{_public_base_url(request)}/auth/google/callback"
    await create_oauth_state(db, state=state, redirect_uri=redirect_uri, user_id=user_id)
    return RedirectResponse(
        build_google_authorize_url(state, redirect_uri, login_hint=login_hint),
        status_code=303,
    )


async def _user_for_google_login(db: AsyncSession, google_email: str) -> User:
    email = google_email.strip().lower()
    result = await db.execute(select(User).where(func.lower(User.email) == email))
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
    db: AsyncSession = Depends(get_db),
):
    _public_base_url(request)
    user, auth_source = await resolve_api_user_optional(request, db)
    if user:
        if auth_source == "session":
            apply_session_cookie(response, user.id, request=request)
        j_res = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
        job = j_res.scalar_one_or_none()
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        has_cookie = c_res.scalar_one_or_none() is not None
        o_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        oauth = o_res.scalar_one_or_none()
        return {
            "authenticated": True,
            "email": user.email,
            "google_email": oauth.google_email if oauth else None,
            "user_id": user.id,
            "stream_url": job.stream_url if job else "",
            "mode": job.mode if job else ("cookie" if has_cookie else "oauth"),
            "interval_seconds": job.interval_seconds if job else 30,
            "has_cookie": has_cookie,
            "has_oauth": oauth is not None,
        }
    return {
        "authenticated": False,
        "email": None,
        "user_id": None,
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
            <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:2rem;max-width:720px;margin:0 auto;line-height:1.6;background:#080c0a;color:#f2f6f3;">
            <h1 style="color:#f2df68;margin-bottom:0.5rem;">Database Connection Notice</h1>
            <p style="color:#a3b1a7;">The database is currently unreachable. If your free Render PostgreSQL database expired (Render free databases expire after 30 days), you do <b>not</b> need to pay for premium!</p>
            <div style="background:rgba(222,203,75,0.12);border:1px solid rgba(222,203,75,0.34);border-radius:10px;padding:1rem;margin:1rem 0;">
                <b style="color:#f7ecaa;">Error details:</b>
                <pre style="background:#0e1411;border:1px solid rgba(214,232,220,0.1);padding:0.75rem;border-radius:6px;overflow:auto;font-size:0.875rem;margin-top:0.5rem;color:#f9efb9;">{db_error}</pre>
            </div>
            <h3>How to fix this for FREE:</h3>
            <ol style="padding-left:1.25rem;color:#a3b1a7;">
                <li style="margin-bottom:0.75rem;">
                    <b style="color:#f2f6f3;">Option 1: Free permanent cloud Postgres (Recommended)</b><br>
                    Create a free Postgres database on <a href="https://neon.tech" target="_blank" style="color:#4ade80;">Neon.tech</a> (free forever, 0.5 GB, 30s setup, no credit card required).<br>
                    Copy the connection string, go to your Render Web Service &rarr; <b>Environment</b> &rarr; set <code>DATABASE_URL</code> to the Neon URL &rarr; Save Changes.
                </li>
                <li style="margin-bottom:0.75rem;">
                    <b style="color:#f2f6f3;">Option 2: Local SQLite</b><br>
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
    sync_token = ""
    if user:
        async with async_session_factory() as db:
            # Reload user within this session so sync_token updates persist cleanly
            u_res = await db.execute(select(User).where(User.id == user.id))
            db_user = u_res.scalar_one_or_none() or user
            sync_token = await ensure_user_sync_token(db, db_user)

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

    auth_error = request.query_params.get("auth_error")
    auth_error_msg = None
    if auth_error == "account_mismatch":
        expected = request.query_params.get("expected", "")
        got = request.query_params.get("got", "")
        auth_error_msg = (
            f"Account mismatch blocked: you are signed in as {expected}, "
            f"but authorized {got}. Sign out or switch accounts to use {got}."
        )
    elif auth_error == "account_in_use":
        got = request.query_params.get("got", "")
        auth_error_msg = (
            f"Cannot link {got} to this session because {got} already belongs to another user account. "
            "Sign out and continue with Google as that account instead."
        )

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
            "sync_token": sync_token,
            "running": bool(user and is_bot_running(user.id)),
            "settings": get_settings(),
            "auth_error_msg": auth_error_msg,
            "conversation_stats": get_conversation_db_stats(),
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
    """One-step sign-in / switch account: Google account + YouTube scope."""
    return await _begin_google_oauth(request, db, None)


@router.get("/auth/google/start")
async def google_start(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reconnect or grant YouTube access for the currently signed-in account."""
    res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
    oauth = res.scalar_one_or_none()
    hint = None
    if oauth and oauth.google_email:
        hint = oauth.google_email
    elif user.email and not user.email.endswith(".local"):
        hint = user.email
    return await _begin_google_oauth(request, db, user.id, login_hint=hint)


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return RedirectResponse(f"/login?error={quote(error)}", status_code=303)
    if not code or not state:
        return RedirectResponse("/login?error=missing_code", status_code=303)

    pending = await pop_oauth_state(db, state)
    if not pending:
        return RedirectResponse("/login?error=invalid_state", status_code=303)

    linked_user_id = pending.user_id
    redirect_uri = pending.redirect_uri

    try:
        token_data = await exchange_code_for_tokens(code, redirect_uri)
    except Exception:
        return RedirectResponse("/login?error=oauth_exchange_failed", status_code=303)

    refresh = token_data.get("refresh_token")
    access = token_data.get("access_token")

    google_email = None
    if access:
        google_email = await fetch_google_email(access)

    if not google_email:
        return RedirectResponse("/login?error=no_google_email", status_code=303)

    google_email_norm = google_email.strip().lower()

    if linked_user_id is None:
        if not refresh:
            return RedirectResponse("/login?error=no_refresh_token", status_code=303)
        user = await _user_for_google_login(db, google_email_norm)
        user_id = user.id
    else:
        user_id = linked_user_id
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return RedirectResponse("/login?error=session_expired", status_code=303)

        # Prevent silent account mismatch or overwriting another user's Google account
        existing_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user_id))
        existing_oauth = existing_res.scalar_one_or_none()

        if existing_oauth and existing_oauth.google_email:
            expected_email = existing_oauth.google_email.strip().lower()
            if expected_email != google_email_norm:
                return RedirectResponse(
                    f"/?auth_error=account_mismatch&expected={quote(expected_email)}&got={quote(google_email_norm)}",
                    status_code=303,
                )

        user_email_norm = user.email.strip().lower()
        if user_email_norm.endswith(("@gmail.com", "@googlemail.com")) and user_email_norm != google_email_norm:
            return RedirectResponse(
                f"/?auth_error=account_mismatch&expected={quote(user_email_norm)}&got={quote(google_email_norm)}",
                status_code=303,
            )

        other_user_res = await db.execute(
            select(User).where(func.lower(User.email) == google_email_norm, User.id != user_id)
        )
        if other_user_res.scalar_one_or_none():
            return RedirectResponse(
                f"/?auth_error=account_in_use&got={quote(google_email_norm)}",
                status_code=303,
            )

        other_oauth_res = await db.execute(
            select(OAuthToken).where(
                func.lower(OAuthToken.google_email) == google_email_norm,
                OAuthToken.user_id != user_id,
            )
        )
        if other_oauth_res.scalar_one_or_none():
            return RedirectResponse(
                f"/?auth_error=account_in_use&got={quote(google_email_norm)}",
                status_code=303,
            )

        if not refresh:
            refresh = await get_user_refresh_token(db, user_id)
            if not refresh:
                return RedirectResponse("/login?error=no_refresh_token", status_code=303)

    await save_oauth_token(db, user_id, refresh, google_email_norm)

    home_url = f"{_public_base_url(request)}/"
    response = RedirectResponse(home_url, status_code=303)
    apply_session_cookie(response, user_id, request=request)
    return response


@router.get("/api/auth/sync-token")
async def get_sync_token(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u_res = await db.execute(select(User).where(User.id == user.id))
    db_user = u_res.scalar_one_or_none() or user
    token = await ensure_user_sync_token(db, db_user)
    return {
        "ok": True,
        "sync_token": token,
        "user_id": db_user.id,
        "email": db_user.email,
    }


@router.post("/api/auth/sync-token/rotate")
async def rotate_sync_token(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    u_res = await db.execute(select(User).where(User.id == user.id))
    db_user = u_res.scalar_one_or_none() or user
    token = await ensure_user_sync_token(db, db_user, regenerate=True)
    return {
        "ok": True,
        "sync_token": token,
        "user_id": db_user.id,
        "email": db_user.email,
    }


@router.post("/api/auth/cookie")
async def save_cookie(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    cookie_val = None
    is_json = False
    data: dict = {}
    expected_user_id_raw = None
    body_sync_token = None
    auto_sync = False

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        is_json = True
        try:
            data = await request.json()
            cookie_val = data.get("cookie", "")
            expected_user_id_raw = data.get("expected_user_id")
            body_sync_token = data.get("sync_token")
            auto_sync = bool(data.get("auto_sync", False))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    else:
        form = await request.form()
        cookie_val = form.get("cookie", "")
        expected_user_id_raw = form.get("expected_user_id")

    user, auth_source = await resolve_api_user_optional(request, db, body_token=body_sync_token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Not signed in. Sign in to Comment Bot or configure your Extension Sync Key.",
        )

    if expected_user_id_raw is not None and str(expected_user_id_raw).strip() != "":
        try:
            expected_id = int(str(expected_user_id_raw).strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid expected_user_id") from exc
        if expected_id != user.id:
            raise HTTPException(
                status_code=409,
                detail="Account mismatch: browser session switched to another user. Please refresh the page.",
            )

    requested_email = (data.get("email") or "").strip().lower() if is_json else ""
    if requested_email:
        o_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        oauth = o_res.scalar_one_or_none()
        allowed_emails = {user.email.strip().lower()}
        if oauth and oauth.google_email:
            allowed_emails.add(oauth.google_email.strip().lower())
        if requested_email not in allowed_emails:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Account mismatch: requested {requested_email}, "
                    f"but authenticated account is {user.email}."
                ),
            )

    text_val = (str(cookie_val) if cookie_val is not None else "").strip()
    if not text_val:
        raise HTTPException(status_code=400, detail="Cookie string cannot be empty")

    encrypted = encrypt_text(text_val)
    res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
    existing = res.scalar_one_or_none()
    if existing:
        existing.cookie_encrypted = encrypted
    else:
        db.add(UserCookie(user_id=user.id, cookie_encrypted=encrypted))

    # Invalidate ONLY this user's cached InnerTube sessions
    invalidate_innertube_cache(user_id=user.id)

    res_job = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = res_job.scalar_one_or_none()
    if job:
        # Never hijack an actively running OAuth bot into cookie mode during background auto-sync
        if not (auto_sync and job.status == BotJobStatus.RUNNING.value and job.mode == "oauth"):
            if not auto_sync:
                job.mode = "cookie"
        if job.mode == "cookie":
            job.last_error = None
            if job.status == BotJobStatus.ERROR.value:
                job.status = BotJobStatus.STOPPED.value
    await db.commit()

    if is_json or "application/json" in request.headers.get("accept", ""):
        res_json = JSONResponse(
            {
                "ok": True,
                "message": "YouTube Cookie updated successfully",
                "user": user.email,
                "user_id": user.id,
            }
        )
        if auth_source == "session":
            apply_session_cookie(res_json, user.id, request=request)
        return res_json

    res_redir = RedirectResponse("/", status_code=303)
    if auth_source == "session":
        apply_session_cookie(res_redir, user.id, request=request)
    return res_redir


class StreamUrlBody(BaseModel):
    stream_url: str


@router.get("/api/settings/stream-url")
async def get_stream_url_setting(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user, _ = await resolve_api_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    return {
        "ok": True,
        "user_id": user.id,
        "stream_url": job.stream_url if job else "",
        "video_id": job.video_id if job else "",
        "mode": job.mode if job else "oauth",
        "interval_seconds": job.interval_seconds if job else 30,
    }


@router.post("/api/settings/stream-url")
async def save_stream_url_setting(
    request: Request,
    body: StreamUrlBody,
    db: AsyncSession = Depends(get_db),
):
    user, _ = await resolve_api_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
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

    # Persist stream_url, video_id, mode, and interval to database first
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
        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user.id))
        if not c_res.scalar_one_or_none():
            raise HTTPException(
                status_code=400,
                detail="Web Cookie not configured for this account. Sync/save your YouTube cookie first or select Google OAuth API.",
            )
    else:
        o_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
        if not o_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Connect Google/YouTube first (tap Connect Google above).")

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
    return {"ok": True, "video_title": title, "mode": mode, "status": "running", "user_id": user.id}


@router.post("/api/bots/stop")
async def api_stop_bot(user: User = Depends(get_current_user)):
    await stop_bot_task(user.id)
    return {"ok": True, "status": "stopped", "user_id": user.id}


@router.get("/api/bots/status")
async def api_bot_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BotJob).where(BotJob.user_id == user.id))
    job = result.scalar_one_or_none()
    o_res = await db.execute(select(OAuthToken).where(OAuthToken.user_id == user.id))
    oauth = o_res.scalar_one_or_none()
    if not job:
        return {
            "user_id": user.id,
            "email": user.email,
            "google_email": oauth.google_email if oauth else None,
            "status": "stopped",
            "running": False,
            "interval_seconds": 30,
            "mode": "oauth",
            "video_id": "",
        }
    return {
        "user_id": user.id,
        "email": user.email,
        "google_email": oauth.google_email if oauth else None,
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
    phrases = await _load_custom_phrases(db, user.id)
    gen = MessageGenerator(custom_phrases=phrases)
    return {"ok": True, "message": gen.next_message()}


@router.get("/api/chats/conversation-stats")
async def conversation_chat_stats(
    user: User = Depends(get_current_user),
):
    return {"ok": True, "user_id": user.id, **get_conversation_db_stats()}
