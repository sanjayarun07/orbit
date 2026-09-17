from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
from app import streaming
from app.routing import backends
from app.routing.decided import decided_by
from app.routing.resolver import resolve as resolve_route
from app.routing.semantic import embedding_router

@trace(name="resolve_intent")
async def resolve_intent_node(state: AgentState) -> dict:
    decided_by.set("none")   # a cached or rule-anchored decision reaches no model
    update = await resolve_route(state, backends.intent_call_lm(), embedding_factory=embedding_router)
    streaming.emit("status", text=f"Routed: {update.get('intent', '?')}" + (f" · {', '.join(update.get('capabilities') or [])}" if update.get("capabilities") else ""))
    return update


def route_by_intent(state: AgentState) -> str:
    return state["intent"]
