from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
from app.capability_router import is_trade_cancellation, is_trade_confirmation, is_trade_modifier, is_execution_explanation

@trace(name="general", as_type="agent")
async def general_node(state: AgentState) -> dict:
    """No tool calls: a single lightweight LM call for chit-chat/conceptual replies."""
    if state.get("clarification"):
        return {"answer": state["clarification"], "trajectory": None}
    if is_trade_cancellation(state["request"]):
        return {
            "answer": (
                "The pending trade has been dismissed in this chat. Nothing was submitted. "
                "Tell me the new route whenever you want to start again."
            ),
            "trajectory": None,
        }
    if is_trade_confirmation(state["request"]):
        return {
            "answer": (
                "For safety, a typed confirmation does not execute a swap. Review the latest "
                "swap card and use its confirmation button so your connected wallet can show "
                "the exact transaction before signing."
            ),
            "trajectory": None,
        }
    if is_trade_modifier(state["request"]):
        return {
            "answer": (
                "There is no active trade in this chat to update. Start a new swap by naming "
                "the amount, payment token and chain, and the token and chain you want to receive."
            ),
            "trajectory": None,
        }
    if is_execution_explanation(state["request"]):
        return {
            "answer": (
                "Relay is the routing and execution layer Orbit uses for cross-chain swaps "
                "and swaps involving non-Solana chains. Orbit first parses your message into "
                "the source chain, destination chain, amount, input token, output token, and "
                "slippage. It then resolves exact token contracts against Relay's live chain "
                "data and requests a fresh quote.\n\n"
                "You review the amount received, fees, price impact, route, and destination "
                "address before approving. After you confirm, your connected wallet signs the "
                "required transaction; Orbit never receives your private key. Relay executes "
                "the route and reports each bridge or swap step until the destination funds "
                "arrive. Quotes and Solana blockhashes are short-lived, so an expired approval "
                "is refreshed instead of reused.\n\n"
                "Orbit uses Jupiter for same-chain Solana swaps. Relay is used for cross-chain "
                "routes and Base, Robinhood Chain, Ethereum, Arbitrum, and other supported "
                "non-Solana activity. No transaction is created from an explanatory question."
            ),
            "trajectory": None,
        }
    result = await runtime._call_lm(
        runtime.general_agent, request=state["request"], conversation_history=state.get("history", "")
    )
    return {"answer": result.answer, "trajectory": None}
