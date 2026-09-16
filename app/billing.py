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

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app import accounts, credits, notifications
from app.billing_plans import PACKS, PLANS, get_pack, get_plan
from app.db import get_pg_pool
from app.settings import settings

logger = logging.getLogger(__name__)

_seen_events: set[str] = set()          # received (in-memory store)
_processed_events: set[str] = set()     # handled to completion; a redelivery is then a duplicate


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


def _api_list_active_subscriptions(customer_id: str) -> list[dict]:
    """Every subscription Stripe still considers live for this customer."""
    found: list[dict] = []
    for status in ("active", "trialing", "past_due"):
        page = _stripe().Subscription.list(customer=customer_id, status=status, limit=100)
        found.extend({"id": item["id"], "status": item["status"]} for item in page.get("data", []))
    return found


def _api_cancel_subscription(subscription_id: str) -> dict:
    subscription = _stripe().Subscription.cancel(subscription_id)
    return {"id": subscription["id"], "status": subscription["status"]}


def _api_create_portal(customer_id: str, return_url: str) -> str:
    session = _stripe().billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session["url"]


def _api_list_invoices(customer_id: str, limit: int = 12) -> list[dict]:
    invoices = _stripe().Invoice.list(customer=customer_id, limit=limit)
    return [json.loads(json.dumps(item)) for item in invoices.get("data", [])]


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
        # One live subscription per account. Checkout always creates a NEW
        # subscription, so a subscribed user choosing another plan used to end
        # up paying for two, while the app stored -- and on deletion cancelled
        # -- only one id. Plan changes go through the billing portal, which
        # updates the subscription Stripe already has.
        # "Not known to be over" rather than "known to be live": a record can
        # carry a subscription id with no status at all (rows from before the
        # status column, or a webhook that never arrived), and starting a
        # second subscription on top of an unknown one is the worse mistake.
        if user.get("stripe_subscription_id") and user.get("subscription_status") not in TERMINAL_STATUSES:
            raise SubscriptionExists(
                "This account already has a subscription. Change plans from the billing portal instead of starting another."
            )
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
            # So pack purchases show up in the invoices list too.
            "invoice_creation": {"enabled": True},
            # Propagates to the PaymentIntent and its Charge, so a later
            # charge.refunded event still tells us whose credits to claw back.
            "payment_intent_data": {"metadata": metadata},
            "metadata": metadata,
        }
    else:
        raise ValueError("kind must be 'subscription' or 'pack'")
    session = _api_create_checkout(params)
    return {"url": session["url"], "session_id": session["id"]}


class SubscriptionCancelFailed(Exception):
    """Stripe would not confirm the cancellation; the caller must not proceed."""


class SubscriptionExists(Exception):
    """The account already has a live subscription; plan changes go through the portal."""


LIVE_STATUSES = {"active", "trialing", "past_due"}


async def cancel_subscription_for(user: dict) -> dict:
    """Cancel this account's subscription before its billing link is destroyed.

    Deleting the account first would strip the Stripe customer and subscription
    ids while the subscription kept renewing, leaving a charge nobody at Orbit
    could map back to a person. Raises rather than swallowing a failure: it is
    recoverable (the operator retries, or cancels in Stripe), and deleting
    anyway is not.
    """
    subscription_id = user.get("stripe_subscription_id")
    customer_id = user.get("stripe_customer_id")
    if not subscription_id and not customer_id:
        return {"status": "none"}
    if not configured():
        # No Stripe credentials here, but the account claims a subscription --
        # refuse rather than silently orphan a live one.
        if subscription_id and user.get("subscription_status") in LIVE_STATUSES:
            raise SubscriptionCancelFailed(
                "This account has a subscription but Stripe is not configured, so it cannot be cancelled."
            )
        return {"status": "none"}
    # Cancel every live subscription Stripe has for the customer, not only the
    # one id the app remembered: an overlapping subscription the app never
    # recorded would otherwise keep charging after the account was gone.
    targets: list[str] = []
    if customer_id:
        try:
            targets = [item["id"] for item in await asyncio.to_thread(_api_list_active_subscriptions, customer_id)]
        except Exception as exc:
            raise SubscriptionCancelFailed(
                "Stripe could not list this account's subscriptions, so the account was not deleted. Try again shortly."
            ) from exc
    if subscription_id and subscription_id not in targets and user.get("subscription_status") in LIVE_STATUSES:
        targets.append(subscription_id)
    cancelled: list[str] = []
    for target in targets:
        try:
            result = await asyncio.to_thread(_api_cancel_subscription, target)
            cancelled.append(result["id"])
        except BillingNotConfigured:
            raise
        except Exception as exc:
            if "No such subscription" in str(exc) or "resource_missing" in str(exc):
                continue
            raise SubscriptionCancelFailed(
                "A subscription could not be cancelled, so the account was not deleted. Try again shortly."
            ) from exc
    return {"status": "cancelled" if cancelled else "none", "subscriptions": cancelled}


