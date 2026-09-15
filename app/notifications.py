"""User notifications: the low-credit warning and purchase receipts.

Both are preferences on the account (`low_credit_alert`, `low_credit_threshold`,
`receipts`) and both go out through app/emailer.py, so without an email
provider they are no-ops that never block a chat turn or a webhook.
"""

from __future__ import annotations

import logging

from app import accounts, emailer
from app.credits import month_key
from app.settings import settings

logger = logging.getLogger(__name__)

DEFAULTS = {"low_credit_alert": True, "low_credit_threshold": 20, "receipts": True, "product_updates": False}


def preferences(user: dict) -> dict:
    prefs = user.get("preferences") or {}
    return {key: prefs.get(key, default) for key, default in DEFAULTS.items()}


async def maybe_low_credit_alert(user: dict, balance: int) -> bool:
    """Email once per calendar month when the balance drops to the threshold."""
    prefs = preferences(user)
    if not prefs["low_credit_alert"] or balance > int(prefs["low_credit_threshold"]):
        return False
    marker = (user.get("preferences") or {}).get("low_credit_alerted_month")
    if marker == month_key():
        return False
    sent = await emailer.send_email(
        user["email"],
        f"{settings.product_name}: you're down to {balance} credits",
        f"<p>Your {settings.product_name} balance is <strong>{balance} credits</strong>.</p>"
        f"<p>Your monthly allowance renews automatically; you can also buy a credit pack or upgrade any time from "
        f"<a href=\"{settings.public_base_url}/ui/\">Profile → Billing</a>.</p>",
        text=f"Your {settings.product_name} balance is {balance} credits. Buy a pack or upgrade from Profile -> Billing.",
    )
    if sent:
        await accounts.update_user(user["id"], preferences={"low_credit_alerted_month": month_key()})
    return sent


async def send_receipt(user: dict, description: str, amount_total: int | None, currency: str | None) -> bool:
    if not preferences(user)["receipts"]:
        return False
    amount = f"{(amount_total or 0) / 100:,.2f} {(currency or 'usd').upper()}" if amount_total else "—"
    return await emailer.send_email(
        user["email"],
        f"{settings.product_name} receipt: {description}",
        f"<p>Thanks — <strong>{description}</strong> is on your account.</p><p>Amount: {amount}. "
        f"Stripe also sends its own invoice; both are available under Profile → Billing.</p>",
        text=f"{description} is on your account. Amount: {amount}.",
    )
