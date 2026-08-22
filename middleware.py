import logging
import time
import asyncio
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import config
from logging_config import bind_request_id, new_request_id

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseMiddleware):
    """Stamp every inbound update with a fresh request_id stored in a
    ContextVar so log records across the whole update inherit it.

    Also exposes the id via `data["request_id"]` for handlers that want to
    attach it to outbound API calls (e.g., correlation with Telegram's
    own update_id) without going through the logging system.
    """

    def __init__(self) -> None:
        super().__init__()

    async def __call__(self, handler, event, data):
        update_id = None
        if event is not None:
            update_id = getattr(event, "update_id", None)
        new_request_id()
        if update_id is not None:
            # Bind the request id to something traceable (rounded update_id + uuid)
            from logging_config import bind_request_id as _bind
            _bind(f"upd{update_id}-" + _current_rid_short())
        data["request_id"] = get_request_id_safe()
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("Unhandled exception in handler")
            raise


def _current_rid_short() -> str:
    from logging_config import get_request_id as _get
    return _get()[:8]


def get_request_id_safe() -> str:
    from logging_config import get_request_id as _get
    return _get()


class RateLimitMiddleware(BaseMiddleware):
    """Hybrid rate limiter -- in-memory with fast access.
    MED-03 FIX: GC runs every 100 events (not 1000) to prevent RAM growth."""
    def __init__(self, max_requests: int = 20, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self.user_requests: dict[int, list[float]] = {}
        self._event_counter = 0
        super().__init__()

    def _cleanup_old_entries(self, now: float) -> None:
        """Remove entries for users with no recent requests."""
        cutoff = now - self.window
        stale_keys = [uid for uid, ts_list in self.user_requests.items()
                      if not ts_list or ts_list[-1] < cutoff]
        for k in stale_keys:
            del self.user_requests[k]

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if event.from_user else None
        if user_id is None:
            return await handler(event, data)

        if user_id in config.ADMIN_IDS:
            # M-7 FIX: admins still rate-limited at 5x the normal limit
            # (prevents flood abuse if admin account is compromised)
            admin_key = f"admin_{user_id}"
            now2 = time.time()
            if admin_key not in self.user_requests:
                self.user_requests[admin_key] = []
            self.user_requests[admin_key] = [
                t for t in self.user_requests[admin_key] if (now2 - t) < self.window
            ]
            if len(self.user_requests[admin_key]) >= self.max_requests * 5:
                logger.warning(f"Admin rate limit hit for {user_id}")
                if isinstance(event, Message):
                    await event.answer("Слишком много запросов.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Слишком много запросов.", show_alert=True)
                return
            self.user_requests[admin_key].append(now2)
            return await handler(event, data)

        now = time.time()

        # MED-03 FIX: periodic GC every 100 events regardless of dict size
        self._event_counter += 1
        if self._event_counter % 100 == 0:
            self._cleanup_old_entries(now)

        if user_id not in self.user_requests:
            self.user_requests[user_id] = []

        self.user_requests[user_id] = [
            t for t in self.user_requests[user_id] if (now - t) < self.window
        ]

        if len(self.user_requests[user_id]) >= self.max_requests:
            logger.warning(f"Rate limit hit for user {user_id}: {len(self.user_requests[user_id])} requests in {self.window}s")
            if isinstance(event, Message):
                await event.answer("Слишком много запросов. Подождите немного.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Слишком много запросов. Пожалуйста подождите.", show_alert=True)
            return

        self.user_requests[user_id].append(now)
        return await handler(event, data)


class AdminCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if event.from_user else None
        if user_id is not None and user_id in config.ADMIN_IDS:
            data["is_admin"] = True
        else:
            data["is_admin"] = False
        return await handler(event, data)


class GlobalConcurrencyMiddleware(BaseMiddleware):
    """SEC-13 FIX: bound total concurrent handler executions.

    Per-user rate limiting (RateLimitMiddleware) throttles abuse from a single
    account, but cannot protect against a *global* flood (e.g. 10k users
    firing at once) which would exhaust the DB connection pool (min=2, max=10)
    and the single bot session. This middleware serializes the downstream
    handler chain behind a semaphore, providing backpressure instead of letting
    every update acquire a pool connection simultaneously.

    The semaphore wraps the rest of the chain (handlers + inner middleware), so
    a handler that awaits a long DB call holds the permit for that duration,
    which is exactly the backpressure we want.
    """
    def __init__(self, limit: int = 64):
        super().__init__()
        self._sem = asyncio.Semaphore(max(1, limit))

    async def __call__(self, handler, event, data):
        async with self._sem:
            return await handler(event, data)
