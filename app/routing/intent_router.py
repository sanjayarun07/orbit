"""Prioritized semantic routing rules.

Rules decide what the user is asking for.  Provider selection is an explicit
result of chain evidence, never a fallback for an incomplete request.
"""

from __future__ import annotations

import re

from .contracts import CapabilityRoute, WorkflowIntent
from .controls import is_charter_command, is_execution_explanation, is_team_command, is_trade_cancellation, is_trade_confirmation
from .entities import extract_chains, has_evm_address, has_solana_address
from . import lexicon as lx
from .speech import has_competing_speech


def _route(
    intent: WorkflowIntent,
    capabilities: tuple[str, ...],
    chains: tuple[str, ...],
    *,
    reason: str,
    confidence: float = 1.0,
    execution_provider=None,
    mode="read",
    missing_fields: tuple[str, ...] = (),
) -> CapabilityRoute:
    return CapabilityRoute(
        intent, capabilities, chains, confidence, "rules", execution_provider,
        mode, missing_fields, reason,
    )


def plan_execution_route(request: str, chains: tuple[str, ...]) -> CapabilityRoute:
    if not chains and (lx.SOL_PAYMENT.search(request) or lx.SOL_SOURCE.search(request)):
        chains = ("solana",)

    explicit_cross_chain = bool(lx.CROSS_CHAIN.search(request) or len(chains) > 1)
    evm_evidence = has_evm_address(request) or any(chain != "solana" for chain in chains)
    solana_evidence = has_solana_address(request) or chains == ("solana",)

    if explicit_cross_chain or evm_evidence:
        return _route(
            "cross_chain_swap", ("cross_chain_swap", "token_resolve", "token_security"),
            chains, reason="relay_chain_evidence", execution_provider="relay", mode="quote",
        )
    if solana_evidence:
        return _route(
            "trade", ("swap", "token_resolve", "token_security"), chains,
            reason="solana_chain_evidence", execution_provider="jupiter", mode="quote",
        )

    # A ticker and a buy verb do not identify a chain or execution provider.
    # Keep this as a generic collecting trade rather than incorrectly opening
    # the Relay workflow and inheriting a stale cross-chain route.
    return _route(
        "trade", ("token_resolve", "token_security"), (), confidence=.96,
        reason="execution_chain_required", mode="collect", missing_fields=("chain",),
    )


NEWS_EXPLAINER = re.compile(
    r"^\s*(?:tell me (?:more )?about (?:this|that|it|the (?:news|headline|story))|explain (?:this|the) (?:news|headline|story)|"
    r"why does this matter|what does this mean|why is this (?:news|important))\b[^:\n]{0,80}:\s*\S",
    re.I,
)


def _knowledge_ask(request: str) -> bool:
    """A 'what is / how does / who competes with' ask naming a protocol in the
    knowledge registry. Deterministic evidence, so it anchors like an address."""
    try:
        from app.knowledge import tool as knowledge_tool

        return knowledge_tool.matches(request)
    except Exception:
        return False


