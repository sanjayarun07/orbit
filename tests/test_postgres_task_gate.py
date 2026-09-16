"""Opt-in real-Postgres tests for the task activation gate.

These exist because the ordinary suite cannot test this code path at all.
tests/conftest.py replaces `get_pg_pool` with a function returning None on the
accounts, credits, api_keys, billing and tasks modules, so every in-suite task
test exercises the in-memory asyncio-lock branch of `tasks.account_task_gate`.
The `pg_advisory_xact_lock` branch -- the one production runs -- had no test
coverage until this file, and the pool-exhaustion deadlock the review found
lives entirely in that branch. A storm test written in the ordinary suite
passed against the broken code for exactly that reason.

Run with an isolated database, never the development one:

    TEST_DATABASE_URL=postgresql://localhost/orbit_test pytest tests/test_postgres_task_gate.py

Each test creates a disposable schema and drops it afterwards.
"""
import asyncio
from contextlib import asynccontextmanager
import os
from uuid import uuid4

import asyncpg
import pytest

from app import accounts, db, task_scheduling, tasks
from app.settings import settings

requires_postgres = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="isolated Postgres DSN not set; the advisory-lock branch of the task gate is UNVERIFIED in this run",
)


@asynccontextmanager
async def isolated_pool(monkeypatch, max_size: int):
    """A pool of exactly `max_size` connections on a throwaway schema, wired
    into the modules the conftest fixture had pointed at nothing."""
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "orbit_gate_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=max_size, server_settings={"search_path": schema})
        async with pool.acquire() as conn:
            await conn.execute(db._PLANS_TABLE_SQL)

        async def get_pool():
            return pool

        for module in (tasks, accounts):
            monkeypatch.setattr(module, "get_pg_pool", get_pool)
        yield pool
    finally:
        if pool is not None:
            # terminate, not close: a deadlocked pool never releases, and
            # close() would wait for it forever.
            pool.terminate()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _paused_tasks(user: dict, count: int) -> list[dict]:
    made = []
    for index in range(count):
        task = await tasks.create_task(user, "reminder", {"message": f"t{index}"}, {"every_minutes": 60})
        await tasks.update_task(task["id"], user["id"], status="paused")
        made.append(task)
    return made


@requires_postgres
def test_more_concurrent_activations_than_pool_connections_all_complete(monkeypatch):
    """The reviewed deadlock. The gate held a pool connection for its lock and
    then asked the pool for a second one to count and write. With at least as
    many concurrent activations as the pool has connections, every one held a
    lock-connection and none could get a work-connection. Against the unfixed
    gate this test times out; lock, count and write now share one connection."""
    pool_size = 10
    callers = pool_size + 4

    async def run():
        async with isolated_pool(monkeypatch, pool_size):
            user, _ = await accounts.get_or_create_user("storm@example.com")
            made = await _paused_tasks(user, callers)

            async def resume(task):
                try:
                    await task_scheduling.update_task(task["id"], user["id"], user=user, status="active")
                    return "active"
                except Exception as exc:
                    return type(exc).__name__

            try:
                return await asyncio.wait_for(asyncio.gather(*(resume(t) for t in made)), timeout=30)
            except asyncio.TimeoutError:
                return None

    outcomes = asyncio.run(run())
    assert outcomes is not None, (
        f"{callers} concurrent activations against a pool of {pool_size} never completed: "
        "the gate exhausted the pool"
    )
    limit = tasks.TASK_LIMITS.get("free", 3)
    assert outcomes.count("active") == limit, outcomes
    assert all(o in ("active", "ServiceError") for o in outcomes), outcomes


@requires_postgres
def test_the_gate_never_goes_back_to_the_pool_while_holding_its_lock(monkeypatch):
    """The property the one-connection fix guarantees, asserted directly rather
    than through a timeout: while an activation gate is held, nothing asks the
    pool for another connection."""

    async def run():
        async with isolated_pool(monkeypatch, 4) as pool:
            user, _ = await accounts.get_or_create_user("no-reacquire@example.com")
            (task,) = await _paused_tasks(user, 1)

            holding = 0
            reacquired = []
            # asyncpg's Pool uses __slots__, so the instance attribute cannot be
            # patched; the class attribute can, and the spy keeps to this pool.
            real_acquire = asyncpg.pool.Pool.acquire

            def spy_acquire(self, *args, **kwargs):
                if self is pool and holding:
                    reacquired.append(True)
                return real_acquire(self, *args, **kwargs)

            real_gate = tasks.account_task_gate

            @asynccontextmanager
            async def counted_gate(user_id):
                nonlocal holding
                async with real_gate(user_id) as connection:
                    holding += 1
                    try:
                        yield connection
                    finally:
                        holding -= 1

            monkeypatch.setattr(asyncpg.pool.Pool, "acquire", spy_acquire)
            monkeypatch.setattr(tasks, "account_task_gate", counted_gate)
            await task_scheduling.update_task(task["id"], user["id"], user=user, status="active")
            return reacquired

    assert asyncio.run(run()) == [], "the gate asked the pool for another connection while holding its lock"


@requires_postgres
def test_the_advisory_lock_serialises_activations_per_account(monkeypatch):
    """Coverage for the branch production runs: under the real advisory lock,
    no two activations for one account are inside the check-and-write window
    at once. Not a differential against the old gate -- that one was also a
    mutex -- but the first time this branch has been exercised at all."""

    async def run():
        async with isolated_pool(monkeypatch, 10):
            user, _ = await accounts.get_or_create_user("serialised@example.com")
            made = await _paused_tasks(user, 6)

            inside = 0
            overlaps = []
            real_check = tasks.assert_can_activate

            async def instrumented(*args, **kwargs):
                nonlocal inside
                inside += 1
                if inside > 1:
                    overlaps.append(inside)
                try:
                    await real_check(*args, **kwargs)
                    await asyncio.sleep(0)
                finally:
                    inside -= 1

            monkeypatch.setattr(tasks, "assert_can_activate", instrumented)

            async def resume(task):
                try:
                    await task_scheduling.update_task(task["id"], user["id"], user=user, status="active")
                    return True
                except Exception:
                    return False

            results = await asyncio.wait_for(asyncio.gather(*(resume(t) for t in made)), timeout=30)
            active = [t for t in await tasks.list_tasks(user["id"], include_done=False) if t["status"] == "active"]
            return overlaps, results, len(active)

    overlaps, results, active = asyncio.run(run())
    limit = tasks.TASK_LIMITS.get("free", 3)
    assert overlaps == [], f"activations overlapped under the advisory lock: {overlaps}"
    assert sum(results) == limit and active == limit
