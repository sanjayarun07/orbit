from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import asyncio
import re
from datetime import datetime
from functools import lru_cache
import dspy
from app.agent import search_verified_tokens, token_safety_warnings
from app.capability_router import extract_chains
from app.market_brief import crypto_market_brief
from app.perplexity_tools import PERPLEXITY_FUNCTIONS, perplexity_available
from app.provider_registry import get_provider_router
from app.source_cards import extract_source_cards
from app.web_search import append_web_sources, is_crypto_trends_query, web_search

@lru_cache(maxsize=64)
def _research_agent_for(tool_names: tuple[str, ...]):
    selected = []
    available = {tool.__name__: tool for tool in runtime._mcp_registry.all()}
    for name in tool_names:
        if name in available:
            selected.append(available[name])
    return dspy.ReAct(
        runtime.ResearchAnswer,
        tools=[
            search_verified_tokens,
            token_safety_warnings,
            web_search,
            *(PERPLEXITY_FUNCTIONS if perplexity_available() else ()),
            *selected,
        ],
        max_iters=4,
    )


def _research_agent(request: str, capabilities: tuple[str, ...] = (), chains: tuple[str, ...] = ()):
    selected = runtime._mcp_registry.select(request, capabilities=capabilities, chains=chains)
    return _research_agent_for(tuple(tool.__name__ for tool in selected))


_TOKEN_ADDRESS = re.compile(
    r"(?<![0-9a-fA-F])(0x[0-9a-fA-F]{40})(?![0-9a-fA-F])"
    r"|(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])"
)


def _portfolio_field(observation: str, label: str) -> str | None:
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*([^\n]+)", observation, re.IGNORECASE)
    return match.group(1).strip() if match else None


def wallet_portfolio_answer(wallet: str, observation: str) -> str:
    """Build a concise factual summary without a second model invocation."""
    profile = f"https://app.nansen.ai/profiler?address={wallet.lower()}"
    answer = f"Wallet [{wallet}]({profile}) has current portfolio data shown below."
    net_value = _portfolio_field(observation, "Net Value (USD)")
    assets = _portfolio_field(observation, "Total Assets (USD)")
    debts = _portfolio_field(observation, "Total Debts (USD)")
    account_value = _portfolio_field(observation, "Account Value (USD)")
    notional = _portfolio_field(observation, "Total Notional (USD)")
    if net_value and assets and debts:
        answer += (
            f" DeFi net value is **${net_value}**, with **${assets} in assets** and "
            f"**${debts} in debt**, so debt materially offsets the supplied assets."
        )
    if account_value and notional:
        answer += (
            f" The Hyperliquid account value is **${account_value}** against "
            f"**${notional} total notional**, indicating substantial leveraged exposure."
        )
    if "## Perp Positions" in observation:
        answer += " Review leverage, liquidation levels, funding, and unrealized PnL in the position card."
    return answer


def _direct_provider_capability(request: str, capabilities: set[str], chains: tuple[str, ...]) -> str | None:
    """Choose the provider-neutral capability; the router chooses the provider."""
    for capability in (
        "url_fetch",
        "project_intelligence",
        "vc_intelligence",
        "people_intelligence",
        "equity_research",
        "finance_data",
        "token_security",
        "token_discovery",
        "market_data",
        "wallet_intelligence",
        "defi_data",
        "listing_events",
        "web_research",
    ):
        if capability not in capabilities:
            continue
        if capability in {"market_data", "token_discovery", "token_security"} and not (
            is_crypto_trends_query(request)
            or chains
            or re.search(
                r"\b(?:token|coin|pair|dex|liquidity|volume|gainers?|new|launch|"
                r"contract|mint|holders?|rug|scam|honeypot|audit|safe(?:ty)?|"
                r"security|sellability|meme(?:coin)?)\b",
                request,
                re.IGNORECASE,
            )
        ):
            continue
        if get_provider_router().candidates(request, capability, chains):
            return capability
    return None


