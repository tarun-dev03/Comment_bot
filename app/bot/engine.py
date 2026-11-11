import asyncio
import logging
import random
import time
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.google_oauth import get_access_token_for_user, invalidate_token_cache
from app.bot.messages import MessageGenerator
from app.config import get_settings
from app.db import async_session_factory
from app.models import BotJob, BotJobStatus, UserCookie, UserPhrase
from app.security import decrypt_text, encrypt_text
from app.youtube.live_chat import (
    YouTubeAPIError,
    insert_live_chat_message,
    invalidate_innertube_cache,
    send_innertube_live_chat_message,
)

logger = logging.getLogger(__name__)

_active_tasks: dict[int, asyncio.Task] = {}
_stop_flags: dict[int, asyncio.Event] = {}
_generators: dict[int, MessageGenerator] = {}
_user_locks: dict[int, asyncio.Lock] = {}
_restarting_users: set[int] = set()
_last_known_public_url: str | None = None
_last_global_keepalive: float = 0.0


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


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
    """Send periodic health ping to keep Render web container awake while any bot is running."""
    settings = get_settings()
    app_url = _last_known_public_url or (settings.app_url or "").rstrip("/")
    if app_url.startswith("http://") or app_url.startswith("https://"):
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{app_url}/health")
        except Exception:
            pass


async def _persist_job_error(
    user_id: int,
    message: str,
    *,
    mark_error_status: bool = False,
) -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
        job = result.scalar_one_or_none()
        if not job:
            return
        job.last_error = message
        if mark_error_status:
            job.status = BotJobStatus.ERROR.value
        await db.commit()


async def _persist_job_success(
    user_id: int,
    *,
    today: str,
    mode: str,
    original_cookie_encrypted: str | None,
    cookie_str: str | None,
    updated_cookie_str: str | None,
) -> bool:
    """Persist successful comment send. Returns False if job was stopped externally."""
    async with async_session_factory() as db:
        result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
        job = result.scalar_one_or_none()
        if not job or job.status != BotJobStatus.RUNNING.value:
            return False

        if job.quota_day != today:
            job.quota_day = today
            job.messages_sent_today = 0

        job.messages_sent += 1
        job.messages_sent_today += 1
        job.last_message_at = datetime.now(UTC)
        job.last_error = None

        if (
            mode == "cookie"
            and updated_cookie_str
            and cookie_str
            and updated_cookie_str != cookie_str
            and original_cookie_encrypted is not None
        ):
            c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user_id))
            row_cookie = c_res.scalar_one_or_none()
            # Only persist rotated cookies if user/extension hasn't written a newer cookie mid-flight
            if row_cookie and row_cookie.cookie_encrypted == original_cookie_encrypted:
                row_cookie.cookie_encrypted = encrypt_text(updated_cookie_str)

        await db.commit()
        return True


