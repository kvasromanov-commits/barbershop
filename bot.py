import asyncio
import html as _html
import logging
import os
import signal

from aiogram import Bot, Dispatcher, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import ErrorEvent, Message

import db as _db_module
import keyboards
from config import BOT_TOKEN, load_config_from_db
from emoji_config import E
from handlers.admin import router as admin_router
from handlers.booking import router as booking_router
from handlers.info import router as info_router
from handlers.start import router as start_router
from logging_config import configure_logging
from middleware import (
    AdminCheckMiddleware,
    GlobalConcurrencyMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from monitoring import get_health_status, start_monitoring
from scheduler import shutdown_scheduler, start_scheduler
from storage import delete_old_scheduler_jobs, init_db

logger = logging.getLogger(__name__)


async def check_webhook_conflict(bot) -> str | None:
    """BUG-6 FIX: detect an externally-configured webhook that would conflict
    with polling mode.

    Telegram only allows one way of receiving updates. If a webhook is still set
    (e.g. from a previous webhook deployment) while this process polls, every
    getUpdates call raises TelegramConflictError. We surface the conflict to
    admins at startup so it is noticed and fixed instead of silently breaking
    the bot. Returns the offending webhook url (str) if set, else None. Errors
    are non-critical and swallowed.
    """
    try:
        wh = await bot.get_webhook_info()
    except Exception as e:  # network/API issues must not block startup
        logger.warning(f"get_webhook_info failed (non-critical): {e}")
        return None
    if not wh.url:
        return None
    logger.warning(
        f"Active external webhook detected: {wh.url!r} — bot runs in polling mode; getUpdates conflicts possible."
    )
    from config import ADMIN_IDS as _AIDS

    for _aid in _AIDS:
        try:
            await bot.send_message(
                _aid,
                f"{E.WARNING} <b>Внимание:</b> обнаружен активный webhook "
                f"<code>{_html.escape(wh.url)}</code>. Бот работает в режиме polling — "
                f"возможен конфликт getUpdates (TelegramConflictError). "
                f"Рекомендуется отключить webhook.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return wh.url


# HIGH-PRODUCTION: structured logging (JSON or human-readable).
# Set LOG_JSON=1 in env for fluentbit/Loki/CloudWatch ingestion.
configure_logging()
logger = logging.getLogger(__name__)


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Please configure .env")
        return

    # Proxy configuration (optional - set PROXY_URL in .env if Telegram is blocked)
    proxy = os.getenv("PROXY_URL", None)  # e.g., "http://proxy.example.com:8080"

    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiohttp import ClientTimeout

        timeout = ClientTimeout(total=60, connect=30, sock_connect=30, sock_read=30)
        session = AiohttpSession(proxy=proxy, timeout=timeout)
        bot = Bot(token=BOT_TOKEN, session=session)
        # P0 FIX: mask credentials in proxy URL to prevent leakage to logs/Sentry
        masked = proxy
        if "@" in masked:
            _scheme, _, _creds_host = masked.partition("://")
            _creds, _, _host = _creds_host.partition("@")
            if ":" in _creds:
                _user, _, _ = _creds.partition(":")
                masked = f"{_scheme}://{_user}:****@{_host}"
            else:
                masked = f"{_scheme}://****@{_host}"
        logger.info(f"Using proxy: {masked}")
    else:
        bot = Bot(token=BOT_TOKEN)
        logger.info("No proxy configured, using direct connection")

    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        try:
            # FIX: aiogram 3.x built-in RedisStorage (redis>=4.2 is dep of aioredis==2.0.1)
            from aiogram.fsm.storage.redis import RedisStorage

            storage = RedisStorage.from_url(redis_url)
            # Mask password in Redis URL for logs
            masked_url = redis_url
            if "@" in masked_url:
                _parts = masked_url.split("@", 1)
                _pcreds = _parts[0].rsplit(":", 1)
                if len(_pcreds) == 2:
                    masked_url = f"{_pcreds[0]}:****@{_parts[1]}"
            logger.info(f"Using Redis FSM storage: {masked_url}")
        except Exception as e:
            logger.warning(f"Redis not available, falling back to FileStorage: {e}")
            from fsm_storage import FileStorage

            storage = FileStorage()
    else:
        from fsm_storage import FileStorage

        storage = FileStorage()
        logger.info("Using FileStorage (no REDIS_URL set)")

    dp = Dispatcher(storage=storage)

    # HIGH-PRODUCTION: RequestID middleware MUST run first so every subsequent
    # middleware / handler inherits the same correlation id in logs.
    dp.message.middleware(RequestIDMiddleware())
    dp.callback_query.middleware(RequestIDMiddleware())

    # BUG-004 FIX: Register RateLimitMiddleware to prevent flooding
    dp.message.middleware(RateLimitMiddleware(max_requests=20, window=60))
    dp.callback_query.middleware(RateLimitMiddleware(max_requests=20, window=60))

    # SEC-13 FIX: bound global concurrent handler executions (DoS / pool exhaustion)
    from config import GLOBAL_CONCURRENCY

    dp.message.middleware(GlobalConcurrencyMiddleware(limit=GLOBAL_CONCURRENCY))
    dp.callback_query.middleware(GlobalConcurrencyMiddleware(limit=GLOBAL_CONCURRENCY))

    # NEW-001 FIX: Register AdminCheckMiddleware to set is_admin in data
    dp.message.middleware(AdminCheckMiddleware())
    dp.callback_query.middleware(AdminCheckMiddleware())

    dp.include_router(start_router)
    dp.include_router(booking_router)
    dp.include_router(info_router)
    dp.include_router(admin_router)

    # ROOT CAUSE FIX: handlers on dp run BEFORE sub-routers in aiogram 3.x,
    # so @dp.callback_query consumed callbacks before booking_router could handle them.
    # Solution: last-priority Router included after all other routers.
    from aiogram import Router as _FBRouter

    _fallback = _FBRouter(name="fallback")

    @_fallback.message(F.text, ~F.text.regexp(r"^/"), StateFilter(None))
    async def fsm_fallback_handler(message: Message, state: FSMContext):
        """Reached only when no other router matched + user has no FSM state."""
        await message.answer(
            f"{E.INFO} Напишите /start для начала работы.", reply_markup=keyboards.back_to_main_kb(), parse_mode="HTML"
        )

    from aiogram.types import CallbackQuery as callback_query  # noqa: N813

    @_fallback.callback_query()
    async def callback_fallback_handler(callback: callback_query, state: FSMContext):
        """Reached only when ALL routers failed to match - definitely unhandled."""
        await callback.answer("Сессия устарела. Нажмите /start", show_alert=True)

    # MUST be last so all other routers get priority over fallback
    dp.include_router(_fallback)

    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        import traceback as _tb

        exc = event.exception
        err_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))[:3000]
        logger.error("Global error: %s", exc, exc_info=True)

        # USER-CAUSED errors: no admin notification, just redirect user
        # KeyError = stale FSM data, ValueError/AttributeError = bad user input
        _user_errors = (KeyError, ValueError, AttributeError)
        _is_user_error = isinstance(exc, _user_errors)

        if not _is_user_error:
            # Real bug - notify admins with full traceback
            from config import ADMIN_IDS as _AIDS

            for _aid in _AIDS:
                try:
                    await bot.send_message(
                        _aid,
                        "⚠️ <b>Ошибка бота</b>\n" + f"<pre>{err_text}</pre>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        # Answer user
        try:
            if event.update.message:
                await event.update.message.answer("Произошла ошибка. Попробуйте позже.")
            elif event.update.callback_query:
                await event.update.callback_query.answer("Произошла ошибка. Попробуйте позже.", show_alert=True)
        except Exception:
            pass

    await _db_module.init_pool()
    await init_db()
    logger.info("Database initialized")
    # FIX: чистим stale slot_locks от прерванных сессий
    from storage import cleanup_slot_locks_on_startup

    await cleanup_slot_locks_on_startup()

    # M-2 FIX: on first start (empty settings table) persist defaults to DB
    try:
        from storage import get_all_settings

        _existing = await get_all_settings()
        if not _existing:
            logger.info("First startup detected: saving default config to DB")
            from config import save_config_to_db

            await save_config_to_db()
    except Exception as _e:
        logger.warning(f"First-startup config save failed: {_e}")
    await load_config_from_db()
    logger.info("Config loaded from DB")

    # Устанавливаем команды бота автоматически
    from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    from config import ADMIN_IDS

    user_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="me", description="Мой профиль и записи"),
        BotCommand(command="master", description="Все мастера"),
        BotCommand(command="waitlist", description="Мой лист ожидания"),
        BotCommand(command="cancel", description="Отменить запись"),
        BotCommand(command="help", description="Справка по командам"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Для каждого администратора — расширенный список команд
    admin_commands = [
        *user_commands,
        BotCommand(command="admin", description="Панель администратора"),
    ]
    # /admin is hidden from regular users — only visible in admin scope
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning(f"Could not set admin commands for {admin_id}: {e}")

    logger.info("Bot commands set")

    # Warn if no admins configured
    from config import ADMIN_IDS

    if not ADMIN_IDS:
        logger.warning("WARNING: ADMIN_IDS is empty! No one can access the admin panel.")
        logger.warning("Set ADMIN_IDS in .env: ADMIN_IDS=your_telegram_id")

    await delete_old_scheduler_jobs()

    # MED-006 FIX: Auto-complete past-due bookings on startup
    try:
        from scheduler import auto_complete_booking as _auto_complete
        from storage import get_past_bookings_for_completion

        _past = await get_past_bookings_for_completion()
        if _past:
            logger.info(f"Found {len(_past)} past-due bookings to auto-complete")
            for _b in _past:
                try:
                    await _auto_complete(bot, _b)
                except Exception as _e:
                    logger.error(f"Failed to auto-complete {_b['id']}: {_e}")
    except Exception as _e:
        logger.error(f"Startup past-due recovery failed: {_e}")

    # FSM-reset: clear stale states on restart (prevent stuck users)
    # TASK-01: Don't clear all states on startup - instead add fallback handler
    # Removing automatic state clearing to preserve user context across restarts
    # if hasattr(storage, 'clear_all_states'):
    #     await storage.clear_all_states()
    #     logger.info("FSM states cleared on startup")

    # MED-006 FIX: Backup moved to daily scheduler job (3:30 AM)
    # Removed from startup to avoid blocking bot initialization

    await start_scheduler(bot)
    start_monitoring()

    health = await get_health_status()
    logger.info(f"Health: {health}")

    # CONFLICT FIX: сбрасываем webhook и старые апдейты перед стартом поллинга.
    # Если бот запускается повторно (Railway redeploy / restart), это устраняет
    # "TelegramConflictError: terminated by other getUpdates request".
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted, pending updates dropped")
    except Exception as e:
        logger.warning(f"delete_webhook failed (non-critical): {e}")

    # BUG-6 FIX: surface any *external* webhook still configured — it would
    # conflict with polling and cause TelegramConflictError.
    try:
        await check_webhook_conflict(bot)
    except Exception as e:
        logger.warning(f"check_webhook_conflict failed (non-critical): {e}")

    # DEPLOY FIX: Railway zero-downtime starts new container before stopping old.
    # Telegram allows only ONE getUpdates session -> TelegramConflictError.
    # Retry up to 120s until the old container is stopped by Railway.
    from aiogram.exceptions import TelegramConflictError

    # HIGH-PRODUCTION: graceful shutdown via SIGTERM/SIGINT signal handler.
    # On Linux (Railway): Railway sends SIGTERM and waits grace period before SIGKILL.
    # We translate this into a clean drain of the polling loop, then close DB pool,
    # bot session, and FSM storage.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(_sig_name: str = "SIGNAL") -> None:
        if not stop_event.is_set():
            logger.info(f"Shutdown requested via {_sig_name}; draining…")
            stop_event.set()

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(_sig, _request_stop, _sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows: only SIGINT works via signal.signal; SIGTERM is best-effort.
            try:
                signal.signal(_sig, lambda s, _f, _sig=_sig: _request_stop(_sig.name))
            except (ValueError, OSError):
                logger.warning(f"Could not install handler for {_sig.name}")

    async def _run_polling() -> None:
        for _attempt in range(24):
            try:
                logger.info(f"Starting polling (attempt {_attempt + 1}/24)...")
                await dp.start_polling(bot, drop_pending_updates=True)
                return
            except TelegramConflictError:
                if _attempt < 23:
                    logger.warning(f"TelegramConflictError: another instance active. Retry {_attempt + 1}/24 in 5s...")
                    await asyncio.sleep(5)
                else:
                    logger.error("TelegramConflictError: max retries exceeded.")
                    raise
            except asyncio.CancelledError:
                logger.info("Polling task cancelled by shutdown signal")
                raise

    polling_task = asyncio.create_task(_run_polling(), name="aiogram-polling")

    # Wait for either: external stop event, or polling exiting on its own
    await asyncio.wait(
        {asyncio.create_task(stop_event.wait(), name="stop-waiter"), polling_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if not polling_task.done():
        logger.info("Requesting polling stop…")
        # Tell aiogram to leave long-poll cleanly, then give it 10s to drain.
        try:
            await asyncio.wait_for(_drain_polling(dp, polling_task), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Polling did not drain within 10s; cancelling")
            polling_task.cancel()
            try:
                await polling_task
            except (asyncio.CancelledError, Exception):
                pass

    if stop_event.is_set():
        # Stop waiter is done — cancel it; polling already completed/drained above
        for t in asyncio.all_tasks():
            if t.get_name() == "stop-waiter" and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # Graceful shutdown sequence: scheduler → FSM storage → DB pool → bot session.
    try:
        shutdown_scheduler()
    except Exception as _e:
        logger.warning(f"shutdown_scheduler() failed: {_e}")
    try:
        _storage = getattr(dp, "storage", None)
        if _storage is not None and hasattr(_storage, "close"):
            _close = _storage.close()
            if asyncio.iscoroutine(_close):
                await _close
    except Exception as _e:
        logger.warning(f"FSM storage close failed: {_e}")
    try:
        await _db_module.close_pool()
    except Exception as _e:
        logger.warning(f"db.close_pool() failed: {_e}")
    try:
        await bot.session.close()
    except Exception as _e:
        logger.warning(f"bot.session.close() failed: {_e}")
    logger.info("Bot stopped cleanly")


async def _drain_polling(dp, polling_task: asyncio.Task) -> None:
    """Try to make aiogram exit long-poll cleanly so in-flight handlers finish."""
    try:
        # aiogram 3.x exposes Dispatcher.stop_polling as async; safe-call it.
        stop = getattr(dp, "stop_polling", None)
        if callable(stop):
            res = stop()
            if asyncio.iscoroutine(res):
                await res
    except Exception as _e:
        logger.warning(f"dp.stop_polling() raised: {_e}")
    try:
        await asyncio.wait_for(polling_task, timeout=8.0)
    except asyncio.TimeoutError:
        raise


if __name__ == "__main__":
    asyncio.run(main())
