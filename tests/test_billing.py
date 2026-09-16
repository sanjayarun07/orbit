"""Stripe billing against fixtures: Checkout/Portal session parameters, and the
webhook path end to end -- real signature verification through the Stripe SDK
on locally signed payloads, idempotent ledger effects per event id, plan
changes from subscription events, and pro-rata clawback on refunds."""
import asyncio
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import accounts, billing, credits, main
from app.billing_plans import FREE, PRO
from app.settings import settings
from tests.conftest import sign_in

WEBHOOK_SECRET = "whsec_testfixture_secret"


@pytest.fixture(autouse=True)
def stripe_fixture_env(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fixture")
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
    monkeypatch.setattr(settings, "stripe_price_max", "price_max")
    monkeypatch.setattr(settings, "stripe_price_pack_500", "price_pack_500")
    monkeypatch.setattr(settings, "public_base_url", "https://orbit.example.com")

    async def no_pool():
        return None

    monkeypatch.setattr(billing, "get_pg_pool", no_pool)
    billing.reset()
    yield
    billing.reset()


@pytest.fixture
def stripe_api(monkeypatch):
    """The SDK shim: records what Orbit would send to Stripe."""
    calls = {"customers": [], "checkouts": [], "portals": []}

    def create_customer(email, user_id):
        calls["customers"].append((email, user_id))
        return f"cus_{len(calls['customers'])}"

    def create_checkout(params):
        calls["checkouts"].append(params)
        return {"id": f"cs_{len(calls['checkouts'])}", "url": "https://checkout.stripe.com/c/pay/cs_test"}

    def create_portal(customer_id, return_url):
        calls["portals"].append((customer_id, return_url))
        return "https://billing.stripe.com/p/session/test"

    monkeypatch.setattr(billing, "_api_create_customer", create_customer)
    monkeypatch.setattr(billing, "_api_create_checkout", create_checkout)
    monkeypatch.setattr(billing, "_api_create_portal", create_portal)
    return calls


def signed(payload: dict, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> tuple[bytes, str]:
    """Sign a payload the way Stripe does (t=..., v1=HMAC-SHA256 of 't.payload')."""
    body = json.dumps(payload).encode()
    ts = timestamp or int(time.time())
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={digest}"


def event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "object": "event", "api_version": "2024-06-20", "type": event_type,
            "created": int(time.time()), "livemode": False, "data": {"object": obj}}


def post_event(client, ev: dict, **kwargs):
    body, signature = signed(ev, **kwargs)
    return client.post("/billing/webhook", content=body, headers={"stripe-signature": signature, "content-type": "application/json"})


# ----------------------------------------------------------------------------
# Checkout / portal
# ----------------------------------------------------------------------------

def test_checkout_for_a_plan_creates_a_customer_once_and_a_subscription_session(stripe_api):
    client = TestClient(main.app)
    sign_in(client, email="buyer@example.com")
    first = client.post("/billing/checkout", json={"kind": "subscription", "item_id": "pro"})
    assert first.status_code == 200 and first.json()["url"].startswith("https://checkout.stripe.com/")
    params = stripe_api["checkouts"][0]
    assert params["mode"] == "subscription" and params["line_items"] == [{"price": "price_pro", "quantity": 1}]
    assert params["customer"] == "cus_1" and params["payment_method_types"] == ["card"]
    assert params["metadata"]["plan_id"] == "pro" and params["subscription_data"]["metadata"]["plan_id"] == "pro"
    assert params["success_url"] == "https://orbit.example.com/ui/?billing=success&kind=subscription"
    # Second checkout reuses the stored Stripe customer.
    client.post("/billing/checkout", json={"kind": "pack", "item_id": "pack_500"})
    assert len(stripe_api["customers"]) == 1 and stripe_api["checkouts"][1]["customer"] == "cus_1"
    pack = stripe_api["checkouts"][1]
    assert pack["mode"] == "payment" and pack["payment_intent_data"]["metadata"]["credits"] == "500"
    assert client.get("/me").json()["billing"]["has_billing_account"] is True


def test_checkout_validates_items_and_configuration(stripe_api, monkeypatch):
    client = TestClient(main.app)
    sign_in(client)
    assert client.post("/billing/checkout", json={"kind": "subscription", "item_id": "free"}).status_code == 400
    assert client.post("/billing/checkout", json={"kind": "pack", "item_id": "pack_2000"}).status_code == 400  # no price configured
    assert client.post("/billing/checkout", json={"kind": "pack", "item_id": "nope"}).status_code == 400
    assert TestClient(main.app).post("/billing/checkout", json={"kind": "pack", "item_id": "pack_500"}).status_code == 401
    monkeypatch.setattr(settings, "stripe_crypto_enabled", True)
    client.post("/billing/checkout", json={"kind": "pack", "item_id": "pack_500"})
    assert stripe_api["checkouts"][-1]["payment_method_types"] == ["card", "crypto"]
    monkeypatch.setattr(settings, "stripe_secret_key", None)
    assert client.post("/billing/checkout", json={"kind": "pack", "item_id": "pack_500"}).status_code == 503
    assert client.get("/billing/plans").json()["configured"] is False


def test_portal_needs_a_billing_account(stripe_api):
    client = TestClient(main.app)
    sign_in(client)
    assert client.post("/billing/portal").status_code == 400
    client.post("/billing/checkout", json={"kind": "subscription", "item_id": "pro"})
    portal = client.post("/billing/portal")
    assert portal.status_code == 200 and portal.json()["url"].startswith("https://billing.stripe.com/")
    assert stripe_api["portals"][0] == ("cus_1", "https://orbit.example.com/ui/?billing=portal")


# ----------------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------------

def test_webhook_rejects_bad_signatures_and_stale_timestamps():
    client = TestClient(main.app)
    ev = event("evt_bad", "invoice.paid", {})
    assert post_event(client, ev, secret="whsec_wrong").status_code == 400
    assert post_event(client, ev, timestamp=int(time.time()) - 3600).status_code == 400  # outside tolerance
    assert client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": ""}).status_code == 400


def test_pack_purchase_credits_once_even_when_redelivered():
    client = TestClient(main.app)
    me = sign_in(client, email="buyer@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_1", "object": "checkout.session", "mode": "payment", "payment_status": "paid",
               "customer": "cus_9", "client_reference_id": user_id, "payment_intent": "pi_1", "amount_total": 600, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}}
    first = post_event(client, event("evt_1", "checkout.session.completed", session))
    assert first.status_code == 200 and first.json()["status"] == "credited" and first.json()["credits"] == 500
    again = post_event(client, event("evt_1", "checkout.session.completed", session))
    assert again.json()["status"] == "duplicate"
    assert client.get("/me").json()["credits"]["balance"] == FREE.monthly_credits + 500
    assert client.get("/me").json()["billing"]["has_billing_account"] is True  # customer id captured from the session
    ledger = client.get("/me/credits").json()["ledger"]
    assert any(row["ref_type"] == "stripe_event" and row["ref_id"] == "evt_1" and row["delta"] == 500 for row in ledger)


