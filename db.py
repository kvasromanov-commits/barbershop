import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)
_pool = None
_use_pg: bool = False

# MED-1 FIX: SQLite concurrency hardening.
# `busy_timeout` is a *per-connection* pragma and is NOT persisted in the database
# file (unlike journal_mode). Because `acquire()` opens a new connection per call,
# every connection must (re)apply it. Without it, concurrent writers under
# GLOBAL_CONCURRENCY immediately raise "database is locked" instead of waiting for
# the WAL write lock. Configurable via env (milliseconds); 30000 = 30 s.
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))


async def init_pool() -> None:
    """Initialise the DB pool.

    PostgreSQL: pool with retry/backoff so a transient startup delay of the
    Postgres container (e.g. on Railway zero-downtime deploy) does not kill
    the bot. Five attempts: 1s → 2s → 4s → 8s → 16s (cumulative ~31 s).
    SQLite: synchronous open on first query, so no retry needed.
    """
    global _pool, _use_pg
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        import asyncpg

        # P1 FIX: pool size + command timeout from env (defaults preserve previous behaviour)
        import config as _cfg

        last_exc: BaseException | None = None
        for attempt in range(5):
            try:
                _pool = await asyncpg.create_pool(
                    database_url,
                    min_size=_cfg.DB_POOL_MIN,
                    max_size=_cfg.DB_POOL_MAX,
                    command_timeout=_cfg.DB_COMMAND_TIMEOUT,
                    statement_cache_size=0,
                )
                _use_pg = True
                logger.info(
                    f"db.py: PostgreSQL pool initialised "
                    f"(min={_cfg.DB_POOL_MIN}, max={_cfg.DB_POOL_MAX}, "
                    f"command_timeout={_cfg.DB_COMMAND_TIMEOUT}s)"
                )
                return
            except Exception as e:  # connection refused, auth fail, DNS, etc.
                last_exc = e
                delay = 2**attempt
                logger.warning(
                    f"db.py: PG connect attempt {attempt + 1}/5 failed: {type(e).__name__}: {e}. Retrying in {delay}s."
                )
                await asyncio.sleep(delay)
        # All retries exhausted
        logger.error(f"db.py: PostgreSQL pool could NOT be initialised after 5 attempts: {last_exc}")
        raise last_exc if last_exc else RuntimeError("PostgreSQL init failed")
    else:
        _use_pg = False
        logger.info("db.py: SQLite mode (no DATABASE_URL)")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def is_postgres() -> bool:
    return _use_pg


def _q2d(sql: str) -> str:
    """Convert ? placeholders to $1, $2, ... for PostgreSQL."""
    n = 0
    r = []
    for c in sql:
        if c == "?":
            n += 1
            r.append(f"${n}")
        else:
            r.append(c)
    return "".join(r)


_RX_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