def _provider_trajectory(result, request: str, capability: str) -> dict:
    trajectory: dict = {}
    failures = list(result.failures)
    failed_names = list(result.attempted[:-1]) if result.attempted else []
    index = 0
    for name, failure in zip(failed_names, failures):
        trajectory[f"thought_{index}"] = f"Try {name} for the {capability} capability."
        trajectory[f"tool_name_{index}"] = name
        trajectory[f"tool_args_{index}"] = {"request": request, "capability": capability}
        trajectory[f"observation_{index}"] = f"Provider tool call failed: {failure}"
        index += 1
    trajectory[f"thought_{index}"] = f"Use the best healthy provider available for {capability}."
    trajectory[f"tool_name_{index}"] = result.tool
    trajectory[f"tool_args_{index}"] = {"request": request, "capability": capability}
    trajectory[f"observation_{index}"] = result.output
    trajectory[f"tool_sources_{index}"] = extract_source_cards(result.output)
    return trajectory


async def _equity_research(state: dict) -> dict:
    """Retrieve complementary Perplexity evidence concurrently, then synthesize it."""
    request = state["request"]
    finance_request = (
        f"Equity financial-data research for this user request: {request}\n\n"
        "Identify the instrument unambiguously. Retrieve the latest available price and "
        "timestamp, session move, market cap, valuation, 52-week range, recent reported "
        "earnings and guidance, next earnings date, and consensus estimates or targets "
        "when available. Label delayed data and estimate periods precisely."
    )
    news_request = (
        f"Equity catalyst and news research for this user request: {request}\n\n"
        "Find the most recent company-specific and sector news that plausibly explains "
        "the move. Include event dates, publication dates, primary company or regulatory "
        "sources where possible, and upcoming catalysts. Exclude loosely related crypto "
        "stories unless they materially affect the listed equity."
    )
    router = get_provider_router()

    async def retrieve(prompt: str, capability: str):
        return await asyncio.to_thread(
            router.route,
            prompt,
            capability,
            (),
            provider="perplexity",
        )

    finance, news = await asyncio.gather(
        retrieve(finance_request, "finance_data"),
        retrieve(news_request, "web_research"),
        return_exceptions=True,
    )
    trajectory: dict = {}
    evidence: list[str] = []
    tasks = (
        ("financials, earnings, and market data", finance_request, "finance_data", finance),
        ("recent news and catalysts", news_request, "web_research", news),
    )
    for index, (label, prompt, capability, result) in enumerate(tasks):
        trajectory[f"thought_{index}"] = f"Retrieve current {label} from Perplexity."
        trajectory[f"tool_args_{index}"] = {"query": prompt}
        if isinstance(result, Exception):
            trajectory[f"tool_name_{index}"] = (
                "perplexity_finance_search" if capability == "finance_data" else "perplexity_web_search"
            )
            trajectory[f"observation_{index}"] = f"Provider tool call failed: {result}"
            trajectory[f"tool_sources_{index}"] = []
            evidence.append(f"{label}: unavailable ({result})")
        else:
            trajectory[f"tool_name_{index}"] = result.tool
            trajectory[f"observation_{index}"] = result.output
            trajectory[f"tool_sources_{index}"] = extract_source_cards(result.output)
            evidence.append(f"{label}:\n{result.output}")

    if all(isinstance(result, Exception) for result in (finance, news)):
        return {
            "answer": (
                "Perplexity could not retrieve the financial or news evidence needed for "
                "this equity analysis. Check its quota and health in the provider dashboard, "
                "then retry."
            ),
            "trajectory": trajectory,
        }

    synthesis = await runtime._call_lm(
        runtime.equity_research_synthesizer,
        request=request,
        conversation_history=state.get("history", ""),
        as_of_date=datetime.now().astimezone().isoformat(timespec="minutes"),
        financial_evidence=evidence[0],
        news_evidence=evidence[1],
    )
    return {
        "answer": append_web_sources(synthesis.answer, trajectory),
        "trajectory": trajectory,
    }


