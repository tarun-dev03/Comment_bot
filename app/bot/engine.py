import asyncio
import logging
import random
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.google_oauth import get_access_token_for_user
from app.bot.messages import MessageGenerator
from app.config import get_settings
from app.db import async_session_factory
from app.models import BotJob, BotJobStatus, UserPhrase
from app.youtube.live_chat import YouTubeAPIError, insert_live_chat_message

logger = logging.getLogger(__name__)

_active_tasks: dict[int, asyncio.Task] = {}
_stop_flags: dict[int, asyncio.Event] = {}
_generators: dict[int, MessageGenerator] = {}
_last_known_public_url: str | None = None


def record_public_url(url: str) -> None:
    global _last_known_public_url
    if url and (url.startswith("http://") or url.startswith("https://")):
        if "localhost" not in url and "127.0.0.1" not in url:
            _last_known_public_url = url.rstrip("/")
        elif not _last_known_public_url:
            _last_known_public_url = url.rstrip("/")


def _today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def _load_custom_phrases(db: AsyncSession, user_id: int) -> list[str]:
    result = await db.execute(select(UserPhrase.phrase).where(UserPhrase.user_id == user_id))
    return [row[0] for row in result.all()]


async def _keep_alive_ping() -> None:
    """Send periodic health ping to keep Render web container awake while bot is running."""
    settings = get_settings()
    app_url = _last_known_public_url or (settings.app_url or "").rstrip("/")
    if app_url.startswith("http://") or app_url.startswith("https://"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{app_url}/health")
        except Exception:
            pass


async def _bot_loop(user_id: int) -> None:
    stop_event = _stop_flags[user_id]
    consecutive_errors = 0
    consecutive_auth_errors = 0
    max_consecutive_transient_errors = 5
    max_consecutive_auth_errors = 4
    last_keepalive = 0.0

    try:
        while not stop_event.is_set():
            import time
            now_ts = time.time()
            if now_ts - last_keepalive > 240:  # ping every 4 mins to prevent Render sleep
                last_keepalive = now_ts
                asyncio.create_task(_keep_alive_ping())

            try:
                async with async_session_factory() as db:
                    result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
                    job = result.scalar_one_or_none()
                    if not job or job.status != BotJobStatus.RUNNING.value:
                        break

                    today = _today_key()
                    if job.quota_day != today:
                        job.quota_day = today
                        job.messages_sent_today = 0

                    interval_sec = job.interval_seconds or 30
                    mode = job.mode or "oauth"

                    phrases = await _load_custom_phrases(db, user_id)
                    if user_id not in _generators:
                        _generators[user_id] = MessageGenerator(custom_phrases=phrases)
                    else:
                        _generators[user_id].update_custom_phrases(phrases)
                    generator = _generators[user_id]
                    text = generator.next_message()

                    try:
                        if mode == "cookie":
                            from app.models import UserCookie
                            from app.security import decrypt_text
                            from app.youtube.live_chat import send_innertube_live_chat_message

                            c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user_id))
                            row_cookie = c_res.scalar_one_or_none()
                            if not row_cookie:
                                job.status = BotJobStatus.ERROR.value
                                job.last_error = "YouTube Cookie missing. Save your YouTube Cookie in dashboard settings."
                                await db.commit()
                                break
                            cookie_str = decrypt_text(row_cookie.cookie_encrypted)
                            updated_cookie_str = await send_innertube_live_chat_message(cookie_str, job.video_id, text)
                            if updated_cookie_str and updated_cookie_str != cookie_str:
                                from app.security import encrypt_text
                                row_cookie.cookie_encrypted = encrypt_text(updated_cookie_str)
                        else:
                            access = await get_access_token_for_user(db, user_id)
                            if not access:
                                consecutive_auth_errors += 1
                                if consecutive_auth_errors >= max_consecutive_auth_errors:
                                    job.status = BotJobStatus.ERROR.value
                                    job.last_error = "Google/YouTube authorization missing or expired. Please tap Reconnect Google."
                                    await db.commit()
                                    break
                                backoff = min(15 * consecutive_auth_errors, 60)
                                job.last_error = f"Refreshing YouTube token ({consecutive_auth_errors}/{max_consecutive_auth_errors}). Retrying in {backoff}s..."
                                await db.commit()
                                try:
                                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                    break
                                except TimeoutError:
                                    continue
                            await insert_live_chat_message(access, job.live_chat_id, text)

                        consecutive_errors = 0
                        consecutive_auth_errors = 0
                        job.messages_sent += 1
                        job.messages_sent_today += 1
                        job.last_message_at = datetime.now(UTC)
                        job.last_error = None
                        await db.commit()
                    except YouTubeAPIError as e:
                        if e.is_auth:
                            consecutive_auth_errors += 1
                            if mode == "oauth":
                                from app.auth.google_oauth import invalidate_token_cache
                                invalidate_token_cache(user_id)
                                access_retry = await get_access_token_for_user(db, user_id, force_refresh=True)
                                if access_retry:
                                    try:
                                        await insert_live_chat_message(access_retry, job.live_chat_id, text)
                                        consecutive_errors = 0
                                        consecutive_auth_errors = 0
                                        job.messages_sent += 1
                                        job.messages_sent_today += 1
                                        job.last_message_at = datetime.now(UTC)
                                        job.last_error = None
                                        await db.commit()
                                        continue
                                    except YouTubeAPIError as e_retry:
                                        e = e_retry

                                if consecutive_auth_errors >= max_consecutive_auth_errors:
                                    job.status = BotJobStatus.ERROR.value
                                    job.last_error = f"YouTube authorization error: {e}. Please tap Reconnect Google."
                                    await db.commit()
                                    break
                                backoff = min(15 * consecutive_auth_errors, 60)
                                job.last_error = f"Refreshing YouTube authorization ({consecutive_auth_errors}/{max_consecutive_auth_errors}). Retrying in {backoff}s..."
                                await db.commit()
                                try:
                                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                    break
                                except TimeoutError:
                                    continue
                            else:
                                from app.youtube.live_chat import invalidate_innertube_cache
                                invalidate_innertube_cache(job.video_id)
                                if consecutive_auth_errors >= max_consecutive_auth_errors:
                                    job.status = BotJobStatus.ERROR.value
                                    job.last_error = f"YouTube Cookie auth error: {e}"
                                    await db.commit()
                                    break
                                backoff = min(10 * consecutive_auth_errors, 60)
                                job.last_error = f"Cookie session notice ({consecutive_auth_errors}/{max_consecutive_auth_errors}): {e}. Retrying in {backoff}s..."
                                await db.commit()
                                try:
                                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                    break
                                except TimeoutError:
                                    continue

                        if e.is_quota:
                            job.status = BotJobStatus.ERROR.value
                            if mode == "cookie":
                                job.last_error = f"YouTube Web Chat error: {e}"
                            else:
                                job.last_error = (
                                    "YouTube API daily quota limit reached (10,000 units/day). "
                                    "Quota resets at 00:00 PST (midnight). Switch to YouTube Web Cookie Mode for quota-free sending."
                                )
                            await db.commit()
                            break
                        elif e.status_code == 400 and any(k in str(e).lower() for k in ("filter", "confirm")):
                            logger.warning("Message was filtered by YouTube for user %s: %s", user_id, e)
                            job.last_error = "Notice: A message was filtered by YouTube chat. Continuing with next comment..."
                            await db.commit()
                            # Standard interval delay will proceed below
                        elif e.is_transient:
                            consecutive_errors += 1
                            logger.warning(
                                "Transient YouTube error for user %s (%d/%d): %s",
                                user_id, consecutive_errors, max_consecutive_transient_errors, e
                            )
                            if consecutive_errors >= max_consecutive_transient_errors:
                                job.status = BotJobStatus.ERROR.value
                                job.last_error = f"Bot stopped after {consecutive_errors} network/API errors: {e}"
                                await db.commit()
                                break
                            backoff = min(15 * (2 ** (consecutive_errors - 1)), 180)
                            job.last_error = f"Temporary warning ({consecutive_errors}/{max_consecutive_transient_errors}): {e}. Retrying..."
                            await db.commit()
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                break
                            except TimeoutError:
                                continue
                        else:
                            job.status = BotJobStatus.ERROR.value
                            job.last_error = str(e)
                            await db.commit()
                            break
                    except Exception as exc:
                        consecutive_errors += 1
                        logger.exception("Unexpected error in bot message iteration for user %s (%d/%d)", user_id, consecutive_errors, max_consecutive_transient_errors)
                        if consecutive_errors >= max_consecutive_transient_errors:
                            job.status = BotJobStatus.ERROR.value
                            job.last_error = f"Bot stopped after {consecutive_errors} unexpected errors: {exc}"
                            await db.commit()
                            break
                        backoff = min(10 * (2 ** (consecutive_errors - 1)), 60)
                        job.last_error = f"Temporary warning ({consecutive_errors}/{max_consecutive_transient_errors}): {exc}. Retrying..."
                        await db.commit()
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                            break
                        except TimeoutError:
                            continue
            except Exception as db_exc:
                consecutive_errors += 1
                logger.exception("Database/session error in bot loop for user %s (%d/%d): %s", user_id, consecutive_errors, max_consecutive_transient_errors, db_exc)
                if consecutive_errors >= max_consecutive_transient_errors:
                    break
                backoff = min(5 * consecutive_errors, 30)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    break
                except TimeoutError:
                    continue

            delay = random.uniform(interval_sec, interval_sec + 5)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
    finally:
        try:
            async with async_session_factory() as db:
                result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
                job = result.scalar_one_or_none()
                if job and job.status == BotJobStatus.RUNNING.value:
                    job.status = BotJobStatus.STOPPED.value
                    await db.commit()
        except Exception as exc:
            logger.warning("Could not update final bot status in DB for user %s: %s", user_id, exc)
        _active_tasks.pop(user_id, None)
        _stop_flags.pop(user_id, None)
        _generators.pop(user_id, None)