async def _bot_loop(user_id: int, stop_event: asyncio.Event) -> None:
    global _last_global_keepalive
    consecutive_errors = 0
    consecutive_auth_errors = 0
    max_consecutive_transient_errors = 5
    max_consecutive_auth_errors = 4
    current_task = asyncio.current_task()

    try:
        while not stop_event.is_set():
            now_ts = time.time()
            if now_ts - _last_global_keepalive > 240:
                _last_global_keepalive = now_ts
                asyncio.create_task(_keep_alive_ping())

            try:
                # Phase 1: Brief DB read — release DB session before any HTTP call or sleep
                today = _today_key()
                interval_sec = 30
                mode = "oauth"
                video_id = ""
                live_chat_id = ""
                cookie_str: str | None = None
                original_cookie_encrypted: str | None = None
                access_token: str | None = None

                async with async_session_factory() as db:
                    result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
                    job = result.scalar_one_or_none()
                    if not job or job.status != BotJobStatus.RUNNING.value:
                        break

                    if job.quota_day != today:
                        job.quota_day = today
                        job.messages_sent_today = 0
                        await db.commit()

                    interval_sec = job.interval_seconds or 30
                    mode = job.mode or "oauth"
                    video_id = job.video_id
                    live_chat_id = job.live_chat_id

                    phrases = await _load_custom_phrases(db, user_id)
                    if user_id not in _generators:
                        _generators[user_id] = MessageGenerator(custom_phrases=phrases)
                    else:
                        _generators[user_id].update_custom_phrases(phrases)

                    if mode == "cookie":
                        c_res = await db.execute(select(UserCookie).where(UserCookie.user_id == user_id))
                        row_cookie = c_res.scalar_one_or_none()
                        if not row_cookie:
                            job.status = BotJobStatus.ERROR.value
                            job.last_error = "YouTube Cookie missing. Save your YouTube Cookie in dashboard settings."
                            await db.commit()
                            break
                        original_cookie_encrypted = row_cookie.cookie_encrypted
                        cookie_str = decrypt_text(original_cookie_encrypted)
                    else:
                        access_token = await get_access_token_for_user(db, user_id)

                if stop_event.is_set():
                    break

                # Phase 2: Perform YouTube HTTP operation completely outside DB transaction
                text = _generators[user_id].next_message()
                updated_cookie_str: str | None = None

                try:
                    if mode == "cookie":
                        assert cookie_str is not None
                        updated_cookie_str = await send_innertube_live_chat_message(
                            cookie_str, video_id, text, user_id=user_id
                        )
                    else:
                        if not access_token:
                            consecutive_auth_errors += 1
                            if consecutive_auth_errors >= max_consecutive_auth_errors:
                                await _persist_job_error(
                                    user_id,
                                    "Google/YouTube authorization missing or expired. Please tap Reconnect Google.",
                                    mark_error_status=True,
                                )
                                break
                            backoff = min(15 * consecutive_auth_errors, 60)
                            await _persist_job_error(
                                user_id,
                                f"Refreshing YouTube token ({consecutive_auth_errors}/{max_consecutive_auth_errors}). Retrying in {backoff}s...",
                            )
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                break
                            except TimeoutError:
                                continue

                        await insert_live_chat_message(access_token, live_chat_id, text)

                    # Phase 3: Persist success briefly
                    consecutive_errors = 0
                    consecutive_auth_errors = 0
                    still_running = await _persist_job_success(
                        user_id,
                        today=today,
                        mode=mode,
                        original_cookie_encrypted=original_cookie_encrypted,
                        cookie_str=cookie_str,
                        updated_cookie_str=updated_cookie_str,
                    )
                    if not still_running:
                        break
                except YouTubeAPIError as e:
                    if e.is_auth:
                        consecutive_auth_errors += 1
                        if mode == "oauth":
                            invalidate_token_cache(user_id)
                            async with async_session_factory() as db:
                                access_retry = await get_access_token_for_user(db, user_id, force_refresh=True)
                            if access_retry:
                                try:
                                    await insert_live_chat_message(access_retry, live_chat_id, text)
                                    consecutive_errors = 0
                                    consecutive_auth_errors = 0
                                    still_running = await _persist_job_success(
                                        user_id,
                                        today=today,
                                        mode=mode,
                                        original_cookie_encrypted=None,
                                        cookie_str=None,
                                        updated_cookie_str=None,
                                    )
                                    if not still_running:
                                        break
                                    delay = random.uniform(interval_sec, interval_sec + 5)
                                    try:
                                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                                        break
                                    except TimeoutError:
                                        continue
                                except YouTubeAPIError as e_retry:
                                    e = e_retry

                            if consecutive_auth_errors >= max_consecutive_auth_errors:
                                await _persist_job_error(
                                    user_id,
                                    f"YouTube authorization error: {e}. Please tap Reconnect Google.",
                                    mark_error_status=True,
                                )
                                break
                            backoff = min(15 * consecutive_auth_errors, 60)
                            await _persist_job_error(
                                user_id,
                                f"Refreshing YouTube authorization ({consecutive_auth_errors}/{max_consecutive_auth_errors}). Retrying in {backoff}s...",
                            )
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                break
                            except TimeoutError:
                                continue
                        else:
                            invalidate_innertube_cache(video_id=video_id, user_id=user_id)
                            if consecutive_auth_errors >= max_consecutive_auth_errors:
                                await _persist_job_error(
                                    user_id,
                                    f"YouTube Cookie auth error: {e}",
                                    mark_error_status=True,
                                )
                                break
                            backoff = min(10 * consecutive_auth_errors, 60)
                            await _persist_job_error(
                                user_id,
                                f"Cookie session notice ({consecutive_auth_errors}/{max_consecutive_auth_errors}): {e}. Retrying in {backoff}s...",
                            )
                            try:
                                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                                break
                            except TimeoutError:
                                continue

                    if e.is_quota:
                        err_msg = (
                            f"YouTube Web Chat error: {e}"
                            if mode == "cookie"
                            else (
                                "YouTube API daily quota limit reached (10,000 units/day). "
                                "Quota resets at 00:00 PST (midnight). Switch to YouTube Web Cookie Mode for quota-free sending."
                            )
                        )
                        await _persist_job_error(user_id, err_msg, mark_error_status=True)
                        break
                    elif e.status_code == 400 and any(k in str(e).lower() for k in ("filter", "confirm")):
                        logger.warning("Message was filtered by YouTube for user %s: %s", user_id, e)
                        await _persist_job_error(
                            user_id,
                            "Notice: A message was filtered by YouTube chat. Continuing with next comment...",
                        )
                    elif e.is_transient:
                        consecutive_errors += 1
                        logger.warning(
                            "Transient YouTube error for user %s (%d/%d): %s",
                            user_id,
                            consecutive_errors,
                            max_consecutive_transient_errors,
                            e,
                        )
                        if consecutive_errors >= max_consecutive_transient_errors:
                            await _persist_job_error(
                                user_id,
                                f"Bot stopped after {consecutive_errors} network/API errors: {e}",
                                mark_error_status=True,
                            )
                            break
                        backoff = min(15 * (2 ** (consecutive_errors - 1)), 180)
                        await _persist_job_error(
                            user_id,
                            f"Temporary warning ({consecutive_errors}/{max_consecutive_transient_errors}): {e}. Retrying...",
                        )
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                            break
                        except TimeoutError:
                            continue
                    else:
                        await _persist_job_error(user_id, str(e), mark_error_status=True)
                        break
                except Exception as exc:
                    consecutive_errors += 1
                    logger.exception(
                        "Unexpected error in bot message iteration for user %s (%d/%d)",
                        user_id,
                        consecutive_errors,
                        max_consecutive_transient_errors,
                    )
                    if consecutive_errors >= max_consecutive_transient_errors:
                        await _persist_job_error(
                            user_id,
                            f"Bot stopped after {consecutive_errors} unexpected errors: {exc}",
                            mark_error_status=True,
                        )
                        break
                    backoff = min(10 * (2 ** (consecutive_errors - 1)), 60)
                    await _persist_job_error(
                        user_id,
                        f"Temporary warning ({consecutive_errors}/{max_consecutive_transient_errors}): {exc}. Retrying...",
                    )
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                        break
                    except TimeoutError:
                        continue
            except Exception as db_exc:
                consecutive_errors += 1
                logger.exception(
                    "Database/session error in bot loop for user %s (%d/%d): %s",
                    user_id,
                    consecutive_errors,
                    max_consecutive_transient_errors,
                    db_exc,
                )
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
        is_owner = _active_tasks.get(user_id) is current_task
        if is_owner and user_id not in _restarting_users:
            try:
                async with async_session_factory() as db:
                    result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
                    job = result.scalar_one_or_none()
                    if job and job.status == BotJobStatus.RUNNING.value:
                        job.status = BotJobStatus.STOPPED.value
                        await db.commit()
            except Exception as exc:
                logger.warning("Could not update final bot status in DB for user %s: %s", user_id, exc)
        if is_owner:
            _active_tasks.pop(user_id, None)
            _stop_flags.pop(user_id, None)
            _generators.pop(user_id, None)


