"""Opt-in real Postgres tests, isolated in a disposable UUID schema."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import asyncpg
import pytest

from app import db, plans, relay_tracking
from app.models import SwapProposal, TradePlan


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="isolated Postgres integration DSN not set")
def test_postgres_claim_restart_reconciliation_and_relay_turn_uniqueness(monkeypatch):
    async def run():
        dsn = os.environ["TEST_DATABASE_URL"]
        schema = "orbit_test_" + uuid4().hex
        admin = await asyncpg.connect(dsn)
        pool = None
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            async def open_pool():
                return await asyncpg.create_pool(dsn, min_size=1, max_size=4, server_settings={"search_path":schema})
            pool = await open_pool()
            async with pool.acquire() as conn:
                await conn.execute(db._PLANS_TABLE_SQL)
            async def get_pool():
                return pool
            monkeypatch.setattr(plans, "get_pg_pool", get_pool)
            monkeypatch.setattr(relay_tracking, "get_pg_pool", get_pool)
            now = datetime.now(timezone.utc)
            plan = TradePlan(plan_id="pg-plan", status="pending_confirmation", created_at=now,
                expires_at=now+timedelta(minutes=2), wallet_address="test-wallet",
                proposal=SwapProposal(input_mint="in",output_mint="out",amount_atomic=1,slippage_bps=10,reason="test"),
                quote={}, input_token={"mint":"in","name":"Input","symbol":"IN","decimals":9},
                output_token={"mint":"out","name":"Output","symbol":"OUT","decimals":6},
                simulation={"ok":True}, confirmation_text="CONFIRM pg-plan")
            await plans._store_plan(plan)
            outcomes = await asyncio.gather(*[plans.claim_plan_submission(plan.model_copy(), "test-signature") for _ in range(4)], return_exceptions=True)
            assert sum(x is None for x in outcomes) == 1
            # New pool simulates a worker restart; no process-memory record used.
            await pool.close()
            pool = await open_pool()
            recovered = await plans.get_plan(plan.plan_id)
            assert recovered.status == "submitting"
            assert recovered.submission_signature == "test-signature"
            stale = recovered.model_copy()
            await plans.update_submission(recovered, "executed")
            await plans.update_submission(stale, "submission_unknown")
            assert (await plans.get_plan(plan.plan_id)).status == "executed"
            new_plan = plan.model_copy(update={"plan_id":"replaced"})
            await plans._store_plan(new_plan)
            await plans.mark_plan_superseded(new_plan.plan_id)
            with pytest.raises(ValueError):
                await plans.claim_plan_submission(new_plan, "never-sent")
            claims = await asyncio.gather(relay_tracking.track("request-1234567890", "session", 1), relay_tracking.track("different-1234567890", "session", 1))
            assert sum(x["execution_claimed"] for x in claims) == 1
            assert await relay_tracking.for_turn("session", 1)
        finally:
            if pool is not None:
                await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()
    asyncio.run(run())