async def list_invoices(user: dict) -> list[dict]:
    """The customer's recent invoices (subscriptions and, via invoice_creation,
    credit packs) with Stripe's hosted page and PDF links."""
    if not configured():
        raise BillingNotConfigured("Billing is not configured")
    if not user.get("stripe_customer_id"):
        return []
    out = []
    for invoice in _api_list_invoices(user["stripe_customer_id"]):
        out.append({
            "id": invoice.get("id"), "number": invoice.get("number"), "status": invoice.get("status"),
            "amount_paid": invoice.get("amount_paid"), "amount_due": invoice.get("amount_due"), "currency": invoice.get("currency"),
            "created": invoice.get("created"), "description": _invoice_description(invoice),
            "hosted_invoice_url": invoice.get("hosted_invoice_url"), "invoice_pdf": invoice.get("invoice_pdf"),
        })
    return out


def _invoice_description(invoice: dict) -> str:
    lines = ((invoice.get("lines") or {}).get("data")) or []
    names = [str(line.get("description") or "") for line in lines if line.get("description")]
    return "; ".join(names)[:160] or "Orbit"


async def create_portal(user: dict, base_url: str) -> str:
    if not configured():
        raise BillingNotConfigured("Billing is not configured")
    if not user.get("stripe_customer_id"):
        raise ValueError("No billing account yet -- subscribe or buy credits first")
    return _api_create_portal(user["stripe_customer_id"], f"{base_url.rstrip('/')}/ui/?billing=portal")


# ----------------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------------

_recent_events: list[dict] = []


def _api_retrieve_event(event_id: str) -> dict:
    return json.loads(json.dumps(_stripe().Event.retrieve(event_id)))


async def list_events(limit: int = 50) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT id, type, received_at FROM stripe_events ORDER BY received_at DESC LIMIT $1", max(1, min(int(limit), 500)))
        return [{"id": r["id"], "type": r["type"], "received_at": r["received_at"].isoformat()} for r in rows]
    return list(reversed(_recent_events))[: max(1, min(int(limit), 500))]


async def replay_event(event_id: str) -> dict:
    """Fetch an event from Stripe and run its handler again. Safe because every
    credit effect is keyed by the event id (a replay can't double-credit); it
    exists for the case where a handler failed after the event was recorded."""
    if not configured():
        raise BillingNotConfigured("Billing is not configured")
    event = _api_retrieve_event(event_id)
    handler = _HANDLERS.get(event.get("type"))
    if handler is None:
        return {"event": event_id, "type": event.get("type"), "status": "ignored"}
    obj = (event.get("data") or {}).get("object") or {}
    result = await handler(event_id, obj, event.get("created"))
    return {"event": event_id, "type": event.get("type"), "replayed": True, **result}


