from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import re
from app.capability_router import extract_cross_chain_draft, is_trade_modifier
from app.models import CrossChainSwapDraft, SwapProposal
from app.plans import create_trade_plan
from app.settings import settings
from app.trade_context import complete_swap_fields

@trace(name="trade_planner", as_type="agent")
async def trade_planner_node(state: AgentState) -> dict:
    if not state.get("wallet_address"):
        return {"answer": "I need a wallet address to prepare a swap.", "trajectory": None}
    if state.get("execution_provider") is None and "chain" in state.get("missing_fields", []):
        draft = extract_cross_chain_draft(state["request"], tuple(state.get("chains", [])))
        subject = f" {draft['output_token']}" if draft.get("output_token") else " this token"
        return {
            "answer": (
                f"Which chain is{subject} on? I need the network before choosing the execution provider. "
                "Solana swaps use Jupiter; Base, Robinhood Chain, other EVM chains, and cross-chain swaps use Relay. "
                "No quote or transaction has been created."
            ),
            "trajectory": None,
        }
    request = _effective_request(state)
    result = await runtime._call_lm(
        runtime.trade_planner,
        request=request,
        wallet_address=state["wallet_address"],
        conversation_history=state.get("history", ""),
    )
    update: dict = {"answer": result.answer, "trajectory": getattr(result, "trajectory", None)}
    if result.should_propose_swap:
        input_mint, output_mint, amount_atomic, slippage_bps = complete_swap_fields(
            request,
            state.get("history", ""),
            result.input_mint,
            result.output_mint,
            result.amount_atomic,
            result.slippage_bps,
        )
        values = [input_mint, output_mint, amount_atomic, slippage_bps]
        if all(value is not None for value in values):
            update["proposal"] = SwapProposal(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_atomic=amount_atomic,
                slippage_bps=slippage_bps,
                reason=result.proposal_reason or "User requested swap",
            )
        else:
            labels = ("input token", "output token", "amount", "maximum slippage")
            missing = [label for label, value in zip(labels, values) if value is None]
            update["answer"] = (
                "I can prepare this Solana swap once you provide "
                + ", ".join(missing)
                + ". No trade plan has been created yet."
            )
    return update


@trace(name="cross_chain_swap", as_type="agent")
async def cross_chain_swap_node(state: AgentState) -> dict:
    """Prepare a chat-native Relay quote without server-side signing."""
    chains = state.get("chains", [])
    values = extract_cross_chain_draft(state["request"], tuple(chains))
    required = ("source_chain", "destination_chain", "amount", "input_token", "output_token")
    # The latest message is authoritative; only typed active workflow fields
    # can fill an explicitly continuing request.
    has_named_subject = bool(values.get("input_token") or values.get("output_token"))
    elliptical_followup = bool(
        re.search(r"\bbuy\s+(?:it\s+)?for\b", state["request"], re.IGNORECASE)
    )
    starts_new_trade = bool(
        re.search(r"\b(?:swap|buy|sell|exchange|bridge|trade|convert|move)\b", state["request"], re.IGNORECASE)
        and has_named_subject
        and not is_trade_modifier(state["request"])
        and not elliptical_followup
    )
    active = (state.get("session_context") or {}).get("active_workflow") or {}
    if not starts_new_trade and active.get("intent") in {"trade", "cross_chain_swap"}:
        prior = {
            "source_chain": active.get("source_chain"),
            "destination_chain": active.get("destination_chain"),
            "amount": active.get("amount"),
            "input_token": active.get("input_token"),
            "output_token": active.get("output_token"),
            "recipient": active.get("recipient"),
            "slippage_bps": active.get("max_slippage_bps", active.get("slippage_bps")),
        }
        values = {key: value if value is not None else prior.get(key) for key, value in values.items()}
    # A single explicit non-Solana chain means an intra-chain Relay swap.
    if values.get("source_chain") and not values.get("destination_chain"):
        values["destination_chain"] = values["source_chain"]

    missing = [key for key in required if values.get(key) is None]
    if missing:
        labels = {
            "source_chain": "the source chain",
            "destination_chain": "the destination chain",
            "amount": "the amount",
            "input_token": "the token you’re paying with",
            "output_token": "the token you want to receive",
        }
        requested = [labels[key] for key in missing]
        if len(requested) == 1:
            detail = requested[0]
        else:
            detail = ", ".join(requested[:-1]) + f", and {requested[-1]}"
        subject = ""
        if values.get("output_token") and re.search(r"\bbuy\b", state["request"], re.IGNORECASE):
            subject = f"I understand you want to buy {values['output_token']}. "
        return {
            "answer": (
                f"{subject}I need {detail} before I can prepare the swap. "
                "For example: `Swap 0.1 ETH on Base to USDC on Base with max 50 bps slippage`."
            ),
            "trajectory": None,
            "cross_chain_swap": None,
        }

    draft = CrossChainSwapDraft(**values)
    slippage = f" with max {draft.slippage_bps} bps slippage" if draft.slippage_bps is not None else ""
    return {
        "answer": (
            f"I’m preparing a Relay quote to swap {draft.amount} {draft.input_token} on "
            f"{draft.source_chain.title()} to {draft.output_token} on "
            f"{draft.destination_chain.title()}{slippage}. Review the live quote below; "
            "nothing will be signed or submitted until you confirm."
        ),
        "trajectory": None,
        "cross_chain_swap": draft,
    }


def risk_check_node(state: AgentState) -> dict:
    """Deterministic guardrail: DSPy never gets to bypass the slippage ceiling."""
    proposal = state.get("proposal")
    if proposal is None:
        return {}
    if proposal.slippage_bps > settings.max_slippage_bps:
        return {
            "proposal": None,
            "error": f"Requested slippage exceeds the configured maximum of {settings.max_slippage_bps} bps",
        }
    return {}


def route_after_risk_check(state: AgentState) -> str:
    return "quote" if state.get("proposal") is not None else "finalize"


@trace(name="quote_and_simulate")
async def quote_and_simulate_node(state: AgentState) -> dict:
    """Deterministic: token resolution, notional/impact limits, quote, and simulation."""
    proposal = state["proposal"]
    if state.get("execution_provider") != "jupiter":
        return {"error": "A resolved Jupiter route is required for a Solana trade plan."}
    try:
        plan = await create_trade_plan(state["wallet_address"], proposal)
        return {"trade_plan": plan}
    except ValueError as exc:
        detail = str(exc).strip()
        if len(detail) > 400 or "<!DOCTYPE" in detail or "jsonrpc" in detail.lower():
            detail = "The trade provider could not prepare a safe quote. Please retry shortly."
        return {"error": detail}


def finalize_trade_node(state: AgentState) -> dict:
    if state.get("trade_plan"):
        plan = state["trade_plan"]
        return {
            "answer": (
                "A reviewable trade plan has been prepared. Check the token addresses, amount, "
                "estimated output, warnings, price impact, and successful simulation in the card. "
                "Nothing has been submitted. To execute it, confirm with the exact text: "
                f"`{plan.confirmation_text}`."
            )
        }
    if state.get("error"):
        return {"answer": (state.get("answer") or "") + f" Trade plan not created: {state['error']}"}
    return {}
