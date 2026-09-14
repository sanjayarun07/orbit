from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
from app.capability_router import (
    extract_charter,
    is_charter_clear,
    is_charter_set,
    is_charter_show,
    is_execution_explanation,
    is_team_disable,
    is_team_enable,
    is_team_status,
    is_trade_cancellation,
    is_trade_confirmation,
    is_trade_modifier,
)

@trace(name="general", as_type="agent")
async def general_node(state: AgentState) -> dict:
    """No tool calls: a single lightweight LM call for chit-chat/conceptual replies."""
    if state.get("clarification"):
        return {"answer": state["clarification"], "trajectory": None}
    request = state["request"]
    current_charter = (state.get("session_context") or {}).get("risk_charter")
    if is_charter_set(request):
        rules = extract_charter(request)
        if not rules:
            return {"answer": "Tell me the rules after the command, e.g. `set my risk charter: only verified tokens, max $10 per trade, max 1% of portfolio`.", "trajectory": None}
        # The charter is persisted by advance_session_context; echo it back so
        # the user sees exactly what the Risk agent will now enforce.
        return {
            "answer": (
                "Risk charter updated. The Risk agent will now check every proposed trade "
                f"against these rules before showing you a confirmation card:\n\n> {rules}\n\n"
                "Say `show my risk charter` to review it or `clear my risk charter` to remove it."
            ),
            "trajectory": None,
        }
    if is_charter_show(request):
        if current_charter:
            return {"answer": f"Your active risk charter:\n\n> {current_charter}", "trajectory": None}
        return {"answer": "No risk charter is set — trades run in advisory mode (risk is shown but never blocks; the built-in caps still apply). Set one with `set my risk charter: ...`.", "trajectory": None}
    if is_charter_clear(request):
        return {"answer": "Risk charter cleared. Trades return to advisory mode — the Risk agent will show risk metrics but won't block; the built-in slippage/notional/impact/security caps still apply.", "trajectory": None}
    if is_team_enable(request):
        return {
            "answer": (
                "🖥️ Team mode ON. Your requests now run through the trading desk: a Coordinator "
                "fans each one out to Market Research, Execution, and Risk, then hands you one "
                "answer (and a reviewable card for trades). It's slower and costs more per turn "
                "than the single-agent flow. Say `disable team mode` to switch back."
            ),
            "trajectory": None,
        }
    if is_team_disable(request):
        return {"answer": "Team mode OFF. Back to the single-agent flow.", "trajectory": None}
    if is_team_status(request):
        on = bool((state.get("session_context") or {}).get("team_mode"))
        return {"answer": f"Team mode is currently **{'ON' if on else 'OFF'}**." + ("" if on else " Say `enable team mode` to turn on the trading desk."), "trajectory": None}
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