async def record_event(event_id: str, event_type: str) -> bool:
    """True when the event still needs handling: the first delivery, or a
    redelivery of one whose handler failed (received but never marked
    processed). A processed event is a duplicate."""
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            """
            INSERT INTO stripe_events (id, type) VALUES ($1, $2)
            ON CONFLICT (id) DO UPDATE SET type = EXCLUDED.type WHERE stripe_events.processed_at IS NULL
            RETURNING id
            """,
            event_id, event_type,
        )
        return row is not None
    if event_id in _processed_events:
        return False
    if event_id not in _seen_events:
        _seen_events.add(event_id)
        _recent_events.append({"id": event_id, "type": event_type, "received_at": datetime.now(timezone.utc).isoformat()})
        del _recent_events[:-500]
    return True


async def mark_event_processed(event_id: str) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute("UPDATE stripe_events SET processed_at = NOW() WHERE id = $1", event_id)
        return
    _processed_events.add(event_id)


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
        await mark_event_processed(event_id)
        return {"event": event_id, "type": event_type, "status": "ignored"}
    try:
        result = await handler(event_id, obj, event.get("created"))
    except Exception:
        # Left unprocessed: Stripe's redelivery (or a manual replay) runs the
        # handler again; every credit effect is keyed by event id, so a
        # partially applied event cannot double-credit.
        logger.exception("stripe webhook %s (%s) failed", event_id, event_type)
        raise
    await mark_event_processed(event_id)
    return {"event": event_id, "type": event_type, **result}


async def _on_checkout_completed(event_id: str, session: dict, event_created: int | None = None) -> dict:
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
        if applied:
            await notifications.send_receipt(user, f"{amount:,} credits ({pack.name if pack else 'credit pack'})", session.get("amount_total"), session.get("currency"))
        return {"status": "credited" if applied else "duplicate", "credits": amount}
    if session.get("mode") == "subscription":
        plan_id = metadata.get("plan_id")
        if plan_id in PLANS:
            # The same ordering rule as every other subscription-state writer.
            # A redelivered or late checkout.session.completed for a
            # subscription the customer has since replaced must not make it
            # current again -- and this handler used to write unconditionally.
            superseded = _superseded_subscription(
                user, {"id": session.get("subscription"), "created": session.get("created")}, event_created)
            if superseded:
                return {"status": "ignored", "reason": superseded,
                        "active_subscription": user.get("stripe_subscription_id"),
                        "event_subscription": session.get("subscription")}
            # `created` here is the checkout session's, which is the moment this
            # subscription came into being. It is the ordering key every later
            # subscription event is compared against.
            fields = {"plan_id": plan_id, "stripe_subscription_id": session.get("subscription"),
                      "subscription_status": "active", **_event_stamp(event_created)}
            if session.get("created") is not None:
                fields["subscription_created"] = int(session["created"])
            await accounts.update_user(user["id"], **fields)
        return {"status": "subscribed", "plan_id": plan_id}
    return {"status": "ignored"}


def _plan_from_subscription(subscription: dict) -> str | None:
    """The plan this subscription is actually being charged for.

    Price first, metadata only as a fallback. Metadata is written once at
    checkout and never updated, so after an upgrade or downgrade it still names
    the plan the customer used to be on -- trusting it first meant a
    Max-priced subscription kept Pro entitlements indefinitely. The price on
    the subscription's items is what Stripe bills, so it is what we grant.
    """
    items = ((subscription.get("items") or {}).get("data")) or []
    for item in items:
        price = (item.get("price") or {}).get("id")
        for plan in PLANS.values():
            if price and price == plan.stripe_price_id():
                return plan.id
    metadata = subscription.get("metadata") or {}
    if metadata.get("plan_id") in PLANS:
        return metadata["plan_id"]
    return None


