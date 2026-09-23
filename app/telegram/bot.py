"""One Telegram update in, one conversation turn out.

The bot owns no agent logic. Everything topical goes to
`app.execution_policy.execute_chat_turn` -- the same call POST /chat and the
MCP server make, with the same admission, per-session lock, budgets,
validation, credits and persistence. What lives here is the four things a
transport is allowed to decide: who is calling (app/telegram/identity.py),
which conversation this is, when a group message is addressed to us, and how
an AgentResponse looks in a chat (app/telegram/render.py).

Commands are a thin layer over the same turn wherever one will do: /desk and
/charter send the phrases the general node already understands, rather than
growing a second, drifting vocabulary for the same controls.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import OrderedDict

from app import execution_policy, sessions, session_access, task_scheduling
from app.identity import Identity
from app.models import ChatRequest, QuickAction
from app.service_errors import ServiceError
from app.settings import settings
from app.telegram import client, identity as tg_identity, render
from app.telegram.progress import Progress

logger = logging.getLogger(__name__)

# ChatRequest caps a message at 2000 characters; Telegram allows 4096. A
# longer message is truncated rather than refused -- the tail of a pasted
# article is not usually the question.
MAX_INBOUND_CHARS = 2000

# Telegram re-delivers an update until it gets a 200, and a slow turn makes
# that likely. Each turn is charged, so a replay must not run twice.
_seen_updates: "OrderedDict[int, bool]" = OrderedDict()
_SEEN_LIMIT = 2048

_BOT_USERNAME: str | None = None
_ADDRESS = re.compile(r"^(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$")

HELP = (
    "<b>Orbit</b> — a Web3 copilot with live on-chain evidence.\n\n"
    "Just ask, in your own words:\n"
    "• <i>deep dive on BONK</i>\n"
    "• <i>is 0x… safe?</i> — paste any contract address\n"
    "• <i>what's in vitalik.eth?</i>\n"
    "• <i>what if SOL drops 20%?</i>\n"
    "• <i>swap 2 SOL for USDC</i> — quoted here, signed in your wallet\n"
    "• <i>alert me when SOL drops below 180</i> — I message you here\n\n"
    "<b>Commands</b>\n"
    "/new — start a fresh conversation\n"
    "/wallet &lt;address&gt; — follow a public wallet, read-only\n"
    "/desk on|off — the multi-agent trading desk\n"
    "/charter — show the risk rules in force\n"
    "/tasks — your alerts, reminders and briefs\n"
    "/account — your plan, credits and whether you are connected\n"
    "/link — use your existing Orbit account\n"
    "/app — open Orbit\n"
    "/unlink — detach this Telegram account\n\n"
    "<i>Orbit never asks for a private key or seed phrase, and nothing here can sign.</i>"
)

# Said on first contact and again when credits run out -- the two moments a
# user who ALREADY has an Orbit account would otherwise quietly build a second
# one. There is no automatic mapping between a Telegram id and a web account
# (Telegram knows neither your email nor your wallet), so the only way one
# account stays one account is to say so before the fork happens.
LINK_HINT = (
    "Already use Orbit on the web? Send /link to use the same account — "
    "same plan, credits, wallets and risk charter."
)


def remember_update(update_id: int | None) -> bool:
    """True the first time an update id is seen, False on a redelivery."""
    if update_id is None:
        return True
    if update_id in _seen_updates:
        return False
    _seen_updates[update_id] = True
    while len(_seen_updates) > _SEEN_LIMIT:
        _seen_updates.popitem(last=False)
    return True


async def bot_username() -> str | None:
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        me = await client.get_me()
        _BOT_USERNAME = (me or {}).get("username")
    return _BOT_USERNAME


def app_url(session_id: str) -> str:
    """The browser hand-off for this conversation.

    The same URL shape the MCP server hands an agent host: the conversation
    opens in the UI with its pending quote live, where the user's own wallet
    signs it. Always reached with a plain `url` button, never `web_app` --
    Telegram's webview has its own cookie jar (the user would arrive signed
    out) and no browser-extension wallets at all.
    """
    return f"{settings.public_base_url.rstrip('/')}/ui/?session={session_id}"


async def handle_update(update: dict) -> None:
    """Entry point. Never raises: a crash here would make Telegram redeliver."""
    try:
        if not remember_update(update.get("update_id")):
            logger.info("telegram: ignoring redelivered update %s", update.get("update_id"))
            return
        if "callback_query" in update:
            await _handle_callback(update["callback_query"])
        elif "message" in update:
            await _handle_message(update["message"])
    except Exception:
        logger.exception("telegram: update handling failed")


# --------------------------------------------------------------------- message

async def _handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    tg_user = message.get("from") or {}
    if tg_user.get("is_bot"):
        return
    chat_id = chat.get("id")
    if chat_id is None or not tg_user.get("id"):
        return
    thread_id = message.get("message_thread_id")
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        await client.send_message(chat_id, "I can only read text for now.", thread_id=thread_id)
        return

    is_group = chat.get("type") in ("group", "supergroup")
    text = await _strip_mention(text)
    if is_group and not await _addressed_to_us(message, text):
        return

    if text.startswith("/"):
        await _handle_command(message, text, chat_id, thread_id, tg_user)
        return
    await _run_turn(chat_id, thread_id, tg_user, text, reply_to=message.get("message_id") if is_group else None)


async def _strip_mention(text: str) -> str:
    username = await bot_username()
    if username and text.lower().startswith(f"@{username.lower()}"):
        return text[len(username) + 1:].strip()
    return text


async def _addressed_to_us(message: dict, text: str) -> bool:
    """In a group, answer only what is meant for us.

    A bot that replies to every message in a shared channel is spam, and every
    reply is a charged turn against someone's account.
    """
    username = await bot_username()
    if text.startswith("/"):
        return True
    reply_to = (message.get("reply_to_message") or {}).get("from") or {}
    if username and reply_to.get("username") == username:
        return True
    if username and f"@{username.lower()}" in (message.get("text") or "").lower():
        return True
    return False


# -------------------------------------------------------------------- commands

async def _handle_command(message: dict, text: str, chat_id: int, thread_id: int | None, tg_user: dict) -> None:
    head, _, argument = text.partition(" ")
    command = head.split("@", 1)[0].lower().lstrip("/")
    argument = argument.strip()
    session_id = tg_identity.session_id(chat_id, thread_id, tg_user.get("id"))

    if command == "start":
        await _handle_start(chat_id, thread_id, tg_user, argument)
        return
    if command == "help":
        await client.send_message(chat_id, HELP, thread_id=thread_id)
        return
    if command == "link":
        await _handle_link(chat_id, thread_id, tg_user)
        return
    if command == "new":
        await sessions.clear_history(session_id)
        await client.send_message(chat_id, "Started a fresh conversation. Earlier context is cleared.", thread_id=thread_id)
        return
    if command == "app":
        await client.send_message(
            chat_id, "Open Orbit to connect a wallet, review a quote and sign it.", thread_id=thread_id,
            reply_markup={"inline_keyboard": [[{"text": "Open Orbit", "url": app_url(session_id)}]]},
        )
        return
    if command == "wallet":
        await _handle_wallet(chat_id, thread_id, tg_user, argument, session_id)
        return
    if command == "unlink":
        removed = await tg_identity.unlink(tg_user)
        await client.send_message(
            chat_id,
            "This Telegram account is detached. Your next message starts a new Orbit account."
            if removed else "This Telegram account was not linked.",
            thread_id=thread_id,
        )
        return
    # The rest are phrasings the general node already handles, so the bot does
    # not grow a second vocabulary for controls the web UI states in words.
    if command == "desk":
        mapped = "enable team mode" if argument.lower() in ("on", "enable", "yes") else "disable team mode"
        await _run_turn(chat_id, thread_id, tg_user, mapped)
        return
    if command == "charter":
        await _run_turn(chat_id, thread_id, tg_user, "show my risk charter")
        return
    if command in ("tasks", "alerts"):
        await _run_turn(chat_id, thread_id, tg_user, "show my tasks")
        return
    if command in ("account", "me", "status"):
        await _handle_account(chat_id, thread_id, tg_user)
        return
    await client.send_message(chat_id, f"Unknown command. {HELP}", thread_id=thread_id)


async def _handle_start(chat_id: int, thread_id: int | None, tg_user: dict, payload: str) -> None:
    if payload:
        token = await tg_identity.consume_link_token(payload)
        target = (token or {}).get("user_id") if (token or {}).get("kind") == "web" else None
        if target is None:
            await client.send_message(
                chat_id, "That link has expired or was already used. Generate a new one from Profile in the web app.",
                thread_id=thread_id,
            )
            return
        account = await tg_identity.link_to_account(tg_user, target)
        if account is None:
            await client.send_message(chat_id, "That account no longer exists.", thread_id=thread_id)
            return
        await client.send_message(
            chat_id, "✅ Linked. This chat now shares your Orbit account — same plan, credits, "
            "wallets and risk charter.\n\nSend /account any time to check.",
            thread_id=thread_id,
        )
        return
    account, created = await tg_identity.account_for(tg_user)
    opening = (
        "👋 <b>Orbit</b> is ready. Ask me anything about a token, a wallet or a market — "
        "I answer with live on-chain evidence and name my sources.\n\n"
        if created else "Welcome back.\n\n"
    )
    tail = f"\n\n<i>{LINK_HINT}</i>" if created and not await _is_linked(account) else ""
    await client.send_message(chat_id, opening + HELP + tail, thread_id=thread_id)


async def _is_linked(account: dict) -> bool:
    """Whether this account has a way in other than Telegram -- an email or a
    wallet. If it has one, the user already linked and must not be nagged."""
    from app import accounts

    if account.get("email"):
        return True
    return bool(await accounts.list_wallets(account["id"]))


async def _handle_account(chat_id: int, thread_id: int | None, tg_user: dict) -> None:
    """Where do I stand? The question the web answers with a Settings screen.

    A Telegram-only user has no other way to see their plan, their balance or
    whether the link they started actually completed -- and "did it work" is
    the first thing anyone asks after tapping a link.
    """
    from app import accounts, credits

    account, _ = await tg_identity.account_for(tg_user)
    identity = await tg_identity.identity_for(tg_user)
    balance = await credits.balance(identity.account_id)
    wallets = await accounts.list_wallets(account["id"])
    context = await sessions.get_session_context(tg_identity.session_id(chat_id, thread_id, tg_user.get("id")))

    lines = [
        "<b>Your Orbit account</b>",
        f"Plan: {html.escape(identity.plan.name)}",
        f"Credits: <b>{balance:,}</b>",
    ]
    if account.get("email"):
        lines.append(f"Signed in as: {html.escape(account['email'])}")
    if wallets:
        shown = ", ".join(f"{w['address'][:6]}…{w['address'][-4:]}" for w in wallets[:3])
        lines.append(f"Wallets: <code>{html.escape(shown)}</code>")
    following = context.get("mcp_wallet_address")
    if following:
        lines.append(f"Following: <code>{html.escape(following)}</code> (read-only)")
    if context.get("risk_charter"):
        lines.append("Risk charter: set")

    linked = await _is_linked(account)
    lines.append("")
    lines.append(
        "🔗 <b>Connected to your web account.</b> Same credits and plan on both."
        if linked else
        "⚠️ <b>Not connected to a web account.</b> This chat has an Orbit account of "
        "its own. Send /link to use the same one everywhere."
    )
    base = settings.public_base_url.rstrip("/")
    await client.send_message(
        chat_id, "\n".join(lines), thread_id=thread_id,
        reply_markup={"inline_keyboard": [[{"text": "Open Orbit", "url": f"{base}/ui/"}]]},
    )


async def _handle_link(chat_id: int, thread_id: int | None, tg_user: dict) -> None:
    """Telegram cannot mint the link token: it is a credential for a web
    account, so it is minted by a signed-in browser (POST /me/telegram/link).
    All the bot can do is say where, and open the door."""
    account, _ = await tg_identity.account_for(tg_user)
    if await _is_linked(account):
        await client.send_message(
            chat_id, "✅ This chat is already on your Orbit account — same plan, credits and wallets.",
            thread_id=thread_id,
        )
        return
    # The link carries a token that already identifies this Telegram account,
    # so the person signs in once on the other side and the connection
    # completes by itself. No button to find, no second step to explain.
    token = await tg_identity.start_link_from_telegram(tg_user)
    base = settings.public_base_url.rstrip("/")
    await client.send_message(
        chat_id,
        "<b>Use one Orbit account everywhere</b>\n\n"
        "Tap below and sign in however you like — a wallet such as MetaMask or "
        "Phantom, or an email. <i>Email is not required.</i>\n\n"
        "The moment you are signed in, this chat joins that account: same credits, "
        "plan, wallets and risk charter, and your alerts arrive here.\n\n"
        f"<i>The link is personal and works once, within "
        f"{settings.telegram_link_ttl_seconds // 60} minutes. Do not forward it.</i>",
        thread_id=thread_id,
        reply_markup={"inline_keyboard": [[{"text": "Sign in and connect", "url": f"{base}/ui/?tglink={token}"}]]},
    )


async def _handle_wallet(chat_id: int, thread_id: int | None, tg_user: dict, argument: str, session_id: str) -> None:
    """Follow a PUBLIC address. The refusal below is the important half."""
    address = argument.strip()
    if not address:
        await client.send_message(chat_id, "Send a public address: <code>/wallet &lt;address&gt;</code>", thread_id=thread_id)
        return
    if len(address) >= 60 or address.count(" ") >= 11:
        await client.send_message(
            chat_id,
            "⛔ That looks like key material, not a public address. Orbit only ever accepts a public wallet "
            "address, and never a private key or seed phrase. If you pasted a seed phrase here, treat it as "
            "compromised and move your funds now.",
            thread_id=thread_id,
        )
        return
    if not _ADDRESS.match(address):
        await client.send_message(chat_id, "That is not a Solana base58 or EVM 0x… address.", thread_id=thread_id)
        return
    identity = await tg_identity.identity_for(tg_user)
    try:
        await session_access.require_session_access(session_id, identity, claim=True)
    except session_access.SessionAccessDenied:
        await client.send_message(chat_id, "This conversation belongs to another account.", thread_id=thread_id)
        return
    context = await sessions.get_session_context(session_id)
    context["mcp_wallet_address"] = address
    await sessions.save_session_context(session_id, context)
    await client.send_message(
        chat_id,
        f"Following <code>{html.escape(address)}</code>, read-only. Ask about holdings, activity or exposure.",
        thread_id=thread_id,
    )


# ------------------------------------------------------------------- callbacks

async def _handle_callback(query: dict) -> None:
    data = str(query.get("data") or "")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    tg_user = query.get("from") or {}
    thread_id = message.get("message_thread_id")
    query_id = query.get("id")
    if chat_id is None or not query_id:
        return
    await client.answer_callback_query(query_id)

    session_id = tg_identity.session_id(chat_id, thread_id, tg_user.get("id"))
    kind, _, raw_index = data.partition(":")
    try:
        index = int(raw_index)
    except ValueError:
        return

    if kind == "sg":
        suggestion = await _stored_list(session_id, "suggestions", index)
        if suggestion:
            await _run_turn(chat_id, thread_id, tg_user, str(suggestion))
        return
    if kind != "qa":
        return
    stored = await _stored_list(session_id, "quick_actions", index)
    if stored is None:
        await client.send_message(chat_id, "That button belongs to an older answer — ask again.", thread_id=thread_id)
        return
    try:
        action = QuickAction(**stored)
    except Exception:
        logger.warning("telegram: stored quick action %s did not validate", index, exc_info=True)
        return
    # The prompt must be the message, and the action must match what the
    # server persisted; execute_chat_turn re-checks both and refuses otherwise.
    await _run_turn(chat_id, thread_id, tg_user, action.prompt, quick_action=action)


async def _stored_list(session_id: str, field: str, index: int):
    """One entry of the latest assistant message's persisted `field`.

    Read back from the conversation rather than carried in the callback: a
    callback payload is client-supplied and only 64 bytes, so the button names
    a position and the server's own record supplies the content.
    """
    messages = await sessions.get_messages(session_id)
    latest = next((item for item in reversed(messages) if item.get("role") == "assistant"), None)
    items = (latest or {}).get(field) or []
    return items[index] if 0 <= index < len(items) else None


# ------------------------------------------------------------------- the turn

async def _run_turn(chat_id: int, thread_id: int | None, tg_user: dict, text: str,
                    *, quick_action: QuickAction | None = None, reply_to: int | None = None) -> None:
    session_id = tg_identity.session_id(chat_id, thread_id, tg_user.get("id"))
    identity = await tg_identity.identity_for(tg_user)
    body = ChatRequest(
        message=text[:MAX_INBOUND_CHARS],
        session_id=session_id,
        quick_action=quick_action,
    )
    try:
        await session_access.require_session_access(session_id, identity, claim=True)
    except session_access.SessionAccessDenied:
        await client.send_message(chat_id, "This conversation belongs to another account.", thread_id=thread_id)
        return

    await client.send_chat_action(chat_id, "typing", thread_id=thread_id)
    # "remind me", "alert me when SOL drops" asked HERE must arrive here: an
    # inbox row is invisible to someone who never opens the web app. Scoped to
    # the turn, so a task created from the browser still defaults to the inbox.
    channel_token = task_scheduling.use_channel("telegram" if _is_private(chat_id) else None)
    async with Progress(chat_id, thread_id) as progress:
        # A tracked task, waited on -- NOT asyncio.wait_for, which cancels the
        # coroutine it is waiting on. The turn was admitted and charged before
        # the first provider call, so cancelling it at the timeout throws away
        # paid work and leaves the conversation without its answer. It runs to
        # completion either way; the only question is whether the user is
        # still being held on the line, and past the timeout they are not.
        turn = execution_policy.background(execution_policy.execute_chat_turn(body, identity))
        try:
            done, _pending = await asyncio.wait({turn}, timeout=settings.telegram_turn_timeout_seconds)
        finally:
            # The task copied this context when it was created, so it keeps
            # delivering to Telegram even after the contextvar is reset here.
            task_scheduling.reset_channel(channel_token)
        if turn not in done:
            await progress.fail("Still working on that one — it is taking a while. I will send the answer here as soon as it lands.")
            execution_policy.background(_deliver_when_ready(chat_id, thread_id, turn))
            return
        try:
            response = turn.result()
        except ServiceError as exc:
            await progress.fail(_friendly_error(exc))
            return
        except Exception:
            logger.exception("telegram: chat turn failed")
            await progress.fail("Something went wrong on my side. Try that again.")
            return
        await _deliver(progress, chat_id, thread_id, response, reply_to=reply_to)


async def _deliver_when_ready(chat_id: int, thread_id: int | None, turn: asyncio.Task) -> None:
    """Send the answer of a turn that outlived the user's attention."""
    try:
        response = await turn
    except ServiceError as exc:
        await client.send_message(chat_id, _friendly_error(exc), thread_id=thread_id)
        return
    except Exception:
        logger.exception("telegram: late chat turn failed")
        await client.send_message(chat_id, "That one did not finish. Try asking again.", thread_id=thread_id)
        return
    for chunk in render.render_answer(response):
        await client.send_message(chat_id, chunk, thread_id=thread_id)
    card = render.render_trade_plan(response)
    if card:
        await client.send_message(
            chat_id, card, thread_id=thread_id,
            reply_markup=render.keyboard(response, app_url=app_url(response.session_id)),
        )


