from typing import Literal, TypedDict
from app.capability_router import WorkflowIntent
from app.models import CrossChainSwapDraft, SwapProposal, TradePlan

class AgentState(TypedDict, total=False):
    request: str
    contextual_request: str
    wallet_address: str
    history: str
    intent: WorkflowIntent
    capabilities: list[str]
    chains: list[str]
    route_source: str
    execution_provider: Literal["jupiter", "relay"] | None
    missing_fields: list[str]
    routing_decision: dict
    clarification: str
    answer: str
    trajectory: dict | None
    proposal: SwapProposal | None
    trade_plan: TradePlan | None
    cross_chain_swap: CrossChainSwapDraft | None
    risk_assessment: object | None
    team_subintent: str
    team_report: dict | None
    error: str | None
    session_context: dict
    quick_action: dict | None
    pending_token: dict | None


def effective_request(state: AgentState) -> str:
    """Return the server-resolved prompt while preserving the user utterance."""
    return state.get("contextual_request") or state["request"]
