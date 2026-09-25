"""The HTTP edge: Telegram's webhook, and the web UI's link endpoint.

Two rules shape this file.

**Answer immediately.** Telegram redelivers an update until it receives a 200,
and a deep dive takes over a minute. So the route validates, hands the update
to a tracked background task (drained at shutdown like every other
fire-and-forget in the app) and returns at once. The turn's own answer reaches
the user as a Telegram message, not as this response body.

**Prove it is Telegram.** The URL carries a secret path segment and the
request must echo the same value in `X-Telegram-Bot-Api-Secret-Token`. Without
both, anyone who learned the path could post fabricated updates that spend a
user's credits and write into their conversation.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app import execution_policy
from app.identity import Identity, require_browser_session
from app.settings import settings
from app.telegram import bot, client, identity as tg_identity

logger = logging.getLogger(__name__)

router = APIRouter()



def webhook_url() -> str:
    return f"{settings.public_base_url.rstrip('/')}/telegram/webhook/{client.webhook_secret()}"


@router.post("/telegram/webhook/{secret}", include_in_schema=False)
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = client.webhook_secret()
    if not client.enabled() or not expected:
        # An unconfigured integration has no inbound side either: no account
        # creation, no turn, for anyone who posts here.
        raise HTTPException(404, "Not found")
    # compare_digest on both halves: the path is what an attacker would have
    # to guess, the header is what only Telegram can send back.
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(404, "Not found")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(403, "Bad secret token")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(400, "Malformed update")
    if not isinstance(update, dict):
        raise HTTPException(400, "Malformed update")
    execution_policy.background(bot.handle_update(update))
    return {"ok": True}


@router.post("/me/telegram/link")
async def create_telegram_link(identity: Identity = Depends(require_browser_session)):
    """Mint the one-time `t.me/<bot>?start=<token>` link shown in Profile.

    Requires a browser session, not an API key: this hands out a credential
    that binds a messaging account to this Anvaya account, which is a sign-in
    act and belongs to the person in front of the browser.
    """
    if not client.enabled():
        raise HTTPException(503, "Telegram is not configured for this deployment")
    username = await bot.bot_username()
    if not username:
        raise HTTPException(503, "Telegram bot is unreachable")
    token = await tg_identity.start_link(identity.user["id"])
    return {
        "url": f"https://t.me/{username}?start={token}",
        "expires_in": settings.telegram_link_ttl_seconds,
    }


@router.get("/auth/telegram/claim/preview")
async def preview_telegram_link(token: str, identity: Identity = Depends(require_browser_session)):
    """What a `?tglink=` would connect, before anything is connected: the
    Telegram account's name, and a confirmation nonce bound to this browser
    session and this token. The page shows the name and a Connect button;
    nothing is attached until the person clicks it."""
    token = (token or "").strip()
    payload = await tg_identity.peek_link_token(token) if token else None
    if not payload or payload.get("kind") != "telegram" or not payload.get("id"):
        raise HTTPException(400, "That link has expired or was already used. Send /link in Telegram for a new one.")
    tg_user = {"id": payload["id"], "username": payload.get("username"), "first_name": payload.get("first_name"), "last_name": payload.get("last_name")}
    nonce = await tg_identity.start_claim_confirmation(token, identity.user["id"])
    return {"display": tg_identity.display_name(tg_user) or f"Telegram user {payload['id']}", "confirmation": nonce,
            "account": identity.user.get("email") or "this account"}


@router.post("/auth/telegram/claim")
async def claim_telegram_link(body: dict, identity: Identity = Depends(require_browser_session)):
    """Finish a link the user started in Telegram (`/link` -> `?tglink=`).

    Three proofs meet here and none is sufficient alone: the token proves the
    holder started this from a particular Telegram account, the browser
    session proves which Anvaya account they just signed into, and the
    confirmation nonce proves this person previewed the connection in this
    session and chose to make it. A link a stranger sent therefore attaches
    nothing on its own (review of 330bc651).
    """
    token = str((body or {}).get("token") or "").strip()
    confirmation = str((body or {}).get("confirmation") or "").strip()
    if not token:
        raise HTTPException(400, "Missing token")
    if not confirmation:
        raise HTTPException(400, "Confirm the connection first")
    bound = await tg_identity.take_claim_confirmation(confirmation)
    if not bound or bound.get("token") != token or bound.get("user_id") != identity.user["id"]:
        raise HTTPException(400, "That confirmation does not match this link or this session. Open the link again and confirm.")
    payload = await tg_identity.consume_link_token(token)
    if not payload or payload.get("kind") != "telegram" or not payload.get("id"):
        raise HTTPException(400, "That link has expired or was already used. Send /link in Telegram for a new one.")
    tg_user = {"id": payload["id"], "username": payload.get("username"),
               "first_name": payload.get("first_name"), "last_name": payload.get("last_name")}
    account = await tg_identity.link_to_account(tg_user, identity.user["id"])
    if account is None:
        raise HTTPException(404, "Account not found")
    # Tell them in the place they started, so the loop visibly closes.
    await client.send_message(
        payload["id"],
        "✅ <b>Connected.</b> This chat is now on your Anvaya account — same credits, "
        "plan, wallets and risk charter. Alerts and briefs will arrive here.\n\n"
        "Send /account any time to check where you stand.",
    )
    return {"linked": True, "display": tg_identity.display_name(tg_user)}


@router.get("/me/telegram")
async def my_telegram(identity: Identity = Depends(require_browser_session)):
    from app import accounts

    linked = [item for item in await accounts.list_identities(identity.user["id"])
              if item["provider"] == tg_identity.PROVIDER]
    return {"configured": client.enabled(), "linked": linked}


@router.delete("/me/telegram/{external_id}")
async def unlink_telegram(external_id: str, identity: Identity = Depends(require_browser_session)):
    from app import accounts

    owner = await accounts.find_identity_owner(tg_identity.PROVIDER, external_id)
    # Ownership, not merely knowing the id: a Telegram user id is public.
    if owner is None or owner["id"] != identity.user["id"]:
        raise HTTPException(404, "Not linked to this account")
    await accounts.unlink_identity(tg_identity.PROVIDER, external_id)
    return {"unlinked": True}


async def claim_webhook() -> None:
    """Point the bot at this deployment, once, at startup."""
    if not client.enabled() or not settings.telegram_set_webhook_on_start:
        return
    if settings.public_base_url.startswith("http://localhost"):
        # Telegram will not call a localhost URL; say so rather than letting a
        # developer wonder why the bot is silent.
        logger.warning("telegram: PUBLIC_BASE_URL is localhost; set the webhook manually or use a tunnel")
        return
    if await client.set_webhook(webhook_url()):
        logger.info("telegram: webhook claimed at %s/telegram/webhook/***", settings.public_base_url.rstrip("/"))
    else:
        logger.warning("telegram: could not claim the webhook")
