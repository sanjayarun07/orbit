"""Who may see or execute a trade plan. One rule, every transport.

The companion to session_access.py, and it exists for the same reason: the
HTTP API and the MCP server both reach trade plans, and a rule written twice
is a rule that drifts. The MCP tool returned whole plans -- `confirmation_text`
included, which is the only secret the execution endpoints check -- with no
ownership test at all, so the HTTP gate could simply be walked around.

The ownership key is the *principal*, not the billing account. Team members are
billed to their owner's account, so `identity.account_id` is shared by every
member of a team; using it as an ownership key made one teammate's quoted trade
reachable and executable by another. Who pays and who owns are different
questions, and only one of them is authorization.

Reported as "not found", never "forbidden", so plan ids cannot be probed.
"""

from __future__ import annotations

from app import plans


class PlanAccessDenied(Exception):
    """The caller may not see or act on this trade plan."""


def plan_principal(identity) -> str | None:
    if identity is None:
        return None
    return getattr(identity, "principal_id", None)


async def require_plan_access(plan_id: str, identity):
    """Return the plan when `identity` may use it, else raise.

    A plan that cannot be loaded is not this function's problem: the caller's
    own handler reports a missing or expired plan, and answering differently
    here would turn the gate into an existence oracle.

    The load goes through `plans.get_plan` as a module attribute rather than a
    name imported at module load. That is deliberate: every transport must read
    the plan through the same function this gate read, or the gate can pass on
    "no such plan" while the handler goes on to find one.
    """
    try:
        plan = await plans.get_plan(plan_id)
    except (KeyError, ValueError):
        return None
    owner = getattr(plan, "owner_account_id", None)
    # Plans quoted before ownership existed carry no owner. They stay reachable
    # by whoever holds the id and expire within plan_ttl_seconds.
    if owner is None:
        return plan
    if plan_principal(identity) != owner:
        raise PlanAccessDenied(plan_id)
    return plan
