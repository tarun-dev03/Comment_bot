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
    settings = get_settings()
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    max_duration_seconds = 3600  # Run for 1 hour

    try:
        while not stop_event.is_set():
            if loop.time() - start_time >= max_duration_seconds:
                logger.info("Bot for user %s completed 1 hour runtime limit", user_id)
                break

            async with async_session_factory() as db:
                result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
                job = result.scalar_one_or_none()
                if not job or job.status != BotJobStatus.RUNNING.value:
                    break

                today = _today_key()
                if job.quota_day != today:
                    job.quota_day = today
                    job.messages_sent_today = 0

                access = await get_access_token_for_user(db, user_id)
                if not access:
                    job.status = BotJobStatus.ERROR.value
                    job.last_error = "Google/YouTube not connected or token expired."
                    await db.commit()
                    break

                phrases = await _load_custom_phrases(db, user_id)
                if user_id not in _generators:
                    _generators[user_id] = MessageGenerator(custom_phrases=phrases)
                generator = _generators[user_id]
                text = generator.next_message()

                try:
                    await insert_live_chat_message(access, job.live_chat_id, text)
                except YouTubeAPIError as e:
                    job.status = BotJobStatus.ERROR.value
                    job.last_error = str(e)
                    await db.commit()
                    break

                job.messages_sent += 1
                job.messages_sent_today += 1
                job.last_message_at = datetime.now(UTC)
                job.last_error = None
                await db.commit()

            delay = random.uniform(10, 20)
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
