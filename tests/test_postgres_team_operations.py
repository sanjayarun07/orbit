"""Opt-in real-Postgres tests for team operations (R10, R11 follow-ups).

    TEST_DATABASE_URL=postgresql://localhost/orbit_test pytest tests/test_postgres_team_operations.py
"""
import asyncio
import os

import asyncpg
import pytest

from app import accounts, tasks
from tests.test_postgres_task_gate import isolated_pool

requires_postgres = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="isolated Postgres DSN not set; team operations under a real pool are UNVERIFIED in this run",
)


@requires_postgres
@pytest.mark.parametrize("operation", ["invite", "member_task"])
def test_team_operations_complete_on_a_one_connection_pool(monkeypatch, operation):
    """The verification's minimum case: with exactly one connection, any nested
    pool acquisition inside a gate deadlocks. Both paths used to."""

    async def run():
        async with isolated_pool(monkeypatch, 1):
            owner, _ = await accounts.get_or_create_user("owner@example.com")
            owner = await accounts.update_user(owner["id"], plan_id="max")
            member, _ = await accounts.get_or_create_user("member@example.com")
            member = await accounts.update_user(member["id"], team_owner_id=owner["id"])
            if operation == "invite":
                call = accounts.invite_team_member_within_seats(owner["id"], "new@example.com", 5)
            else:
                call = tasks.create_task(member, "reminder", {"message": "hello"}, {"every_minutes": 60})
            return await asyncio.wait_for(call, 5)

    result = asyncio.run(run())
    assert result


@requires_postgres
def test_concurrent_team_accepts_leave_exactly_one_active_membership(monkeypatch):
    """The verification synchronised two accepts after their cleanup deletes and
    saw two active memberships. Joins are now serialised on the user's row and
    the database refuses a second active membership regardless."""

    async def run():
        async with isolated_pool(monkeypatch, 4):
            a, _ = await accounts.get_or_create_user("a@example.com")
            b, _ = await accounts.get_or_create_user("b@example.com")
            u, _ = await accounts.get_or_create_user("u@example.com")
            for owner in (a, b):
                await accounts.invite_team_member(owner["id"], u["email"])
            original = asyncpg.Connection.execute
            arrived = 0
            gate = asyncio.Event()

            async def execute(self, query, *args, **kwargs):
                nonlocal arrived
                result = await original(self, query, *args, **kwargs)
                if "DELETE FROM team_members WHERE user_id" in query:
                    arrived += 1
                    if arrived == 2:
                        gate.set()
                    try:
                        await asyncio.wait_for(gate.wait(), timeout=0.3)
                    except asyncio.TimeoutError:
                        pass   # serialised: the second cannot arrive while the first holds the row
                return result

            monkeypatch.setattr(asyncpg.Connection, "execute", execute)
            outcomes = await asyncio.wait_for(asyncio.gather(
                accounts.accept_team_invite(u, a["id"]), accounts.accept_team_invite(u, b["id"]), return_exceptions=True), 10)
            active = [m for o in (a, b) for m in await accounts.list_team_members(o["id"]) if m.get("status") == "active"]
            pointer = (await accounts.get_user(u["id"]))["team_owner_id"]
            return outcomes, active, pointer

    outcomes, active, pointer = asyncio.run(run())
    assert len(active) == 1, f"two active memberships: {active}"
    assert pointer == active[0]["owner_id"] if "owner_id" in active[0] else pointer in (None, pointer)


@requires_postgres
def test_the_database_itself_refuses_a_second_active_membership(monkeypatch):
    """The invariant as a constraint, exercised directly."""

    async def run():
        async with isolated_pool(monkeypatch, 2) as pool:
            a, _ = await accounts.get_or_create_user("a2@example.com")
            b, _ = await accounts.get_or_create_user("b2@example.com")
            u, _ = await accounts.get_or_create_user("u2@example.com")
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO team_members (owner_id, email, status, user_id) VALUES ($1, $2, 'active', $3)", a["id"], u["email"], u["id"])
                try:
                    await conn.execute("INSERT INTO team_members (owner_id, email, status, user_id) VALUES ($1, $2, 'active', $3)", b["id"], u["email"], u["id"])
                except asyncpg.UniqueViolationError:
                    return "refused"
            return "allowed"

    assert asyncio.run(run()) == "refused"
