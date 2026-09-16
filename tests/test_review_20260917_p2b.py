"""R09, R10 and R11 of the 2026-09-17 consolidated review, as regression
tests: the review's reproductions inverted, with controls."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import accounts, credits, identity, limits, main, task_scheduling, tasks
from app.settings import settings
from tests.conftest import sign_in


# --- R09 · task recovery neither loses work nor repeats it -------------------

def test_a_one_shot_brief_whose_provider_failed_is_not_marked_done(monkeypatch):
    """The review's reproduction, inverted: an evaluation that failed is an
    error to retry, not a condition that did not fire."""

    async def run():
        user, _ = await accounts.get_or_create_user("oneshot@example.com")
        await credits.append(credits.user_account_id(user["id"]), 5, "test", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"at": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()})
        monkeypatch.setattr(tasks, "_now", lambda: datetime.now(timezone.utc) + timedelta(seconds=5))
        monkeypatch.setattr(tasks, "compose_brief", AsyncMock(side_effect=RuntimeError("provider offline")))
        result = await tasks.run_task(task)
        return result, await tasks.get_task(task["id"])

    result, stored = asyncio.run(run())
    assert result["result"] == "error" and result["status"] != "done"
    assert stored["status"] == "active" and stored["next_run_at"] is not None, "the promised brief was dropped"
    assert stored["claimed_occurrence"], "the occurrence identity was cleared, so a retry would be a new charge"


def test_a_retry_after_a_post_delivery_write_failure_does_not_deliver_or_charge_again(monkeypatch):
    """The review's reproduction, inverted: delivery and charge succeeded and
    only the final bookkeeping write failed; the retry must find both done."""

    async def run():
        user, _ = await accounts.get_or_create_user("retry@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 5, "test", "test", "seed")
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "body")))
        base = datetime.now(timezone.utc)
        task = await tasks.create_task(user, "brief", {}, {"at": (base + timedelta(seconds=2)).isoformat()})
        monkeypatch.setattr(tasks, "_now", lambda: base + timedelta(seconds=5))
        original = tasks.update_task
        calls = 0

        async def fail_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient write failure after delivery")
            return await original(*args, **kwargs)

        monkeypatch.setattr(tasks, "update_task", fail_first)
        first = await tasks.run_task(task)
        monkeypatch.setattr(tasks, "_now", lambda: base + timedelta(minutes=10))
        second = await tasks.run_task(await tasks.get_task(task["id"]))
        return first, second, await credits.balance(account), await tasks.inbox(user["id"])

    first, second, balance, inbox = asyncio.run(run())
    assert first["result"] == "error"
    assert balance == 4, f"the retry charged again (balance {balance})"
    assert len(inbox) == 1, "the retry delivered the same occurrence twice"


def test_a_healthy_one_shot_brief_still_completes(monkeypatch):
    async def run():
        user, _ = await accounts.get_or_create_user("oneshot-ok@example.com")
        await credits.append(credits.user_account_id(user["id"]), 5, "test", "test", "seed")
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "body")))
        base = datetime.now(timezone.utc)
        task = await tasks.create_task(user, "brief", {}, {"at": (base + timedelta(seconds=2)).isoformat()})
        monkeypatch.setattr(tasks, "_now", lambda: base + timedelta(seconds=5))
        result = await tasks.run_task(task)
        return result, await tasks.get_task(task["id"])

    result, stored = asyncio.run(run())
    assert result["fired"] and stored["status"] == "done"


# --- R10 · team membership is exclusive and seat allocation is serialised ----

def test_an_old_team_owner_cannot_detach_a_member_who_moved_to_another_team():
    """The review's reproduction, inverted."""

    async def run():
        a, _ = await accounts.get_or_create_user("a@example.com")
        b, _ = await accounts.get_or_create_user("b@example.com")
        u, _ = await accounts.get_or_create_user("member@example.com")
        await accounts.invite_team_member(a["id"], u["email"])
        await accounts.invite_team_member(b["id"], u["email"])
        u = await accounts.accept_team_invite(u, a["id"])
        u = await accounts.accept_team_invite(u, b["id"])
        assert u["team_owner_id"] == b["id"]
        # Joining B left A: A has no active record for this member any more.
        a_members = [m for m in await accounts.list_team_members(a["id"]) if m.get("user_id") == u["id"]]
        await accounts.remove_team_member(a["id"], u["email"])
        return a_members, (await accounts.get_user(u["id"]))["team_owner_id"], b["id"]

    a_members, pointer, b_id = asyncio.run(run())
    assert a_members == [], "membership of the old team survived joining the new one"
    assert pointer == b_id, "the old owner detached the member from their new team"


