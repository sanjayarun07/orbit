"""Who is calling: a signed-in user (cookie), an API key (Bearer), or an
anonymous visitor on the trial. One resolution used by /chat, the account
endpoints, the sign-in gates and -- via a context variable set by the MCP
admission middleware -- the MCP server's tool calls.
"""

from __future__ import annotations

import ipaddress
from contextvars import ContextVar
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app import accounts, api_keys, credits
from app.billing_plans import ANONYMOUS, Plan, get_plan
from app.settings import settings

DEVICE_HEADER = "x-orbit-device"


@dataclass
class Identity:
    kind: str  # "user" | "api_key" | "anonymous" | "service"
    account_id: str
    ip: str
    user: dict | None = None
    api_key: dict | None = None
    plan: Plan = field(default_factory=lambda: ANONYMOUS)
    # When the user is a member of someone's team: the owner's user record.
    # Credits and plan come from the owner; the member keeps their own profile.
    team_owner: dict | None = None

    @property
    def signed_in(self) -> bool:
        return self.user is not None

    @property
    def rate_limit_key(self) -> str:
        return self.account_id

    @property
    def principal_id(self) -> str:
        """Who this caller *is*, as opposed to which account pays for them.

        These differ for exactly one case, and it is the one that matters:
        a team member is billed to their owner's account, so `account_id` is
        shared by every member of a team. It is the right key for credits,
        plans and rate limits, and the wrong key for ownership -- using it
        there made one teammate's quoted trade reachable and executable by
        another. Anonymous callers have no user record, and their account id
        already identifies the individual device.
        """
        if self.user is not None:
            return f"user:{self.user['id']}"
        return self.account_id

    def has_scope(self, scope: str) -> bool:
        if self.api_key is None:
            return True
        return scope in (self.api_key.get("scopes") or [])


# Set by the MCP admission middleware for the duration of an MCP request so the
# server's tool handlers (which never see the HTTP request) know the caller.
current_identity: ContextVar[Identity | None] = ContextVar("orbit_current_identity", default=None)


def client_ip(request: Request) -> str:
    """Trust forwarding headers only when the immediate peer is explicitly trusted."""
    peer = request.client.host if request.client else "anonymous"
    trusted = {item.strip() for item in settings.trusted_proxy_hosts.split(",") if item.strip()}
    if peer in trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


async def _identity_for_user(user: dict, ip: str, api_key: dict | None = None) -> Identity:
    billed = user
    team_owner = None
    if user.get("team_owner_id"):
        owner = await accounts.get_user(user["team_owner_id"])
        if owner is not None and get_plan(owner.get("plan_id")).seats > 1:
            billed, team_owner = owner, owner
        else:
            # The team no longer exists or the owner's plan lost its seats.
            await accounts.update_user(user["id"], team_owner_id=None)
            user = {**user, "team_owner_id": None}
    plan = get_plan(billed.get("plan_id"))
    account_id = credits.user_account_id(billed["id"])
    # Paid plans get their allowance from Stripe's invoice.paid webhook; the
    # lazy monthly grant is for the Free tier and admin-set plans without a
    # subscription behind them.
    if plan.price_usd_month <= 0 or not billed.get("stripe_subscription_id"):
        await credits.ensure_monthly_grant(account_id, plan)
    return Identity("api_key" if api_key else "user", account_id, ip, user=user, api_key=api_key, plan=plan, team_owner=team_owner)


async def resolve_identity(request: Request) -> Identity:
    ip = client_ip(request)
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if bearer.startswith(api_keys.KEY_PREFIX):
        record = await api_keys.authenticate(bearer)
        if record is None:
            raise HTTPException(401, "Invalid or revoked API key")
        user = await accounts.get_user(record["user_id"])
        if user is None:
            raise HTTPException(401, "API key belongs to a deleted account")
        if not get_plan(user.get("plan_id")).api_keys:
            raise HTTPException(403, "API keys are available on Pro and Max plans")
        return await _identity_for_user(user, ip, api_key=record)
    user = await accounts.get_session_user(request.cookies.get(accounts.USER_COOKIE))
    if user is not None:
        return await _identity_for_user(user, ip)
    account_id = credits.anonymous_account_id(ip, request.headers.get(DEVICE_HEADER))
    await credits.ensure_trial_grant(account_id, ANONYMOUS)
    return Identity("anonymous", account_id, ip, plan=ANONYMOUS)


async def require_user(request: Request) -> Identity:
    """FastAPI dependency for endpoints that need a signed-in account."""
    identity = await resolve_identity(request)
    if not identity.signed_in:
        raise HTTPException(401, {"error": "sign_in_required", "message": "Sign in with your email to use this."})
    return identity


def service_identity(name: str) -> Identity:
    """An internal caller (tests, workers) with no credit accounting."""
    return Identity("service", f"service:{name}", "internal", plan=get_plan("max"))
