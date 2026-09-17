import json
from app.nodes.state import AgentState, effective_request as _effective_request
from app.nodes import runtime
from app.tracing import trace
import asyncio
import logging
import re
import time
from datetime import datetime
from functools import lru_cache
import dspy
from app.agent import search_verified_tokens, token_safety_warnings
from app.jupiter import WRAPPED_SOL_MINT
from app.capability_router import extract_chains
from app.market_brief import crypto_market_brief
from app.market_overview import crypto_market_overview
from app import event_calendar, why_moving
from app.market_providers import TRENDING_TOKENS
from app.perplexity_tools import PERPLEXITY_FUNCTIONS, perplexity_available
from app.provider_registry import get_provider_router
from app.repeat_guard import guard_tools
from app.token_resolve import bitquery_evm_lookup, clear_winner, token_candidates
from app.token_deepdive import ANALYSIS_RULES, build_token_evidence, extract_market_price, format_evidence_bundle
from app import role_memory
from app.source_cards import extract_source_cards
from app.web_search import append_web_sources, is_crypto_trends_query, web_search

logger = logging.getLogger(__name__)

@lru_cache(maxsize=64)
def _research_agent_for(tool_names: tuple[str, ...]):
    selected = []
    available = {tool.__name__: tool for tool in runtime._mcp_registry.all()}
    for name in tool_names:
        if name in available:
            selected.append(available[name])
    return dspy.ReAct(
        runtime.ResearchAnswer,
        tools=guard_tools([
            search_verified_tokens,
            token_safety_warnings,
            web_search,
            *(PERPLEXITY_FUNCTIONS if perplexity_available() else ()),
            *selected,
        ]),
        max_iters=4,
    )


def _research_agent(
    request: str,
    capabilities: tuple[str, ...] = (),
    chains: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
):
    selected = runtime._mcp_registry.select(request, capabilities=capabilities, chains=chains)
    names = tuple(tool.__name__ for tool in selected if tool.__name__ not in exclude)
    return _research_agent_for(names)


_LEAKED_TOOL_CALL = re.compile(r"\bmcp_[a-zA-Z0-9_]+\s*\(|\(\s*request\s*=")


def _sanitize_react_answer(answer: str) -> str:
    """Defensive check: DSPy's ReAct agent has, on rare iteration-budget
    exhaustion, produced a final answer that leaks a raw unexecuted tool-call
    string instead of a synthesized response. Never surface that to a user.
    """
    if answer and _LEAKED_TOOL_CALL.search(answer):
        return (
            "I couldn't complete this lookup with a clean result. Please try "
            "rephrasing the request, or specify the exact chain and address."
        )
    return answer


_TOKEN_ADDRESS = re.compile(
    r"(?<![0-9a-fA-F])(0x[0-9a-fA-F]{40})(?![0-9a-fA-F])"
    r"|(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])"
)
# Mirrors app.additional_providers._GAINERS_LOSERS / _GAINERS_CATEGORY --
# used only to decide whether crypto_market_brief's broad trends intercept
# should defer to the dedicated gainers/losers tool below. Kept as a local
# constant rather than importing the private ones, matching how each
# provider module in this codebase owns its own chain-name set.
_GAINERS_LOSERS_REQUEST = re.compile(
    r"\bgainers?\b|\blosers?\b|\b(?:top|biggest|best|worst)\s+(?:movers?|performers?)\b|\bwinners?\b", re.IGNORECASE
)
_GAINERS_SUPPORTED_CHAINS = {"solana", "base", "ethereum", "arbitrum", "avalanche", "polygon", "sui", "bsc"}

# Broad "how's the market" asks -> the compact market-overview card (core quotes,
# Fear & Greed, dominance, movers, trending). Kept tight so it never hijacks a
# token-, chain-, or trending-tokens-specific request (those want ranked lists).
# A structured token due-diligence ask -> the multi-dimension deep-dive lens
# (evidence bundle + analysis synthesis). Distinct from a single-metric lookup
# ("holders of X", "price of X"), which stays on the direct tool path.
_DEEPDIVE = re.compile(
    r"\b(?:deep[\s-]*dive|due[\s-]*diligence|full\s+analysis|analy[sz]e|analysis\s+of|"
    r"thoughts?\s+on|is\s+\S+\s+a\s+good\s+(?:buy|investment)|should\s+i\s+(?:buy|invest\s+in))\b",
    re.IGNORECASE,
)
# The token named by an analysis ask -- symbol or $ticker.
_DEEPDIVE_TOKEN = re.compile(
    r"\b(?:deep[\s-]*dive\s+(?:on|into)|due[\s-]*diligence\s+(?:on|for)|analy[sz]e|analysis\s+of|"
    r"thoughts?\s+on|research(?:\s+on)?|should\s+i\s+(?:buy|invest\s+in)|is)\s+(?:the\s+)?\$?"
    r"([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\$([A-Za-z][A-Za-z0-9]{1,9})\b",
    re.IGNORECASE,
)

_MARKET_OVERVIEW = re.compile(
    r"\bmarket\s+(?:overview|summary|snapshot|recap|update|brief|dashboard)\b"
    r"|\b(?:crypto\s+)?markets?\s+(?:overview|summary|snapshot|today|right\s+now|recap|update)\b"
    r"|\bhow(?:'?s| is| are)\s+the\s+(?:crypto\s+)?markets?\b"
    r"|\bstate\s+of\s+the\s+(?:crypto\s+)?market\b"
    r"|\bwhat(?:'?s| is)\s+(?:happening|going\s+on)\s+(?:in|with)\s+(?:crypto|the\s+markets?)\b"
    r"|\bgive\s+me\s+(?:a\s+|the\s+)?(?:crypto\s+)?market\b"
    r"|\b(?:crypto\s+)?markets?\s+trends?\b"
    r"|\btrends?\s+(?:in|on|of|for)\s+(?:the\s+)?(?:crypto\s+)?markets?\b",
    re.IGNORECASE,
)


def _portfolio_field(observation: str, label: str) -> str | None:
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*([^\n]+)", observation, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _strip_dollar(value: str | None) -> str | None:
    return value.lstrip("$") if value else value


def _nonzero(value: str | None) -> bool:
    """True if an extracted markdown field looks like a nonzero amount.

    _portfolio_field returns raw text (e.g. "0", "2.5k", "-643.5"), and a
    literal "0" is a non-empty, truthy *string* -- checking it with a plain
    `if value:` treats an all-zero portfolio as if it had real assets/debt.
    """
    if not value:
        return False
    stripped = value.strip().lstrip("-").rstrip("kKmMbB").replace(",", "")
    try:
        return float(stripped) != 0
    except ValueError:
        return True  # not a plain number (e.g. "N/A") -- don't suppress on a guess


_FAILED_PORTFOLIO_SECTION = re.compile(r"^#\s+(.+?)\n\*\*Error retrieving[^*]*\*\*:\s*([^\n]+)", re.MULTILINE)
_INSUFFICIENT_CREDITS = re.compile(r"insufficient credits", re.IGNORECASE)


def _failed_portfolio_sections(observation: str) -> list[str]:
    """Nansen's address_portfolio response is multi-section (Token Holdings,
    DeFi Positions, Hyperliquid Positions, ...); any subset can fail
    independently (e.g. per-endpoint credit exhaustion) while the rest
    return real data. Detect the failed sections so the answer can disclose
    them, instead of silently reporting only what happened to succeed.
    """
    return [match.group(1).strip() for match in _FAILED_PORTFOLIO_SECTION.finditer(observation)]


_BALANCES_TOOLS = {"goldrush_wallet_balances", "bitquery_wallet_balances"}


