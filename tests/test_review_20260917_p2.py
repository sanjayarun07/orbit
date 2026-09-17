"""The P2 findings of the 2026-09-17 consolidated review, R06 onward, as
regression tests: the review's reproductions with their assertions inverted,
plus a control beside each refusal.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import accounts, credits, identity, limits, main, session_access, sessions, tasks
from app.settings import settings
from tests.conftest import sign_in


def _anonymous_request(device: str, ip: str = "192.0.2.10") -> Request:
    return Request({"type": "http", "headers": [(b"x-orbit-device", device.encode())], "client": (ip, 1234)})


@pytest.fixture(autouse=True)
def _fresh_rate_windows():
    """The in-memory rate and budget windows are module state; one test's
    spent IP budget must not carry into the next."""
    limits._local_windows.clear()
    yield
    limits._local_windows.clear()


# --- R06 · public cost admission cannot be reset from the client -------------

def test_rotating_the_device_id_does_not_mint_unlimited_trials(monkeypatch):
    """The review's reproduction, inverted: after the per-IP budget, a fresh
    device id from the same address gets an account with no credits."""
    monkeypatch.setattr(settings, "trial_accounts_per_ip_per_day", 2)
    monkeypatch.setattr(limits, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(credits, "get_redis", AsyncMock(return_value=None), raising=False)

    async def run():
        balances = []
        for device in ("device-a", "device-b", "device-c", "device-d"):
            ident = await identity.resolve_identity(_anonymous_request(device))
            balances.append(await credits.balance(ident.account_id))
        return balances

    balances = asyncio.run(run())
    assert balances[0] > 0 and balances[1] > 0, "the first devices must still get a trial"
    assert balances[2] == 0 and balances[3] == 0, f"trials kept minting past the IP budget: {balances}"


def test_a_returning_device_does_not_spend_the_ip_budget(monkeypatch):
    """The control: the budget counts new trial accounts, not visits."""
    monkeypatch.setattr(settings, "trial_accounts_per_ip_per_day", 1)
    monkeypatch.setattr(limits, "get_redis", AsyncMock(return_value=None))

    async def run():
        first = await identity.resolve_identity(_anonymous_request("same-device", ip="192.0.2.11"))
        again = await identity.resolve_identity(_anonymous_request("same-device", ip="192.0.2.11"))
        return await credits.balance(first.account_id), first.account_id == again.account_id

    balance, same = asyncio.run(run())
    assert balance > 0 and same


def test_rotating_the_device_id_does_not_reset_the_chat_rate_limit(monkeypatch):
    """The identity bucket is keyed by the device id; the network bucket is not."""
    monkeypatch.setattr(settings, "chat_requests_per_minute", 5)
    monkeypatch.setattr(settings, "chat_requests_per_minute_per_ip", 2)
    monkeypatch.setattr(limits, "get_redis", AsyncMock(return_value=None))

    async def run():
        outcomes = []
        for device in ("rotate-1", "rotate-2", "rotate-3"):
            ident = await identity.resolve_identity(_anonymous_request(device, ip="192.0.2.77"))
            own_ok, _ = await limits.allow_chat_request(ident.rate_limit_key)
            ip_ok, _ = await limits.allow_chat_request_from_ip(ident.ip)
            outcomes.append(own_ok and ip_ok)
        return outcomes

    assert asyncio.run(run()) == [True, True, False]


def test_public_knowledge_search_is_bounded_rate_limited_and_budgeted(monkeypatch):
    from app import knowledge

    monkeypatch.setattr(limits, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(main.kb_retrieval, "search", AsyncMock(return_value=([], type("Plan", (), {"entities": [], "graph_expanded": False})())))
    monkeypatch.setattr(main.kb_retrieval, "build_context", lambda hits: ("", []))
    monkeypatch.setattr(main.kb_tool, "resolver", AsyncMock(return_value=None))
    client = TestClient(main.app)

    assert client.get("/knowledge/search", params={"q": "x" * (settings.knowledge_search_max_query_chars + 1)}).status_code == 413

    monkeypatch.setattr(settings, "knowledge_search_daily_budget", 2)
    assert client.get("/knowledge/search", params={"q": "what is a rollup"}).status_code == 200
    assert client.get("/knowledge/search", params={"q": "what is a rollup"}).status_code == 200
    exhausted = client.get("/knowledge/search", params={"q": "what is a rollup"})
    assert exhausted.status_code == 429 and "budget" in exhausted.json()["detail"]


# --- R07 · every spender uses one atomic charge --------------------------------

def test_two_briefs_cannot_both_spend_the_last_credit(monkeypatch):
    """The review's reproduction, inverted: two occurrences synchronised at
    the balance read used to each debit the single remaining credit."""

    async def run():
        user, _ = await accounts.get_or_create_user("brief@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 1, "test", "test", "seed")
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "body")))
        # The same synchronisation the review used, at the point the old code
        # read the balance; the atomic charge never reads it unlocked.
        original_balance = credits.balance
        arrived = 0
        gate = asyncio.Event()

        async def balance(account_id):
            nonlocal arrived
            value = await original_balance(account_id)
            arrived += 1
            if arrived >= 2:
                gate.set()
            try:
                await asyncio.wait_for(gate.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
            return value

        monkeypatch.setattr(credits, "balance", balance)
        made = [await tasks.create_task(user, "brief", {}, {"every_minutes": 60}) for _ in range(2)]
        results = await asyncio.gather(*(tasks.run_task(t) for t in made))
        return await original_balance(account), results

    balance, results = asyncio.run(run())
    assert balance == 0, f"the account went to {balance}"
    assert sum(1 for r in results if r["fired"]) == 1
    assert sum(1 for r in results if "out of credits" in str(r["result"])) == 1


def test_a_brief_still_charges_once_and_delivers(monkeypatch):
    """The control, including the idempotent re-run of the same occurrence."""

    async def run():
        user, _ = await accounts.get_or_create_user("brief-ok@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 5, "test", "test", "seed")
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "body")))
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        first = await tasks.run_task(task)
        return first, await credits.balance(account), len(await tasks.inbox(user["id"]))

    first, balance, delivered = asyncio.run(run())
    assert first["fired"] and balance == 4 and delivered == 1


def test_a_brief_that_fails_to_compose_refunds_its_charge(monkeypatch):
    """Reserve before the work, release when the work fails FOR GOOD. A retry
    keeps the payment (a refunded retry used to deliver for free), so the
    refund comes when the bounded retries are exhausted; limit 1 here."""
    monkeypatch.setattr(settings, "task_retry_limit", 1)

    async def run():
        user, _ = await accounts.get_or_create_user("brief-refund@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 3, "test", "test", "seed")
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("provider offline")))
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        result = await tasks.run_task(task)
        return result, await credits.balance(account)

    result, balance = asyncio.run(run())
    assert result["result"] == "failed" and balance == 3   # limit reached on the first failure: refunded, dropped


def test_a_members_task_is_billed_by_the_same_rule_chat_uses():
    """A stale team pointer must not keep billing an owner who dropped the
    member; identity resolution clears it, and charging goes through that."""

    async def run():
        member, _ = await accounts.get_or_create_user("stale-member@example.com")
        member = await accounts.update_user(member["id"], team_owner_id="00000000-0000-4000-8000-00000000dead")
        payer = await tasks._billing_account_for(member)
        return payer, credits.user_account_id(member["id"])

    payer, own = asyncio.run(run())
    assert payer == own


# --- R08 · conversation ownership is part of the turn ---------------------------

def test_a_new_conversation_is_owned_before_any_answer_is_returned(monkeypatch):
    """The review's reproduction, inverted: with ownership recording failing,
    there must be no answer at all -- not a private answer on an unowned id."""
    from app import execution_policy
    from app.models import AgentResponse, ChatRequest
    from app.service_errors import ServiceError

    async def run():
        user, _ = await accounts.get_or_create_user("history@example.com")
        ident = identity.Identity("user", "user:" + user["id"], "local", user=user)

        async def fake(body, ident, sid):
            return AgentResponse(session_id=sid, answer="private result", intent="research")

        monkeypatch.setattr(execution_policy, "_execute_chat_turn", fake)
        monkeypatch.setattr(accounts, "touch_chat_session", AsyncMock(side_effect=RuntimeError("database temporarily down")))
        try:
            await execution_policy.execute_chat_turn(ChatRequest(message="private"), ident)
        except ServiceError as exc:
            return exc.status_code
        return None

    assert asyncio.run(run()) == 503


def test_a_new_conversation_is_claimed_before_the_turn_runs(monkeypatch):
    """The control: ownership is recorded before the answer, so an id learned
    from the response is already someone's."""
    from app import execution_policy
    from app.models import AgentResponse, ChatRequest

    async def run():
        user, _ = await accounts.get_or_create_user("owned-first@example.com")
        await credits.append(credits.user_account_id(user["id"]), 10, "test", "test", "seed")
        ident = identity.Identity("user", "user:" + user["id"], "local", user=user)
        seen = {}

        async def fake(body, ident, sid):
            seen["owner_during_turn"] = await accounts.chat_session_owner(sid)
            return AgentResponse(session_id=sid, answer="ok", intent="research")

        monkeypatch.setattr(execution_policy, "_execute_chat_turn", fake)
        result = await execution_policy.execute_chat_turn(ChatRequest(message="hi"), ident)
        return seen["owner_during_turn"], await accounts.chat_session_owner(result.session_id), user["id"]

    during, after, owner = asyncio.run(run())
    assert during == owner and after == owner


