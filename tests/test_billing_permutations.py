"""Billing events in every order.

The review's closure checklist asks for billing sequences run as permutations
rather than as the handful of orderings anyone thought of. A replacement
subscription's four events -- the old one's deletion and the new one's
checkout, update and invoice -- are applied in every one of the 24 orders, and
in every order the account must end on the new subscription with the plan the
new subscription's price buys. Both entitlement and the ledger are asserted.
"""
import asyncio
import itertools

import pytest

from app import accounts, billing, credits
from app.settings import settings

OLD, NEW = "sub_old", "sub_new"
# Envelope times: the old subscription's deletion was issued after the new one
# was created, as happens when a customer upgrades by replacement.
EVENTS = {
    "checkout_new": lambda cust: ("_on_checkout_completed", {"mode": "subscription", "subscription": NEW, "customer": cust,
                                                              "created": 2000, "metadata": {"plan_id": "max"}}, 2000),
    "update_new": lambda cust: ("_on_subscription_updated", {"id": NEW, "customer": cust, "status": "active", "created": 2000,
                                                             "items": {"data": [{"price": {"id": "price_max_perm"}}]}}, 2100),
    "invoice_new": lambda cust: ("_on_invoice_paid", {"id": "in_new", "customer": cust, "subscription": NEW, "created": 2200,
                                                      "lines": {"data": [{"price": {"id": "price_max_perm"}}]}}, 2200),
    "delete_old": lambda cust: ("_on_subscription_deleted", {"id": OLD, "customer": cust, "created": 1000}, 2050),
}


@pytest.mark.parametrize("order", list(itertools.permutations(EVENTS)), ids=lambda o: ">".join(o))
def test_every_order_of_a_replacement_ends_on_the_new_subscription(order, monkeypatch):
    monkeypatch.setattr(settings, "stripe_price_max", "price_max_perm")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_perm")
    customer = "cus_perm_" + "_".join(o[:3] for o in order)

    async def run():
        user, _ = await accounts.get_or_create_user(f"{customer}@example.com")
        await accounts.update_user(user["id"], stripe_customer_id=customer, stripe_subscription_id=OLD,
                                   subscription_created=1000, subscription_status="active", plan_id="pro",
                                   subscription_event_at=1500)
        for index, name in enumerate(order):
            handler_name, obj, event_created = EVENTS[name](customer)
            await getattr(billing, handler_name)(f"evt_{name}_{index}", obj, event_created)
        final = await accounts.get_user(user["id"])
        ledger = await credits.history(credits.user_account_id(user["id"]), limit=50)
        return final, ledger

    final, ledger = asyncio.run(run())
    assert final["stripe_subscription_id"] == NEW, f"{'>'.join(order)}: ended on {final['stripe_subscription_id']}"
    assert final["plan_id"] == "max", f"{'>'.join(order)}: plan {final['plan_id']}"
    assert final["subscription_status"] == "active", f"{'>'.join(order)}: status {final['subscription_status']}"
    # The invoice was paid exactly once, whatever the order, so it credited exactly once.
    invoice_grants = [row for row in ledger if row.get("ref_type") == "stripe_event" and "evt_invoice_new" in str(row.get("ref_id"))]
    assert len(invoice_grants) == 1, f"{'>'.join(order)}: invoice credited {len(invoice_grants)} times"


@pytest.mark.parametrize("order", list(itertools.permutations(["update_old_late", "invoice_old_late", "checkout_old_late"])),
                         ids=lambda o: ">".join(o))
def test_every_order_of_late_events_for_a_cancelled_subscription_leaves_it_cancelled(order):
    """Cancellation followed by every ordering of late events for that same
    subscription, all issued before the cancellation."""
    customer = "cus_late_" + "_".join(o[:4] for o in order)

    async def run():
        user, _ = await accounts.get_or_create_user(f"{customer}@example.com")
        await accounts.update_user(user["id"], stripe_customer_id=customer, stripe_subscription_id=OLD,
                                   subscription_created=1000, subscription_status="active", plan_id="pro")
        await billing._on_subscription_deleted("evt_cancel", {"id": OLD, "customer": customer, "created": 1000}, 5000)
        late = {
            "update_old_late": ("_on_subscription_updated", {"id": OLD, "customer": customer, "status": "active", "created": 1000,
                                                             "metadata": {"plan_id": "pro"}}, 4000),
            "invoice_old_late": ("_on_invoice_paid", {"id": "in_late", "customer": customer, "subscription": OLD, "created": 3900,
                                                      "lines": {"data": [{"metadata": {"plan_id": "pro"}}]}}, 4100),
            "checkout_old_late": ("_on_checkout_completed", {"mode": "subscription", "subscription": OLD, "customer": customer,
                                                             "created": 900, "metadata": {"plan_id": "pro"}}, 3800),
        }
        for index, name in enumerate(order):
            handler_name, obj, event_created = late[name]
            await getattr(billing, handler_name)(f"evt_{name}_{index}", obj, event_created)
        return await accounts.get_user(user["id"])

    final = asyncio.run(run())
    assert final["plan_id"] == "free" and final["subscription_status"] == "canceled", \
        f"{'>'.join(order)}: {final['plan_id']}/{final['subscription_status']}"