async def _bitquery_balances_supplement(wallet: str, chain: str) -> str | None:
    """Best-effort live fallback for Nansen's Token Holdings section when it
    failed (credit exhaustion, outage, etc.).

    GoldRush (Covalent) is preferred over Bitquery when both are configured
    -- registered at a higher priority (10 vs 7) since it's a real indexed
    current-balance snapshot, not a realtime-window approximation (our
    Bitquery plan blocks the combined/archive datasets that would give a
    true current-state read; see app/market_providers.py's BitqueryProvider
    docstring). ProviderRouter's normal priority scoring picks whichever is
    actually available, so this function doesn't need to choose between
    them itself.

    Only spot balances can be substituted this way -- neither provider has
    an equivalent for Nansen's DeFi position aggregation or Hyperliquid
    perps data, so a failure in those sections stays disclosed rather than
    silently patched over. Deliberately avoids the words
    "wallet"/"transactions"/"activity"/"history" in the synthetic request
    text -- those would also match GoldRush/Helius's *transactions* tools,
    which return recent activity, not current balances.
    """
    request = f"Show token balances and holdings for {wallet} on {chain}"
    result = None
    for attempt in range(2):
        result = await asyncio.to_thread(get_provider_router().try_route, request, "wallet_intelligence", (chain,))
        if result is not None:
            break
        # Bitquery's own rate limiting (429s, and a 502 alongside them) was
        # verified live to hit even a single sequential request some of the
        # time -- when every candidate fails that way try_route returns None,
        # and one short-backoff retry recovers most of those without
        # meaningfully slowing down the rare, already-slow path this is on.
        # Harmless no-op overhead when GoldRush (which hasn't shown this
        # issue) is what actually served the request on the first attempt.
        if attempt == 0:
            await asyncio.sleep(0.75)
    if result is None:
        return None
    if result.tool not in _BALANCES_TOOLS:
        # A non-balances wallet_intelligence tool matched instead (e.g. a
        # differently-configured provider); its output shape isn't
        # guaranteed to be balances, so don't splice it in as if it were.
        return None
    return result.output


async def _goldrush_hyperliquid_supplement(wallet: str) -> str | None:
    """Hyperliquid perp + spot positions via GoldRush -- Hyperliquid
    accounts are EVM-addressed, so this only ever applies to 0x wallets.
    """
    if not wallet.startswith("0x"):
        return None
    request = f"Show hyperliquid perp positions and leverage for {wallet}"
    result = await asyncio.to_thread(get_provider_router().try_route, request, "wallet_intelligence", ())
    if result is None or result.tool != "goldrush_hyperliquid_positions":
        return None
    return result.output


async def _nansen_defi_positions(wallet: str, chain: str | None) -> str | None:
    """Nansen's address_portfolio, scoped to mode='defi' only.

    Token Holdings and Hyperliquid Positions are now sourced from GoldRush
    (see _compose_wallet_portfolio), so there's no reason to spend Nansen
    credits on those now-redundant sections -- mode='defi' is the tool's
    own documented option (seen live in its own response text: "Use mode
    parameter ('wallet_balances', 'defi', 'hyperliquid') to focus on
    specific position types"). This is also the one section nothing else
    in this codebase can provide, so a failure here is disclosed by the
    caller, never silently dropped.
    """
    tool = runtime._mcp_registry.get("address_portfolio")
    if tool is None:
        return None
    payload = {"walletAddress": wallet, "mode": "defi"}
    if chain:
        payload["chain"] = chain
    observation = await call_direct_mcp_tool(tool.__name__, {"request": payload})
    if observation.startswith("MCP tool call failed:"):
        return None
    return observation


async def _compose_wallet_portfolio(wallet: str, chain: str | None) -> tuple[str, dict]:
    """The primary wallet-portfolio path: balances + Hyperliquid from
    GoldRush (falling back to Bitquery for balances if GoldRush isn't
    configured), with Nansen's DeFi Positions as optional, non-blocking
    enrichment -- the one section nothing else here provides. Nansen
    credit exhaustion no longer breaks the core answer, since balances and
    Hyperliquid data don't depend on it at all.
    """
    is_evm = wallet.startswith("0x")
    resolved_chain = chain or ("solana" if not is_evm else None)
    if not is_evm:
        # A base58 wallet has exactly one possible chain -- no sweep needed.
        balances_coro = _bitquery_balances_supplement(wallet, "solana")
    elif chain:
        # An explicit chain was named (or resolved from context) -- respect
        # it rather than searching every chain that wasn't asked about.
        balances_coro = _bitquery_balances_supplement(wallet, chain)
    else:
        # No chain was named at all -- sweep supported EVM chains (primary
        # tier first) rather than guessing one, same as the standalone
        # Nansen-Token-Holdings-failure fallback used elsewhere.
        balances_coro = _bitquery_balances_supplement_all_chains(wallet)
    balances, hyperliquid, defi = await asyncio.gather(
        balances_coro,
        _goldrush_hyperliquid_supplement(wallet),
        _nansen_defi_positions(wallet, chain),
    )

    sections = []
    trajectory: dict = {
        "thought_0": "Compose a wallet portfolio from GoldRush balances/Hyperliquid, with Nansen DeFi Positions as optional enrichment.",
        "tool_args_0": {"wallet_address": wallet, "chain": resolved_chain},
    }
    index = 0
    if balances:
        sections.append(f"# Token Holdings\n\n{balances}")
        trajectory[f"tool_name_{index}"] = "goldrush_wallet_balances"
        trajectory[f"observation_{index}"] = balances
        index += 1
    else:
        sections.append(
            "# Token Holdings\n**Error retrieving token balances**: no configured balances "
            "provider (GoldRush, Bitquery) succeeded for this chain."
        )
    if hyperliquid:
        sections.append(hyperliquid)
        trajectory[f"tool_name_{index}"] = "goldrush_hyperliquid_positions"
        trajectory[f"observation_{index}"] = hyperliquid
        index += 1
    if defi:
        sections.append(defi)
        trajectory[f"tool_name_{index}"] = "mcp_nansen_address_portfolio"
        trajectory[f"observation_{index}"] = defi
        index += 1
    else:
        sections.append(
            "# DeFi Positions\n**Error retrieving DeFi positions**: Nansen is unavailable or "
            "out of API credits for this endpoint."
        )

    observation = "\n\n".join(sections)
    answer = wallet_portfolio_answer(wallet, observation, chain=resolved_chain)
    if balances:
        # wallet_portfolio_answer only extracts a handful of named summary
        # fields (Net Value, Account Value, ...) -- a raw balances table has
        # none of those, so without this it would never actually appear in
        # the visible answer (EXPOSE_TOOL_TRAJECTORY is off by default,
        # hiding the raw trajectory observation from the user). Matches the
        # explicit splice-into-answer behavior this exact scenario already
        # had before this refactor.
        answer += f"\n\n## Token Holdings\n\n{balances}"
    return answer, trajectory


# Every EVM chain the Bitquery balance adapter supports (mirrors
# BitqueryProvider._EVM_NETWORKS in app/market_providers.py). Split into a
# primary tier (most common EVM chains, plus Arbitrum where Hyperliquid
# itself settles) tried first, and a secondary tier only checked if the
# primary one found nothing -- cuts the common case from 6 calls to 3.
_BITQUERY_EVM_CHAINS_PRIMARY = ("ethereum", "base", "arbitrum")
_BITQUERY_EVM_CHAINS_SECONDARY = ("bsc", "polygon", "avalanche")
_BITQUERY_EVM_CHAINS = _BITQUERY_EVM_CHAINS_PRIMARY + _BITQUERY_EVM_CHAINS_SECONDARY