def test_the_current_owner_can_still_remove_a_member():
    """The control."""

    async def run():
        owner, _ = await accounts.get_or_create_user("owner-ok@example.com")
        u, _ = await accounts.get_or_create_user("member-ok@example.com")
        await accounts.invite_team_member(owner["id"], u["email"])
        u = await accounts.accept_team_invite(u, owner["id"])
        await accounts.remove_team_member(owner["id"], u["email"])
        return (await accounts.get_user(u["id"]))["team_owner_id"]

    assert asyncio.run(run()) is None


def test_concurrent_invitations_never_exceed_the_seat_count(monkeypatch):
    """The review raced two invitations past the last seat. The count and the
    insert are now serialised per team, so however the callers interleave the
    seat count holds."""

    async def run():
        owner, _ = await accounts.get_or_create_user("seats@example.com")
        owner = await accounts.update_user(owner["id"], plan_id="max")
        seats = (await identity._identity_for_user(owner, "local")).plan.seats
        for n in range(seats - 2):
            await accounts.invite_team_member_within_seats(owner["id"], f"member{n}@example.com", seats)
        # A yield inside the critical section so any interleaving that could
        # happen has the chance to.
        original = accounts.invite_team_member

        async def slow_invite(*args, **kwargs):
            await asyncio.sleep(0)
            return await original(*args, **kwargs)

        monkeypatch.setattr(accounts, "invite_team_member", slow_invite)
        outcomes = await asyncio.gather(
            *(accounts.invite_team_member_within_seats(owner["id"], f"extra{n}@example.com", seats) for n in range(3)),
            return_exceptions=True,
        )
        members = await accounts.list_team_members(owner["id"])
        return outcomes, len(members), seats

    outcomes, member_count, seats = asyncio.run(run())
    assert member_count + 1 <= seats, f"{member_count} members on {seats} seats"
    assert sum(isinstance(o, accounts.SeatsExhausted) for o in outcomes) >= 2


# --- R11 · advertised entitlements are the enforced ones ---------------------

def test_a_team_member_gets_the_task_limit_the_ui_advertises():
    """The review's reproduction, inverted: the effective plan's limit."""

    async def run():
        owner, _ = await accounts.get_or_create_user("max@example.com")
        await accounts.update_user(owner["id"], plan_id="max")
        u, _ = await accounts.get_or_create_user("team@example.com")
        u = await accounts.update_user(u["id"], team_owner_id=owner["id"])
        ident = await identity._identity_for_user(u, "local")
        advertised = (await task_scheduling.list_for_user(ident))["limit"]
        created = 0
        for n in range(advertised):
            await tasks.create_task(u, "reminder", {"message": str(n)}, {"every_minutes": 60})
            created += 1
        with pytest.raises(ValueError):
            await tasks.create_task(u, "reminder", {"message": "extra"}, {"every_minutes": 60})
        return advertised, created

    advertised, created = asyncio.run(run())
    assert advertised == tasks.TASK_LIMITS["max"] and created == advertised


def test_a_paid_team_member_can_use_the_key_they_were_allowed_to_create():
    """The review's reproduction, inverted."""
    client = TestClient(main.app)
    u = sign_in(client)["user"]

    async def link():
        owner, _ = await accounts.get_or_create_user("keyowner@example.com")
        await accounts.update_user(owner["id"], plan_id="max")
        await accounts.update_user(u["id"], team_owner_id=owner["id"])

    asyncio.run(link())
    created = client.post("/me/api-keys", json={"name": "team key", "scopes": ["data"]})
    assert created.status_code == 201, created.text
    key = TestClient(main.app)
    key.headers["Authorization"] = "Bearer " + created.json()["secret"]
    assert key.get("/me/credits").status_code == 200


def test_chat_admission_uses_the_plans_published_rate(monkeypatch):
    monkeypatch.setattr(limits, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "chat_requests_per_minute", 60)

    async def run():
        results = []
        for _ in range(3):
            ok, _ = await limits.allow_chat_request("plan-rate-probe", plan_limit=2)
            results.append(ok)
        return results

    assert asyncio.run(run()) == [True, True, False]