def test_bulk_deletion_keeps_a_conversation_whose_turn_is_still_running(monkeypatch):
    """The review's reproduction, inverted: the held lease means the
    conversation is kept and still owned, and the caller is told."""
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "request_queue_timeout_seconds", 1)

    async def run():
        user, _ = await accounts.get_or_create_user("delete@example.com")
        sid = "review-delete-active"
        await accounts.touch_chat_session(user["id"], sid)
        await sessions.append_turn(sid, "user", "still working")
        lease = await sessions.acquire_session_turn(sid)
        try:
            result = await main.delete_my_conversations(identity.Identity("user", "user:" + user["id"], "local", user=user))
            owner = await accounts.chat_session_owner(sid)
            messages = await sessions.get_messages(sid)
        finally:
            await lease.release()
        return result, owner, messages, user["id"]

    result, owner, messages, user_id = asyncio.run(run())
    assert result["deleted"] == 0 and result["busy"] == 1
    assert owner == user_id and messages, "a conversation mid-turn was deleted from under its lease"


def test_bulk_deletion_still_deletes_idle_conversations(monkeypatch):
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))

    async def run():
        user, _ = await accounts.get_or_create_user("delete-idle@example.com")
        for sid in ("idle-1", "idle-2"):
            await accounts.touch_chat_session(user["id"], sid)
            await sessions.append_turn(sid, "user", "old")
        result = await main.delete_my_conversations(identity.Identity("user", "user:" + user["id"], "local", user=user))
        return result, await accounts.list_chat_sessions(user["id"])

    result, remaining = asyncio.run(run())
    assert result["deleted"] == 2 and result["busy"] == 0 and remaining == []
