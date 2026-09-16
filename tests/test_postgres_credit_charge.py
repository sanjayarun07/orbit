"""Opt-in real-Postgres test for the shared atomic charge (R07).

The in-suite charge test runs the in-memory branch of `credits.charge_once`.
The Postgres branch -- an advisory transaction lock around the balance read and
the debit insert -- is the one production runs, and this is its coverage.

    TEST_DATABASE_URL=postgresql://localhost/orbit_test pytest tests/test_postgres_credit_charge.py
"""
import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from app import credits, db

requires_postgres = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="isolated Postgres DSN not set; the advisory-lock branch of charge_once is UNVERIFIED in this run",
)


@requires_postgres
def test_many_concurrent_charges_never_overdraw_the_account(monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "orbit_charge_" + uuid4().hex

    async def run():
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, server_settings={"search_path": schema})
            async with pool.acquire() as conn:
                await conn.execute(db._PLANS_TABLE_SQL)

            async def get_pool():
                return pool

            monkeypatch.setattr(credits, "get_pg_pool", get_pool)
            account = "user:charge-race"
            await credits.append(account, 3, "test", "test", "seed")

            async def spend(index):
                return await credits.charge_once(account, 1, "task:brief", "task_run", f"occurrence-{index}", {})

            outcomes = await asyncio.gather(*(spend(i) for i in range(12)))
            balance = await credits.balance(account)
            # And a repeat of an occurrence that already charged is free.
            again = await credits.charge_once(account, 1, "task:brief", "task_run", "occurrence-0", {})
            return outcomes, balance, again
        finally:
            if pool is not None:
                pool.terminate()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    outcomes, balance, again = asyncio.run(run())
    assert outcomes.count(credits.ChargeOutcome.CHARGED) == 3, outcomes
    assert outcomes.count(credits.ChargeOutcome.INSUFFICIENT) == 9, outcomes
    assert balance == 0, f"the account was overdrawn to {balance}"
    assert again is credits.ChargeOutcome.ALREADY_CHARGED
