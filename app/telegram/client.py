"""The Telegram Bot API, as much of it as the bot needs.

One small client rather than a framework: the bot does not poll, does not keep
a dispatcher and has no state of its own -- an update arrives on the webhook,
a turn runs through app/execution_policy.py, and one or two messages go back.
That is five API methods and a rate limit, and nothing is kept here that no
caller uses -- an untested convenience is a liability, not a head start.

Nothing here raises on a failed send. A message Telegram refused is a message
the user does not see; it is never a reason to fail a turn that has already
been charged, so every call logs and returns None (the emailer contract).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import httpx

from app.settings import settings

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
# Telegram's own ceiling for a message body. Longer answers are split by
# app/telegram/render.py on a paragraph boundary, never mid-word.
MAX_MESSAGE_CHARS = 4096
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def enabled() -> bool:
    return bool(settings.telegram_bot_token)


def webhook_secret() -> str:
    """The path segment and header value, in that order of preference.

    Derived from the bot token when unset, because the alternative -- a
    guessable path -- would let anyone POST fabricated updates to a configured
    deployment. A derived secret is not a substitute for setting one, but it
    fails closed instead of open, and it is stable across restarts so the
    webhook does not need re-claiming.
    """
    if settings.telegram_webhook_secret:
        return settings.telegram_webhook_secret
    token = settings.telegram_bot_token or ""
    if not token:
        # Nothing to derive from: an unconfigured deployment has no webhook
        # secret at all, so the route cannot be satisfied (review of
        # 330bc651: a constant hash of the empty token accepted forged updates).
        return None
    return hashlib.sha256(f"orbit-telegram-webhook:{token}".encode()).hexdigest()[:32]


async def call(method: str, **params: Any) -> dict | None:
    """One Bot API call. None on any failure, including a Telegram-level
    `ok: false` -- the caller has no better recovery than the next message."""
    if not enabled():
        logger.info("telegram not configured; would call %s", method)
        return None
    url = f"{API_ROOT}/bot{settings.telegram_bot_token}/{method}"
    payload = {key: value for key, value in params.items() if value is not None}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        if response.status_code == 429:
            # Telegram says exactly how long to wait; one retry is enough for
            # the bursts this bot produces (a progress edit and a final send).
            retry_after = float((response.json().get("parameters") or {}).get("retry_after") or 1)
            await asyncio.sleep(min(retry_after, 5.0))
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, json=payload)
        body = response.json()
        if not body.get("ok"):
            logger.warning("telegram %s failed: %s %s", method, response.status_code, str(body.get("description"))[:200])
            return None
        return body.get("result") if isinstance(body.get("result"), dict) else {"result": body.get("result")}
    except (httpx.HTTPError, ValueError):
        logger.warning("telegram %s failed", method, exc_info=True)
        return None


async def send_message(chat_id: int | str, text: str, *, reply_markup: dict | None = None,
                       reply_to: int | None = None, thread_id: int | None = None,
                       preview: bool = False) -> dict | None:
    return await call(
        "sendMessage",
        chat_id=chat_id,
        text=text[:MAX_MESSAGE_CHARS],
        parse_mode="HTML",
        # An answer naming five providers would otherwise render five link
        # previews under it and push the answer itself off the screen.
        link_preview_options={"is_disabled": not preview},
        reply_markup=reply_markup,
        reply_to_message_id=reply_to,
        message_thread_id=thread_id,
    )


async def edit_message_text(chat_id: int | str, message_id: int, text: str,
                            *, reply_markup: dict | None = None) -> dict | None:
    return await call(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text[:MAX_MESSAGE_CHARS],
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
        reply_markup=reply_markup,
    )


async def send_chat_action(chat_id: int | str, action: str = "typing", thread_id: int | None = None) -> None:
    await call("sendChatAction", chat_id=chat_id, action=action, message_thread_id=thread_id)


async def answer_callback_query(query_id: str, text: str | None = None, alert: bool = False) -> None:
    """Every callback must be answered or the user's button spins forever."""
    await call("answerCallbackQuery", callback_query_id=query_id, text=text, show_alert=alert)


async def set_webhook(url: str) -> bool:
    result = await call(
        "setWebhook",
        url=url,
        secret_token=webhook_secret(),
        # `message` for chat, `callback_query` for the buttons on an answer.
        # Nothing else is handled, and an update type nobody handles is a
        # delivery that costs a request and produces a log line.
        allowed_updates=["message", "callback_query"],
        # A restart should not replay the backlog that accumulated while the
        # process was down: those answers would arrive out of context, and
        # each one is charged. New questions only.
        drop_pending_updates=True,
    )
    return result is not None


async def get_me() -> dict | None:
    return await call("getMe")
