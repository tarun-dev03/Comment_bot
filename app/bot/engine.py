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


def _today_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def _load_custom_phrases(db: AsyncSession, user_id: int) -> list[str]:
    result = await db.execute(select(UserPhrase.phrase).where(UserPhrase.user_id == user_id))
    return [row[0] for row in result.all()]


async def _bot_loop(user_id: int) -> None:
    stop_event = _stop_flags[user_id]
    consecutive_errors = 0
    max_consecutive_transient_errors = 5

    try:
        while not stop_event.is_set():
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
                        await send_innertube_live_chat_message(cookie_str, job.video_id, text)
                    else:
                        access = await get_access_token_for_user(db, user_id)
                        if not access:
                            job.status = BotJobStatus.ERROR.value
                            job.last_error = "Google/YouTube authorization missing or expired."
                            await db.commit()
                            break
                        await insert_live_chat_message(access, job.live_chat_id, text)

                    consecutive_errors = 0
                    job.messages_sent += 1
                    job.messages_sent_today += 1
                    job.last_message_at = datetime.now(UTC)
                    job.last_error = None
                    await db.commit()
                except YouTubeAPIError as e:
                    if e.is_auth:
                        if mode == "oauth":
                            from app.auth.google_oauth import invalidate_token_cache
                            invalidate_token_cache(user_id)
                            access_retry = await get_access_token_for_user(db, user_id, force_refresh=True)
                            if access_retry:
                                try:
                                    await insert_live_chat_message(access_retry, job.live_chat_id, text)
                                    consecutive_errors = 0
                                    job.messages_sent += 1
                                    job.messages_sent_today += 1
                                    job.last_message_at = datetime.now(UTC)
                                    job.last_error = None
                                    await db.commit()
                                    continue
                                except YouTubeAPIError as e_retry:
                                    e = e_retry
                        else:
                            job.status = BotJobStatus.ERROR.value
                            job.last_error = f"YouTube Cookie auth error: {e}"
                            await db.commit()
                            break

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
                    logger.exception("Unexpected error in bot loop for user %s", user_id)
                    job.status = BotJobStatus.ERROR.value
                    job.last_error = f"Unexpected error: {exc}"
                    await db.commit()
                    break

            delay = random.uniform(interval_sec, interval_sec + 5)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue
    finally:
        async with async_session_factory() as db:
            result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
            job = result.scalar_one_or_none()
            if job and job.status == BotJobStatus.RUNNING.value:
                job.status = BotJobStatus.STOPPED.value
                await db.commit()
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