async def _sweep_bitquery_chains(wallet: str, chains: tuple[str, ...]) -> list[tuple[str, str | None]]:
    """Run one chain at a time: firing even 2 at once was verified live to
    still occasionally trip Bitquery's own rate limiting (429s and a 502 on
    up to half the chains when tried fully concurrent). This is an
    already-rare fallback path (only reached once Nansen has failed), so the
    extra few seconds of sequential latency is worth it -- a chain silently
    dropping out from a transient rate limit means reporting "no balances"
    on a chain that was never actually checked.
    """
    semaphore = asyncio.Semaphore(1)

    async def bounded(chain: str) -> str | None:
        async with semaphore:
            return await _bitquery_balances_supplement(wallet, chain)

    results = await asyncio.gather(*(bounded(chain) for chain in chains))
    return list(zip(chains, results))


def _positive_chain_results(pairs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    return [(chain, output) for chain, output in pairs if output and "No positive token balances" not in output]


async def _bitquery_balances_supplement_all_chains(wallet: str) -> str | None:
    """Query supported EVM chains when the user never named one, instead of
    guessing a single default.

    Defaulting to one chain (e.g. "ethereum") when none was specified risks
    a confident-looking "no balances found" that's only true for that one
    chain -- the wallet's real spot holdings can sit on any of them (Base,
    Arbitrum, etc.), and Hyperliquid itself settles through Arbitrum, so an
    Arbitrum-only check would have been just as arbitrary a guess. Checks
    the primary (most likely) chains first; the secondary tier is only
    swept if the primary one found nothing positive.
    """
    pairs = await _sweep_bitquery_chains(wallet, _BITQUERY_EVM_CHAINS_PRIMARY)
    positive = _positive_chain_results(pairs)
    if not positive:
        pairs = pairs + await _sweep_bitquery_chains(wallet, _BITQUERY_EVM_CHAINS_SECONDARY)
        positive = _positive_chain_results(pairs)
    attempted = [chain for chain, output in pairs if output is not None]
    if not attempted:
        return None
    if positive:
        return "\n\n".join(f"### {chain.title()}\n\n{output}" for chain, output in positive)
    return (
        f"# Wallet token balances\n\nNo positive token balances were returned for `{wallet}` on any of "
        f"{', '.join(chain.title() for chain in attempted)} within Bitquery's realtime window -- this does "
        "not necessarily mean the wallet is empty; it means no balance-changing activity for this address "
        "was indexed in that window on these chains.\n\n"
        "Source: [Bitquery GraphQL API](https://docs.bitquery.io/)"
    )


_EXPLORER_URLS = {
    "ethereum": "https://etherscan.io/address/{address}",
    "base": "https://basescan.org/address/{address}",
    "arbitrum": "https://arbiscan.io/address/{address}",
    "bsc": "https://bscscan.com/address/{address}",
    "avalanche": "https://snowtrace.io/address/{address}",
    "polygon": "https://polygonscan.com/address/{address}",
    "optimism": "https://optimistic.etherscan.io/address/{address}",
    "solana": "https://solscan.io/account/{address}",
}


def _explorer_url(wallet: str, chain: str | None) -> str:
    """A block-explorer link for whichever chain the data actually came
    from -- not hardcoded to Nansen's profiler, since a wallet answer can
    now be composed entirely from GoldRush (or Bitquery/Helius) without
    Nansen ever being involved.
    """
    is_evm = wallet.startswith("0x")
    resolved = chain if chain in _EXPLORER_URLS else ("ethereum" if is_evm else "solana")
    # EVM explorers are case-insensitive (lowercase matches this app's
    # existing convention); Solana's base58 addresses are case-sensitive
    # and must never be lowercased.
    address = wallet.lower() if is_evm else wallet
    return _EXPLORER_URLS[resolved].format(address=address)


def wallet_portfolio_answer(wallet: str, observation: str, chain: str | None = None) -> str:
    """Build a concise factual summary without a second model invocation."""
    profile = _explorer_url(wallet, chain)
    failed_sections = _failed_portfolio_sections(observation)
    if failed_sections:
        quota_note = (
            " -- Nansen API credits are exhausted for these endpoints"
            if _INSUFFICIENT_CREDITS.search(observation) else ""
        )
        answer = (
            f"Wallet [{wallet}]({profile}): partial portfolio data below. "
            f"**{', '.join(failed_sections)}** could not be retrieved{quota_note}; "
            "treat this as incomplete, not a full picture of the wallet."
        )
    else:
        answer = f"Wallet [{wallet}]({profile}) has current portfolio data shown below."
    # Nansen's fields come as bare numbers (e.g. "2.6k"); GoldRush's Hyperliquid
    # fields are pre-formatted via _money() and already carry a leading "$"
    # (e.g. "$5.9K") -- strip it here so the "$" this template adds below
    # never doubles up into "$$5.9K", regardless of which source supplied
    # the field.
    net_value = _strip_dollar(_portfolio_field(observation, "Net Value (USD)"))
    assets = _strip_dollar(_portfolio_field(observation, "Total Assets (USD)"))
    debts = _strip_dollar(_portfolio_field(observation, "Total Debts (USD)"))
    account_value = _strip_dollar(_portfolio_field(observation, "Account Value (USD)"))
    notional = _strip_dollar(_portfolio_field(observation, "Total Notional (USD)"))
    if net_value and assets and debts and (_nonzero(assets) or _nonzero(debts)):
        answer += (
            f" DeFi net value is **${net_value}**, with **${assets} in assets** and "
            f"**${debts} in debt**, so debt materially offsets the supplied assets."
        )
    elif net_value and assets and debts:
        answer += " No DeFi assets or debt were reported for this wallet."
    if account_value and notional:
        answer += (
            f" The Hyperliquid account value is **${account_value}** against "
            f"**${notional} total notional**, indicating substantial leveraged exposure."
        )
    if "## Perp Positions" in observation:
        answer += " Review leverage, liquidation levels, funding, and unrealized PnL in the position card."
    return answer


_DIRECT_CAPABILITY_ORDER = (
    "knowledge",
    "url_fetch",
    "project_intelligence",
    "vc_intelligence",
    "people_intelligence",
    "equity_research",
    "finance_data",
    "token_security",
    "token_discovery",
    "market_data",
    "market_sentiment",
    "wallet_intelligence",
    "defi_data",
    "listing_events",
    "web_research",
)

# Deterministic, on-chain/market capabilities that a tool's OWN narrow matcher
# is allowed to make reachable even when the intent classifier didn't emit them
# (the reachability backstop -- see research_node). Web-search-backed
# capabilities (web_research, url_fetch, people/vc/project/equity/finance) are
# intentionally excluded: those are the generic fallbacks the classifier
# already owns, and a tool matcher must never quietly pull a request into them.
_BACKSTOP_CAPABILITIES = frozenset({
    "knowledge",
    "market_data", "token_discovery", "token_security",
    "market_sentiment", "defi_data", "listing_events", "wallet_intelligence",
})


def _router_eligible_capabilities(
    request: str, capabilities: set[str], chains: tuple[str, ...]
) -> tuple[str, ...]:
    """Filter the request's capability set down to those that should reach the
    deterministic provider router, in a stable order.

    This is the ONE remaining, non-redundant Layer-2 decision: the gate on
    market_data/token_discovery/token_security keeps a keyword-less request
    (e.g. "tell me about Ethereum" -- no address, no chain, no token/security
    wording) out of the deterministic router so it degrades to ReAct/web
    search instead of forcing a generic pair lookup.

    It deliberately does NOT pick a single winning capability any more.
    Choosing the best tool ACROSS the returned set is left to
    ProviderRouter.try_route_across / _score, which already ranks tools by
    keyword specificity + priority + chain fit. The old fixed capability
    priority order (token_security before token_discovery before market_data)
    discarded that tool-level signal and let a broad tool in an earlier bucket
    beat a specific tool in a later one (e.g. "recent trades" answered with
    top-holders data); ranking the union removes that whole failure mode.
    """
    eligible: list[str] = []
    for capability in _DIRECT_CAPABILITY_ORDER:
        if capability not in capabilities:
            continue
        if capability in {"market_data", "token_discovery", "token_security"} and not (
            is_crypto_trends_query(request)
            or chains
            or re.search(
                r"\b(?:token|coin|pair|dex|liquidity|volume|gainers?|losers?|new|launch|"
                r"contract|mint|holders?|rug|scam|honeypot|audit|safe(?:ty)?|"
                r"security|sellability|meme(?:coin)?|"
                # Hyperliquid perp market data (funding/mark price/open interest) has no
                # chain and no "token/coin" wording -- without these, a bare "funding rate
                # for BTC on Hyperliquid" request skipped market_data entirely (verified
                # live) and fell through to a web-search guess instead of the real,
                # deterministic goldrush_hyperliquid_market tool.
                r"hyperliquid|funding\s*rate|mark\s*price|open\s*interest|perps?|perpetuals?)\b",
                request,
                re.IGNORECASE,
            )
        ):
            continue
        eligible.append(capability)
    return tuple(eligible)


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

    synthesis = await runtime._call_synthesis_lm(
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
# A wallet request that explicitly names Hyperliquid wants the focused positions
# tool (goldrush_hyperliquid_positions), not the broad portfolio composition.
_HYPERLIQUID_REQUEST = re.compile(r"\bhyperliquid\b", re.IGNORECASE)


def _detect_wallet_request(request: str) -> tuple[str, str | None] | None:
    """Returns (address, chain) if this request is an unambiguous wallet
    lookup (address + portfolio/activity/labels-type wording), else None.

    Shared by direct_mcp_request's transactions/labels dispatch (via
    _nansen_wallet_tool_call) and research_node's portfolio dispatch (via
    _compose_wallet_portfolio) -- one detection, two different handlers
    depending on which kind of wallet request it turns out to be.
    """
    address_match = _TOKEN_ADDRESS.search(request)
    lowered = request.lower()
    wallet_lookup = re.search(
        r"\b(?:analy[sz]e|check|inspect|profile|portfolio|holding|holdings|balance|balances|"
        r"leverage|debt|liquidation|transactions?|activity|history|transfers?|labels?|"
        r"positions?|exposure)\b",
        lowered,
    )
    token_subject = re.search(r"\b(?:token|coin|contract|mint|memecoin|meme coin|erc-?20)\b", lowered)
    explicit_wallet = re.search(r"\b(?:wallet|portfolio|balances?|pnl|transactions?|counterparties)\b", lowered)
    if not (address_match and wallet_lookup and (not token_subject or explicit_wallet)):
        return None
    address = address_match.group(1) or address_match.group(2)
    if address.startswith("0x"):
        # A 0x address can never exist on Solana -- if "solana" appears
        # anywhere in the effective request (stale multi-turn context, an
        # incidental earlier mention, an ambiguous follow-up), it must
        # never be picked as this wallet's chain.
        found_chains = [found for found in extract_chains(request) if found != "solana"]
        chain = found_chains[0] if found_chains else None
    else:
        chain = "solana"
    return address, chain


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
            # This tool's own default ("evm", scanning every EVM chain) is
            # measurably slow/unreliable in testing; an explicit chain is much
            # faster. address_portfolio/address_labels default to "all" and
            # are fine left unset, so this default is scoped to this tool only.
            payload = {"address": address, "chain": chain or ("ethereum" if address.startswith("0x") else "solana")}
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


def direct_mcp_request(request: str, *, token_subject: bool = False) -> tuple[str, dict] | None:
    """Resolve common, unambiguous operations without a ReAct planning loop.

    Nansen is tried here, deterministically, before the generic capability
    router (GoldRush/Helius) or the ReAct research agent — see
    _nansen_wallet_tool_call. `token_subject` says the only address in the
    request is a token the symbol resolver injected, so it is never a wallet.
    """
    lowered = request.lower()
    detected = None if token_subject else _detect_wallet_request(request)
    if detected is not None:
        address, chain = detected
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


# A token named by symbol (not address) in a token-data question: "top holders
# of BONK", "safety of BONK", "is BONK a rug", "$BONK". Deliberately excludes
# the greedy "X token" shape so "trending tokens" is never read as a ticker.
_NAMED_TOKEN = re.compile(
    r"\bholders?\s+of\s+(?:the\s+)?\$?([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\b(?:safety|security|liquidity|volume|price|trades?|chart|risk)\s+(?:of|for)\s+(?:the\s+)?\$?([A-Za-z][A-Za-z0-9]{1,9})\b"
    r"|\bis\s+\$?([A-Za-z][A-Za-z0-9]{1,9})\s+(?:a\s+)?(?:safe|rug|honeypot|scam|legit)\b"
    # "if BONK is safe to ape into", "BONK is a scam?" -- the ticker precedes the
    # copula; upper-case only so "it is safe" / "this is legit" never bind.
    r"|\b\$?((?-i:[A-Z][A-Z0-9]{1,9}))\s+is\s+(?:a\s+)?(?:safe|rug|honeypot|scam|legit)\b"
    r"|\$([A-Za-z][A-Za-z0-9]{1,9})\b",
    re.IGNORECASE,
)
_NAMED_STOP = {"THE", "A", "AN", "MY", "THIS", "THAT", "IT", "SOME", "TOP", "NEW", "MINT", "SPL", "USD"}
_SECURITY_ASK = re.compile(r"\b(?:safe|safety|security|rug|honeypot|scam|legit|audit)\b", re.IGNORECASE)


def _resolved_token_record(original_request: str, resolution: "_TokenResolution") -> dict | None:
    """{symbol, address, chain} for a resolution that rewrote the request, so
    the turn can publish the subject to the session focus."""
    if not resolution.chain:
        return None
    match = _TOKEN_ADDRESS.search(resolution.request)
    if not match:
        return None
    tickers = _named_tickers(original_request)
    return {
        "symbol": tickers[-1] if tickers else None,
        "address": match.group(1) or match.group(2),
        "chain": resolution.chain,
    }


class _TokenResolution:
    """Outcome of resolving a token named by symbol in a data question.

    Exactly one shape is meaningful per instance:
    - resolved: `request` rewritten with "<address> on <chain>", `chain` set.
    - ask: `clarification` set (the user must pick a chain/contract) and
      `pending` carries the candidates + original request for the follow-up.
    - no change: `request` unchanged, everything else falsy.
    """

    __slots__ = ("request", "chain", "clarification", "pending")

    def __init__(self, request: str, chain: str | None = None, clarification: str | None = None, pending: dict | None = None):
        self.request = request
        self.chain = chain
        self.clarification = clarification
        self.pending = pending


def _chain_key(chain: str) -> str:
    """Canonicalize a chain label so DEX Screener chainIds and the user's
    wording compare equal (bsc<->bnb, and case)."""
    key = chain.strip().lower()
    return {"bsc": "bnb"}.get(key, key)


def _named_tickers(request: str) -> list[str]:
    tickers: list[str] = []
    for match in _NAMED_TOKEN.finditer(request):
        ticker = next((g for g in match.groups() if g), "").upper()
        if ticker and ticker not in _NAMED_STOP and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def _candidate_line(candidate: dict) -> str:
    liq = candidate.get("liquidity_usd") or 0
    # A candidate carrying a trader count came from Bitquery (traded volume);
    # otherwise the magnitude is DEX Screener pool liquidity. Label truthfully.
    unit = "vol" if candidate.get("traders") is not None else "liq"
    mag = f", ~${liq/1_000_000:.1f}M {unit}" if liq >= 1_000_000 else (f", ~${liq/1_000:.0f}k {unit}" if liq >= 1_000 else "")
    traders = f" · {candidate['traders']} traders" if candidate.get("traders") else ""
    verified = " · Jupiter-verified" if candidate.get("verified") else ""
    return f"{candidate['chain']} `{candidate['address']}`{mag}{traders}{verified}"


# Derivative / mirror venues (perp DEXes, Robinhood's Solana mirror) are not the
# native chain a token's holders/security live on, so they are excluded from
# identity resolution unless the user explicitly names them.
_MIRROR_CHAINS = {"robinhood", "hyperliquid"}
# A non-Solana chain with at least this much DEX Screener liquidity carries a
# real same-ticker token (a genuine cross-chain rival to a verified Solana one),
# not dust -- the Bitquery-independent signal for ambiguity detection.
_RIVAL_EVM_LIQUIDITY = 1_000_000.0
# A rival chain must also show real trading: DEX Screener listings with $342M of
# "liquidity" and $0 of 24h volume (the fake Ethereum BONK) are decoys.
_RIVAL_EVM_VOLUME = 100_000.0
# Cap how many EVM chains we resolve per no-chain resolution (cost/latency).
_MAX_EVM_PROBES = 3


def _jupiter_solana_mint(ticker: str) -> str | None:
    """The canonical Solana mint for a ticker from Jupiter's VERIFIED registry,
    or None. Authoritative for Solana (keyless, verification-tagged) -- unlike a
    DEX Screener text search, it is not polluted by same-ticker copycats."""
    try:
        matches = search_verified_tokens(ticker)
    except Exception:
        return None
    exact = [m for m in matches if (m.get("symbol") or "").upper() == ticker and "verified" in (m.get("tags") or [])]
    chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
    return chosen.get("mint") if chosen and chosen.get("mint") else None


async def _canonical_on_chain(ticker: str, chain: str, strict: bool = False) -> dict | None:
    """The single best (chain, address) candidate for a ticker on ONE chain:
    Jupiter's verified registry for Solana, Bitquery's real-volume ranking for
    EVM (the primary EVM resolver), DEX Screener as the fallback. None if nothing
    on that chain qualifies. This is where "which address on this chain" is
    decided accurately, separate from the cheaper cross-chain "which chain" pick.

    `strict` is for rival detection: when Bitquery answered and found no token
    with real traders on the chain, DEX Screener's entry for it is pollution
    (a "BONK" on Ethereum showing $342M of fake liquidity) and must not turn a
    clean Solana resolution into a "which chain?" question. The fallback stays
    for a Bitquery outage and for chains the user named explicitly.
    """
    key = _chain_key(chain)
    if key == "solana":
        mint = await asyncio.to_thread(_jupiter_solana_mint, ticker)
        if mint:
            return {"chain": "solana", "address": mint, "symbol": ticker, "liquidity_usd": 0.0, "verified": True}
    candidates: list[dict] = []
    if key != "solana":
        candidates, answered = await asyncio.to_thread(bitquery_evm_lookup, ticker, chain)
        if strict and answered and not candidates:
            return None
    if not candidates:
        try:
            ds = await asyncio.to_thread(token_candidates, ticker)
        except Exception:
            ds = []
        candidates = [c for c in ds if _chain_key(c["chain"]) == key]
    return clear_winner(candidates) or (candidates[0] if candidates else None)


async def _resolve_named_token(request: str, capabilities: set[str], user_chains: tuple[str, ...] = ()) -> _TokenResolution:
    """Resolve a token named by SYMBOL in a token-data question to a concrete
    (chain, address), chain-agnostically, so "top holders of PEPE" reaches the
    chain-correct holders tool instead of a generic pair lookup.

    Resolution order:
    1. A chain the user named (router decision or spelled in the request) wins --
       resolve within it (Bitquery for EVM, Jupiter for Solana); never ask.
    2. Otherwise prefer Jupiter's VERIFIED Solana canonical mint -- authoritative
       for a Solana-first app (this is what makes "top holders of BONK" resolve
       cleanly to Solana). If a real same-ticker token also lives on another chain
       (e.g. PEPE: Solana vs Ethereum), it's genuinely ambiguous -> ask.
    3. Otherwise pick the chain by DEX Screener liquidity (a consistent cross-chain
       signal), then resolve the canonical ADDRESS on it via Bitquery; ask when no
       chain clearly dominates. No-op when an address is present, the request isn't
       a token-data question, or nothing matches.
    """
    if _TOKEN_ADDRESS.search(request):
        return _TokenResolution(request)
    # A named-ticker safety ask ("is BONK safe", "if BONK is safe to ape into")
    # is a token-data question whatever the speech layer called it (the model
    # tags it "advice" -> web caps); resolving the mint here is what lets the
    # security dossier tool out-rank a web search downstream.
    security_ask = bool(_SECURITY_ASK.search(request))
    if not (capabilities & {"token_discovery", "token_security", "market_data"}) and not security_ask:
        return _TokenResolution(request)
    tickers = _named_tickers(request)
    if not tickers:
        return _TokenResolution(request)
    ticker = tickers[-1]
    # SOL is Solana's native asset by construction -- never ambiguous across chains.
    if ticker in {"SOL", "WSOL"}:
        return _TokenResolution(f"{request} {WRAPPED_SOL_MINT} on solana", chain="solana")

    def _resolved(candidate: dict) -> _TokenResolution:
        return _TokenResolution(f"{request} {candidate['address']} on {candidate['chain']}", chain=candidate["chain"])

    named_chains = tuple(dict.fromkeys([*user_chains, *extract_chains(request)]))

    # (1) The user named one or more chains -- honor them, no matter which chain.
    if named_chains:
        resolved: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for chain in named_chains:
            candidate = await _canonical_on_chain(ticker, chain)
            if candidate:
                dedupe_key = (_chain_key(candidate["chain"]), candidate["address"].lower())
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    resolved.append(candidate)
        if len(resolved) == 1:
            return _resolved(resolved[0])
        if resolved:
            # The user named several chains and the token lives on more than one
            # -- surface the choice rather than guessing across their chains.
            return _build_ask(request, ticker, resolved)
        # Symbol isn't a token on the named chain (e.g. a perp like BTC on
        # Hyperliquid) -- leave it for the market-data router.
        return _TokenResolution(request)

    # (2) No chain named. Build a candidate set from RELIABLE signals only --
    #     Jupiter's verified flag for Solana, Bitquery real volume for EVM --
    #     because DEX Screener liquidity is polluted for both the chain and the
    #     address (it reported $1B of "Solana AERO" for a token with no holders).
    mint = await asyncio.to_thread(_jupiter_solana_mint, ticker)
    try:
        ds_candidates = await asyncio.to_thread(token_candidates, ticker)
    except Exception:
        ds_candidates = []
    ds_candidates = [c for c in ds_candidates if _chain_key(c["chain"]) not in _MIRROR_CHAINS]
    # DEX Screener liquidity per chain enumerates the RIVAL chains (a same-ticker
    # token with real liquidity, not dust). This signal is Bitquery-independent so
    # ambiguity detection survives a Bitquery outage/402; the per-chain canonical
    # ADDRESS is then resolved via _canonical_on_chain (Bitquery first, DEX
    # Screener fallback) so a real EVM rival is never silently dropped.
    liquidity_by_chain: dict[str, float] = {}
    volume_by_chain: dict[str, float | None] = {}
    for c in ds_candidates:
        key = _chain_key(c["chain"])
        liquidity_by_chain[key] = liquidity_by_chain.get(key, 0.0) + (c.get("liquidity_usd") or 0.0)
        if c.get("volume_24h_usd") is not None:   # unknown (older callers) keeps the liquidity-only rule
            volume_by_chain[key] = (volume_by_chain.get(key) or 0.0) + float(c["volume_24h_usd"])
    rival_chains = [
        k for k in sorted(liquidity_by_chain, key=lambda k: liquidity_by_chain[k], reverse=True)
        if k != "solana" and liquidity_by_chain[k] >= _RIVAL_EVM_LIQUIDITY
        and (volume_by_chain.get(k) is None or volume_by_chain[k] >= _RIVAL_EVM_VOLUME)
    ]

    entries: list[dict] = []
    if mint:
        entries.append({"chain": "solana", "address": mint, "symbol": ticker, "liquidity_usd": 0.0, "verified": True})
    for chain in rival_chains[:_MAX_EVM_PROBES]:
        candidate = await _canonical_on_chain(ticker, chain, strict=True)
        if candidate:
            entries.append(candidate)

    if not entries:
        # No verified Solana token and no real EVM activity -- best effort for a
        # non-verified Solana-only token (e.g. a brand-new memecoin) via the DEX
        # Screener Solana pick; otherwise leave it for the router.
        sol = [c for c in ds_candidates if _chain_key(c["chain"]) == "solana"]
        winner = clear_winner(sol)
        return _resolved(winner) if winner else _TokenResolution(request)
    if len(entries) == 1:
        return _resolved(entries[0])
    # Several real same-ticker tokens across chains. A verified-Solana entry
    # (no volume metric) can't be compared to EVM volume, so ask; among EVM-only
    # entries a clear volume winner resolves.
    if not mint:
        winner = clear_winner(entries)
        if winner is not None:
            return _resolved(winner)
    return _build_ask(request, ticker, entries)


def _build_ask(request: str, ticker: str, candidates: list[dict]) -> _TokenResolution:
    top = candidates[:5]
    listing = "\n".join(f"- {ticker} on {_candidate_line(c)}" for c in top)
    clarification = (
        f"**{ticker}** exists on several chains with comparable liquidity, so I don't want to "
        f"guess which one you mean:\n\n{listing}\n\n"
        "Reply with the chain (e.g. `Base`), the position (`the second one`), or paste the exact "
        "contract address, and I'll pull the data for that token."
    )
    pending = {"original_request": request, "symbol": ticker, "candidates": top}
    return _TokenResolution(request, clarification=clarification, pending=pending)


_DEEPDIVE_STOP = _NAMED_STOP | {
    "MARKET", "MARKETS", "CRYPTO", "TOKEN", "COIN", "GOOD", "BUY", "INVEST",
    "PRICE", "CHART", "PROJECT", "THIS", "THAT", "SOL",
}
# The single invalidation variable the deep-dive is required to end with.
_FLIP_VARIABLE = re.compile(r"what would flip this[:\s]+\**\s*(.+?)(?:\n|$)", re.IGNORECASE)


async def _reflect_due_decisions(chain: str, address: str, price_now: float | None) -> None:
    """Opportunistic reflection: for each earlier decision on this asset now old
    enough to have an outcome, produce one bias-free process lesson from the
    realized move. Never raises; a failed reflection just leaves the case pending."""
    role_memory.mark_stale()
    for case in role_memory.due_for_reflection(chain, address):
        if case.price_at_decision and price_now:
            pct = (price_now - case.price_at_decision) / case.price_at_decision * 100.0
            age_days = (time.time() - case.decided_at) / 86400.0
            realized = f"{pct:+.1f}% over {age_days:.0f} days since the decision"
        else:
            pct = None
            realized = "price outcome unavailable"
        try:
            res = await runtime._call_lm(
                runtime.role_reflection_agent,
                thesis=case.thesis, flip_variable=case.flip_variable, realized_move=realized,
            )
            lesson = (getattr(res, "lesson", "") or "").strip()
        except Exception:
            logger.warning("role-memory reflection failed for %s", case.id, exc_info=True)
            lesson = ""
        role_memory.record_reflection(case.id, pct, lesson or "No process correction produced.")


async def _run_token_deep_dive(state: AgentState, request: str) -> dict | None:
    """Resolve the named token (chain-agnostically -- asks on genuine ambiguity),
    compose the multi-dimension evidence bundle, and synthesize the due-diligence
    verdict. Returns None (fall through to normal research) when no token can be
    resolved from the request."""
    # A deep-dive IS a token-data operation, so resolve with token capabilities
    # regardless of how Layer-1 classified the phrasing (it often tags an
    # "analyze X" ask as web_research, which the resolver would otherwise skip).
    token_caps = {"token_discovery", "token_security", "market_data"}
    # Honor a chain named anywhere in the request ("deep dive on ARB on arbitrum")
    # -- the synthetic resolver query below drops the chain phrase, so thread it here.
    named_chains = tuple(dict.fromkeys([*state.get("chains", []), *extract_chains(request)]))

    address: str | None = None
    chain: str | None = None
    symbol: str | None = None
    addr = _TOKEN_ADDRESS.search(request)
    if addr:
        address = addr.group(1) or addr.group(2)
        if addr.group(2):  # base58 -> Solana by construction
            chain = "solana"
        elif named_chains:
            chain = named_chains[0]
        else:  # EVM address without a chain -- can't screen safely
            return {
                "answer": (
                    f"Which chain is `{address}` on (Ethereum, Base, Arbitrum, BNB, ...)? A token "
                    "deep-dive needs the exact chain — the same address can be different tokens on "
                    "different chains."
                ),
                "trajectory": None,
            }
    else:
        tickers: list[str] = []
        for match in _DEEPDIVE_TOKEN.finditer(request):
            ticker = next((g for g in match.groups() if g), "").upper()
            if ticker and ticker not in _DEEPDIVE_STOP and ticker not in tickers:
                tickers.append(ticker)
        if not tickers:
            return None
        symbol = tickers[-1]
        # Reuse the full chain-agnostic resolver by framing the ticker as a
        # token-data lookup -- so a cross-chain ambiguous ticker ASKS, just like
        # every other token query, instead of guessing.
        resolution = await _resolve_named_token(f"top holders of {tickers[-1]}", token_caps, named_chains)
        if resolution.clarification:
            # The resolver framed the ticker as a synthetic "top holders of X"
            # probe to reuse the ambiguity logic; but the user asked for a
            # deep-dive. Resume THEIR request on disambiguation, not the probe,
            # so the follow-up re-enters the deep-dive lens (not a holders lookup).
            pending = {**(resolution.pending or {}), "original_request": request}
            return {"answer": resolution.clarification, "pending_token": pending}
        resolved = _TOKEN_ADDRESS.search(resolution.request)
        if not resolved or not resolution.chain:
            return None
        address = resolved.group(1) or resolved.group(2)
        chain = resolution.chain

    bundle = await build_token_evidence(address, chain, symbol)
    evidence = format_evidence_bundle(bundle)
    charter = (state.get("session_context") or {}).get("risk_charter")

    # Reflection loop (role memory). Fetching this asset's price is the trigger to
    # reflect on any earlier decisions now old enough to have an outcome; then
    # RECALL prior lessons and feed them into synthesis so the analyst improves.
    price_now = extract_market_price(bundle)
    await _reflect_due_decisions(chain, address, price_now)
    lessons = role_memory.recall_lessons(chain, address)
    learned = "\n".join(f"- {lesson}" for lesson in lessons) if lessons else "none"

    try:
        result = await runtime._call_synthesis_lm(
            runtime.token_deepdive_agent,
            request=request, evidence=evidence,
            analysis_rules=ANALYSIS_RULES,
            user_context=charter or "no profile set",
            learned_lessons=learned,
        )
        answer = (getattr(result, "answer", "") or "").strip() or evidence
    except Exception:
        logger.warning("token deep-dive synthesis failed; returning raw bundle", exc_info=True)
        answer = evidence

    # STORE this decision for a future bias-free reflection (advisory memory only;
    # never a trade or a safety override).
    flip = _FLIP_VARIABLE.search(answer)
    if symbol and price_now is not None:
        role_memory.store_case(
            role="analysis.reasoning", chain=chain, address=address, symbol=symbol,
            thesis=answer.strip().split("\n", 1)[0][:400],
            flip_variable=flip.group(1).strip() if flip else "not stated",
            price_at_decision=price_now,
        )

    trajectory: dict = {
        "thought_0": "Structured token deep-dive: composed a multi-dimension evidence bundle, then synthesized a due-diligence verdict.",
        "tool_name_0": "token_deep_dive",
        "tool_args_0": {"address": address, "chain": chain},
        "observation_0": evidence,
    }
    index = 1
    for dimension in bundle.dimensions:
        if dimension.status == "available" and dimension.source:
            trajectory[f"tool_name_{index}"] = dimension.source
            trajectory[f"observation_{index}"] = dimension.detail
            index += 1
    return {
        "answer": answer,
        "trajectory": trajectory,
        "resolved_token": {"symbol": symbol, "address": address, "chain": chain},
    }


@trace(name="research", as_type="agent")
async def _synthesize_knowledge(request: str, passages: str, history: str) -> str:
    try:
        result = await runtime._call_synthesis_lm(runtime.knowledge_synthesizer, request=request, conversation_history=history or "", passages=passages)
        answer = (getattr(result, "answer", "") or "").strip()
    except Exception:
        logger.warning("knowledge synthesis failed; returning passages", exc_info=True)
        answer = ""
    if not answer:
        return passages
    sources = passages.split("## Sources", 1)[1].strip() if "## Sources" in passages else ""
    if sources and "## Sources" not in answer:
        answer += "\n\n## Sources\n" + sources
    return answer


async def research_node(state: AgentState) -> dict:
    sink: dict = {}
    result = await _research_node(state, sink)
    if sink.get("resolved_token") and not result.get("resolved_token"):
        result = {**result, "resolved_token": sink["resolved_token"]}
    return result


def _shift_trajectory(trajectory: dict, by: int) -> dict:
    """Renumber `<key>_<n>` entries so a card's steps follow another's."""
    out = {}
    for key, value in trajectory.items():
        m = re.fullmatch(r"(.+?)_(\d+)", key)
        out[f"{m.group(1)}_{int(m.group(2)) + by}" if m else key] = value
    return out


async def _research_node(state: AgentState, sink: dict) -> dict:
    request = _effective_request(state)
    # A broad "how's the crypto market" ask -> the compact overview card, composed
    # from live sources we already use. Intercepted up front (before capability
    # routing sends it to a web/equity search) but gated so a chain-scoped,
    # token-specific, or "trending tokens" request still falls through to its
    # ranked-list tools.
    broad_market = bool(
        _MARKET_OVERVIEW.search(request)
        and not _TOKEN_ADDRESS.search(request)
        and not tuple(state.get("chains", []))
        and not TRENDING_TOKENS.search(request)
    )
    # "why is SOL down?" -> the composed market + news card (crypto first, stock
    # otherwise). Checked BEFORE the overview: a message can carry both ("today
    # market trend on crypto. why zec is pumping"), and the specific question
    # is the part a brief alone silently dropped. Both cards are then composed.
    moving = why_moving.match(request)
    if moving and not _TOKEN_ADDRESS.search(request):
        prefer_stock = why_moving.prefers_stock(request) or "equity_research" in set(state.get("capabilities", []))
        answer, trajectory = await why_moving.compose(*moving, prefer_stock=prefer_stock)
        trajectory = {"thought_0": "A 'why is X moving' ask maps to the composed market + news card.", **trajectory} if trajectory else None
        if broad_market:
            overview = await asyncio.to_thread(crypto_market_overview, request)
            answer = f"{overview}\n\n---\n\n{answer}"
            trajectory = {
                "thought_0": "A market-status ask that also asks why one asset is moving: the overview card, then that asset's card.",
                "tool_name_0": "crypto_market_overview", "tool_args_0": {"query": request}, "observation_0": overview,
                **_shift_trajectory(trajectory or {}, 1),
            }
        return {"answer": answer, "trajectory": trajectory}
    if broad_market:
        observation = await asyncio.to_thread(crypto_market_overview, request)
        return {
            "answer": observation,
            "trajectory": {
                "thought_0": "A broad market-status request maps to the composed market-overview card.",
                "tool_name_0": "crypto_market_overview",
                "tool_args_0": {"query": request},
                "observation_0": observation,
            },
        }
    # "what events are coming this week?" -> the dated calendar card.
    if event_calendar.TRIGGER.search(request) and not _TOKEN_ADDRESS.search(request):
        days = 14 if re.search(r"\b(?:next|two)\s+weeks?|fortnight|month\b", request, re.I) else 7
        data = await asyncio.to_thread(event_calendar.get_calendar, days)
        return {"answer": event_calendar.render(data), "trajectory": {
            "thought_0": "A market-events ask maps to the dated calendar card.", "tool_name_0": "market_event_calendar",
            "tool_args_0": {"days": days}, "observation_0": json.dumps(data.get("events", [])[:20]),
        }}
    # A structured token due-diligence ask ("deep dive on X", "analyze X",
    # "thoughts on X") -> the multi-dimension analysis lens. Gated on a resolvable
    # token; a no-token analysis ask (or a bare single-metric lookup) falls through.
    if _DEEPDIVE.search(request) and not TRENDING_TOKENS.search(request):
        deep_dive = await _run_token_deep_dive(state, request)
        if deep_dive is not None:
            return deep_dive
    resolution = await _resolve_named_token(
        request, set(state.get("capabilities", [])), tuple(state.get("chains", []))
    )
    if resolution.clarification:
        # Same-symbol tokens on comparable chains: ask rather than guess, and
        # remember the candidates + original request so a one-word reply
        # ("Base") resolves it next turn (see resolve_pending_token).
        return {"answer": resolution.clarification, "pending_token": resolution.pending}
    sink["resolved_token"] = _resolved_token_record(request, resolution)
    request = resolution.request
    if resolution.chain:
        # Thread the resolved chain to every downstream provider call so the
        # chain-correct tool is used (Solana holders -> bitquery, EVM -> goldrush,
        # etc.); the rewritten "<address> on <chain>" text drives direct routing.
        state = {**state, "chains": [resolution.chain]}
    # Wallet-portfolio requests (address + "portfolio"/"balance"/"holding"/
    # etc. wording, as opposed to transactions/labels wording) are composed
    # from GoldRush (balances, Hyperliquid) + optional Nansen DeFi
    # enrichment -- see _compose_wallet_portfolio's docstring for why this
    # is a separate path from direct_mcp_request (which still handles
    # address_transactions/address_labels unchanged via
    # _nansen_wallet_tool_call, since those already have their own
    # reasonable fallback chains and this refactor is scoped to the
    # Nansen-credit-exhaustion problem on the portfolio call specifically).
    # A symbol the resolver just rewrote to an address is a token by
    # construction; never re-read that address as a wallet ("check if BONK is
    # safe" + injected mint used to become a wallet-portfolio lookup).
    token_subject = bool(resolution.chain)
    detected_wallet = None if token_subject else _detect_wallet_request(request)
    # A Hyperliquid-specific wallet ask goes straight to the focused positions
    # tool -- deterministically, bypassing the LLM ReAct fallback that otherwise
    # sometimes widened it to a Nansen portfolio. The tool returns a clear "no
    # account state" message when the wallet has none, which is a valid focused
    # answer, so we surface it rather than falling back.
    if detected_wallet is not None and _HYPERLIQUID_REQUEST.search(request):
        hl = await asyncio.to_thread(get_provider_router().try_route, request, "wallet_intelligence", ())
        address, _ = detected_wallet
        # Terminal for a Hyperliquid ask: the focused tool's result, or a clean
        # on-topic "unavailable" message -- never fall through to a broad
        # portfolio / Nansen answer, which is confusing for a positions query
        # (and the source of the earlier run-to-run nondeterminism).
        if hl is not None and hl.tool == "goldrush_hyperliquid_positions":
            answer = hl.output
        else:
            answer = (
                f"# Hyperliquid positions\n\nI couldn't retrieve Hyperliquid account state for "
                f"`{address}` right now (the Hyperliquid data source was unavailable). Please try "
                "again shortly."
            )
        return {
            "answer": answer,
            "trajectory": {
                "thought_0": "A Hyperliquid-specific wallet request maps to the focused positions tool.",
                "tool_name_0": "goldrush_hyperliquid_positions",
                "tool_args_0": {"request": request},
                "observation_0": answer,
            },
        }
    if (
        detected_wallet is not None
        and not _WALLET_ACTIVITY_WORDS.search(request)
        and not _WALLET_LABEL_WORDS.search(request)
        # A Hyperliquid-specific ask is handled by the focused intercept above;
        # keep it out of the broad portfolio composition on the fall-through.
        and not _HYPERLIQUID_REQUEST.search(request)
    ):
        address, chain = detected_wallet
        answer, trajectory = await _compose_wallet_portfolio(address, chain)
        return {"answer": answer, "trajectory": trajectory}
    direct = direct_mcp_request(request, token_subject=token_subject)
    failed_direct_tool: str | None = None
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
            # Wallet-portfolio requests no longer reach this point at all --
            # research_node intercepts those earlier and composes the
            # answer via _compose_wallet_portfolio (GoldRush-primary, see
            # above). What's left here is address_transactions,
            # address_labels, token_current_top_holders, token_quant_scores
            # and token_info -- all return an already-formatted observation.
            return {"answer": observation, "trajectory": trajectory}
        # The fast path already proved this exact tool fails for this request;
        # don't let the ReAct fallback below waste iteration budget retrying
        # it blindly (it has no visibility into this attempt). tool_name is
        # already the full wrapped name (e.g. mcp_nansen_address_transactions),
        # matching what _mcp_registry.select()/_research_agent expect.
        failed_direct_tool = tool_name
    capabilities = set(state.get("capabilities", []))
    chains = tuple(state.get("chains", []))
    address_match = _TOKEN_ADDRESS.search(request)
    if not chains and address_match and address_match.group(2):
        # A base58 address is unambiguously Solana in this app -- no other
        # supported chain uses this address format -- so inferring it here
        # (rather than requiring the literal word "solana") avoids an
        # unnecessary clarification round trip for the common case. This
        # matches the inference direct_mcp_request already makes elsewhere.
        chains = ("solana",)
    if "token_security" in capabilities and not chains and address_match and address_match.group(1):
        # An EVM contract's security providers (GoPlus, Honeypot.is,
        # DexScreener) all require a specific chain -- the same 0x address
        # can exist with different, unrelated code on different chains.
        # Without this, the capability silently falls through to
        # token_discovery below and answers with unrelated market data,
        # with no disclosure that no security check actually ran.
        return {
            "answer": (
                f"Which chain is `{address_match.group(1)}` on (Ethereum, Base, Arbitrum, BNB, "
                "Polygon, Avalanche, ...)? A security check needs the exact chain -- the same "
                "contract address can exist with different, unrelated code on different chains."
            ),
            "trajectory": None,
        }
    if "equity_research" in capabilities:
        return await _equity_research(state)
    eligible_capabilities = _router_eligible_capabilities(request, capabilities, chains)
    # Reachability backstop (the permanent fix for classifier/tool vocabulary
    # drift): a specific tool may recognize this request even when the intent
    # classifier never emitted its capability. Surface those capabilities from
    # the tools' OWN narrow matchers and route across them too, so adding a tool
    # with new vocabulary never again requires a matching lexicon edit just to
    # be reachable. Restricted to deterministic capabilities, appended in the
    # existing priority order, and deduped -- global _score still decides the
    # winner. Narrow-gate-only means a keyword-less request surfaces nothing
    # here, preserving the "stay out of the deterministic router" property.
    tool_matched = get_provider_router().matched_capabilities(request, chains)
    backstop = tuple(
        cap for cap in _DIRECT_CAPABILITY_ORDER
        if cap in _BACKSTOP_CAPABILITIES and cap in tool_matched and cap not in eligible_capabilities
    )
    eligible_capabilities = eligible_capabilities + backstop
    has_gainers_request = (
        bool(chains) and chains[0] in _GAINERS_SUPPORTED_CHAINS
        and bool(_GAINERS_LOSERS_REQUEST.search(state["request"]))
    )
    if (
        "market_sentiment" not in eligible_capabilities
        and not has_gainers_request
        and is_crypto_trends_query(state["request"])
        # A token-level "trending tokens on <chain/launchpad>" ask wants an
        # actual ranked token list (dexscreener_boosted_tokens via the router),
        # not the generic narrative brief -- let it fall through.
        and not TRENDING_TOKENS.search(state["request"])
    ):
        # is_crypto_trends_query is intentionally broad ("crypto ... right
        # now" matches it too, and so does "trending ... gainers"), so a
        # request that already resolved to a more specific, dedicated
        # capability -- market_sentiment's Fear & Greed / Altcoin Season
        # snapshot, or an explicit chain-scoped gainers/losers ask -- must
        # not be overridden by the generic multi-chain trends brief.
        # crypto_market_brief has no gainers/losers ranking of its own
        # (verified: it returns narrative categories and boosted-attention
        # tokens, neither of which is a price-%-change ranking), so letting
        # it win here would silently drop half the request. Plain
        # token_discovery/market_data requests are left alone: crypto_market_brief
        # is still the better synthesis for those, and this is exactly what
        # routes "trending tokens on solana" to the chain-scoped brief.
        #
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
    if eligible_capabilities:
        result = await asyncio.to_thread(
            get_provider_router().try_route_across,
            request,
            eligible_capabilities,
            chains,
        )
        if result is not None:
            answer = result.output
            if result.tool == "knowledge_base_search":
                # Retrieval context, not an answer: synthesize over the passages with citations.
                answer = await _synthesize_knowledge(request, result.output, state.get("history", ""))
            return {
                "answer": answer,
                "trajectory": _provider_trajectory(result, request, "/".join(eligible_capabilities)),
            }
        # None means no configured provider across the eligible capabilities
        # succeeded (none eligible, all failed, or budget reached). Fall through
        # to the ReAct/web-research path below, which every ordinary capability
        # miss already uses -- must degrade here, not crash the turn.
        logger.warning(
            "No usable provider across capabilities %s for %r; falling back to ReAct",
            eligible_capabilities, request,
        )
    result = await runtime._call_lm(
        _research_agent(
            request,
            tuple(state.get("capabilities", [])),
            tuple(state.get("chains", [])),
            exclude=(failed_direct_tool,) if failed_direct_tool else (),
        ),
        request=request,
        conversation_history="",
    )
    trajectory = getattr(result, "trajectory", None)
    answer = _sanitize_react_answer(append_web_sources(result.answer, trajectory))
    return {"answer": answer, "trajectory": trajectory}
