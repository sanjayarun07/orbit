"""Stripe billing: hosted Checkout for subscriptions and credit packs, the
Customer Portal, and the webhook that turns Stripe events into ledger rows.

Design rules
- Orbit never sees card or wallet data: Checkout and the Portal are Stripe-
  hosted pages; we only create sessions and read webhooks.
- Every webhook is verified with the signing secret and recorded by event id
  before it is acted on, and every credit movement it causes is a ledger row
  keyed by that event id -- a redelivered event changes nothing.
- The Stripe SDK is reached only through the small `_api` shim below so tests
  (and a missing key) can replace it; with no STRIPE_SECRET_KEY the billing
  endpoints answer 503 rather than pretending.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import accounts, credits
from app.billing_plans import PACKS, PLANS, get_pack, get_plan
from app.db import get_pg_pool
from app.settings import settings

logger = logging.getLogger(__name__)

_seen_events: set[str] = set()


class BillingNotConfigured(Exception):
    pass


class WebhookError(Exception):
    pass


# ----------------------------------------------------------------------------
# Stripe SDK shim (monkeypatched in tests)
# ----------------------------------------------------------------------------

def _stripe():
    if not settings.stripe_secret_key:
        raise BillingNotConfigured("Stripe is not configured (STRIPE_SECRET_KEY)")
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _api_create_customer(email: str, user_id: str) -> str:
    customer = _stripe().Customer.create(email=email, metadata={"user_id": user_id})
    return customer["id"]


def _api_create_checkout(params: dict) -> dict:
    session = _stripe().checkout.Session.create(**params)
    return {"id": session["id"], "url": session["url"]}


def _api_create_portal(customer_id: str, return_url: str) -> str:
    session = _stripe().billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session["url"]


def _api_construct_event(payload: bytes, signature: str) -> dict:
    if not settings.stripe_webhook_secret:
        raise BillingNotConfigured("Stripe webhook secret is not configured (STRIPE_WEBHOOK_SECRET)")
    import stripe

    try:
        event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise WebhookError(str(exc) or "Invalid webhook payload") from exc
    # StripeObject is a dict subclass; json round-trip gives plain nested dicts.
    return json.loads(json.dumps(event))


def configured() -> bool:
    return bool(settings.stripe_secret_key)


def payment_method_types() -> list[str]:
    """Cards always; Stripe's `crypto` method (USDC on Base/Ethereum/Solana/
    Polygon) only when the account has it enabled."""
    return ["card", "crypto"] if settings.stripe_crypto_enabled else ["card"]


# ----------------------------------------------------------------------------
# Checkout + Portal
# ----------------------------------------------------------------------------

async def _customer_id(user: dict) -> str:
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    customer_id = _api_create_customer(user["email"], user["id"])
    await accounts.update_user(user["id"], stripe_customer_id=customer_id)
    return customer_id


async def create_checkout(user: dict, kind: str, item_id: str, base_url: str) -> dict:
    """A hosted Checkout URL for a subscription (`kind="subscription"`, a plan
    id) or a one-time credit pack (`kind="pack"`, a pack id)."""
    if not configured():
        raise BillingNotConfigured("Billing is not configured")
    base = base_url.rstrip("/")
    customer_id = await _customer_id(user)
    common = {
        "customer": customer_id,
        "client_reference_id": user["id"],
        "success_url": f"{base}/ui/?billing=success&kind={kind}",
        "cancel_url": f"{base}/ui/?billing=cancel",
        "payment_method_types": payment_method_types(),
        "allow_promotion_codes": True,
    }
    if settings.stripe_tax_enabled:
        common["automatic_tax"] = {"enabled": True}
        common["customer_update"] = {"address": "auto"}
    if kind == "subscription":
        plan = PLANS.get(item_id)
        if plan is None or plan.price_usd_month <= 0:
            raise ValueError("Choose a paid plan")
        price = plan.stripe_price_id()
        if not price:
            raise ValueError(f"The {plan.name} plan has no Stripe price configured")
        params = {
            **common,
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "subscription_data": {"metadata": {"user_id": user["id"], "plan_id": plan.id}},
            "metadata": {"user_id": user["id"], "kind": "subscription", "plan_id": plan.id},
        }
    elif kind == "pack":
        pack = get_pack(item_id)
        if pack is None:
            raise ValueError("Unknown credit pack")
        price = pack.stripe_price_id()
        if not price:
            raise ValueError(f"The {pack.name} pack has no Stripe price configured")
        metadata = {"user_id": user["id"], "kind": "pack", "pack_id": pack.id, "credits": str(pack.credits)}
        params = {
            **common,
            "mode": "payment",
            "line_items": [{"price": price, "quantity": 1}],
            # Propagates to the PaymentIntent and its Charge, so a later
            # charge.refunded event still tells us whose credits to claw back.
            "payment_intent_data": {"metadata": metadata},
            "metadata": metadata,
        }
    else:
        raise ValueError("kind must be 'subscription' or 'pack'")
    session = _api_create_checkout(params)
    return {"url": session["url"], "session_id": session["id"]}


async def create_portal(user: dict, base_url: str) -> str:
    if not configured():
        raise BillingNotConfigured("Billing is not configured")
    if not user.get("stripe_customer_id"):
        raise ValueError("No billing account yet -- subscribe or buy credits first")
    return _api_create_portal(user["stripe_customer_id"], f"{base_url.rstrip('/')}/ui/?billing=portal")


# ----------------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------------

async def record_event(event_id: str, event_type: str) -> bool:
    """True the first time an event id is seen."""
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "INSERT INTO stripe_events (id, type) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING RETURNING id",
            event_id, event_type,
        )
        return row is not None
    if event_id in _seen_events:
        return False
    _seen_events.add(event_id)
    return True


async def _user_for(obj: dict) -> dict | None:
    """Resolve the Orbit user an event object belongs to: metadata first, then
    the Stripe customer id."""
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id") or obj.get("client_reference_id")
    if user_id:
        user = await accounts.get_user(user_id)
        if user:
            return user
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer:
        return await accounts.get_user_by_stripe_customer(customer)
    return None


async def handle_event(event: dict) -> dict:
    """Apply one verified event. Returns what happened (for logs and tests)."""
    event_id, event_type = event["id"], event["type"]
    obj = (event.get("data") or {}).get("object") or {}
    if not await record_event(event_id, event_type):
        return {"event": event_id, "type": event_type, "status": "duplicate"}
    handler = _HANDLERS.get(event_type)
    if handler is None:
        return {"event": event_id, "type": event_type, "status": "ignored"}
    try:
        result = await handler(event_id, obj)
    except Exception:
        logger.exception("stripe webhook %s (%s) failed", event_id, event_type)
        raise
    return {"event": event_id, "type": event_type, **result}


async def _on_checkout_completed(event_id: str, session: dict) -> dict:
    user = await _user_for(session)
    if user is None:
        return {"status": "no_user"}
    metadata = session.get("metadata") or {}
    if session.get("customer") and not user.get("stripe_customer_id"):
        await accounts.update_user(user["id"], stripe_customer_id=session["customer"])
    if session.get("mode") == "payment" or metadata.get("kind") == "pack":
        if session.get("payment_status") not in (None, "paid"):
            return {"status": "unpaid"}
        pack = get_pack(metadata.get("pack_id"))
        amount = int(metadata.get("credits") or (pack.credits if pack else 0))
        if amount <= 0:
            return {"status": "no_credits"}
        applied = await credits.grant(
            credits.user_account_id(user["id"]), amount, f"purchase:{metadata.get('pack_id') or 'pack'}",
            "stripe_event", event_id,
            {"payment_intent": session.get("payment_intent"), "amount_total": session.get("amount_total"), "currency": session.get("currency")},
        )
        return {"status": "credited" if applied else "duplicate", "credits": amount}
    if session.get("mode") == "subscription":
        plan_id = metadata.get("plan_id")
        if plan_id in PLANS:
            await accounts.update_user(
                user["id"], plan_id=plan_id, stripe_subscription_id=session.get("subscription"), subscription_status="active",
            )
        return {"status": "subscribed", "plan_id": plan_id}
    return {"status": "ignored"}


def _plan_from_subscription(subscription: dict) -> str | None:
    metadata = subscription.get("metadata") or {}
    if metadata.get("plan_id") in PLANS:
        return metadata["plan_id"]
    items = ((subscription.get("items") or {}).get("data")) or []
    for item in items:
        price = (item.get("price") or {}).get("id")
        for plan in PLANS.values():
            if price and price == plan.stripe_price_id():
                return plan.id
    return None


async def _on_invoice_paid(event_id: str, invoice: dict) -> dict:
    """The monthly allowance for a paid plan is granted here, per invoice --
    not by the lazy grant used for the Free tier."""
    user = await _user_for(invoice)
    if user is None:
        return {"status": "no_user"}
    lines = ((invoice.get("lines") or {}).get("data")) or []
    plan_id = None
    for line in lines:
        price = (line.get("price") or {}).get("id") or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
        meta = line.get("metadata") or {}
        if meta.get("plan_id") in PLANS:
            plan_id = meta["plan_id"]
            break
        for plan in PLANS.values():
            if price and price == plan.stripe_price_id():
                plan_id = plan.id
                break
        if plan_id:
            break
    plan_id = plan_id or user.get("plan_id")
    plan = get_plan(plan_id)
    if plan.price_usd_month <= 0:
        return {"status": "ignored", "reason": "free plan"}
    subscription = invoice.get("subscription")
    if isinstance(subscription, dict):
        subscription = subscription.get("id")
    await accounts.update_user(
        user["id"], plan_id=plan.id, subscription_status="active",
        **({"stripe_subscription_id": subscription} if subscription else {}),
    )
    applied = await credits.grant(
        credits.user_account_id(user["id"]), plan.monthly_credits, f"subscription:{plan.id}",
        "stripe_event", event_id, {"invoice": invoice.get("id"), "period_end": invoice.get("period_end")},
    )
    return {"status": "credited" if applied else "duplicate", "plan_id": plan.id, "credits": plan.monthly_credits}


async def _on_subscription_updated(event_id: str, subscription: dict) -> dict:
    user = await _user_for(subscription)
    if user is None:
        return {"status": "no_user"}
    status = subscription.get("status")
    plan_id = _plan_from_subscription(subscription)
    fields: dict[str, Any] = {"subscription_status": status, "stripe_subscription_id": subscription.get("id")}
    if status in {"active", "trialing", "past_due"} and plan_id:
        fields["plan_id"] = plan_id
    elif status in {"canceled", "unpaid", "incomplete_expired"}:
        fields["plan_id"] = "free"
    await accounts.update_user(user["id"], **fields)
    return {"status": "updated", "plan_id": fields.get("plan_id", user.get("plan_id")), "subscription_status": status}


async def _on_subscription_deleted(event_id: str, subscription: dict) -> dict:
    user = await _user_for(subscription)
    if user is None:
        return {"status": "no_user"}
    await accounts.update_user(user["id"], plan_id="free", subscription_status="canceled", stripe_subscription_id=None)
    return {"status": "downgraded", "plan_id": "free"}


async def _on_charge_refunded(event_id: str, charge: dict) -> dict:
    """Claw back the credits of a refunded pack, pro rata for partial refunds."""
    metadata = charge.get("metadata") or {}
    if metadata.get("kind") != "pack":
        return {"status": "ignored", "reason": "not a credit pack"}
    user = await _user_for(charge)
    if user is None:
        return {"status": "no_user"}
    total = int(charge.get("amount") or 0)
    refunded = int(charge.get("amount_refunded") or 0)
    bought = int(metadata.get("credits") or 0)
    if total <= 0 or refunded <= 0 or bought <= 0:
        return {"status": "ignored"}
    clawback = bought if refunded >= total else round(bought * refunded / total)
    applied = await credits.append(
        credits.user_account_id(user["id"]), -clawback, f"refund:{metadata.get('pack_id') or 'pack'}",
        "stripe_event", event_id, {"charge": charge.get("id"), "amount_refunded": refunded},
    )
    return {"status": "clawed_back" if applied else "duplicate", "credits": -clawback}


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "invoice.paid": _on_invoice_paid,
    "customer.subscription.updated": _on_subscription_updated,
    "customer.subscription.deleted": _on_subscription_deleted,
    "charge.refunded": _on_charge_refunded,
}
HANDLED_EVENTS = tuple(_HANDLERS)


def public_config() -> dict:
    return {
        "configured": configured(),
        "payment_methods": payment_method_types() if configured() else [],
        "packs": [pack.public() for pack in PACKS.values()],
    }


def reset() -> None:
    _seen_events.clear()
