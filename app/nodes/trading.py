from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import asyncio
import logging
import re

import httpx

from app.capability_router import extract_cross_chain_draft, is_trade_modifier
from app.routing.speech import bare_number_bps
from app.nodes.research import _sanitize_react_answer
from app.models import CrossChainSwapDraft, RiskAssessment, SwapProposal, TradePlan
from app.plans import create_trade_plan, mark_plan_superseded
from app.portfolio import build_portfolio_snapshot
from app import deployment
from app.settings import settings
from app.trade_context import complete_swap_fields

logger = logging.getLogger(__name__)

def _research_mode_answer(draft_text: str) -> dict:
    """A swap request in research mode. The HTTP routes refuse to move funds
    and the browser card said so -- but this node still wrote "I'm preparing
    a quote", locked the intent and warned about destination gas above that
    card, promising in one breath what it refused in the next. The server
    knows the mode before it writes a word, so it says so first and offers
    what this deployment can do."""
    return {
        "answer": (
            f"This deployment is running in research mode, so I can't prepare or sign swaps -- including {draft_text}. "
            "No quote was prepared and nothing was locked. I can still research the route: ask for the tokens' prices, "
            "liquidity and safety, or the bridges and fees between the two chains, and you can execute elsewhere."
        ),
        "trajectory": None,
        "cross_chain_swap": None,
    }


_NO_WALLET_ANSWER = (
    "I need a connected wallet to prepare a swap. Connect one with **Connect wallet** "
    "(or enter a public address), then reply `connected` and I'll pick this swap right back up."
)

