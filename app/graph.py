"""LangGraph composition and public agent runner; handlers live in app.nodes."""
from dataclasses import dataclass
from langgraph.graph import END, START, StateGraph
from app.capability_router import WorkflowIntent
from app.context_entities import resolve_contextual_request
from app.models import CrossChainSwapDraft, TradePlan
from app.tracing import trace
from app.nodes.state import AgentState
from app.nodes.routing import resolve_intent_node, route_by_intent
from app.nodes.general import general_node
from app.nodes.research import research_node
from app.nodes.portfolio import portfolio_node
from app.nodes.trading import (trade_planner_node, cross_chain_swap_node, risk_check_node,
    route_after_risk_check, quote_and_simulate_node, finalize_trade_node)

def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("resolve_intent", resolve_intent_node)
    builder.add_node("general", general_node)
    builder.add_node("research", research_node)
    builder.add_node("portfolio", portfolio_node)
    builder.add_node("trade_planner", trade_planner_node)
    builder.add_node("cross_chain_swap", cross_chain_swap_node)
    builder.add_node("risk_check", risk_check_node)
    builder.add_node("quote_and_simulate", quote_and_simulate_node)
    builder.add_node("finalize_trade", finalize_trade_node)

    builder.add_edge(START, "resolve_intent")
    builder.add_conditional_edges(
        "resolve_intent",
        route_by_intent,
        {
            "general": "general",
            "research": "research",
            "portfolio": "portfolio",
            "trade": "trade_planner",
            "cross_chain_swap": "cross_chain_swap",
        },
    )
    builder.add_edge("general", END)
    builder.add_edge("research", END)
    builder.add_edge("portfolio", END)
    builder.add_edge("trade_planner", "risk_check")
    builder.add_edge("cross_chain_swap", END)
    builder.add_conditional_edges(
        "risk_check", route_after_risk_check, {"quote": "quote_and_simulate", "finalize": "finalize_trade"}
    )
    builder.add_edge("quote_and_simulate", "finalize_trade")
    builder.add_edge("finalize_trade", END)

    # Chat history is already stored in Redis/Postgres-backed application state;
    # a per-process LangGraph checkpointer only accumulates duplicate snapshots.
    return builder.compile()


graph = build_graph()


@dataclass
class AgentRun:
    answer: str
    trajectory: dict | None
    trade_plan: TradePlan | None
    intent: WorkflowIntent
    capabilities: list[str]
    cross_chain_swap: CrossChainSwapDraft | None = None
    routing_decision: dict | None = None

    def __iter__(self):
        """Retain compatibility with callers unpacking the original three values."""
        yield self.answer
        yield self.trajectory
        yield self.trade_plan


async def run_agent(
    request: str,
    wallet_address: str,
    history: str = "",
    session_context: dict | None = None,
    quick_action: dict | None = None,
) -> AgentRun:
    resolved_request = resolve_contextual_request(request, history, session_context)
    return await _run_agent_traced(
        request, resolved_request, wallet_address, history, session_context or {}, quick_action
    )


@trace(name="run_agent", as_type="chain")
async def _run_agent_traced(
    request: str,
    contextual_request: str,
    wallet_address: str,
    history: str = "",
    session_context: dict | None = None,
    quick_action: dict | None = None,
) -> AgentRun:
    final_state = await graph.ainvoke(
        {
            "request": request,
            "contextual_request": contextual_request,
            "wallet_address": wallet_address,
            "history": history,
            "session_context": session_context or {},
            "quick_action": quick_action,
        },
    )
    answer = final_state["answer"]
    decision = final_state.get("routing_decision") or {}
    if answer and decision.get("speech_act") == "advice":
        # A recommendation-shaped request ("should I buy X?") is answered as
        # research, never routed to a quote workflow; make that framing explicit
        # to the user rather than presenting the answer as investment advice.
        answer = answer.rstrip() + (
            "\n\n_This is general information, not financial advice. Do your own "
            "research and consider your own risk tolerance before acting._"
        )
    return AgentRun(
        answer=answer,
        trajectory=final_state.get("trajectory"),
        trade_plan=final_state.get("trade_plan"),
        intent=final_state["intent"],
        capabilities=final_state.get("capabilities", []),
        cross_chain_swap=final_state.get("cross_chain_swap"),
        routing_decision=final_state.get("routing_decision"),
    )
