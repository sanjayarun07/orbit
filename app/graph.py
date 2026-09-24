"""LangGraph composition and public agent runner; handlers live in app.nodes."""
from dataclasses import dataclass
from langgraph.graph import END, START, StateGraph
from app.call_budget import current_budget, start_budget as start_call_budget, reset_budget as reset_call_budget
from app.capability_router import WorkflowIntent
from app.context_entities import resolve_contextual_request
from app.routing.controls import is_wallet_connected_ack
from app.models import CrossChainSwapDraft, RiskAssessment, TradePlan
from app.provider_analytics import emit_turn_budget_event
from app.settings import settings
from app.tracing import trace
from app.nodes.state import AgentState
from app.nodes.routing import resolve_intent_node, route_by_intent
from app.nodes.general import general_node
from app.nodes.research import research_node
from app.nodes.portfolio import portfolio_node
from app.nodes.trading import (trade_planner_node, cross_chain_swap_node, risk_check_node,
    route_after_risk_check, quote_and_simulate_node, charter_risk_node, finalize_trade_node)
from app.nodes.team import team_node

def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("resolve_intent", resolve_intent_node)
    builder.add_node("general", general_node)
    builder.add_node("research", research_node)
    builder.add_node("portfolio", portfolio_node)
    builder.add_node("trade_planner", trade_planner_node)
    builder.add_node("cross_chain_swap", cross_chain_swap_node)
    builder.add_node("team", team_node)
    builder.add_node("risk_check", risk_check_node)
    builder.add_node("quote_and_simulate", quote_and_simulate_node)
    builder.add_node("charter_risk", charter_risk_node)
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
            "team": "team",
        },
    )
    builder.add_edge("general", END)
    builder.add_edge("research", END)
    builder.add_edge("portfolio", END)
    builder.add_edge("team", END)
    builder.add_edge("trade_planner", "risk_check")
    builder.add_edge("cross_chain_swap", END)
    builder.add_conditional_edges(
        "risk_check", route_after_risk_check, {"quote": "quote_and_simulate", "finalize": "finalize_trade"}
    )
    # The soft, charter-driven Risk gate sits after the real quote and before
    # the CONFIRM card; the no-plan path (deterministic cap failure) skips it.
    builder.add_edge("quote_and_simulate", "charter_risk")
    builder.add_edge("charter_risk", "finalize_trade")
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
    risk_assessment: RiskAssessment | None = None
    team_report: dict | None = None
    pending_token: dict | None = None
    pending_wallet_request: str | None = None
    resolved_token: dict | None = None
    answer_gate: dict | None = None
    job_id: str | None = None
    job_attached: bool = False
    control: bool = False       # a reply to a command about the user's own things (tasks, watched exits): no related questions
    contract: dict | None = None    # the question contract this turn answered (kind, subject, venue, window, filters), for the next turn's follow-ups

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
    parked = (session_context or {}).get("pending_wallet_request")
    if parked and is_wallet_connected_ack(request):
        # The previous turn refused a swap for lack of a wallet; "yes connected"
        # means run that swap now, not a fresh (and meaningless) request.
        request = parked
    resolved_request = resolve_contextual_request(request, history, session_context)
    from app import research_objective
    resolved_request = research_objective.attach(resolved_request, session_context)
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
    # One call/cost budget per chat turn, shared across every node via
    # contextvars -- see app/call_budget.py for why this isn't threaded
    # through AgentState instead.
    budget_token = start_call_budget(settings.max_external_calls_per_turn, settings.max_paid_data_cost_usd_per_turn)
    final_state: dict | None = None
    try:
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
    finally:
        # Snapshot before reset_call_budget clears the contextvar -- there's
        # no telemetry to emit for a turn where start_call_budget was never
        # reached (impossible here) or the budget was somehow already gone.
        budget = current_budget()
        reset_call_budget(budget_token)
        if budget is not None:
            emit_turn_budget_event(
                intent=(final_state or {}).get("intent") or "unknown",
                calls_used=budget.calls,
                cost_usd=budget.cost_usd,
                calls_ceiling=budget.max_calls,
                cost_ceiling=budget.max_cost_usd,
                capped=budget.exceeded(),
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
        risk_assessment=final_state.get("risk_assessment"),
        team_report=final_state.get("team_report"),
        pending_token=final_state.get("pending_token"),
        pending_wallet_request=final_state.get("pending_wallet_request"),
        resolved_token=final_state.get("resolved_token"),
        answer_gate=final_state.get("answer_gate"),
        job_id=final_state.get("job_id"),
        job_attached=bool(final_state.get("job_attached")),
        contract=final_state.get("contract"),
    )
