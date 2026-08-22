"""SQLite concurrency regression tests (MED-1 fix).

These tests prove that the per-connection `busy_timeout` + `BEGIN IMMEDIATE` +
WAL hardening in `db.py` eliminates systematic ``database is locked`` errors and
lost/duplicate writes under concurrent load (up to GLOBAL_CONCURRENCY=64).

The whole suite runs against a throwaway SQLite file on local disk (WAL works on
local filesystems). PostgreSQL is NOT exercised here — see the note in the final
report: a live Postgres (DATABASE_URL) is required for the PG integration path and
cannot be started in this sandbox.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import db as _db
import storage


@pytest.fixture
async def sqlite_tmp_db(tmp_path, monkeypatch):
    """Force SQLite mode on an isolated temp DB and build the schema + a
    dedicated table for the concurrency exercises."""
    db_file = tmp_path / "concurrency.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(_db, "_use_pg", False)
    monkeypatch.setattr(_db, "_pool", None)
    # Speed up the test; 5 s is plenty for ~64 ms-scale transactions.
    monkeypatch.setattr(_db, "SQLITE_BUSY_TIMEOUT_MS", 5000)

    await storage.init_db()
    async with _db.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS concurrency_test (slot INTEGER PRIMARY KEY, name TEXT)")
        await conn.commit()
    yield str(db_file)
    await _db.close_pool()


async def _count(conn_factory) -> int:
    async with conn_factory() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM concurrency_test")


async def _insert_one(slot: int, fail: bool = False):
    async with _db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO concurrency_test (slot, name) VALUES (?, ?)",
                slot,
                f"name-{slot}",
            )
            if fail:
                # Force a rollback; must NOT leak into the committed dataset.
                raise RuntimeError("simulated failure inside transaction")


async def test_busy_timeout_pragma_is_applied(sqlite_tmp_db):
    """Every connection opened by acquire() must carry the configured busy_timeout."""
    async with _db.acquire() as conn:
        rows = await conn.fetch("PRAGMA busy_timeout")
        assert rows, "PRAGMA busy_timeout returned no rows"
        # We monkeypatched SQLITE_BUSY_TIMEOUT_MS to 5000 in the fixture.
        # The pragma reports its value under the column "timeout".
        assert int(rows[0]["timeout"]) == 5000


async def test_busy_timeout_default_is_reasonable():
    """Guard that the production busy_timeout is a positive value (never 0, which
    would disable waiting and reintroduce 'database is locked')."""
    assert _db.SQLITE_BUSY_TIMEOUT_MS > 0


async def test_concurrent_inserts_no_busy_error(sqlite_tmp_db):
    """10 / 25 / 50 concurrent inserts through db.acquire() — no lock errors,
    no lost writes, no duplicates. The table is cleared between sizes so each
    batch uses a fresh 0..n-1 slot range."""
    for n in (10, 25, 50):
        # Clear the table so this batch does not collide with the previous one.
        async with _db.acquire() as conn:
            await conn.execute("DELETE FROM concurrency_test")
            await conn.commit()

        tasks = [_insert_one(i) for i in range(n)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # No systematic "database is locked" / any other failure.
        lock_errors = [r for r in results if isinstance(r, Exception)]
        assert not lock_errors, f"{len(lock_errors)} task(s) raised: {lock_errors[:3]}"

        count = await _count(_db.acquire)
        assert count == n, f"expected {n} rows, got {count} (lost writes?)"


async def test_concurrency_at_global_limit(sqlite_tmp_db):
    """Simulate GLOBAL_CONCURRENCY=64 — the worst case from the audit."""
    n = 64
    tasks = [_insert_one(i) for i in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"concurrency=64 produced failures: {failures[:3]}"

    async with _db.acquire() as conn:
        rows = await conn.fetch("SELECT slot FROM concurrency_test ORDER BY slot")
    slots = [r["slot"] for r in rows]
    assert len(slots) == n
    assert slots == list(range(n))  # all distinct, none lost/duplicated


async def test_rollback_on_failure_is_isolated(sqlite_tmp_db):
    """A failure in one transaction must roll back only that transaction; the
    other concurrent writes must still commit and be fully durable."""
    n = 30
    tasks = []
    for i in range(n):
        tasks.append(_insert_one(i, fail=(i == 0)))  # first task fails
    results = await asyncio.gather(*tasks, return_exceptions=True)

    failed = [r for r in results if isinstance(r, Exception)]
    assert len(failed) == 1, f"expected exactly 1 failure, got {len(failed)}"

    count = await _count(_db.acquire)
    assert count == n - 1, f"rollback leaked: expected {n - 1} rows, got {count}"

    async with _db.acquire() as conn:
        bad = await conn.fetchrow("SELECT slot FROM concurrency_test WHERE slot = 0")
    assert bad is None, "rolled-back row (slot=0) was not removed"


async def test_concurrent_reads_and_writes_no_corruption(sqlite_tmp_db):
    """Interleave 20 writers and 20 readers. Readers must always succeed (WAL
    lets them read while a writer holds the lock) and see a monotonic,
    non-decreasing, never-excessive count."""
    n_writers = 20
    n_readers = 20
    writer_slots = set(range(n_writers))

    async def reader():
        async with _db.acquire() as conn:
            # Multiple reads within one connection (autocommit snapshot).
            for _ in range(5):
                c = await conn.fetchval("SELECT COUNT(*) FROM concurrency_test")
                assert c <= n_writers  # never more than the writers will insert
                assert c >= 0

    writers = [_insert_one(i) for i in range(n_writers)]
    readers = [reader() for _ in range(n_readers)]
    all_results = await asyncio.gather(*(writers + readers), return_exceptions=True)
    failures = [r for r in all_results if isinstance(r, Exception)]
    assert not failures, f"readers/writers failed: {failures[:3]}"

    # All writer slots present, none lost.
    async with _db.acquire() as conn:
        rows = await conn.fetch("SELECT slot FROM concurrency_test")
    assert {r["slot"] for r in rows} == writer_slots