@trace(name="trade_planner", as_type="agent")
async def trade_planner_node(state: AgentState) -> dict:
    if not deployment.execution_enabled():
        return _research_mode_answer("this one")
    if not state.get("wallet_address"):
        return {"answer": _NO_WALLET_ANSWER, "trajectory": None, "pending_wallet_request": state["request"]}
    if state.get("execution_provider") is None and "chain" in state.get("missing_fields", []):
        draft = extract_cross_chain_draft(state["request"], tuple(state.get("chains", [])))
        output_token = draft.get("output_token")
        subject = output_token or "this asset"
        # An unrecognized symbol is not necessarily crypto -- a real equity
        # ticker outside the curated registry (app/routing/instruments.json)
        # reaches this same branch. Never presuppose the domain: ask, and
        # point toward research instead of assuming a token/chain exists.
        research_hint = (
            f' If {subject} is a stock or other equity, I can research it -- try "research {subject}" -- '
            "but I can't place equity trades."
            if output_token else ""
        )
        return {
            "answer": (
                f"I need more information before I can continue with {subject}. If it's a crypto token, "
                "tell me which chain it's on (Solana, Base, Ethereum, etc.) so I can pick the right "
                f"execution provider -- Solana swaps use Jupiter, other chains and cross-chain swaps use "
                f"Relay.{research_hint} No quote or transaction has been created."
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
    update: dict = {"answer": _sanitize_react_answer(result.answer), "trajectory": getattr(result, "trajectory", None)}
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
    if not deployment.execution_enabled():
        values = extract_cross_chain_draft(state["request"], tuple(state.get("chains", [])))
        what = " ".join(str(values.get(k)) for k in ("amount", "input_token") if values.get(k)) or "this swap"
        return _research_mode_answer(f"{what} to {values.get('output_token')} on {values.get('destination_chain')}" if values.get("output_token") else what)
    if not state.get("wallet_address"):
        # trade_planner_node (the Solana/Jupiter path) has always gated on
        # this; this path did not, so a request could reach "I'm preparing a
        # Relay quote... nothing will be signed until you confirm" with no
        # wallet connected at all. The frontend's getFreshQuote() would still
        # refuse to sign, but only after implying a swap was already underway
        # -- and on some setups a bare EVM provider present-but-unconnected
        # would auto-prompt eth_requestAccounts as a side effect of loading
        # the quote, bypassing the app's own connect flow. Gate here instead,
        # before any draft or quote is prepared.
        return {
            "answer": _NO_WALLET_ANSWER,
            "trajectory": None,
            "cross_chain_swap": None,
            "pending_wallet_request": state["request"],
        }
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
        # "what slippage?" answered with just a number: slippage, in basis
        # points (a percent converts) -- only while the amount is known, so a
        # bare number can never be mistaken for one.
        if values.get("slippage_bps") is None and prior.get("amount") is not None:
            bare = bare_number_bps(state["request"])
            if bare is not None:
                values["slippage_bps"] = bare
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
    # The same charter gate the Jupiter path applies, on the fields a draft can
    # be checked against (chains, slippage, verified-only). Notional is checked
    # by the browser against the live Relay quote before anything is signed.
    fields = (state.get("session_context") or {}).get("risk_charter_fields") or {}
    if (state.get("session_context") or {}).get("risk_charter") and fields:
        violations, unresolved = _charter_draft_violations(fields, draft)
        if violations or unresolved:
            detail = "; ".join(violations + [f"could not check {u}" for u in unresolved]) + "."
            return {
                "answer": f"🚫 Blocked by your risk charter: {detail} No quote was prepared; adjust the trade or your charter and try again.",
                "trajectory": None,
                "cross_chain_swap": None,
                "risk_assessment": RiskAssessment(verdict="blocked", summary=detail, charter_applied=True),
            }
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
    except httpx.HTTPError as exc:
        # A provider outage or rate limit (Jupiter 429 on /swap) is a "try again"
        # reply, never a 500: nothing was signed or submitted.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return {"error": "Jupiter is rate-limiting quotes right now. Nothing was submitted; please try again in a moment."}
        return {"error": f"The trade provider is unavailable ({status or exc.__class__.__name__}). Nothing was submitted; please try again shortly."}


def _trade_summary(plan: TradePlan) -> str:
    """One-line factual summary of the quoted plan for the Risk agent."""
    input_token, output_token = plan.input_token, plan.output_token
    amount = plan.proposal.amount_atomic / (10 ** input_token.decimals)
    impact = float((plan.quote or {}).get("priceImpactPct") or 0) * 100
    notional = f"${plan.input_value_usd:.2f}" if plan.input_value_usd is not None else "unknown"
    warnings = "; ".join(plan.warnings) if plan.warnings else "none"
    return (
        f"Swap {amount:g} {input_token.symbol} -> {output_token.symbol}. "
        f"USD notional: {notional}. Price impact: {impact:.2f}%. "
        f"Max slippage: {plan.proposal.slippage_bps} bps. "
        f"Output token verified by Jupiter: {output_token.verified}. "
        f"Security warnings: {warnings}."
    )


def _charter_draft_violations(fields: dict, draft: CrossChainSwapDraft) -> tuple[list[str], list[str]]:
    """The charter against a Relay draft (no quote yet): chains, slippage and
    the verified-only rule. Returns (violations, unresolved)."""
    violations: list[str] = []
    unresolved: list[str] = []
    chains = [c.lower() for c in (fields.get("allowed_chains") or [])]
    if chains:
        for label, chain in (("source", draft.source_chain), ("destination", draft.destination_chain)):
            if chain and chain.lower() not in chains:
                violations.append(f"the {label} chain is {chain} but your charter allows only " + ", ".join(chains))
    bps = fields.get("max_slippage_bps")
    if bps is not None:
        if draft.slippage_bps is None:
            unresolved.append(f"slippage (none was requested; your max is {bps} bps, so state one at or below it)")
        elif draft.slippage_bps > bps:
            violations.append(f"slippage is {draft.slippage_bps} bps but your max is {bps} bps")
    if fields.get("verified_only"):
        # Relay tokens are not in Jupiter's verified registry; the rule cannot pass here.
        violations.append("your charter allows only Jupiter-verified tokens, which cannot be confirmed for a cross-chain swap")
    return violations, unresolved


def _charter_field_violations(fields: dict, plan, total_usd: float | None, holdings: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """Deterministic checks of the structured charter against the real quote.
    Returns (violations, unresolved). A rule that cannot be evaluated -- no
    USD notional, no portfolio value -- is reported as unresolved rather than
    silently passed: the charter can only make things stricter, never guess.
    The position limit is post-trade: what you already hold of the output
    token plus this trade, as a share of the portfolio."""
    violations: list[str] = []
    unresolved: list[str] = []
    notional = plan.input_value_usd
    cap = fields.get("max_trade_usd")
    if cap is not None:
        if notional is None:
            unresolved.append(f"max per trade (${cap:,.2f}): the trade's USD value is unknown")
        elif notional > cap:
            violations.append(f"the trade is ${notional:,.2f} but your max per trade is ${cap:,.2f}")
    pct = fields.get("max_position_pct")
    if pct is not None:
        if notional is None or not total_usd:
            unresolved.append(f"max position {pct:g}%: your portfolio value is unavailable right now")
        else:
            existing = 0.0
            for holding in holdings or []:
                if holding.get("mint") == plan.output_token.mint and holding.get("usd_value") is not None:
                    existing = float(holding["usd_value"])
                    break
            share = (existing + notional) / total_usd * 100
            if share > pct:
                held = f" (you already hold ${existing:,.2f} of {plan.output_token.symbol})" if existing else ""
                violations.append(f"after this trade {plan.output_token.symbol} would be {share:.1f}% of your ${total_usd:,.2f} portfolio{held} but your max per position is {pct:g}%")
    bps = fields.get("max_slippage_bps")
    if bps is not None and plan.proposal.slippage_bps > bps:
        violations.append(f"slippage is {plan.proposal.slippage_bps} bps but your max is {bps} bps")
    if fields.get("verified_only") and not plan.output_token.verified:
        violations.append(f"{plan.output_token.symbol} is not Jupiter-verified and your charter allows only verified tokens")
    chains = fields.get("allowed_chains") or []
    if chains and "solana" not in chains:
        violations.append("this is a Solana swap but your charter allows only " + ", ".join(chains))
    return violations, unresolved


@trace(name="charter_risk", as_type="agent")
async def charter_risk_node(state: AgentState) -> dict:
    """Soft, user-configurable Risk gate between the quote and the CONFIRM card.

    Runs AFTER the deterministic caps (create_trade_plan) so it has the real
    quote, and BEFORE the card. Advisory when no charter is set (annotate,
    never block); a hard veto against a set charter (supersede the plan so its
    CONFIRM token can't execute, suppress the card).
    """
    plan = state.get("trade_plan")
    if plan is None:
        return {}
    context = state.get("session_context") or {}
    charter = (context.get("risk_charter") or "").strip()
    fields = context.get("risk_charter_fields") or {}

    portfolio_context = "unknown"
    total_usd: float | None = None
    holdings: list[dict] = []
    try:
        # Bounded: a slow snapshot leaves the "% of portfolio" rule unresolved
        # (reported as such, never silently passed) rather than timing out the quote.
        snapshot = await asyncio.wait_for(build_portfolio_snapshot(state["wallet_address"]), timeout=settings.risk_snapshot_timeout_seconds)
        total = snapshot.get("total_usd_value")
        holdings = list(snapshot.get("holdings") or [])
        if total is not None:
            total_usd = float(total)
            portfolio_context = f"Total wallet value: ${total_usd:.2f}" + (" (partial snapshot)" if snapshot.get("partial") else "")
    except Exception:
        logger.warning("charter_risk: portfolio snapshot unavailable", exc_info=True)

    if charter and fields:
        violations, unresolved = _charter_field_violations(fields, plan, total_usd, holdings)
        if violations or unresolved:
            # A rule the charter set but that cannot be evaluated blocks too: the
            # user asked for a limit, and "unknown" is not "within the limit".
            await mark_plan_superseded(plan.plan_id)
            detail = "; ".join(violations + [f"could not check {u}" for u in unresolved]) + "."
            what = "Blocked by your risk charter" if violations else "Your risk charter could not be verified"
            hint = "adjust the trade or your charter and try again" if violations else "try again in a moment, or relax that rule"
            return {
                "trade_plan": None,
                "answer": f"🚫 {what}: {detail} No confirmation card was created; {hint}.",
                "risk_assessment": RiskAssessment(verdict="blocked" if violations else "unresolved", summary=detail, charter_applied=True),
            }
        if not fields.get("notes"):
            # Every rule was checked exactly; nothing is left for the model to interpret.
            checked = [k for k in ("max_trade_usd", "max_position_pct", "max_slippage_bps", "verified_only", "allowed_chains") if fields.get(k)]
            return {"risk_assessment": RiskAssessment(
                verdict="ok", summary="Within your risk rules (checked: " + ", ".join(c.replace("_", " ") for c in checked) + ").",
                charter_applied=True)}

    try:
        result = await runtime._call_lm(
            runtime.risk_agent,
            charter=charter,
            trade_summary=_trade_summary(plan),
            portfolio_context=portfolio_context,
        )
        verdict = "blocked" if str(getattr(result, "verdict", "ok")).strip().lower() == "blocked" else "ok"
        summary = (getattr(result, "summary", "") or "").strip()
        blocked_reason = (getattr(result, "blocked_reason", "") or "").strip()
    except Exception:
        # Never let a Risk-agent failure decide a trade either way: fall back to
        # advisory (the deterministic caps already passed to reach here).
        logger.warning("charter_risk: risk agent failed; passing through advisory", exc_info=True)
        return {"risk_assessment": RiskAssessment(verdict="ok", summary="Risk check unavailable.", charter_applied=bool(charter))}

    # A set charter can only make things STRICTER: without one, never block.
    if charter and verdict == "blocked":
        await mark_plan_superseded(plan.plan_id)
        detail = blocked_reason or summary or "A rule in your risk charter was not met."
        return {
            "trade_plan": None,
            "answer": f"🚫 Blocked by your risk charter: {detail} No confirmation card was created; adjust the trade or your charter and try again.",
            "risk_assessment": RiskAssessment(verdict="blocked", summary=summary or detail, charter_applied=True),
        }
    return {"risk_assessment": RiskAssessment(verdict="ok", summary=summary or "Within your risk rules.", charter_applied=bool(charter))}


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