async def _on_invoice_paid(event_id: str, invoice: dict, event_created: int | None = None) -> dict:
    """The monthly allowance for a paid plan is granted here, per invoice --
    not by the lazy grant used for the Free tier."""
    user = await _user_for(invoice)
    if user is None:
        return {"status": "no_user"}
    lines = ((invoice.get("lines") or {}).get("data")) or []
    plan_id = _plan_from_lines(lines)
    subscription = invoice.get("subscription")
    if not subscription:
        # API versions from 2025-03-31 (Basil) moved this under `parent`.
        subscription = ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    if isinstance(subscription, dict):
        subscription = subscription.get("id")
    # Only a subscription invoice whose line matches a plan price grants the
    # monthly allowance. A credit-pack invoice (one-off Checkout with
    # invoice_creation) has no plan line and no subscription: its credits were
    # granted by checkout.session.completed, never here.
    if not plan_id or not subscription:
        return {"status": "ignored", "reason": "not a subscription invoice"}
    plan = get_plan(plan_id)
    if plan.price_usd_month <= 0:
        return {"status": "ignored", "reason": "free plan"}
    # Two different facts, handled separately. The payment happened, so the
    # credits it bought are granted regardless -- keyed by event id, so a
    # replay cannot double-grant. Which plan the account is ON is a separate
    # question, and an invoice never answers it: invoices renew subscriptions,
    # they do not establish which one is current. So the entitlement is
    # written only when the invoice belongs to the subscription the account is
    # already on (or the account has none yet). An invoice for a subscription
    # the customer has moved on from credits the payment and changes nothing
    # else; before this it silently made that old subscription current again.
    current = user.get("stripe_subscription_id")
    entitlement_written = False
    stale = _superseded_subscription(user, {"id": subscription, "created": invoice.get("created")}, event_created)
    if (not current or subscription == current) and not stale:
        await accounts.update_user(
            user["id"], plan_id=plan.id, subscription_status="active", stripe_subscription_id=subscription,
            **_event_stamp(event_created),
        )
        entitlement_written = True
    applied = await credits.grant(
        credits.user_account_id(user["id"]), plan.monthly_credits, f"subscription:{plan.id}",
        "stripe_event", event_id, {"invoice": invoice.get("id"), "period_end": invoice.get("period_end")},
    )
    return {"status": "credited" if applied else "duplicate", "plan_id": plan.id, "credits": plan.monthly_credits,
            "entitlement": "updated" if entitlement_written else "unchanged",
            **({} if entitlement_written else {"active_subscription": current, "invoice_subscription": subscription})}


TERMINAL_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def _superseded_subscription(user: dict, subscription: dict, event_created: int | None = None) -> str | None:
    """Why this event must not change the account's entitlement.

    One rule for every subscription-state writer -- update, delete, checkout
    and invoice alike. Two orderings matter and they need different keys:

    * A *different* subscription than the one recorded applies only when it is
      demonstrably newer by its own `created`. That is what makes an upgrade or
      a resubscribe work, and what stops a late event for a replaced
      subscription putting it back.
    * The *same* subscription is ordered by the event envelope's `created`,
      recorded as `subscription_event_at` whenever a writer applies. A
      subscription's own creation time cannot tell an old state from a new one
      -- which is exactly how a late update, invoice or checkout for a
      subscription that had been cancelled restored paid access. Cancellation
      is therefore recorded, not erased: the id and timestamps stay, the
      status becomes terminal, and only an event demonstrably newer than the
      cancellation may move it again (Stripe does reactivate before period
      end). With no envelope time to compare, an event for a cancelled
      subscription is stale.
    """
    event_subscription = subscription.get("id")
    current = user.get("stripe_subscription_id")
    if not event_subscription or not current:
        return None
    if event_subscription != current:
        sub_created = subscription.get("created")
        current_created = user.get("subscription_created")
        if sub_created is not None and current_created is not None:
            return "a newer subscription is active" if int(sub_created) < int(current_created) else None
        return "a different subscription is active"
    last_applied = user.get("subscription_event_at")
    if event_created is not None and last_applied is not None and int(event_created) < int(last_applied):
        return "an older event for the current subscription"
    if user.get("subscription_status") in TERMINAL_STATUSES:
        if event_created is None or last_applied is None or int(event_created) <= int(last_applied):
            return "the subscription was cancelled; only a newer event may change it"
    return None


