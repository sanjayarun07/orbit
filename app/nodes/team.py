"""The multi-agent trading desk (team mode).

One Coordinator node that fans a request out to Market Research, Execution, and
Risk -- reusing the existing single-agent nodes as callable specialists -- and
folds their outputs into one answer (plus the reviewable CONFIRM card for a
trade that clears Risk). Enabled per session via `session_context["team_mode"]`;
routing to here happens in app/routing/resolver.py.
"""

import asyncio
import logging
import re

from app.agent import search_verified_tokens
from app.jupiter import WRAPPED_SOL_MINT
from app import streaming
from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.nodes.research import research_node, _TOKEN_ADDRESS
from app.token_resolve import clear_winner, token_candidates
from app.nodes.trading import (
    trade_planner_node,
    risk_check_node,
    quote_and_simulate_node,
    charter_risk_node,
    finalize_trade_node,
)
from app.answer_validator import _headline_metrics
from app.provider_registry import get_provider_router
from app.tracing import trace

logger = logging.getLogger(__name__)

# Extract the asset the desk should research: the token being acquired/analyzed.
_ASSET = re.compile(
    r"\b(?:buy|long|acquire|get|ape\s+into|analy[sz]e|research|about|to|into)\s+(?:some\s+|more\s+|the\s+|a\s+)?\$?([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\bthoughts?\s+on\s+\$?([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\bhow(?:'s| is| about)\s+\$?([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\$([A-Za-z][A-Za-z0-9]{1,9})\b",
    re.IGNORECASE,
)
_STABLES = {"USDC", "USDT", "DAI", "USD", "USDE", "PYUSD", "FDUSD", "USDS", "USDBC"}
_ASSET_STOP = {"SOME", "MORE", "THE", "A", "AN", "IT", "THIS", "THAT", "ME", "MY", "TOKEN", "COIN", "CRYPTO", "NOW"}


