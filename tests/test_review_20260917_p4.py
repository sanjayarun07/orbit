"""Follow-ups from the verification of a1b477ba: the five findings that were
still partial (R04, R05, R08, R09, R10), as regression tests. Each is the
verification's own reproduction with its assertion inverted, plus the controls
that show the fixed path still works. The Postgres-only parts (R09 hydration,
R10 join validation and the duplicate-membership migration) are in
tests/test_postgres_review_p4.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import accounts, billing, credits, execution_policy, identity, main, session_access, sessions, tasks
from app.service_errors import ServiceError
from app.settings import settings


# --- R04 · one writer path; ties reconcile against Stripe ------------------------

def _authority(monkeypatch, **state):
    """Stripe configured, answering a subscription retrieval with `state`."""
    monkeypatch.setattr(billing, "configured", lambda: True)
    retrieved = []

    def retrieve(sid):
        retrieved.append(sid)
        return {"id": "sub_a", "created": 100, **state}

    monkeypatch.setattr(billing, "_api_retrieve_subscription", retrieve)
    return retrieved


async def _cancelled_account(email):
    user, _ = await accounts.get_or_create_user(email)
    await accounts.update_user(user["id"], stripe_subscription_id="sub_a", subscription_created=100,
                               subscription_event_at=300, subscription_status="canceled", plan_id="free")
    return user


@pytest.mark.parametrize("writer", ["checkout", "invoice", "update"])
def test_a_tied_writer_cannot_restore_a_cancelled_subscription(monkeypatch, writer):
    """The verification's reproduction, inverted: an event at the very second
    of the recorded cancellation, claiming Pro, with Stripe saying the
    subscription is canceled. Every writer consults Stripe and writes what
    Stripe holds -- a checkout used to take the tie as agreement, an invoice
    used to take the retrieval as a boolean and write its own plan."""
    retrieved = _authority(monkeypatch, status="canceled", metadata={"plan_id": "pro"})

    async def run():
        user = await _cancelled_account(f"tie-{writer}@example.com")
        meta = {"user_id": user["id"], "plan_id": "pro"}
        account = credits.user_account_id(user["id"])
        before = await credits.balance(account)
        if writer == "checkout":
            result = await billing._on_checkout_completed("late", {"mode": "subscription", "subscription": "sub_a", "created": 100, "metadata": meta}, 300)
        elif writer == "invoice":
            result = await billing._on_invoice_paid("late", {"id": "in_a", "subscription": "sub_a", "created": 300, "metadata": meta,
                                                             "lines": {"data": [{"metadata": {"plan_id": "pro"}}]}}, 300)
        else:
            result = await billing._on_subscription_updated("late", {"id": "sub_a", "created": 100, "status": "active", "metadata": meta}, 300)
        return result, await accounts.get_user(user["id"]), await credits.balance(account) - before

    result, final, granted = asyncio.run(run())
    assert final["plan_id"] == "free" and final["subscription_status"] == "canceled", final
    assert retrieved == ["sub_a"], "the tie was not reconciled against Stripe"
    if writer == "invoice":
        # The payment is a separate fact from the entitlement: the credits are
        # granted, and the entitlement is whatever Stripe holds (here, the
        # cancellation written back), never the invoice's own plan.
        assert result["status"] == "credited" and granted > 0


@pytest.mark.parametrize("order", [["max", "pro"], ["pro", "max"]], ids=["max-then-pro", "pro-then-max"])
def test_tied_updates_apply_the_price_stripe_holds_in_either_order(monkeypatch, order):
    """Authority by price, not metadata: the retrieved subscription's item is
    the Max price while its (stale) metadata still says Pro."""
    monkeypatch.setattr(settings, "stripe_price_max", "price_max")
    _authority(monkeypatch, status="active", metadata={"plan_id": "pro"}, items={"data": [{"price": {"id": "price_max"}}]})

    async def run():
        user, _ = await accounts.get_or_create_user(f"price-{'-'.join(order)}@example.com")
        await accounts.update_user(user["id"], stripe_subscription_id="sub_a", subscription_created=100,
                                   subscription_event_at=300, subscription_status="active", plan_id="pro")
        for plan in order:
            await billing._on_subscription_updated("evt_" + plan, {"id": "sub_a", "created": 100, "status": "active",
                                                                   "metadata": {"user_id": user["id"], "plan_id": plan}}, 300)
        return await accounts.get_user(user["id"])

    final = asyncio.run(run())
    assert final["plan_id"] == "max" and final["subscription_status"] == "active"


def test_a_tied_deletion_is_reconciled_like_any_other_writer(monkeypatch):
    retrieved = _authority(monkeypatch, status="canceled", metadata={"plan_id": "pro"})

    async def run():
        user, _ = await accounts.get_or_create_user("tie-delete@example.com")
        await accounts.update_user(user["id"], stripe_subscription_id="sub_a", subscription_created=100,
                                   subscription_event_at=300, subscription_status="active", plan_id="pro")
        result = await billing._on_subscription_deleted("del", {"id": "sub_a", "created": 100, "status": "canceled",
                                                                "metadata": {"user_id": user["id"], "plan_id": "pro"}}, 300)
        return result, await accounts.get_user(user["id"])

    result, final = asyncio.run(run())
    assert result["status"] == "downgraded" and final["plan_id"] == "free" and final["subscription_status"] == "canceled"
    assert retrieved == ["sub_a"]


def test_a_tie_stripe_cannot_answer_is_left_for_redelivery_not_marked_processed(monkeypatch):
    """The verification saw the first delivery ignored and the second refused
    as a duplicate. Now the first raises -- the webhook answers 500, Stripe
    redelivers -- and the redelivery, with Stripe reachable, applies."""
    monkeypatch.setattr(billing, "configured", lambda: True)
    answers = {"up": False}

    def retrieve(sid):
        if not answers["up"]:
            raise RuntimeError("Stripe temporarily unavailable")
        return {"id": "sub_a", "created": 100, "status": "active", "metadata": {"plan_id": "max"}}

    monkeypatch.setattr(billing, "_api_retrieve_subscription", retrieve)

    async def run():
        user, _ = await accounts.get_or_create_user("tie-outage@example.com")
        await accounts.update_user(user["id"], stripe_subscription_id="sub_a", subscription_created=100,
                                   subscription_event_at=300, subscription_status="active", plan_id="pro")
        event = {"id": "evt_tie_outage", "type": "customer.subscription.updated", "created": 300,
                 "data": {"object": {"id": "sub_a", "created": 100, "status": "active", "metadata": {"user_id": user["id"], "plan_id": "max"}}}}
        with pytest.raises(billing.ReconciliationUnavailable):
            await billing.handle_event(event)
        unchanged = (await accounts.get_user(user["id"]))["plan_id"]
        answers["up"] = True
        redelivered = await billing.handle_event(event)
        return unchanged, redelivered, await accounts.get_user(user["id"])

    unchanged, redelivered, final = asyncio.run(run())
    assert unchanged == "pro", "an unreconciled tie changed the account"
    assert redelivered["status"] == "updated" and final["plan_id"] == "max", redelivered


@pytest.mark.parametrize("order", [["max", "pro"], ["pro", "max"]], ids=["max-then-pro", "pro-then-max"])
def test_without_stripe_a_conflicting_tie_leaves_the_record_as_it_was_in_either_order(order):
    """No authority to ask: the agreeing event changes nothing and the
    conflicting one is ignored, so the record is its prior state in both
    orders. The assertion is on that one state, not on an order-dependent
    outcome."""
    async def run():
        user, _ = await accounts.get_or_create_user(f"nostripe-{'-'.join(order)}@example.com")
        await accounts.update_user(user["id"], stripe_subscription_id="sub_a", subscription_created=100,
                                   subscription_event_at=300, subscription_status="active", plan_id="pro")
        for plan in order:
            await billing._on_subscription_updated("evt_" + plan, {"id": "sub_a", "created": 100, "status": "active",
                                                                   "metadata": {"user_id": user["id"], "plan_id": plan}}, 300)
        return await accounts.get_user(user["id"])

    final = asyncio.run(run())
    assert final["plan_id"] == "pro" and final["subscription_status"] == "active"


# --- R05 · a durable checkout intent; the earlier session is settled with Stripe --

def _checkout_harness(monkeypatch, *, state="open", retrieve_fails=False):
    """A Stripe that honours idempotency keys, as Stripe does: a replayed
    create returns the session the first call made."""
    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
    monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda cid: [])
    h = SimpleNamespace(created=[], calls=[], expired=[], retrieved=[], by_key={})

    def create(params):
        h.calls.append(params)
        key = params.get("idempotency_key")
        if key in h.by_key:
            return h.by_key[key]
        h.created.append(params)
        session = {"id": f"cs_{len(h.created)}", "url": f"https://example.com/{len(h.created)}"}
        h.by_key[key] = session
        return session

    def retrieve(sid):
        h.retrieved.append(sid)
        if retrieve_fails:
            raise RuntimeError("Stripe unavailable")
        return {"id": sid, "status": state, "url": f"https://example.com/{sid}"}

    monkeypatch.setattr(billing, "_api_create_checkout", create)
    monkeypatch.setattr(billing, "_api_retrieve_checkout", retrieve)
    monkeypatch.setattr(billing, "_api_expire_checkout", lambda sid: h.expired.append(sid) or {"id": sid, "status": "expired"})
    return h


async def _customer(email):
    user, _ = await accounts.get_or_create_user(email)
    return await accounts.update_user(user["id"], stripe_customer_id="cus_" + email.split("@")[0])


def test_a_checkout_past_its_window_is_expired_at_stripe_before_it_is_replaced(monkeypatch):
    """The verification advanced the clock past the hold and saw a second
    session with the first left payable and neither carrying an expiry."""
    h = _checkout_harness(monkeypatch)
    clock = [10_000]
    monkeypatch.setattr(billing.time, "time", lambda: clock[0])

    async def run():
        user = await _customer("window@example.com")
        first = await billing.create_checkout(user, "subscription", "pro", "https://example.com")
        clock[0] += billing._checkout_window() + 1
        second = await billing.create_checkout(await accounts.get_user(user["id"]), "subscription", "pro", "https://example.com")
        return first, second

    first, second = asyncio.run(run())
    assert h.expired == [first["session_id"]], "the earlier open session was not expired before a replacement"
    assert second["session_id"] != first["session_id"] and len(h.created) == 2
    assert [p["expires_at"] for p in h.created] == [10_000 + billing._checkout_window(), clock[0] + billing._checkout_window()]


def test_a_completed_checkout_awaiting_its_webhook_blocks_a_replacement(monkeypatch):
    h = _checkout_harness(monkeypatch, state="complete")

    async def run():
        user = await _customer("paid@example.com")
        await accounts.update_user(user["id"], checkout_session_id="cs_paid", checkout_started_at=1)   # long past the window
        with pytest.raises(billing.CheckoutPending):
            await billing.create_checkout(await accounts.get_user(user["id"]), "subscription", "pro", "https://example.com")

    asyncio.run(run())
    assert h.retrieved == ["cs_paid"] and h.created == [] and h.expired == []


def test_an_earlier_session_stripe_cannot_confirm_fails_closed(monkeypatch):
    h = _checkout_harness(monkeypatch, retrieve_fails=True)

    async def run():
        user = await _customer("unconfirmed@example.com")
        await accounts.update_user(user["id"], checkout_session_id="cs_old", checkout_started_at=1)
        with pytest.raises(billing.CheckoutPending):
            await billing.create_checkout(await accounts.get_user(user["id"]), "subscription", "pro", "https://example.com")

    asyncio.run(run())
    assert h.created == []


def test_a_session_created_but_never_recorded_is_replayed_with_its_own_key(monkeypatch):
    """The verification failed the local write after the remote creation and
    saw a second session. The intent -- and its idempotency key -- is durable
    before the remote call, so the retry replays the identical request and
    Stripe answers with the session it already made."""
    h = _checkout_harness(monkeypatch)
    original = accounts.update_user

    async def fail_once(uid, db=None, **fields):
        if fields.get("checkout_session_id") == "cs_1":
            raise RuntimeError("database write failure")
        return await original(uid, db=db, **fields)

    monkeypatch.setattr(accounts, "update_user", fail_once)

    async def run():
        user = await _customer("crash@example.com")
        with pytest.raises(RuntimeError):
            await billing.create_checkout(user, "subscription", "pro", "https://example.com")
        intent = (await accounts.get_user(user["id"]))["checkout_session_id"]
        monkeypatch.setattr(accounts, "update_user", original)
        result = await billing.create_checkout(await accounts.get_user(user["id"]), "subscription", "pro", "https://example.com")
        return intent, result, await accounts.get_user(user["id"])

    intent, result, stored = asyncio.run(run())
    assert intent.startswith(billing.INTENT_PREFIX), "no durable intent survived the failed write"
    assert len(h.created) == 1 and result["session_id"] == "cs_1", "a second session was created"
    assert len(h.calls) == 2 and h.calls[0]["idempotency_key"] == h.calls[1]["idempotency_key"] == intent[len(billing.INTENT_PREFIX):]
    assert h.calls[0] == h.calls[1], "a replay must be the identical request, or Stripe rejects it"
    assert stored["checkout_session_id"] == "cs_1"


def test_completion_of_an_intents_session_clears_the_pending_record(monkeypatch):
    async def run():
        user = await _customer("intent-complete@example.com")
        await accounts.update_user(user["id"], checkout_session_id=billing.INTENT_PREFIX + "k", checkout_started_at=10**10)
        await billing._on_checkout_completed("evt", {"id": "cs_k", "mode": "subscription", "subscription": "sub_k",
                                                     "created": 5000, "metadata": {"user_id": user["id"], "plan_id": "pro"}}, 5000)
        return await accounts.get_user(user["id"])

    stored = asyncio.run(run())
    assert stored["checkout_session_id"] is None and stored["stripe_subscription_id"] == "sub_k" and stored["plan_id"] == "pro"


# --- R08 · a queued turn re-checks its admission under the lease ------------------

def _chat_harness(monkeypatch):
    from app.graph import AgentRun
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "request_queue_timeout_seconds", 3)
    monkeypatch.setattr(execution_policy, "allow_chat_request", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy, "allow_chat_request_from_ip", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy.tool_outcomes, "record_turn", AsyncMock())
    monkeypatch.setattr(execution_policy.research_gaps, "record", AsyncMock())
    monkeypatch.setattr(execution_policy.notifications, "maybe_low_credit_alert", AsyncMock())
    monkeypatch.setattr(execution_policy, "run_agent", AsyncMock(return_value=AgentRun(
        answer="private account answer", trajectory=None, trade_plan=None, intent="general", capabilities=[])))


async def _queued_turn(monkeypatch, sid, caller, message="my private question"):
    """Start a real chat turn and pause it between its admission and its lease."""
    from app.models import ChatRequest
    reached, resume = asyncio.Event(), asyncio.Event()
    original = execution_policy.acquire_session_turn

    async def gate(session_id):
        reached.set()
        await resume.wait()
        return await original(session_id)

    monkeypatch.setattr(execution_policy, "acquire_session_turn", gate)
    chat = asyncio.create_task(execution_policy.execute_chat_turn(ChatRequest(message=message, session_id=sid), caller))
    await reached.wait()
    return chat, resume


def _status(error: ServiceError) -> int:
    return getattr(error, "status_code", None) or getattr(error, "status", None) or error.args[0]


def test_a_queued_turn_is_refused_once_its_conversation_was_deleted_and_claimed_by_another_account(monkeypatch):
    """The verification's interleaving through the real execute_chat_turn,
    inverted: A's turn passes admission; deletion removes A's ownership; B
    claims the id; A's turn resumes. It must fail, and B's conversation must
    not carry A's question or answer."""
    _chat_harness(monkeypatch)

    async def run():
        user, _ = await accounts.get_or_create_user("owner@example.com")
        other, _ = await accounts.get_or_create_user("other@example.com")
        caller = await identity._identity_for_user(user, "local")
        intruder = await identity._identity_for_user(other, "other")
        sid = "p4-queued-chat"
        await accounts.touch_chat_session(user["id"], sid)
        chat, resume = await _queued_turn(monkeypatch, sid, caller)
        await main.delete_my_conversations(caller)
        await session_access.require_session_access(sid, intruder, claim=True)
        resume.set()
        with pytest.raises(ServiceError) as caught:
            await chat
        return _status(caught.value), await accounts.chat_session_owner(sid), await sessions.get_messages(sid), other["id"]

    status, owner, messages, other_id = asyncio.run(run())
    assert status == 404
    assert owner == other_id and messages == [], "A's queued turn wrote into B's conversation"


