"""Follow-ups from the verification of 8cbbd1bc: the eight findings that were
partially closed, as regression tests. Each is the verification's own
reproduction with its assertion inverted, plus a control."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import base58
import pytest
from fastapi.testclient import TestClient

from app import accounts, api_keys, billing, credits, execution, identity, main, plans, sessions, tasks
from app.settings import settings
from tests.conftest import sign_in


# --- R02 · moving funds is a browser act ------------------------------------

def _custodial_plan(monkeypatch, user):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from app.models import SwapProposal, TradePlan

    signer = Keypair()
    now = datetime.now(timezone.utc)
    plan = TradePlan(plan_id="custodial-key", status="pending_confirmation", created_at=now, expires_at=now + timedelta(minutes=5),
                     wallet_address=str(signer.pubkey()),
                     proposal=SwapProposal(input_mint="in", output_mint="out", amount_atomic=1, slippage_bps=10, reason="t"),
                     quote={}, input_token={"mint": "in", "name": "In", "symbol": "IN", "decimals": 9},
                     output_token={"mint": "out", "name": "Out", "symbol": "OUT", "decimals": 6},
                     simulation={"ok": True}, confirmation_text="CONFIRM custodial-key", owner_account_id="user:" + user["id"])
    message = MessageV0.try_compile(signer.pubkey(), [], [], Hash.default())
    tx = base64.b64encode(bytes(VersionedTransaction(message, [signer]))).decode()
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "allow_custodial_signing", True)
    monkeypatch.setattr(settings, "solana_private_key", base58.b58encode(bytes(signer)).decode())
    monkeypatch.setattr(settings, "custodial_signing_principals", "user:" + user["id"])
    monkeypatch.setattr(plans, "get_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(execution, "get_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(execution, "get_prepared_transaction", AsyncMock(return_value=tx))
    monkeypatch.setattr(execution, "simulate_transaction", AsyncMock(return_value={"ok": True}))
    signed = []

    async def capture(p, t, encoded):
        signed.append(t)
        return {"status": "submitted", "signature": str(t.signatures[0])}

    monkeypatch.setattr(execution, "_broadcast_reviewed", capture)
    return plan, signed


def test_an_allowlisted_accounts_data_key_cannot_have_the_server_sign(monkeypatch):
    """Execution enabled, owner allowlisted, so no other guard can hide the
    missing identity-type check."""
    browser = TestClient(main.app)
    user = sign_in(browser, plan_id="pro")["user"]
    plan, signed = _custodial_plan(monkeypatch, user)
    _, secret = asyncio.run(api_keys.create(user["id"], "read", ["data"]))
    key = TestClient(main.app)
    key.headers["Authorization"] = "Bearer " + secret
    response = key.post("/trade-plans/custodial-key/confirm", json={"confirmation_text": plan.confirmation_text})
    assert response.status_code == 403, response.text
    assert signed == [], "an API key had the server sign"
    # The same plan is still executable by the authorized browser.
    assert browser.post("/trade-plans/custodial-key/confirm", json={"confirmation_text": plan.confirmation_text}).status_code == 200
    assert len(signed) == 1


def test_every_execution_route_refuses_an_api_key_regardless_of_scope():
    browser = TestClient(main.app)
    sign_in(browser, plan_id="pro")
    created = browser.post("/me/api-keys", json={"name": "k", "scopes": ["chat", "data", "mcp"]})
    key = TestClient(main.app)
    key.headers["Authorization"] = "Bearer " + created.json()["secret"]
    for path, body in [("/trade-plans/x/confirm", {"confirmation_text": "CONFIRM x"}),
                       ("/trade-plans/x/wallet-transaction", {"confirmation_text": "CONFIRM x"}),
                       ("/trade-plans/x/submit-wallet-transaction", {"confirmation_text": "CONFIRM x", "signed_transaction": "AA=="})]:
        assert key.post(path, json=body).status_code == 403, path


# --- R04 · entitlement writes are atomic and ties are deterministic ----------

def test_a_slower_older_event_cannot_overwrite_a_newer_one(monkeypatch):
    """The verification's interleaving, inverted: the older event is held
    just before its write while the newer one completes."""

    async def run():
        user, _ = await accounts.get_or_create_user("order@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_order", stripe_subscription_id="sub_a", subscription_created=100,
                                   subscription_event_at=200, subscription_status="active", plan_id="pro")
        original = accounts.update_user
        old_waiting, new_done = asyncio.Event(), asyncio.Event()

        async def update(uid, db=None, **fields):
            if fields.get("subscription_event_at") == 300:
                old_waiting.set()
                try:
                    await asyncio.wait_for(new_done.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            out = await original(uid, db=db, **fields)
            if fields.get("subscription_event_at") == 400:
                new_done.set()
            return out

        monkeypatch.setattr(accounts, "update_user", update)
        base = {"id": "sub_a", "created": 100, "status": "active", "customer": "cus_order"}
        old = asyncio.create_task(billing._on_subscription_updated("old", {**base, "metadata": {"plan_id": "pro"}}, 300))
        await asyncio.sleep(0.05)
        await billing._on_subscription_updated("new", {**base, "metadata": {"plan_id": "max"}}, 400)
        await old
        return await accounts.get_user(user["id"])

    current = asyncio.run(run())
    assert current["plan_id"] == "max" and current["subscription_event_at"] == 400, current


def test_same_second_updates_do_not_depend_on_delivery_order():
    async def run():
        user, _ = await accounts.get_or_create_user("tie@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_tie", stripe_subscription_id="sub_a",
                                   subscription_created=100, subscription_status="active", plan_id="pro")
        for plan in ["max", "pro"]:
            await billing._on_subscription_updated("evt_" + plan, {"id": "sub_a", "created": 100, "status": "active",
                                                                   "customer": "cus_tie", "metadata": {"plan_id": plan}}, 300)
        first_order = (await accounts.get_user(user["id"]))["plan_id"]
        user2, _ = await accounts.get_or_create_user("tie2@example.com")
        await accounts.update_user(user2["id"], stripe_customer_id="cus_tie2", stripe_subscription_id="sub_a",
                                   subscription_created=100, subscription_status="active", plan_id="pro")
        for plan in ["pro", "max"]:
            await billing._on_subscription_updated("evt2_" + plan, {"id": "sub_a", "created": 100, "status": "active",
                                                                    "customer": "cus_tie2", "metadata": {"plan_id": plan}}, 300)
        return first_order, (await accounts.get_user(user2["id"]))["plan_id"]

    first, second = asyncio.run(run())
    # The rule is "first applied wins": the outcome is a function of arrival
    # order in both cases, but it is deterministic and never last-wins.
    assert first == "max" and second == "pro"


# --- R05 · one checkout in flight; unknown state fails closed ----------------

def test_a_second_subscription_checkout_is_refused_while_one_is_pending(monkeypatch):
    async def run():
        user, _ = await accounts.get_or_create_user("checkout@example.com")
        user = await accounts.update_user(user["id"], stripe_customer_id="cus_x")
        monkeypatch.setattr(billing, "configured", lambda: True)
        monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
        monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [])
        calls = []
        monkeypatch.setattr(billing, "_api_create_checkout", lambda params: calls.append(params) or {"id": f"cs_{len(calls)}", "url": "https://example.com/c"})
        first = await billing.create_checkout(user, "subscription", "pro", "https://example.com")
        with pytest.raises(billing.CheckoutPending):
            await billing.create_checkout(await accounts.get_user(user["id"]), "subscription", "pro", "https://example.com")
        return first, calls, await accounts.get_user(user["id"])

    first, calls, stored = asyncio.run(run())
    assert len(calls) == 1 and stored["checkout_session_id"] == first["session_id"]


def test_a_live_subscription_stripe_knows_about_blocks_checkout_even_before_its_webhook(monkeypatch):
    async def run():
        user, _ = await accounts.get_or_create_user("reconcile@example.com")
        user = await accounts.update_user(user["id"], stripe_customer_id="cus_r")
        monkeypatch.setattr(billing, "configured", lambda: True)
        monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
        monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [{"id": "sub_unseen", "status": "active"}])
        monkeypatch.setattr(billing, "_api_create_checkout", lambda params: pytest.fail("a checkout was created over a live subscription"))
        with pytest.raises(billing.SubscriptionExists):
            await billing.create_checkout(user, "subscription", "pro", "https://example.com")
        return await accounts.get_user(user["id"])

    stored = asyncio.run(run())
    assert stored["stripe_subscription_id"] == "sub_unseen"


def test_a_completed_checkout_clears_the_pending_record_so_the_next_one_is_allowed(monkeypatch):
    async def run():
        user, _ = await accounts.get_or_create_user("clears@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_c", checkout_session_id="cs_1", checkout_started_at=10**10)
        await billing._on_checkout_completed("evt", {"id": "cs_1", "mode": "subscription", "subscription": "sub_1", "customer": "cus_c",
                                                     "created": 5000, "metadata": {"plan_id": "pro"}}, 5000)
        return await accounts.get_user(user["id"])

    stored = asyncio.run(run())
    assert stored["checkout_session_id"] is None and stored["stripe_subscription_id"] == "sub_1"


@pytest.mark.parametrize("configured", [False, True])
def test_an_unknown_status_subscription_is_never_silently_discarded(monkeypatch, configured):
    """Cancellation is at least as cautious as checkout: not known to be over
    means it must be cancelled, or the deletion must not proceed."""
    monkeypatch.setattr(billing, "configured", lambda: configured)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_harness" if configured else None)
    cancelled = []
    monkeypatch.setattr(billing, "_api_cancel_subscription", lambda sid: cancelled.append(sid) or {"id": sid, "status": "canceled"})
    user = {"id": "u", "stripe_subscription_id": "sub_existing", "subscription_status": None}
    if configured:
        result = asyncio.run(billing.cancel_subscription_for(user))
        assert cancelled == ["sub_existing"] and result["status"] == "cancelled"
    else:
        with pytest.raises(billing.SubscriptionCancelFailed):
            asyncio.run(billing.cancel_subscription_for(user))


# --- R08 · deletion is one transition, under the lease -----------------------

def test_a_turn_starting_during_bulk_deletion_cannot_leave_private_history_unowned(monkeypatch):
    """The verification committed a turn between the lease release and the
    ownership removal. Ownership now goes with the transcript, inside the
    lease, so a turn that starts during deletion waits and finds an empty,
    unowned conversation -- never messages without an owner."""
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "request_queue_timeout_seconds", 2)

    async def run():
        user, _ = await accounts.get_or_create_user("bulk@example.com")
        sid = "bulk-race"
        await accounts.touch_chat_session(user["id"], sid)
        await sessions.append_turn(sid, "user", "old private")
        original_clear = sessions.clear_history
        turn_result = {}

        async def racing_turn():
            lease = await sessions.acquire_session_turn(sid)
            try:
                turn_result["owner_when_acquired"] = await accounts.chat_session_owner(sid)
                turn_result["messages_when_acquired"] = await sessions.get_messages(sid)
            finally:
                await lease.release()

        async def clear_then_race(session_id):
            await original_clear(session_id)
            # The competing turn starts NOW, while deletion still holds the lease.
            turn_result["task"] = asyncio.create_task(racing_turn())
            await asyncio.sleep(0.05)

        monkeypatch.setattr(main, "clear_history", clear_then_race)
        result = await main.delete_my_conversations(identity.Identity("user", "user:" + user["id"], "local", user=user))
        await turn_result["task"]
        return result, turn_result, await accounts.chat_session_owner(sid), await sessions.get_messages(sid)

    result, turn, owner_after, messages_after = asyncio.run(run())
    assert result["deleted"] == 1
    assert turn["owner_when_acquired"] is None and turn["messages_when_acquired"] == [], turn
    assert not (messages_after and owner_after is None), "messages exist on an unowned conversation"


def test_account_deletion_waits_for_or_refuses_busy_conversations(monkeypatch):
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "request_queue_timeout_seconds", 1)
    client = TestClient(main.app)
    me = sign_in(client, email="busy-delete@example.com")["user"]

    async def hold():
        await accounts.touch_chat_session(me["id"], "held-during-delete")
        return await sessions.acquire_session_turn("held-during-delete")

    lease = asyncio.run(hold())
    try:
        response = client.request("DELETE", "/me", json={"confirm_email": "busy-delete@example.com"})
        assert response.status_code == 409, response.text
        assert client.get("/me").json()["authenticated"] is True
    finally:
        asyncio.run(lease.release())


# --- R09 · a retry keeps its payment; bounded retries refund --------------------

def test_a_retry_after_a_failed_composition_delivers_exactly_once_for_exactly_one_charge(monkeypatch):
    async def run():
        user, _ = await accounts.get_or_create_user("refund@example.com")
        account = credits.user_account_id(user["id"])
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        await credits.append(account, 1, "seed", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("provider offline")))
        first = await tasks.run_task(task)
        after_failure = await credits.balance(account)
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "paid content")))
        monkeypatch.setattr(tasks, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=10))
        second = await tasks.run_task(await tasks.get_task(task["id"]))
        return first, after_failure, second, await credits.balance(account), len(await tasks.inbox(user["id"]))

    first, after_failure, second, final, delivered = asyncio.run(run())
    assert first["result"] == "error" and after_failure == 0, "the failed attempt refunded, so the retry would be free"
    assert second["fired"] and delivered == 1 and final == 0, "delivered without a net charge, or charged twice"


def test_repeated_failures_are_bounded_and_the_occurrence_is_refunded(monkeypatch):
    monkeypatch.setattr(settings, "task_retry_limit", 3)

    async def run():
        user, _ = await accounts.get_or_create_user("bounded@example.com")
        account = credits.user_account_id(user["id"])
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        await credits.append(account, 1, "seed", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("provider offline")))
        results = []
        for minutes in (0, 10, 20):
            monkeypatch.setattr(tasks, "_now", lambda m=minutes: datetime.now(timezone.utc) + timedelta(minutes=m))
            results.append(await tasks.run_task(await tasks.get_task(task["id"])))
        return results, await credits.balance(account), await tasks.get_task(task["id"])

    results, balance, stored = asyncio.run(run())
    assert [r["result"] for r in results] == ["error", "error", "failed"]
    assert balance == 1, "the exhausted occurrence was not refunded"
    assert stored["claimed_occurrence"] is None and stored["retry_count"] == 0
    assert stored["status"] == "active" and stored["next_run_at"] is not None


def test_a_refunded_occurrences_credit_spent_elsewhere_is_not_available_to_the_next_occurrence(monkeypatch):
    monkeypatch.setattr(settings, "task_retry_limit", 1)

    async def run():
        user, _ = await accounts.get_or_create_user("spent-elsewhere@example.com")
        account = credits.user_account_id(user["id"])
        monkeypatch.setattr(settings, "credit_cost_brief", 1)
        await credits.append(account, 1, "seed", "test", "seed")
        task = await tasks.create_task(user, "brief", {}, {"every_minutes": 60})
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(side_effect=RuntimeError("offline")))
        await tasks.run_task(task)                                  # fails, refunded (limit 1)
        assert await credits.balance(account) == 1
        await credits.append(account, -1, "chat", "turn", "elsewhere")   # spent on something else
        monkeypatch.setattr(tasks, "evaluate", AsyncMock(return_value=(True, "delivered", "body")))
        monkeypatch.setattr(tasks, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=10))
        result = await tasks.run_task(await tasks.get_task(task["id"]))
        return result, await credits.balance(account), len(await tasks.inbox(user["id"]))

    result, balance, delivered = asyncio.run(run())
    assert "out of credits" in str(result["result"]) and balance == 0 and delivered == 0


# --- R12 · the fingerprint names the model ------------------------------------

def test_a_model_change_within_a_provider_reindexes(monkeypatch):
    from app.knowledge import ingest
    from app.knowledge.models import NormalizedDocument
    from app.knowledge.store import MemoryStore

    async def run():
        store = MemoryStore()
        doc = lambda: NormalizedDocument("docs", "protocol_docs", "https://example.com/a", "protocol:a", "A", "Some information worth embedding.", "same")
        monkeypatch.setattr(ingest, "apply_facts", AsyncMock(return_value=0))
        monkeypatch.setattr(ingest, "get_embedder", lambda: SimpleNamespace(name="openai", model="text-embedding-3-small", dim=256, embed=lambda t: [[1.0, 0.0] for _ in t]))
        await ingest.ingest_document(doc(), store)
        monkeypatch.setattr(ingest, "get_embedder", lambda: SimpleNamespace(name="openai", model="text-embedding-3-large", dim=256, embed=lambda t: [[0.0, 1.0] for _ in t]))
        result = await ingest.ingest_document(doc(), store)
        return result, {tuple(c.embedding) for c in store.chunks.values()}

    result, vectors = asyncio.run(run())
    assert result[0] is True and vectors == {(0.0, 1.0)}, (result, vectors)