async def _resolve_research_asset(request: str, chains: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """Resolve the request's primary asset to a concrete (address, chain) so the
    providers have a real lookup target (they key off addresses). Chain-agnostic
    via DEX Screener, with a Jupiter/Solana fallback. Returns (address, chain) or
    (None, None). Best-effort and read-only: a miss -- including a genuinely
    ambiguous cross-chain symbol -- falls back to the framed research routing,
    which asks the user rather than guessing."""
    address = _TOKEN_ADDRESS.search(request)
    if address:
        return (address.group(1) or address.group(2)), None
    tickers = []
    for match in _ASSET.finditer(request):
        ticker = next((g for g in match.groups() if g), "").upper()
        if ticker and ticker not in _ASSET_STOP and ticker not in tickers:
            tickers.append(ticker)
    # The research target is a non-stablecoin (a swap into USDC has nothing
    # worth a thesis); take the last such ticker mentioned.
    ticker = next((t for t in reversed(tickers) if t not in _STABLES), None)
    if not ticker:
        return None, None
    if ticker in {"SOL", "WSOL"}:
        return WRAPPED_SOL_MINT, "solana"
    # Verified Solana canonical first (authoritative, immune to DEX Screener's
    # same-ticker pollution) -- mirrors the research resolver, and is what lets a
    # common Solana token like BONK resolve here instead of deferring on a noisy
    # DEX Screener result. Non-verified tickers fall through to DEX Screener.
    if not chains or "solana" in {c.lower() for c in chains}:
        try:
            verified = await asyncio.to_thread(search_verified_tokens, ticker)
        except Exception:
            verified = []
        exact = [m for m in verified if (m.get("symbol") or "").upper() == ticker and "verified" in (m.get("tags") or [])]
        if len(exact) == 1 and exact[0].get("mint"):
            return exact[0]["mint"], "solana"
    try:
        candidates = await asyncio.to_thread(token_candidates, ticker, chains)
    except Exception:
        candidates = []
    winner = clear_winner(candidates)
    if winner is not None:
        return winner["address"], winner["chain"]
    if candidates:
        # Comparable same-symbol tokens on different chains: don't guess here --
        # let the framed research routing surface the disambiguation question.
        return None, None
    try:
        matches = await asyncio.to_thread(search_verified_tokens, ticker)
    except Exception:
        return None, None
    exact = [m for m in matches if (m.get("symbol") or "").upper() == ticker and "verified" in (m.get("tags") or [])]
    chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
    return (chosen["mint"], "solana") if chosen and chosen.get("mint") else (None, None)


# Cap on distinct market-snapshot providers to query per desk turn. We stop as
# soon as two of them return comparable headline metrics (so the step-7 validator
# can cross-check price/liquidity/volume across sources), but never call more than
# this many -- the desk is already the costlier path.
_MAX_SNAPSHOT_PROVIDERS = 3


async def _asset_market_data(mint: str, chain: str) -> tuple[str, dict]:
    """Real market snapshot + holders for a resolved mint, via the router.

    The market snapshot is fetched from up to `_MAX_SNAPSHOT_PROVIDERS` DISTINCT
    providers (stopping once two return comparable headline metrics), so the
    validator's cross-source consistency check has independent same-token figures
    to compare -- providers legitimately differ on volume/liquidity by pool
    coverage. Returns (concatenated_text, trajectory) where the trajectory carries
    one `observation_i` per source (each tagged with the token subject so the
    consistency check can confirm they concern the same token)."""
    router = get_provider_router()
    parts: list[str] = []
    trajectory: dict = {}
    index = 0
    subject = f"**Token**: {mint} on {chain}"

    def _record(tool: str, output: str) -> None:
        nonlocal index
        parts.append(output)
        trajectory[f"tool_name_{index}"] = tool
        # Tag each observation with the resolved subject so the consistency
        # check can confirm the sources concern the same token (its own output
        # may name the token but not always the address).
        trajectory[f"observation_{index}"] = f"{subject}\n{output}"
        index += 1

    snapshot_query = f"price, liquidity and 24h volume for token {mint} on {chain}"
    snapshot_caps = ("market_data", "token_discovery")
    try:
        ranked = await asyncio.to_thread(router.candidates, snapshot_query, "market_data", (chain,))
    except Exception:
        ranked = []
        logger.warning("team: snapshot candidate lookup failed", exc_info=True)
    tried: list[str] = []
    comparable = 0
    for tool in ranked:
        if tool.provider in tried:
            continue
        tried.append(tool.provider)
        try:
            result = await asyncio.to_thread(
                router.try_route_across, snapshot_query, snapshot_caps, (chain,), provider=tool.provider
            )
        except Exception:
            logger.warning("team: snapshot fetch failed for %s", tool.provider, exc_info=True)
            result = None
        if result is not None:
            _record(result.tool, result.output)
            if _headline_metrics(result.output):
                comparable += 1
        if comparable >= 2 or len(tried) >= _MAX_SNAPSHOT_PROVIDERS:
            break

    try:
        holders = await asyncio.to_thread(
            router.try_route_across, f"top holders for token {mint} on {chain}",
            ("token_discovery", "token_security"), (chain,),
        )
        if holders is not None:
            _record(holders.tool, holders.output)
    except Exception:
        logger.warning("team: holders fetch failed", exc_info=True)

    return "\n\n".join(parts), trajectory


async def _market_research(state: AgentState, request: str) -> tuple[str, int | None, dict | None, dict | None]:
    """Fetch real data via the research stack, then synthesize a desk thesis.

    Returns (thesis, conviction, trajectory, pending_token); pending_token is set
    when the asset symbol is ambiguous across chains and the desk must ask.
    """
    market_data = ""
    trajectory = None
    pending_token = None

    # Resolve the asset to a concrete (address, chain) so the providers have a
    # real lookup target (they key off addresses, so a bare "should I buy SOL"
    # otherwise returns thin data). On a hit, pull a real snapshot + holders.
    mint, chain = await _resolve_research_asset(request, tuple(state.get("chains", [])))
    if mint:
        market_data, asset_trajectory = await _asset_market_data(mint, chain or "solana")
        # Surface the per-source snapshot observations so the step-7 validator can
        # cross-check the same token's metrics across providers (consistency).
        if asset_trajectory:
            trajectory = asset_trajectory

    if not market_data.strip():
        # Fallback: the framed query through the normal research routing (which
        # asks the user when the symbol is a genuinely ambiguous cross-chain one).
        research_state = dict(state)
        framed = f"Current price, market structure, liquidity, funding and holders for: {request}"
        research_state["request"] = framed
        research_state["contextual_request"] = framed
        research_state["capabilities"] = ["market_data", "token_discovery", "token_security"]
        try:
            update = await research_node(research_state)
            market_data = update.get("answer") or "No market data returned."
            trajectory = update.get("trajectory")
            pending_token = update.get("pending_token")
        except Exception:
            logger.warning("team: market research data fetch failed", exc_info=True)
            market_data = "Market data unavailable right now."
    if pending_token is not None:
        # A disambiguation question is not a thesis -- surface it verbatim and
        # skip synthesis so the desk asks cleanly.
        return market_data, None, trajectory, pending_token
    try:
        streaming.emit("status", text="Market Research is writing the thesis")
        result = await runtime._call_lm(runtime.market_research_agent, asset=request, market_data=market_data)
        thesis = (getattr(result, "thesis", "") or "").strip() or market_data
        conviction = getattr(result, "conviction", None)
    except Exception:
        logger.warning("team: market research synthesis failed; using raw data", exc_info=True)
        thesis, conviction = market_data, None
    return thesis, conviction, trajectory, None


async def _execution_and_risk(state: AgentState) -> tuple[str, object | None, object | None]:
    """Run the existing Execution -> Risk sub-pipeline as callable specialists."""
    work = dict(state)
    work.update(await trade_planner_node(work))
    work.update(risk_check_node(work))
    if work.get("proposal") is not None and not work.get("error"):
        work.update(await quote_and_simulate_node(work))
        work.update(await charter_risk_node(work))
    trade_plan = work.get("trade_plan")
    risk_assessment = work.get("risk_assessment")
    if trade_plan is not None:
        execution_answer = finalize_trade_node(work).get("answer") or (work.get("answer") or "")
    else:
        # Missing fields, a deterministic-cap/quote error, or a Risk block --
        # the node already wrote a user-facing message.
        execution_answer = work.get("answer") or "No executable order could be drafted."
    return execution_answer, trade_plan, risk_assessment


@trace(name="team", as_type="chain")
async def team_node(state: AgentState) -> dict:
    request = _effective_request(state)
    subintent = state.get("team_subintent") or "analysis"

    thesis, conviction, research_trajectory, pending_token = await _market_research(state, request)
    if pending_token is not None:
        # The desk can't research an ambiguous cross-chain symbol without knowing
        # the chain -- ask, and remember the candidates for the follow-up.
        return {"intent": "research", "answer": thesis, "pending_token": pending_token}

    execution_answer = "n/a"
    trade_plan = None
    risk_assessment = None
    if subintent == "trade":
        streaming.emit("status", text="Execution and Risk are drafting the order")
        execution_answer, trade_plan, risk_assessment = await _execution_and_risk(state)
        risk_line = (
            f"{risk_assessment.verdict}: {risk_assessment.summary}" if risk_assessment is not None else "not evaluated"
        )
    else:
        charter = (state.get("session_context") or {}).get("risk_charter")
        risk_line = f"User risk charter to respect when advising on sizing: {charter}" if charter else "No risk charter set (advisory)."

    try:
        streaming.emit("status", text="The Coordinator is folding the desk's answers together")
        synth = await runtime.answer(
            runtime.team_coordinator,
            request=request,
            market_research=thesis,
            execution=execution_answer,
            risk=risk_line,
        )
        answer = (getattr(synth, "answer", "") or "").strip() or thesis
    except Exception:
        logger.warning("team: coordinator synthesis failed; returning thesis", exc_info=True)
        answer = thesis

    team_report = {
        "market_research": thesis,
        "conviction": conviction,
        "execution": execution_answer if subintent == "trade" else None,
        "risk": risk_line if subintent == "trade" else None,
        "subintent": subintent,
    }
    return {
        # Surface the effective intent so downstream (response schema, active-
        # workflow tracking, metrics) treats a desk trade as a trade and a desk
        # analysis as research. Routing into the team already happened on the
        # pre-override "team" intent.
        "intent": "trade" if subintent == "trade" else "research",
        "answer": answer,
        "trade_plan": trade_plan,
        "risk_assessment": risk_assessment,
        "trajectory": {"market_research": research_trajectory} if research_trajectory else None,
        "team_report": team_report,
    }