def test_a_queued_turn_of_a_deleted_account_is_refused(monkeypatch):
    _chat_harness(monkeypatch)

    async def run():
        user, _ = await accounts.get_or_create_user("gone@example.com")
        caller = await identity._identity_for_user(user, "local")
        sid = "p4-deleted-account"
        await accounts.touch_chat_session(user["id"], sid)
        chat, resume = await _queued_turn(monkeypatch, sid, caller)
        await main._delete_conversations_under_lease(user["id"])
        await accounts.delete_user(user["id"])
        resume.set()
        with pytest.raises(ServiceError) as caught:
            await chat
        return _status(caught.value), await sessions.get_messages(sid)

    status, messages = asyncio.run(run())
    assert status == 404 and messages == []


def test_the_same_queued_turn_completes_when_nothing_changed_while_it_waited(monkeypatch):
    """Control for the two refusals: the re-check under the lease is not a
    refusal of queued turns as such."""
    _chat_harness(monkeypatch)

    async def run():
        user, _ = await accounts.get_or_create_user("patient@example.com")
        caller = await identity._identity_for_user(user, "local")
        sid = "p4-queued-ok"
        await accounts.touch_chat_session(user["id"], sid)
        chat, resume = await _queued_turn(monkeypatch, sid, caller)
        resume.set()
        response = await chat
        return response, await accounts.chat_session_owner(sid), await sessions.get_messages(sid), user["id"]

    response, owner, messages, user_id = asyncio.run(run())
    assert response.answer == "private account answer" and owner == user_id
    assert any(m["content"] == "private account answer" for m in messages)


