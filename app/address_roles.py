"""Which role an address plays in this conversation: a token mint or a wallet.

A wallet tool given a token mint reads a "portfolio" that belongs to no one
(expanded journeys, 2026-09-24: "which has a better exit for a $1,000
position?" after BONK holders ran the wallet portfolio tool on BONK's mint
and wrote "the user holds $125K of SOL"). The turn binds the roles the
session already settled -- the focus token, the focus wallet -- and every
wallet reader refuses an address the conversation typed as a token.

Bound per turn in a ContextVar, which `asyncio.to_thread` propagates; a tool
run outside the turn's context sees no roles and behaves as before.
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_ROLES: ContextVar[dict[str, str]] = ContextVar("address_roles", default={})


def bind_turn(session_context: dict | None) -> Token:
    """Bind the roles the session context settles for this turn."""
    roles: dict[str, str] = {}
    context = session_context or {}
    focus = context.get("focus") or {}
    if focus.get("address") and focus.get("kind") in ("token", "wallet"):
        roles[str(focus["address"]).lower()] = focus["kind"]
    pending = context.get("pending_token") or {}
    if isinstance(pending, dict) and pending.get("address"):
        roles.setdefault(str(pending["address"]).lower(), "token")
    connected = (context.get("connected_wallet") or {}).get("address")
    if connected:
        roles[str(connected).lower()] = "wallet"
    return _ROLES.set(roles)


def end_turn(token: Token) -> None:
    _ROLES.reset(token)


def role_of(address: str | None) -> str | None:
    if not address:
        return None
    return _ROLES.get().get(address.lower())


def not_a_wallet(address: str | None) -> str | None:
    """The refusal when an address the conversation typed as a token is about
    to be read as a wallet; None when it may be read."""
    if role_of(address) != "token":
        return None
    short = f"{address[:6]}…{address[-4:]}"
    return (f"`{short}` is the token contract this conversation is about, not a wallet, so there is no portfolio to read at it. "
            "Connect a wallet or paste a wallet address for holdings; for the token itself ask for its holders, pools or an exit at a size.")