def _is_private(chat_id: int | str) -> bool:
    """A task belongs to one account, and its holdings and thresholds are not
    the group's business, so a task created in a group keeps the inbox
    default rather than pushing into the shared chat."""
    return not tg_identity.is_group(chat_id)


async def _deliver(progress: Progress, chat_id: int, thread_id: int | None, response, *, reply_to: int | None) -> None:
    chunks = render.render_answer(response)
    card = render.render_trade_plan(response)
    if card:
        chunks.append(card)
    markup = render.keyboard(response, app_url=app_url(response.session_id))

    # The last chunk carries the keyboard, so the buttons sit under the end of
    # the answer rather than in the middle of it.
    for position, chunk in enumerate(chunks):
        last = position == len(chunks) - 1
        keyboard = markup if last else None
        if position == 0 and await progress.finish(chunk, reply_markup=keyboard):
            continue
        await client.send_message(
            chat_id, chunk, reply_markup=keyboard, thread_id=thread_id,
            reply_to=reply_to if position == 0 else None,
        )


def _friendly_error(exc: ServiceError) -> str:
    detail = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    if exc.status_code == 402:
        return (f"⛔ {html.escape(detail)}\n\nTop up or upgrade from Profile in the web app.\n\n"
                f"<i>{LINK_HINT}</i>")
    if exc.status_code == 409:
        return "I am still working on your last question here. One at a time."
    if exc.status_code == 429:
        return "That is more questions than the plan allows right now. Give it a minute."
    return f"⛔ {html.escape(detail)}"