_WALLET_ACTIVITY_WORDS = re.compile(r"\b(?:transactions?|activity|history|transfers?)\b", re.IGNORECASE)
_WALLET_LABEL_WORDS = re.compile(r"\b(?:labels?|identity|entity)\b", re.IGNORECASE)


def _nansen_wallet_tool_call(address: str, chain: str | None, request: str) -> tuple[str, dict] | None:
    """Pick the best-fit Nansen tool for a wallet-intelligence request and build
    its exact argument shape (schemas differ: address_portfolio takes
    walletAddress; address_transactions/address_labels take address).

    Nansen is the primary wallet-intelligence and on-chain-intelligence source.
    Callers fall back to the capability-router providers (GoldRush/Helius) only
    when this returns None (Nansen not discovered) or the call itself fails.
    """
    if _WALLET_ACTIVITY_WORDS.search(request):
        tool = runtime._mcp_registry.get("address_transactions")
        if tool is not None:
            payload = {"address": address}
            if chain:
                payload["chain"] = chain
            return tool.__name__, {"request": payload}
    if _WALLET_LABEL_WORDS.search(request):
        tool = runtime._mcp_registry.get("address_labels")
        if tool is not None:
            payload = {"address": address}
            if chain:
                payload["chain"] = chain
            return tool.__name__, {"request": payload}
    tool = runtime._mcp_registry.get("address_portfolio")
    if tool is not None:
        payload = {"walletAddress": address, "mode": "all"}
        if chain:
            payload["chain"] = chain
        return tool.__name__, {"request": payload}
    return None


def direct_mcp_request(request: str) -> tuple[str, dict] | None:
    """Resolve common, unambiguous operations without a ReAct planning loop.

    Nansen is tried here, deterministically, before the generic capability
    router (GoldRush/Helius) or the ReAct research agent — see
    _nansen_wallet_tool_call.
    """
    address_match = _TOKEN_ADDRESS.search(request)  # matches EVM 0x… or Solana base58
    lowered = request.lower()
    wallet_lookup = re.search(
        r"\b(?:analy[sz]e|check|inspect|profile|portfolio|holding|holdings|balance|balances|"
        r"leverage|debt|liquidation|transactions?|activity|history|transfers?|labels?)\b",
        lowered,
    )
    token_subject = re.search(r"\b(?:token|coin|contract|mint|memecoin|meme coin|erc-?20)\b", lowered)
    explicit_wallet = re.search(r"\b(?:wallet|portfolio|balances?|pnl|transactions?|counterparties)\b", lowered)
    if address_match and wallet_lookup and (not token_subject or explicit_wallet):
        address = address_match.group(1) or address_match.group(2)
        if address.startswith("0x"):
            found_chains = extract_chains(request)
            chain = found_chains[0] if found_chains else None
        else:
            chain = "solana"
        resolved = _nansen_wallet_tool_call(address, chain, request)
        if resolved is not None:
            return resolved
    token_address = _TOKEN_ADDRESS.search(request)
    if token_address and re.search(r"\b(?:top\s+)?holders?\b", lowered):
        tool = runtime._mcp_registry.get("token_current_top_holders")
        if tool is not None:
            value = token_address.group(1) or token_address.group(2)
            chains = extract_chains(request)
            chain = chains[0] if chains else ("solana" if not value.startswith("0x") else "ethereum")
            return tool.__name__, {
                "request": {
                    "chain": chain,
                    "tokenAddress": value,
                    "mode": "onchain_tokens",
                    "labelType": "top_100_holders",
                    "orderBy": "holding_size",
                    "order_by_direction": "DESC",
                    "page": 1,
                }
            }
    # Narrowly scoped to Nansen-specific vocabulary ("quant score", "nansen
    # score") or explicit token-info phrasing. Generic "tell me about TOKEN"
    # stays on the free, always-fresh DexScreener/CoinGecko path below — this
    # only intercepts requests that specifically want Nansen's proprietary
    # on-chain scoring, which no other configured provider offers at all.
    if token_address and re.search(
        r"\b(?:quant\s+score|nansen\s+score|token\s+(?:info|overview|details))\b", lowered
    ):
        value = token_address.group(1) or token_address.group(2)
        chains = extract_chains(request)
        chain = chains[0] if chains else ("solana" if not value.startswith("0x") else "ethereum")
        tool_name = "token_quant_scores" if re.search(r"\b(?:quant|nansen)\s+score\b", lowered) else "token_info"
        tool = runtime._mcp_registry.get(tool_name)
        if tool is not None:
            return tool.__name__, {"request": {"chain": chain, "tokenAddress": value}}
    return None
