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

import re
from contextvars import ContextVar, Token

_ROLES: ContextVar[dict[str, str]] = ContextVar("address_roles", default={})


_ADDRESS = re.compile(r"(?<![A-Za-z0-9])(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_WALLET_NEAR = re.compile(r"\b(?:wallet|address|portfolio|holdings|treat\s+\S+\s+as\s+a\s+wallet)\b", re.I)
_TOKEN_NEAR = re.compile(r"\b(?:token|mint|contract|coin)\b", re.I)


def roles_in_request(request: str) -> dict[str, str]:
    """The role the request's own words give each address in it: "public
    wallet 3aHL…", "treat 3aHL… as a wallet" type it a wallet; "token 0x…",
    "mint …" type it a token (live UI test 2026-09-25: a Birdeye token card
    was rendered for a wallet address the user had typed as a wallet)."""
    out: dict[str, str] = {}
    for m in _ADDRESS.finditer(request or ""):
        window = (request or "")[max(0, m.start() - 60): m.end() + 40]
        wallet, token = bool(_WALLET_NEAR.search(window)), bool(_TOKEN_NEAR.search(window))
        if wallet and not token:
            out[m.group(1).lower()] = "wallet"
        elif token and not wallet:
            out[m.group(1).lower()] = "token"
        elif wallet and token and re.search(r"\bas\s+a\s+wallet\b|\bnot\s+(?:the\s+)?(?:\w+\s+)?(?:mint|token)\b", window, re.I):
            out[m.group(1).lower()] = "wallet"
    return out


def bind_turn(session_context: dict | None, request: str | None = None) -> Token:
    """Bind the roles the session context settles for this turn; the
    request's own typing of an address wins over the session's."""
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
    roles.update(roles_in_request(request or ""))
    return _ROLES.set(roles)


def end_turn(token: Token) -> None:
    _ROLES.reset(token)


def role_of(address: str | None) -> str | None:
    if not address:
        return None
    return _ROLES.get().get(address.lower())


def not_a_token(address: str | None) -> str | None:
    """The refusal when an address typed as a wallet is about to be read as
    a token contract; None when it may be read."""
    if role_of(address) != "wallet":
        return None
    short = f"{address[:6]}…{address[-4:]}"
    return f"`{short}` is a wallet in this conversation, not a token contract: there is no token overview or pair to read at it."


def not_a_wallet(address: str | None) -> str | None:
    """The refusal when an address the conversation typed as a token is about
    to be read as a wallet; None when it may be read."""
    if role_of(address) != "token":
        return None
    short = f"{address[:6]}…{address[-4:]}"
    return (f"`{short}` is the token contract this conversation is about, not a wallet, so there is no portfolio to read at it. "
            "Connect a wallet or paste a wallet address for holdings; for the token itself ask for its holders, pools or an exit at a size.")