# --- R09 · a delivered occurrence is completed, never refunded ----------------------

def test_a_delivered_occurrence_whose_bookkeeping_fails_is_completed_not_refunded(monkeypatch):
    """The verification failed the completion write after delivery three
    times and saw the delivered brief refunded. The inbox row is the delivery
    record: the recovery finishes the occurrence's bookkeeping instead."""
    monkeypatch.setattr(settings, "task_retry_limit", 3)
    monkeypatch.setattr(settings, "credit_cost_brief", 1)

    async def run():
        user, _ = await accounts.get_or_create_user("delivered@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 1, "seed", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "paid content")))
        original = tasks.update_task
        failed = []

        async def fail_completion(*args, **fields):
            if fields.get("last_result") == "delivered":
                failed.append(fields)
                raise RuntimeError("completion write unavailable")
            return await original(*args, **fields)

        monkeypatch.setattr(tasks, "update_task", fail_completion)
        result = await tasks.run_task(await tasks.get_task(task["id"]))
        stored = await tasks.get_task(task["id"])
        return result, failed, stored, len(await tasks.inbox(user["id"])), await credits.balance(account)

    result, failed, stored, delivered, balance = asyncio.run(run())
    assert len(failed) == 1, "the completion write did not fail as arranged"
    assert result["fired"] and result.get("recovered"), result
    assert delivered == 1 and balance == 0, "delivered work was refunded, or delivered twice"
    assert stored["claimed_occurrence"] is None and stored["retry_count"] == 0 and stored["fire_count"] == 1
    assert stored["status"] == "active" and stored["next_run_at"] is not None


def test_an_undelivered_occurrence_is_still_refunded_when_its_retries_run_out(monkeypatch):
    """Control: the inbox check narrows the refund to what was never
    delivered; it does not remove it."""
    monkeypatch.setattr(settings, "task_retry_limit", 2)
    monkeypatch.setattr(settings, "credit_cost_brief", 1)

    async def run():
        user, _ = await accounts.get_or_create_user("undelivered@example.com")
        account = credits.user_account_id(user["id"])
        await credits.append(account, 1, "seed", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("offline")))
        results = []
        for minutes in (0, 10):
            monkeypatch.setattr(tasks, "_now", lambda m=minutes: datetime.now(timezone.utc) + timedelta(minutes=m))
            results.append(await tasks.run_task(await tasks.get_task(task["id"])))
        return results, await credits.balance(account), len(await tasks.inbox(user["id"]))

    results, balance, delivered = asyncio.run(run())
    assert [r["result"] for r in results] == ["error", "failed"] and balance == 1 and delivered == 0