class DBConn:
    __slots__ = ("_conn", "_is_pg")

    def __init__(self, conn, is_pg: bool):
        self._conn = conn
        self._is_pg = is_pg

    async def fetch(self, sql: str, *a) -> list[dict]:
        if self._is_pg:
            return [dict(r) for r in await self._conn.fetch(_q2d(sql), *a)]
        import aiosqlite

        self._conn.row_factory = aiosqlite.Row
        async with self._conn.execute(sql, a) as c:
            return [dict(r) for r in await c.fetchall()]

    async def fetchrow(self, sql: str, *a) -> dict | None:
        if self._is_pg:
            r = await self._conn.fetchrow(_q2d(sql), *a)
            return dict(r) if r else None
        import aiosqlite

        self._conn.row_factory = aiosqlite.Row
        async with self._conn.execute(sql, a) as c:
            r = await c.fetchone()
            return dict(r) if r else None

    async def fetchval(self, sql: str, *a) -> Any:
        if self._is_pg:
            return await self._conn.fetchval(_q2d(sql), *a)
        async with self._conn.execute(sql, a) as c:
            r = await c.fetchone()
            return r[0] if r else None

    async def execute(self, sql: str, *a) -> None:
        """Execute a statement.  INSERT OR IGNORE is auto-translated for PG."""
        if self._is_pg:
            # Detect BEFORE substitution
            _had_ignore = bool(_RX_INSERT_OR_IGNORE.search(sql))
            sql = _RX_INSERT_OR_IGNORE.sub("INSERT INTO", sql)
            if _had_ignore and "ON CONFLICT" not in sql.upper():
                sql = sql.rstrip() + " ON CONFLICT DO NOTHING"
            await self._conn.execute(_q2d(sql), *a)
        else:
            await self._conn.execute(sql, a)

    async def commit(self) -> None:
        if not self._is_pg:
            await self._conn.commit()

    async def execute_count(self, sql: str, *a) -> int:
        """Execute DELETE/UPDATE and return number of affected rows (both backends)."""
        if self._is_pg:
            status = await self._conn.execute(_q2d(sql), *a)
            try:
                return int(str(status).split()[-1])
            except Exception:
                return 0
        else:
            async with self._conn.execute(sql, a) as cur:
                count = cur.rowcount
            await self._conn.commit()
            return count if count >= 0 else 0

    async def upsert(self, table: str, conflict_cols: list[str], data: dict) -> None:
        """INSERT ... ON CONFLICT (conflict_cols) DO UPDATE SET ...  (both backends)."""
        cols = list(data.keys())
        vals = list(data.values())
        cols_sql = ", ".join(cols)
        if self._is_pg:
            pg_ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
            conflict = ", ".join(conflict_cols)
            update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict_cols)
            sql = f"INSERT INTO {table} ({cols_sql}) VALUES ({pg_ph}) ON CONFLICT ({conflict}) DO " + (
                f"UPDATE SET {update_set}" if update_set else "NOTHING"
            )
            await self._conn.execute(sql, *vals)
        else:
            ph = ", ".join("?" for _ in vals)
            await self._conn.execute(f"INSERT OR REPLACE INTO {table} ({cols_sql}) VALUES ({ph})", vals)
            await self._conn.commit()

    @asynccontextmanager
    async def transaction(self):
        if self._is_pg:
            async with self._conn.transaction():
                yield self
        else:
            # MED-1 FIX: BEGIN IMMEDIATE (not EXCLUSIVE) under WAL. It acquires the
            # write (RESERVED) lock up front so the lock is held for the whole
            # transaction and other writers wait via busy_timeout instead of
            # erroring. Unlike EXCLUSIVE it still allows concurrent *readers*
            # (WAL readers do not need the write lock), which reduces contention.
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                await self._conn.commit()
            except Exception:
                await self._conn.execute("ROLLBACK")
                raise


@asynccontextmanager
async def acquire():
    """Acquire a DB connection (PostgreSQL pool or SQLite file)."""
    if _use_pg:
        async with _pool.acquire() as raw:
            yield DBConn(raw, True)
    else:
        import aiosqlite

        import config

        async with aiosqlite.connect(config.DB_PATH) as raw:
            # MED-1 FIX: per-connection pragmas. `journal_mode=WAL` and
            # `synchronous=NORMAL` are already persisted by storage._init_sqlite,
            # but re-asserting them is idempotent and keeps connections opened
            # before init consistent. `busy_timeout` is the critical one (see
            # SQLITE_BUSY_TIMEOUT_MS): it makes concurrent writers wait for the
            # write lock instead of raising "database is locked". A pragma failure
            # must never break the request, so it is best-effort.
            try:
                await raw.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                await raw.execute("PRAGMA journal_mode=WAL")
                await raw.execute("PRAGMA synchronous=NORMAL")
            except Exception as _pe:  # pragma setup must not break acquire
                logger.warning("db.acquire: failed to set SQLite pragmas: %s", type(_pe).__name__)
            yield DBConn(raw, False)