async def start_bot_task(user_id: int) -> None:
    await stop_bot_task(user_id, update_db=False)
    async with async_session_factory() as db:
        phrases = await _load_custom_phrases(db, user_id)
    _generators[user_id] = MessageGenerator(custom_phrases=phrases)
    _stop_flags[user_id] = asyncio.Event()
    task = asyncio.create_task(_bot_loop(user_id))
    _active_tasks[user_id] = task


async def stop_bot_task(user_id: int, update_db: bool = True) -> None:
    event = _stop_flags.get(user_id)
    if event:
        event.set()
    task = _active_tasks.get(user_id)
    if task:
        try:
            await asyncio.wait_for(task, timeout=15.0)
        except TimeoutError:
            task.cancel()

    if update_db:
        async with async_session_factory() as db:
            result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
            job = result.scalar_one_or_none()
            if job and job.status == BotJobStatus.RUNNING.value:
                job.status = BotJobStatus.STOPPED.value
                await db.commit()


def is_bot_running(user_id: int) -> bool:
    task = _active_tasks.get(user_id)
    return task is not None and not task.done()


async def resume_running_bots() -> None:
    """Auto-resume any bots that were marked RUNNING before a server reboot or restart."""
    try:
        async with async_session_factory() as db:
            result = await db.execute(select(BotJob).where(BotJob.status == BotJobStatus.RUNNING.value))
            jobs = result.scalars().all()
            for job in jobs:
                if job.user_id not in _active_tasks:
                    logger.info("Auto-resuming bot job for user %s", job.user_id)
                    await start_bot_task(job.user_id)
    except Exception as exc:
        logger.warning("Failed to auto-resume running bots on startup: %s", exc)