def _event_stamp(event_created: int | None) -> dict:
    return {"subscription_event_at": int(event_created)} if event_created is not None else {}


def _plan_from_lines(lines: list[dict]) -> str | None:
    """The plan an invoice's lines are charging for. Price first, metadata as a
    fallback, the same order as _plan_from_subscription -- this loop used to be
    metadata-first, so a Max-priced invoice carrying stale Pro metadata restored
    Pro entitlements and granted the wrong allowance."""
    for line in lines:
        price = (line.get("price") or {}).get("id") or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
        for plan in PLANS.values():
            if price and price == plan.stripe_price_id():
                return plan.id
    for line in lines:
        meta = line.get("metadata") or {}
        if meta.get("plan_id") in PLANS:
            return meta["plan_id"]
    return None


async def _on_subscription_updated(event_id: str, subscription: dict, event_created: int | None = None) -> dict:
    user = await _user_for(subscription)
    if user is None:
        return {"status": "no_user"}
    superseded = _superseded_subscription(user, subscription, event_created)
    if superseded:
        return {"status": "ignored", "reason": superseded,
                "active_subscription": user.get("stripe_subscription_id"),
                "event_subscription": subscription.get("id")}
    status = subscription.get("status")
    plan_id = _plan_from_subscription(subscription)
    fields: dict[str, Any] = {"subscription_status": status, "stripe_subscription_id": subscription.get("id"),
                              **_event_stamp(event_created)}
    if subscription.get("created") is not None:
        fields["subscription_created"] = int(subscription["created"])
    if status in {"active", "trialing", "past_due"} and plan_id:
        fields["plan_id"] = plan_id
    elif status in {"canceled", "unpaid", "incomplete_expired"}:
        fields["plan_id"] = "free"
    await accounts.update_user(user["id"], **fields)
    return {"status": "updated", "plan_id": fields.get("plan_id", user.get("plan_id")), "subscription_status": status}


async def _on_subscription_deleted(event_id: str, subscription: dict, event_created: int | None = None) -> dict:
    user = await _user_for(subscription)
    if user is None:
        return {"status": "no_user"}
    superseded = _superseded_subscription(user, subscription, event_created)
    if superseded:
        return {"status": "ignored", "reason": superseded,
                "active_subscription": user.get("stripe_subscription_id"),
                "event_subscription": subscription.get("id")}
    # Keep the id and its timestamps: clearing them meant a late event for this
    # very subscription found "no current subscription" and restored paid
    # access. The terminal status is what says it is over.
    fields = {"plan_id": "free", "subscription_status": "canceled", **_event_stamp(event_created)}
    if not user.get("stripe_subscription_id") and subscription.get("id"):
        fields["stripe_subscription_id"] = subscription["id"]
    await accounts.update_user(user["id"], **fields)
    return {"status": "downgraded", "plan_id": "free"}


async def _on_charge_refunded(event_id: str, charge: dict, event_created: int | None = None) -> dict:
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
    # `amount_refunded` is cumulative across a charge's refunds, so the target
    # clawback is too; only the part not yet taken by earlier refund events is
    # deducted now (25% then 50% of a 100-credit pack costs 25 + 25, not 25 + 50).
    account_id = credits.user_account_id(user["id"])
    target = bought if refunded >= total else round(bought * refunded / total)
    deducted, total_clawed = await credits.claw_back(
        account_id, str(charge.get("id")), target, f"refund:{metadata.get('pack_id') or 'pack'}",
        "stripe_event", event_id, {"charge": charge.get("id"), "amount_refunded": refunded},
    )
    if deducted <= 0:
        return {"status": "duplicate", "credits": 0, "clawed_back_total": total_clawed}
    return {"status": "clawed_back", "credits": -deducted, "clawed_back_total": total_clawed}


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
    _processed_events.clear()
    _recent_events.clear()
