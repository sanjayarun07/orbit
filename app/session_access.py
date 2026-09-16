"""Who may touch a conversation. One rule, used by the HTTP API and the MCP
server alike:

- an owned conversation is reachable only by its owner (the account that
  first used it while signed in);
- an unowned one stays open (anonymous ids are random and never mapped) and,
  when `claim` is set, becomes the signed-in caller's;
- a miss is reported as "not found", never "forbidden", so ids cannot be probed.

Ownership never transfers (see accounts.touch_chat_session).
"""

from __future__ import annotations

from app import accounts


class SessionAccessDenied(Exception):
    """The caller may not operate on this conversation."""


def caller_user_id(identity) -> str | None:
    if identity is None or isinstance(identity, str):
        return None
    if not getattr(identity, "signed_in", False):
        return None
    user = getattr(identity, "user", None) or {}
    return str(user["id"]) if user.get("id") else None


async def require_session_access(session_id: str | None, identity, claim: bool = False) -> None:
    """Raise SessionAccessDenied unless `identity` may use `session_id`.
    A missing session id is always fine (a new conversation)."""
    if not session_id:
        return
    owner = await accounts.chat_session_owner(session_id)
    user_id = caller_user_id(identity)
    if owner is not None and owner != user_id:
        raise SessionAccessDenied(session_id)
    if owner is None and claim and user_id:
        # Two signed-in callers can both see an unowned conversation; the
        # store hands it to exactly one. The loser is denied, not admitted.
        if not await accounts.touch_chat_session(user_id, session_id):
            raise SessionAccessDenied(session_id)
