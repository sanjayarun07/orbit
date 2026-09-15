"""Per-chat-turn external-call/cost budget.

contextvars-based rather than threaded through AgentState: LangGraph nodes
have no existing shared-context mechanism (see app/graph.py), and
contextvars are correctly isolated per concurrent request under asyncio --
each chat turn starts its own budget and never leaks into another.

Deliberately non-raising. The two choke points every external call goes
through -- MCPGateway.call() (app/mcp_tools.py) and ProviderRouter.route()
(app/provider_router.py) -- already have established, universally-handled
failure conventions ("MCP tool call failed: ..." string prefix, and a
RuntimeError when no candidate succeeds). Reusing those instead of a new
exception type means every existing disclosure/fallback code path already
handles a budget cutoff correctly, with no risk of a new exception being
silently swallowed by one of the several broad `except RuntimeError`/
`except Exception` handlers already scattered through the node files.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class CallBudget:
    max_calls: int
    max_cost_usd: float
    calls: int = 0
    cost_usd: float = 0.0

    def exceeded(self) -> bool:
        return self.calls > self.max_calls or self.cost_usd > self.max_cost_usd


_current: contextvars.ContextVar[CallBudget | None] = contextvars.ContextVar("call_budget", default=None)


def start_budget(max_calls: int, max_cost_usd: float) -> contextvars.Token:
    """Call once per chat turn (app.graph._run_agent_traced). Returns a
    token to pass to reset_budget in a finally block."""
    return _current.set(CallBudget(max_calls=max_calls, max_cost_usd=max_cost_usd))


def reset_budget(token: contextvars.Token) -> None:
    _current.reset(token)


def current_budget() -> CallBudget | None:
    return _current.get()


def charge_and_check(cost_usd: float = 0.0) -> bool:
    """Record one external call against the active turn's budget.

    Returns True if the call may proceed, False once either the call-count
    or cost ceiling has been exceeded -- callers treat False exactly like
    any other provider-unavailable outcome. A no-op (always returns True)
    when no budget is active (e.g. the admin sandbox, a one-off script, or
    any call made outside a chat turn) -- this is intentionally opt-in per
    caller, not a global gate.
    """
    budget = _current.get()
    if budget is None:
        return True
    budget.calls += 1
    budget.cost_usd += cost_usd
    return not budget.exceeded()