def test_subscription_lifecycle_sets_plan_and_grants_allowance_per_invoice():
    client = TestClient(main.app)
    me = sign_in(client, email="sub@example.com")
    user_id = me["user"]["id"]
    checkout = {"id": "cs_2", "object": "checkout.session", "mode": "subscription", "customer": "cus_2",
                "subscription": "sub_1", "client_reference_id": user_id,
                "metadata": {"user_id": user_id, "kind": "subscription", "plan_id": "pro"}}
    assert post_event(client, event("evt_2", "checkout.session.completed", checkout)).json()["status"] == "subscribed"
    assert client.get("/me").json()["plan"]["id"] == "pro"
    # The lazy Free grant already happened this month; Pro's allowance must come from the invoice, exactly once.
    invoice = {"id": "in_1", "object": "invoice", "customer": "cus_2", "subscription": "sub_1", "period_end": 1_800_000_000,
               "lines": {"data": [{"price": {"id": "price_pro"}, "metadata": {}}]}}
    paid = post_event(client, event("evt_3", "invoice.paid", invoice)).json()
    assert paid["status"] == "credited" and paid["credits"] == PRO.monthly_credits
    assert post_event(client, event("evt_3", "invoice.paid", invoice)).json()["status"] == "duplicate"
    balance = client.get("/me").json()["credits"]["balance"]
    assert balance == FREE.monthly_credits + PRO.monthly_credits
    # With a live subscription the lazy grant no longer fires for the paid plan.
    assert client.get("/me").json()["credits"]["balance"] == balance
    # Upgrade via the Portal: subscription.updated with the Max price.
    updated = {"id": "sub_1", "object": "subscription", "customer": "cus_2", "status": "active",
               "metadata": {"user_id": user_id}, "items": {"data": [{"price": {"id": "price_max"}}]}}
    assert post_event(client, event("evt_4", "customer.subscription.updated", updated)).json()["plan_id"] == "max"
    assert client.get("/me").json()["plan"]["id"] == "max"
    # Cancellation downgrades to Free; credits already granted stay.
    deleted = {"id": "sub_1", "object": "subscription", "customer": "cus_2", "status": "canceled", "metadata": {"user_id": user_id}}
    assert post_event(client, event("evt_5", "customer.subscription.deleted", deleted)).json()["plan_id"] == "free"
    me = client.get("/me").json()
    assert me["plan"]["id"] == "free" and me["credits"]["balance"] == balance and me["billing"]["subscription_status"] == "canceled"


