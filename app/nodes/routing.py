from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
from app.routing.resolver import resolve as resolve_route
from app.routing.semantic import embedding_router

@trace(name="resolve_intent")
async def resolve_intent_node(state: AgentState) -> dict:
    return await resolve_route(state, runtime._call_lm, embedding_factory=embedding_router)


def route_by_intent(state: AgentState) -> str:
    return state["intent"]