# A deterministic fast-path call exists to be fast: if the direct tool is
# slow or failing, the user should fall back to the next tier in a few
# seconds, not wait out the same ceiling (mcp_call_timeout_seconds, 45s by
# default) used for a full ReAct-mediated MCP call. asyncio.wait_for can't
# truly cancel the underlying thread call, but it stops the request from
# blocking on it — the MCP result cache still benefits from whatever
# completes in the background.
_FAST_PATH_TIMEOUT_SECONDS = 8.0


async def call_direct_mcp_tool(tool_name: str, arguments: dict) -> str:
    tool = next(item for item in runtime._mcp_registry.all() if item.__name__ == tool_name)
    try:
        return await asyncio.wait_for(asyncio.to_thread(tool, **arguments), timeout=_FAST_PATH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return "MCP tool call failed: fast-path lookup exceeded its short timeout"


@trace(name="research", as_type="agent")
async def research_node(state: AgentState) -> dict:
    request = _effective_request(state)
    direct = direct_mcp_request(request)
    if direct is not None:
        tool_name, arguments = direct
        observation = await call_direct_mcp_tool(tool_name, arguments)
        trajectory = {
            "thought_0": "The request maps directly to a known provider lookup.",
            "tool_name_0": tool_name,
            "tool_args_0": arguments,
            "observation_0": observation,
        }
        if not observation.startswith("MCP tool call failed:"):
            # Only the wallet-portfolio direct tool carries a walletAddress;
            # other direct tools (e.g. top-holders) return an already-formatted
            # observation that should be returned as-is.
            wallet = arguments["request"].get("walletAddress")
            answer = wallet_portfolio_answer(wallet, observation) if wallet else observation
            return {"answer": answer, "trajectory": trajectory}
        # Preserve the failed direct attempt in the normal agent's activity.
    capabilities = set(state.get("capabilities", []))
    chains = tuple(state.get("chains", []))
    if "equity_research" in capabilities:
        return await _equity_research(state)
    direct_capability = _direct_provider_capability(request, capabilities, chains)
    if is_crypto_trends_query(state["request"]):
        # The hosted search tool already returns a grounded, cited response.
        # Returning it directly avoids a second model pass changing dates,
        # prices, or citations and avoids unrelated MCP attempts.
        arguments = {"query": state["request"]}
        observation = await asyncio.to_thread(crypto_market_brief, **arguments)
        return {
            "answer": observation,
            "trajectory": {
                "thought_0": "This request requires current information from the live web.",
                "tool_name_0": "crypto_market_brief",
                "tool_args_0": arguments,
                "observation_0": observation,
            },
        }
    if direct_capability is not None:
        result = await asyncio.to_thread(
            get_provider_router().route,
            request,
            direct_capability,
            chains,
        )
        return {
            "answer": result.output,
            "trajectory": _provider_trajectory(result, request, direct_capability),
        }
    result = await runtime._call_lm(
        _research_agent(
            request,
            tuple(state.get("capabilities", [])),
            tuple(state.get("chains", [])),
        ),
        request=request,
        conversation_history="",
    )
    trajectory = getattr(result, "trajectory", None)
    return {"answer": append_web_sources(result.answer, trajectory), "trajectory": trajectory}