def route_capabilities(request: str) -> CapabilityRoute | None:
    """Resolve high-confidence chat requests without an extra model call."""
    chains = extract_chains(request)
    stripped = request.strip()

    # Control and conceptual messages have absolute priority over content rules.
    if lx.CONCEPTUAL.match(stripped) or is_execution_explanation(request):
        return _route("general", (), chains, reason="conceptual")
    if is_trade_cancellation(request):
        return _route("general", (), chains, reason="trade_cancel", mode="control")
    if is_trade_confirmation(request):
        return _route("general", (), chains, reason="trade_confirm", mode="control")
    # Risk-charter commands are pure control: handled in general_node and
    # persisted by advance_session_context; they never touch the trade path.
    if is_charter_command(request):
        return _route("general", (), chains, reason="risk_charter", mode="control")
    # Team-mode toggle is pure control too (enable/disable/status the desk).
    if is_team_command(request):
        return _route("general", (), chains, reason="team_mode", mode="control")

    # Equity language is checked before execution verbs: "buy-rated NVDA" and
    # "recent stock move" are research, not orders.
    if lx.EQUITY.search(request) or (
        lx.EQUITY_TICKER.search(request)
        and not re.search(r"\b(?:swap|buy|sell|exchange|bridge|trade|convert)\b", request, re.I)
    ):
        return _route("research", ("equity_research",), chains, reason="equity")
    # Checked ahead of the generic TRADE rule and PORTFOLIO_SCENARIO below --
    # more specific than either (a real swap-quote simulation, not a price-
    # shock stress test), and must catch past-tense phrasing ("sold") TRADE
    # doesn't match at all. Never reaches plan_execution_route -- this is a
    # dedicated read-only path (see app/plans.py's simulate_swap()).
    if lx.SIMULATE_TRADE.search(request) and not (
        lx.ADVICE_QUESTION.search(request) and not lx.OWN_POSITION_OR_AMOUNT.search(request)
    ):
        return _route("portfolio", ("trade_simulation", "portfolio"), chains, reason="trade_simulation")
    if lx.TRADE.search(request) or lx.IMPERATIVE_MOVE.search(request):
        if has_competing_speech(request):
            return _route("general", (), chains, reason="semantic_required", confidence=0.0, mode="collect")
        return plan_execution_route(request, chains)

    # Own-wallet phrasings, only when no address is pasted: "recent activity
    # for this wallet 0x..." names an address, and that address is what the
    # question is about, whatever "this wallet" says. With an address the
    # request falls through to the address rules below (a lookup).
    pasted_address = bool(lx.ADDRESS.search(request))
    # Perp positions are a wallet's state: the user's own, or a pasted
    # address looked up read-only. Before the market rule, whose perp
    # vocabulary would otherwise make this a market-data lookup.
    if lx.PERP_POSITIONS.search(request) and (pasted_address or lx.OWN_POSITION_OR_AMOUNT.search(request)):
        return _route("portfolio", ("perp_positions", "wallet_intelligence"), chains, reason="perp_positions")
    if lx.OWN_TOKEN_BALANCE.search(request) and not pasted_address:
        return _route("portfolio", ("token_balance", "portfolio", "wallet_intelligence"), chains, reason="own_token_balance")
    if lx.OWN_TOKEN_HOLDINGS.search(request) and not pasted_address:
        return _route("portfolio", ("token_holdings", "portfolio", "wallet_intelligence"), chains, reason="own_token_holdings")
    if lx.OWN_WALLET_ACTIVITY.search(request) and not pasted_address:
        return _route("portfolio", ("wallet_transactions", "wallet_intelligence"), chains, reason="own_wallet_activity")
    if lx.WALLET_HEALTH.search(request) and not pasted_address:
        return _route("portfolio", ("wallet_health", "portfolio", "wallet_intelligence"), chains, reason="wallet_health")
    if lx.PORTFOLIO_SCENARIO.search(request) and re.search(r"\b(?:my|connected)\b", request, re.I):
        return _route("portfolio", ("portfolio_scenario", "portfolio"), chains, reason="portfolio_scenario")
    if lx.OWN_WALLET.search(request) and not lx.ADDRESS.search(request):
        return _route("portfolio", ("portfolio", "wallet_intelligence"), chains, reason="own_wallet")

    # A headline explainer ("Tell me more about this and why it matters for the
    # market: Bitcoin ETFs lose $450M as CLARITY Act stalls") -- the home tiles
    # generate this shape -- is a news question: web research, never a token
    # lookup on words like "Bitcoin" that happen to appear in the headline.
    if NEWS_EXPLAINER.match(request):
        return _route("research", ("web_research",), chains, reason="news_explainer")
    if _knowledge_ask(request):
        return _route("research", ("knowledge", "web_research"), chains, reason="knowledge_base")
    if lx.ADDRESS.search(request) and lx.TOKEN.search(request) and not lx.WALLET_OWNER.search(request):
        capabilities = ["token_discovery", "token_security"]
        if lx.MARKET.search(request):
            capabilities.append("market_data")
        return _route("research", tuple(capabilities), chains, reason="token_address")
    if lx.ADDRESS.search(request) and (lx.WALLET.search(request) or re.search(r"\b(?:analyze|check|inspect|profile)\b", request, re.I)):
        return _route("research", ("wallet_intelligence",), chains, reason="wallet_address")
    if lx.TOKEN.search(request) and lx.SECURITY.search(request):
        return _route("research", ("token_security", "token_discovery"), chains, reason="token_security")
    if lx.SECURITY_STRONG.search(request):
        # Crypto-only security jargon ("rug", "honeypot") or a $TICKER paired
        # with a safety word identifies a token-security question even without
        # the word "token"/"coin"/"contract" appearing anywhere in the message.
        return _route("research", ("token_security", "token_discovery"), chains, reason="token_security_jargon")
    if lx.TOKEN.search(request) and not lx.LISTING.search(request):
        capabilities = ["token_discovery"]
        if lx.MARKET.search(request):
            capabilities.append("market_data")
        if lx.CURRENT.search(request):
            capabilities.append("web_research")
        return _route("research", tuple(capabilities), chains, reason="token_research")
    if lx.URL.search(request):
        return _route("research", ("url_fetch",), chains, reason="url")
    if lx.WEB3_PROJECT.search(request):
        return _route("research", ("project_intelligence",), chains, reason="web3_project")
    if lx.WEB3_VC.search(request):
        return _route("research", ("vc_intelligence",), chains, reason="web3_vc")
    if lx.WEB3_PEOPLE.search(request) or lx.PEOPLE.search(request):
        return _route("research", ("people_intelligence",), chains, reason="people")
    if lx.LISTING.search(request):
        return _route("research", ("listing_events",), chains, reason="listing")
    if lx.DEFI.search(request):
        return _route("research", ("defi_data",), chains, reason="defi")
    if lx.FINANCE.search(request) and not lx.MARKET.search(request):
        return _route("research", ("finance_data",), chains, reason="finance")
    if lx.SENTIMENT.search(request):
        # Checked ahead of the generic MARKET rule: "market sentiment"
        # contains the bare word "market" and would otherwise be swallowed
        # by it, losing the dedicated Fear & Greed / Altcoin Season signal.
        return _route("research", ("market_sentiment",), chains, reason="market_sentiment")
    if lx.MARKET.search(request):
        capabilities = ["market_data"]
        if lx.FINANCE.search(request):
            capabilities.append("finance_data")
        if re.search(r"\b(?:trending|gainers?|losers?|new pairs?|discover|token profiles?|launches?)\b", request, re.I):
            capabilities.append("token_discovery")
        if lx.CURRENT.search(request):
            capabilities.append("web_research")
        return _route("research", tuple(capabilities), chains, reason="market")
    if lx.CURRENT.search(request):
        capabilities = ["web_research"]
        if lx.FINANCE.search(request):
            capabilities.insert(0, "finance_data")
        return _route("research", tuple(capabilities), chains, reason="current_information")
    # Last-resort deterministic fallback: a plain information-seeking question
    # that matched none of the specific rules above still gets an unambiguous,
    # read-only route instead of depending on the embedding/model classifier
    # being configured. Lower confidence marks it as a weaker rule match.
    if lx.OPEN_QUESTION.match(request):
        return _route("research", ("web_research",), chains, reason="open_question", confidence=0.6)
    return None


def default_capabilities(intent: WorkflowIntent) -> tuple[str, ...]:
    return {
        "general": (),
        "research": ("web_research",),
        "portfolio": ("portfolio", "wallet_intelligence"),
        "trade": ("swap", "token_resolve", "token_security"),
        "cross_chain_swap": ("cross_chain_swap", "token_resolve", "token_security"),
    }[intent]