def test_refund_claws_back_pack_credits_pro_rata():
    client = TestClient(main.app)
    me = sign_in(client, email="refund@example.com")
    user_id = me["user"]["id"]
    metadata = {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}
    session = {"id": "cs_3", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_3",
               "client_reference_id": user_id, "payment_intent": "pi_3", "metadata": metadata}
    post_event(client, event("evt_6", "checkout.session.completed", session))
    charge = {"id": "ch_3", "object": "charge", "customer": "cus_3", "payment_intent": "pi_3", "amount": 600, "amount_refunded": 300,
              "metadata": metadata}
    partial = post_event(client, event("evt_7", "charge.refunded", charge)).json()
    assert partial["status"] == "clawed_back" and partial["credits"] == -250
    # amount_refunded is cumulative: the full refund takes only what the partial one left.
    full = post_event(client, event("evt_8", "charge.refunded", {**charge, "amount_refunded": 600})).json()
    assert full["credits"] == -250 and full["clawed_back_total"] == 500
    assert client.get("/me").json()["credits"]["balance"] == FREE.monthly_credits + 500 - 500
    # A refund on something that isn't a credit pack is ignored.
    other = post_event(client, event("evt_9", "charge.refunded", {"id": "ch_9", "amount": 100, "amount_refunded": 100, "metadata": {}})).json()
    assert other["status"] == "ignored"


def test_events_for_unknown_users_and_unhandled_types_are_acknowledged():
    client = TestClient(main.app)
    ghost = {"id": "in_x", "object": "invoice", "customer": "cus_unknown", "lines": {"data": []}}
    assert post_event(client, event("evt_10", "invoice.paid", ghost)).json()["status"] == "no_user"
    assert post_event(client, event("evt_11", "payment_intent.created", {})).json()["status"] == "ignored"
    assert client.get("/health").json()["counters"].get("stripe_ignored", 0) >= 1


def test_a_credit_pack_invoice_grants_no_monthly_allowance():
    client = TestClient(main.app)
    me = sign_in(client, email="packer@example.com")
    user_id = me["user"]["id"]
    # A Pro subscriber (plan set directly, as a Checkout would) buys a 500-credit pack.
    asyncio.run(accounts.update_user(user_id, plan_id="pro"))
    session = {"id": "cs_p", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_p",
               "client_reference_id": user_id, "payment_intent": "pi_p", "amount_total": 600, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}}
    credited = post_event(client, event("evt_p1", "checkout.session.completed", session)).json()
    assert credited.get("credits") == 500, credited
    before = client.get("/me").json()["credits"]["balance"]
    # Stripe also emails an invoice for the one-off payment: no subscription, no plan line.
    invoice = {"id": "in_p", "object": "invoice", "customer": "cus_p", "subscription": None, "period_end": 1_800_000_000,
               "lines": {"data": [{"price": {"id": "price_pack_500"}, "metadata": {}}]}}
    paid = post_event(client, event("evt_p2", "invoice.paid", invoice)).json()
    assert paid["status"] == "ignored" and client.get("/me").json()["credits"]["balance"] == before


def test_basil_invoices_carry_the_subscription_under_parent():
    client = TestClient(main.app)
    me = sign_in(client, email="basil@example.com")
    user_id = me["user"]["id"]
    checkout = {"id": "cs_b", "object": "checkout.session", "mode": "subscription", "customer": "cus_b", "subscription": "sub_b",
                "client_reference_id": user_id, "metadata": {"user_id": user_id, "kind": "subscription", "plan_id": "pro"}}
    assert post_event(client, event("evt_b1", "checkout.session.completed", checkout)).json()["status"] == "subscribed"
    # API version 2025-03-31: no top-level `subscription`; the line's price sits under `pricing`.
    invoice = {"id": "in_b", "object": "invoice", "customer": "cus_b", "period_end": 1_800_000_000,
               "parent": {"type": "subscription_details", "subscription_details": {"subscription": "sub_b"}},
               "lines": {"data": [{"pricing": {"price_details": {"price": "price_pro"}}, "metadata": {}}]}}
    paid = post_event(client, event("evt_b2", "invoice.paid", invoice)).json()
    assert paid["status"] == "credited" and paid["credits"] == PRO.monthly_credits
    assert client.get("/me").json()["billing"]["subscription_id" if "subscription_id" in client.get("/me").json()["billing"] else "has_billing_account"]


