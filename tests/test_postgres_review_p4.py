"""Opt-in real-Postgres regressions from the verification of a1b477ba: retry
state read back from the database (R09), joins that write nothing when
rejected (R10), and the migration that reconciles duplicate memberships
before the one-active-membership constraint exists (R10).

    TEST_DATABASE_URL=postgresql://localhost/orbit_test pytest tests/test_postgres_review_p4.py
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app import accounts, credits, db, tasks
from app.settings import settings
from tests.test_postgres_task_gate import isolated_pool

requires_postgres = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="isolated Postgres DSN not set; these paths are UNVERIFIED in this run",
)


@requires_postgres
def test_retry_state_is_read_back_so_retries_are_bounded_and_the_occurrence_refunded(monkeypatch):
    """The verification saw four failures all reported as attempt one, the
    stored count stuck at one, and the credit never refunded: retry_count
    was written but never read back from a row."""

    async def run():
        async with isolated_pool(monkeypatch, 3) as pool:
            async def get_pool():
                return pool
            monkeypatch.setattr(credits, "get_pg_pool", get_pool)
            monkeypatch.setattr(settings, "task_retry_limit", 3)
            monkeypatch.setattr(settings, "credit_cost_brief", 1)
            user, _ = await accounts.get_or_create_user("retry@example.com")
            account = credits.user_account_id(user["id"])
            await credits.append(account, 1, "seed", "test", "seed")
            task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
            monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("offline")))
            base = datetime.now(timezone.utc)
            results, counts = [], []
            for n in range(3):
                monkeypatch.setattr(tasks, "_now", lambda n=n: base + timedelta(minutes=10 * n))
                results.append(await tasks.run_task(await tasks.get_task(task["id"])))
                counts.append(await pool.fetchval("SELECT retry_count FROM user_tasks WHERE id = $1", task["id"]))
            return results, counts, await credits.balance(account), await tasks.get_task(task["id"])

    results, counts, balance, stored = asyncio.run(run())
    assert [r["result"] for r in results] == ["error", "error", "failed"], results
    assert [r.get("attempt") for r in results[:2]] == [1, 2]
    assert counts == [1, 2, 0]
    assert balance == 1, "the exhausted occurrence was not refunded"
    assert stored["retry_count"] == 0 and stored["claimed_occurrence"] is None


@requires_postgres
def test_a_join_with_no_invite_changes_nothing(monkeypatch):
    """The verification accepted a nonexistent invite and saw the user's
    existing membership deleted with the old team pointer left in place."""

    async def run():
        async with isolated_pool(monkeypatch, 3):
            a, _ = await accounts.get_or_create_user("a@example.com")
            b, _ = await accounts.get_or_create_user("b@example.com")
            u, _ = await accounts.get_or_create_user("u@example.com")
            await accounts.invite_team_member(a["id"], u["email"])
            u = await accounts.accept_team_invite(u, a["id"])
            rejected = await accounts.accept_team_invite(u, b["id"])
            return rejected, await accounts.list_team_members(a["id"]), (await accounts.get_user(u["id"]))["team_owner_id"], a["id"]

    rejected, members, pointer, a_id = asyncio.run(run())
    assert rejected is None
    assert [m["status"] for m in members] == ["active"], "a rejected join removed the current membership"
    assert pointer == a_id


@requires_postgres
@pytest.mark.parametrize("kind", ["revoked", "already-accepted"])
def test_a_revoked_or_already_accepted_invite_does_not_disturb_the_current_membership(monkeypatch, kind):
    async def run():
        async with isolated_pool(monkeypatch, 3):
            a, _ = await accounts.get_or_create_user("a@example.com")
            b, _ = await accounts.get_or_create_user("b@example.com")
            u, _ = await accounts.get_or_create_user("u@example.com")
            await accounts.invite_team_member(a["id"], u["email"])
            u = await accounts.accept_team_invite(u, a["id"])
            if kind == "revoked":
                await accounts.invite_team_member(b["id"], u["email"])
                await accounts.remove_team_member(b["id"], u["email"])
                rejected = await accounts.accept_team_invite(u, b["id"])
            else:
                rejected = await accounts.accept_team_invite(u, a["id"])
            return rejected, await accounts.list_team_members(a["id"]), (await accounts.get_user(u["id"]))["team_owner_id"], a["id"]

    rejected, members, pointer, a_id = asyncio.run(run())
    assert rejected is None and [m["status"] for m in members] == ["active"] and pointer == a_id


@requires_postgres
@pytest.mark.parametrize("pointer", ["names-the-older", "unset"])
def test_the_upgrade_reconciles_duplicate_memberships_before_adding_the_constraint(monkeypatch, pointer):
    """The verification recreated the duplicate rows the previous version
    allowed and saw the schema fail to initialise. Now the older data state
    is reconciled first: the membership the user's pointer names survives, or
    with no pointer the most recently accepted; the other is kept in
    team_members_reconciled; the pointer ends on the survivor."""

    async def run():
        async with isolated_pool(monkeypatch, 3) as pool:
            a, _ = await accounts.get_or_create_user("a2@example.com")
            b, _ = await accounts.get_or_create_user("b2@example.com")
            u, _ = await accounts.get_or_create_user("u2@example.com")
            async with pool.acquire() as conn:
                await conn.execute("DROP INDEX team_members_one_active")
                await conn.execute("INSERT INTO team_members (owner_id, email, status, user_id, accepted_at) VALUES ($1, $2, 'active', $3, NOW() - interval '1 day')", a["id"], u["email"], u["id"])
                await conn.execute("INSERT INTO team_members (owner_id, email, status, user_id, accepted_at) VALUES ($1, $2, 'active', $3, NOW())", b["id"], u["email"], u["id"])
                await conn.execute("UPDATE users SET team_owner_id = $2 WHERE id = $1", u["id"], a["id"] if pointer == "names-the-older" else None)
                await conn.execute(db._PLANS_TABLE_SQL)     # the upgrade, on the older data state
                active = await conn.fetch("SELECT owner_id FROM team_members WHERE user_id = $1 AND status = 'active'", u["id"])
                moved = await conn.fetch("SELECT owner_id, reason FROM team_members_reconciled WHERE user_id = $1", u["id"])
                index = await conn.fetchval("SELECT to_regclass('team_members_one_active')")
                final_pointer = await conn.fetchval("SELECT team_owner_id FROM users WHERE id = $1", u["id"])
            return [str(r["owner_id"]) for r in active], [(str(r["owner_id"]), r["reason"]) for r in moved], index, str(final_pointer), a["id"], b["id"]

    active, moved, index, final_pointer, a_id, b_id = asyncio.run(run())
    survivor, loser = (a_id, b_id) if pointer == "names-the-older" else (b_id, a_id)
    assert active == [survivor], f"survivor should be {'the pointed-at' if pointer == 'names-the-older' else 'the most recently accepted'} membership"
    assert moved == [(loser, "duplicate active membership before team_members_one_active")]
    assert index is not None and final_pointer == survivor
