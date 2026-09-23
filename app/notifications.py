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


# ---------------------------------------------------------------------------
# Telegram delivery
#
# The second real delivery channel for tasks. Email reaches an inbox nobody
# opens; a Telegram message reaches the person. The contract is deliberately
# identical to email's (see app/tasks.py): the inbox row is the delivery of
# record, this is best-effort on top of it, and a failure is recorded rather
# than retried -- a retry cannot tell a lost message from a late one, and a
# duplicate price alert at 3am is the worse outcome.
# ---------------------------------------------------------------------------

async def telegram_chat_id(user: dict) -> str | None:
    """The chat to deliver to, or None when this account has no Telegram.

    A Telegram private chat's id IS the user's id, so the identity row's
    `external_id` addresses the DM directly. The bot never delivers a task
    into a group: the task belongs to one account, and its holdings,
    thresholds and reminders are not the group's business.
    """
    linked = [item for item in await accounts.list_identities(user["id"]) if item["provider"] == "telegram"]
    return linked[0]["external_id"] if linked else None


async def send_telegram(user: dict, title: str, body: str) -> bool:
    """One task delivery. False (never raises) when Telegram is unconfigured,
    the account has no linked chat, or the send was refused."""
    from app.telegram import client, render

    if not client.enabled():
        return False
    chat_id = await telegram_chat_id(user)
    if not chat_id:
        return False
    text = f"<b>{_escape(title)}</b>\n\n{render.to_html(body)}"
    chunks = render.split_markdown(body) or [body]
    if len(chunks) > 1:
        # A brief can exceed one message. The title rides the first chunk and
        # the rest follow, rather than the body being silently truncated.
        sent = await client.send_message(chat_id, f"<b>{_escape(title)}</b>\n\n{render.to_html(chunks[0])}")
        for chunk in chunks[1:]:
            await client.send_message(chat_id, render.to_html(chunk))
        return sent is not None
    return await client.send_message(chat_id, text) is not None


def _escape(text: str) -> str:
    import html

    return html.escape(text or "")