def test_a_failed_webhook_is_retried_on_redelivery(monkeypatch):
    client = TestClient(main.app)
    me = sign_in(client, email="retry@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_r", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_r",
               "client_reference_id": user_id, "payment_intent": "pi_r", "amount_total": 600, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}}
    real_grant = credits.grant

    async def flaky(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(credits, "grant", flaky)
    failed = post_event(client, event("evt_r", "checkout.session.completed", session))
    assert failed.status_code == 500          # Stripe will redeliver
    monkeypatch.setattr(credits, "grant", real_grant)
    retried = post_event(client, event("evt_r", "checkout.session.completed", session)).json()
    assert retried["status"] == "credited" and retried["credits"] == 500
    assert post_event(client, event("evt_r", "checkout.session.completed", session)).json()["status"] == "duplicate"
    assert client.get("/me").json()["credits"]["balance"] == FREE.monthly_credits + 500


def test_successive_partial_refunds_deduct_only_their_increment():
    client = TestClient(main.app)
    me = sign_in(client, email="partial@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_q", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_q",
               "client_reference_id": user_id, "payment_intent": "pi_q", "amount_total": 1000, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "100"}}
    assert post_event(client, event("evt_q0", "checkout.session.completed", session)).json()["credits"] == 100
    base = client.get("/me").json()["credits"]["balance"]
    charge = {"id": "ch_q", "object": "charge", "customer": "cus_q", "amount": 1000, "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "100"}}
    first = post_event(client, event("evt_q1", "charge.refunded", {**charge, "amount_refunded": 250})).json()
    assert first["status"] == "clawed_back" and first["credits"] == -25
    second = post_event(client, event("evt_q2", "charge.refunded", {**charge, "amount_refunded": 500})).json()
    assert second["status"] == "clawed_back" and second["credits"] == -25 and second["clawed_back_total"] == 50
    assert client.get("/me").json()["credits"]["balance"] == base - 50
    # A redelivered or unchanged cumulative amount deducts nothing more.
    assert post_event(client, event("evt_q3", "charge.refunded", {**charge, "amount_refunded": 500})).json()["credits"] == 0
    full = post_event(client, event("evt_q4", "charge.refunded", {**charge, "amount_refunded": 1000})).json()
    assert full["credits"] == -50 and client.get("/me").json()["credits"]["balance"] == base - 100


def test_overlapping_refund_events_never_over_deduct(monkeypatch):
    client = TestClient(main.app)
    me = sign_in(client, email="overlap@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_o", "object": "checkout.session", "mode": "payment", "payment_status": "paid", "customer": "cus_o",
               "client_reference_id": user_id, "payment_intent": "pi_o", "amount_total": 1000, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "100"}}
    assert post_event(client, event("evt_o0", "checkout.session.completed", session)).json()["credits"] == 100
    base = client.get("/me").json()["credits"]["balance"]
    # Open a window between reading the running total and appending the
    # increment (on Postgres those are two round trips): without a per-charge
    # lock both events read zero and deduct 25 + 50 instead of 25 + 25.
    real_append = credits.append

    async def slow_append(*args, **kwargs):
        await asyncio.sleep(0.02)
        return await real_append(*args, **kwargs)

    monkeypatch.setattr(credits, "append", slow_append)
    charge = {"id": "ch_o", "object": "charge", "customer": "cus_o", "amount": 1000, "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "100"}}

    async def overlap():
        return await asyncio.gather(billing._on_charge_refunded("evt_o1", {**charge, "amount_refunded": 250}),
                                    billing._on_charge_refunded("evt_o2", {**charge, "amount_refunded": 500}))

    results = asyncio.run(overlap())
    assert sum(-r["credits"] for r in results) == 50 and max(r["clawed_back_total"] for r in results) == 50
    assert asyncio.run(credits.balance(credits.user_account_id(user_id))) == base - 50