async def _stop_bot_task_unlocked(user_id: int, update_db: bool = True) -> None:
    event = _stop_flags.get(user_id)
    if event:
        event.set()
    task = _active_tasks.get(user_id)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        except (asyncio.CancelledError, Exception):
            pass

    if update_db:
        async with async_session_factory() as db:
            result = await db.execute(select(BotJob).where(BotJob.user_id == user_id))
            job = result.scalar_one_or_none()
            if job and job.status == BotJobStatus.RUNNING.value:
                job.status = BotJobStatus.STOPPED.value
                await db.commit()


async def start_bot_task(user_id: int) -> None:
    async with _get_user_lock(user_id):
        _restarting_users.add(user_id)
        try:
            await _stop_bot_task_unlocked(user_id, update_db=False)
        finally:
            _restarting_users.discard(user_id)

        async with async_session_factory() as db:
            phrases = await _load_custom_phrases(db, user_id)

        stop_event = asyncio.Event()
        _generators[user_id] = MessageGenerator(custom_phrases=phrases)
        _stop_flags[user_id] = stop_event
        task = asyncio.create_task(_bot_loop(user_id, stop_event))
        _active_tasks[user_id] = task


async def stop_bot_task(user_id: int, update_db: bool = True) -> None:
    async with _get_user_lock(user_id):
        await _stop_bot_task_unlocked(user_id, update_db=update_db)


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
                if not is_bot_running(job.user_id):
                    logger.info("Auto-resuming bot job for user %s", job.user_id)
                    await start_bot_task(job.user_id)
    except Exception as exc:
        logger.warning("Failed to auto-resume running bots on startup: %s", exc)
