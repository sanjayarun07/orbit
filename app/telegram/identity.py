"""Who is on the other end of a Telegram update.

The rule: a Telegram user is a way IN to an ordinary Orbit account, exactly as
a wallet signature is (app/accounts.py `create_wallet_user`). Their first
message creates an email-less account keyed on their numeric Telegram id;
every message after that resolves to it. From `execute_chat_turn`'s point of
view the caller is `Identity(kind="user", ...)` and nothing downstream --
credits, plan limits, quotas, conversation retention, export, deletion --
needs to know Telegram exists.

Why an account rather than the anonymous path: an anonymous identity is keyed
on an IP plus a device header and retains its conversation for two hours,
which is right for a browser tab and wrong for a chat thread the user will
scroll back through next week. The Telegram id is stable, personal and free,
so the account is real from the first message and the sign-in screen is what
disappears -- not the account.

Linking to an existing web account is the other direction: the web UI mints a
one-time token, the user opens `t.me/<bot>?start=<token>`, and the Telegram
identity is repointed at the account that minted it. The bot's own throwaway
account is then deleted, so a user who chatted before linking does not keep a
second account holding credits they cannot see.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

from app import accounts
from app.db import get_redis
from app.identity import Identity, identity_for_user
from app.settings import settings

logger = logging.getLogger(__name__)

PROVIDER = "telegram"
# The rate-limit and telemetry label for a caller with no client address.
# `Identity.rate_limit_key` is the account id, so this never widens a bucket.
TELEGRAM_IP = "telegram"

_link_tokens: dict[str, tuple[float, str]] = {}   # token -> (expires_at, payload json)


def is_group(chat_id: int | str) -> bool:
    """Telegram gives groups, supergroups and channels a negative id."""
    return str(chat_id).startswith("-")


def session_id(chat_id: int | str, thread_id: int | None = None, user_id: int | str | None = None) -> str:
    """One Orbit conversation per Telegram chat -- per topic in a forum, and
    per person in a group.

    Stable across restarts because it is derived, not stored: the same chat
    always resumes the same conversation, which is what makes follow-ups and
    pronouns work without the bot keeping any state of its own.

    The per-person segment in a group is not a preference. A conversation is
    owned by one account (app/session_access.py), so a single shared group
    conversation meant the first person to ask owned it and everyone else was
    refused forever. And had they not been refused, they would have inherited
    that person's bound wallet, focused entity and risk charter -- someone
    else's "sell half of it" is not your follow-up.
    """
    base = f"tg:{chat_id}"
    if thread_id:
        base = f"{base}:{thread_id}"
    if user_id is not None and is_group(chat_id):
        base = f"{base}:u{user_id}"
    return base


def display_name(user: dict) -> str | None:
    handle = user.get("username")
    if handle:
        return f"@{handle}"
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or None


async def account_for(tg_user: dict) -> tuple[dict, bool]:
    """(Orbit account, created) for a Telegram user. Find-or-create."""
    external_id = str(tg_user.get("id"))
    existing = await accounts.find_identity_owner(PROVIDER, external_id)
    if existing is not None:
        return existing, False
    account = await accounts.create_account()
    await accounts.link_identity(account["id"], PROVIDER, external_id, display_name(tg_user))
    logger.info("telegram: created account %s for tg user %s", account["id"], external_id)
    return account, True


async def identity_for(tg_user: dict) -> Identity:
    """The Identity a chat turn runs as, with plan and monthly grant resolved
    the same way a cookie request resolves them."""
    account, _ = await account_for(tg_user)
    return await identity_for_user(account, TELEGRAM_IP)


# ------------------------------------------------------------ account linking

async def mint_link_token(payload: dict) -> str:
    """One short-lived, single-use token, in whichever direction linking runs.

    Two directions exist and both end at the same place -- one account with a
    Telegram identity attached -- but they start from opposite sides:

    - `{"kind": "web", "user_id": ...}` is minted by a signed-in browser and
      carried into Telegram as the `/start` payload. For a user who is already
      on the web and wants Telegram too.
    - `{"kind": "telegram", "tg_user": {...}}` is minted by the bot and carried
      into the browser as `?tglink=`. For the far commoner case: someone who
      met Orbit in Telegram and is now signing in for the first time. They
      never have to find a button, because the link they tapped already knows
      who they are.

    Either way the token alone proves nothing: it must be presented together
    with the other side's own authentication -- a `/start` from that Telegram
    account, or a signed-in browser session -- before anything is linked.
    """
    token = secrets.token_urlsafe(24)
    body = json.dumps(payload)
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"tg_link:{token}", settings.telegram_link_ttl_seconds, body)
    else:
        _prune_link_tokens()
        _link_tokens[token] = (time.time() + settings.telegram_link_ttl_seconds, body)
    return token


async def start_link(user_id: str) -> str:
    """Web -> Telegram: the token Profile puts in `t.me/<bot>?start=<token>`."""
    return await mint_link_token({"kind": "web", "user_id": user_id})


async def start_link_from_telegram(tg_user: dict) -> str:
    """Telegram -> web: the token `/link` puts in `/ui/?tglink=<token>`.

    It carries the Telegram identity, not an account, because the person does
    not have one yet -- that is the whole point. Whatever account they sign
    into on the other side is the account this identity attaches to.
    """
    return await mint_link_token({
        "kind": "telegram",
        "id": str(tg_user.get("id")),
        "username": tg_user.get("username"),
        "first_name": tg_user.get("first_name"),
        "last_name": tg_user.get("last_name"),
    })


async def peek_link_token(token: str) -> dict | None:
    """The payload this token was minted with, without consuming it: what the
    browser shows the person before they confirm the connection."""
    redis = await get_redis()
    if redis is not None:
        raw = await redis.get(f"tg_link:{token}")
        raw = raw.decode() if isinstance(raw, bytes) else raw
    else:
        entry = _link_tokens.get(token)
        raw = entry[1] if entry is not None and entry[0] >= time.time() else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def start_claim_confirmation(token: str, user_id: str) -> str:
    """A nonce that binds one pending link to the signed-in account that
    previewed it (the browser session proves the account; the nonce is
    checked against that account id and the token).
    The claim must present it: a link opened by a victim is then only
    previewed, never attached, until that person clicks Connect (review of
    330bc651: page load attached whichever Telegram identity minted the URL)."""
    nonce = secrets.token_urlsafe(18)
    body = json.dumps({"token": token, "user_id": user_id})
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"tg_claim:{nonce}", 600, body)
    else:
        _prune_link_tokens()
        _link_tokens[f"claim:{nonce}"] = (time.time() + 600, body)
    return nonce


async def take_claim_confirmation(nonce: str) -> dict | None:
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"tg_claim:{nonce}")
        raw = raw.decode() if isinstance(raw, bytes) else raw
    else:
        entry = _link_tokens.pop(f"claim:{nonce}", None)
        raw = entry[1] if entry is not None and entry[0] >= time.time() else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def consume_link_token(token: str) -> dict | None:
    """The payload this token was minted with, once. None if unknown or expired."""
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"tg_link:{token}")
    else:
        entry = _link_tokens.pop(token, None)
        raw = entry[1] if entry is not None and entry[0] >= time.time() else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _prune_link_tokens() -> None:
    now = time.time()
    for token in [key for key, (expires, _) in _link_tokens.items() if expires < now]:
        _link_tokens.pop(token, None)


async def link_to_account(tg_user: dict, target_user_id: str) -> dict | None:
    """Repoint this Telegram identity at `target_user_id`.

    The account the bot created for them on first contact is deleted when it
    was only ever a shell -- no email, no wallet, no other identity. Deleting
    one that had accrued anything would destroy history the user can still see
    from the web, so that case keeps both and only moves the pointer.
    """
    target = await accounts.get_user(target_user_id)
    if target is None:
        return None
    external_id = str(tg_user.get("id"))
    previous = await accounts.find_identity_owner(PROVIDER, external_id)
    await accounts.link_identity(target["id"], PROVIDER, external_id, display_name(tg_user))
    if previous is not None and previous["id"] != target["id"]:
        await _discard_shell_account(previous)
    return target


async def _discard_shell_account(account: dict) -> None:
    """Delete the bot's own first-contact account, if that is all it ever was.

    Deliberately not conditioned on the credit balance. A shell holds only the
    free monthly allowance, which is granted per account and cannot be moved;
    "it still has credits" would therefore be true of every shell and the row
    would be orphaned forever. Credits that were *bought* imply a Stripe
    customer, and that is checked -- as are the other two ways an account can
    be reached without Telegram.
    """
    if account.get("email") or account.get("stripe_customer_id"):
        return
    if await accounts.list_wallets(account["id"]):
        return
    if await accounts.list_identities(account["id"]):      # another messaging identity still points here
        return
    await accounts.delete_user(account["id"])
    logger.info("telegram: discarded empty prior account %s after link", account["id"])


async def unlink(tg_user: dict) -> bool:
    return await accounts.unlink_identity(PROVIDER, str(tg_user.get("id")))
