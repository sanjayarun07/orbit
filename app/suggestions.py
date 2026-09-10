"""Deterministic, contextual follow-up actions for every chat response."""

import re

from app.context_entities import (
    TokenReference,
    extract_token_reference,
    extract_wallet_reference,
)
from app.models import QuickAction
from app.web_search import is_crypto_trends_query
from app.capability_router import is_execution_explanation

_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_SOLANA_ADDRESS = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
_PRICE = re.compile(r"\b(?:price|worth|market value)\b", re.IGNORECASE)
_NEWS = re.compile(r"\b(?:news|latest|today|current|recent|right now)\b", re.IGNORECASE)
_TRADE = re.compile(r"\b(?:swap|buy|sell|trade|exchange)\b", re.IGNORECASE)
_PORTFOLIO = re.compile(r"\b(?:wallet|portfolio|holdings|balances?)\b", re.IGNORECASE)
_TOKEN_CONTEXT = re.compile(r"\b(?:token|coin|contract|mint|memecoin|meme coin|erc-?20)\b", re.IGNORECASE)
_TRENDING = re.compile(r"\b(?:trending|gainers?|new pairs?|new tokens?|launches?)\b", re.IGNORECASE)
_TOKEN_SECURITY = re.compile(r"\b(?:token|coin|contract|mint)\b.{0,40}\b(?:risk|safe|safety|security|rug|scam)\b", re.IGNORECASE)
_EQUITY = re.compile(r"\b(?:stock|stocks|equity|equities|shares?|earnings|nifty|sensex|nasdaq|nyse)\b", re.IGNORECASE)
_TICKER = re.compile(r"(?:\$|\b(?:NASDAQ|NYSE|NSE|BSE)\s*:\s*)([A-Z][A-Z0-9.-]{0,9})\b")
_ARCHITECTURE = re.compile(r"\b(?:architecture|routing|router|providers?|mcp|scaling|infrastructure|system design)\b", re.IGNORECASE)
_FOLLOWUP = re.compile(
    r"\b(?:it|its|this|that|these|those|they|them|previous|above)\b"
    r"|\b(?:what about|how about|tell me more|more details|explain more|continue|what are the risks)\b",
    re.IGNORECASE,
)
_NAMED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+"
    r"(?:token|coin|memecoin|meme\s+coin)\b",
    re.IGNORECASE,
)


def _contextual_topic(message: str, conversation_history: str) -> str:
    """Use earlier turns only when the new message looks like a continuation."""
    words = re.findall(r"[a-z0-9]+", message.lower())
    if _EQUITY.search(message):
        return message
    if conversation_history and _FOLLOWUP.search(message):
        prior_user_turns = re.findall(r"(?m)^user:\s*(.+)$", conversation_history[-4000:])
        prior_request = prior_user_turns[-1] if prior_user_turns else conversation_history[-1000:]
        return f"{prior_request}\nuser: {message}"
    return message


def _latest_topic(text: str) -> str | None:
    """Choose the most specific recognizable subject in the recent context."""
    patterns = [
        ("architecture", _ARCHITECTURE),
        ("token", _TOKEN_CONTEXT),
        ("wallet", re.compile(r"0x[0-9a-fA-F]{40}|\b(?:wallet|portfolio|holdings|balances?|counterparties|pnl)\b", re.IGNORECASE)),
        ("trade", _TRADE),
        ("token_security", _TOKEN_SECURITY),
        ("equity", _EQUITY),
        ("trending", _TRENDING),
        ("price", _PRICE),
        ("news", _NEWS),
    ]
    return next((kind for kind, pattern in patterns if pattern.search(text)), None)


def suggested_actions(
    message: str,
    intent: str | None = None,
    conversation_history: str = "",
    capabilities: tuple[str, ...] | list[str] = (),
    assistant_answer: str = "",
    preferred_token_address: str | None = None,
    preferred_token_chain: str | None = None,
) -> list[str]:
    """Return up to four useful next prompts without spending model tokens."""
    topic_text = _contextual_topic(message, conversation_history)
    topic = _latest_topic(topic_text)
    capability_set = set(capabilities)
    if "equity_research" in capability_set or _EQUITY.search(message):
        match = _TICKER.search(message)
        if match:
            ticker = match.group(1)
            return [f"Show the latest earnings and guidance for {ticker}", f"Review valuation and fundamentals for {ticker}", f"Summarize recent news and catalysts for {ticker}", f"Compare {ticker} with its closest listed peers"]
        return ["Show the latest earnings and guidance", "Review valuation and fundamentals", "Summarize recent company news", "Compare analyst expectations"]
    explicit_token = bool(_TOKEN_CONTEXT.search(message) and not _PORTFOLIO.search(message))
    wallet_subject = bool(_PORTFOLIO.search(message) or "wallet_intelligence" in capability_set)
    token_reference = (
        TokenReference(preferred_token_address, preferred_token_chain)
        if preferred_token_address
        else extract_token_reference(message, assistant_answer)
    )
    if token_reference is None and _FOLLOWUP.search(message):
        token_reference = extract_token_reference(conversation_history)
    if token_reference is None:
        named = _NAMED_TOKEN.search(message)
        if named and named.group(1).lower() not in {"this", "that", "the", "a", "an"}:
            chain_match = re.search(
                r"\b(solana|base|ethereum|arbitrum|optimism|polygon|bnb|avalanche|robinhood(?:\s+chain)?)\b",
                message,
                re.IGNORECASE,
            )
            chain = chain_match.group(1).lower().replace(" chain", "") if chain_match else None
            token_reference = TokenReference(named.group(1).upper(), chain)
    if token_reference is None and "market_data" in capability_set:
        address_match = _EVM_ADDRESS.search(message) or _SOLANA_ADDRESS.search(message)
        if address_match:
            chain_match = re.search(
                r"\b(solana|base|ethereum|arbitrum|optimism|polygon|bnb|avalanche|robinhood(?:\s+chain)?)\b",
                message,
                re.IGNORECASE,
            )
            chain = chain_match.group(1).lower().replace(" chain", "") if chain_match else None
            token_reference = TokenReference(address_match.group(0), chain)
    wallet_reference = extract_wallet_reference(message, assistant_answer, conversation_history)
    if "wallet_transactions" in capability_set:
        actions = [
            "View Portfolio",
            "Show my SPL token holdings",
            "Check my wallet health",
            "Show my SOL balance",
        ]
    elif "token_holdings" in capability_set:
        actions = [
            "View Portfolio",
            "Show my SOL balance",
            "Check my wallet health",
            "Show my recent transactions",
        ]
    elif "wallet_health" in capability_set:
        actions = [
            "What if my portfolio drops 20%?",
            "Show my current portfolio",
            "Show my unverified token holdings",
            "Check my recent transactions",
        ]
    elif "token_balance" in capability_set:
        balance_match = re.search(
            r"\bhow\s+much(?:\s+of)?\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+(?:do\s+)?i\s+have\b",
            message,
            re.IGNORECASE,
        )
        asset = balance_match.group(1).upper() if balance_match else "this token"
        actions = [
            "View Portfolio",
            f"Show the current price of {asset}",
            f"Check {asset} token safety",
            "Show my recent transactions",
        ]
    elif "portfolio_scenario" in capability_set:
        actions = [
            "Check my wallet health",
            "Show my current portfolio",
            "What if SOL rises 20% in my portfolio?",
            "What if my portfolio drops 40%?",
        ]
    elif is_execution_explanation(message):
        actions = [
            "Show supported Relay chains",
            "Compare bridge fees from Base to Solana",
            "Explain wallet approval security",
            "Start a cross-chain swap",
        ]
    elif intent == "cross_chain_swap":
        subject = (
            token_reference.address
            + (f" on {token_reference.chain.title()}" if token_reference.chain else "")
            if token_reference else "this token"
        )
        actions = [
            f"Check token safety for {subject}",
            f"Show liquidity and volume for {subject}",
            "Compare current bridge fees",
            "View Portfolio",
        ]
    elif is_crypto_trends_query(message) or topic == "trending":
        actions = [
            "Show new pairs on Base",
            "Show trending tokens on Solana",
            "Show trending tokens on Robinhood",
            "View Portfolio",
        ]
    elif (
        explicit_token
        or topic in {"token", "token_security"} and not wallet_subject
        or bool(capability_set & {"token_discovery", "token_security", "market_data"})
        and token_reference is not None
        and "wallet_intelligence" not in capability_set
    ):
        if token_reference:
            subject = token_reference.address + (f" on {token_reference.chain.title()}" if token_reference.chain else "")
            actions = [
                f"Check token safety for {subject}",
                f"Show liquidity and volume for {subject}",
                f"Show top holders for {subject}",
                f"Show recent DEX trades for {subject}",
            ]
        else:
            actions = [
                "Check this token's safety",
                "Show this token's liquidity and volume",
                "Show this token's top holders",
                "Show this token's recent DEX trades",
            ]
    elif _EVM_ADDRESS.search(message) or _PORTFOLIO.search(message) or topic == "wallet":
        if wallet_reference:
            subject = wallet_reference.address + (
                f" on {wallet_reference.chain.title()}" if wallet_reference.chain else ""
            )
            actions = [
                f"Show current portfolio for {subject}",
                f"Review leverage risk for {subject}",
                f"Show recent transactions for {subject}",
                f"Check counterparties for {subject}",
            ]
        else:
            actions = [
                "Show this wallet's current portfolio",
                "Review this wallet's leverage risk",
                "Show this wallet's recent transactions",
                "Check this wallet's counterparties",
            ]
    elif _PRICE.search(message) or topic == "price":
        actions = [
            "Compare Bitcoin and Ethereum",
            "Show the 7-day price trend",
            "What's trending in crypto right now?",
            "View Portfolio",
        ]
    elif _TRADE.search(message) or topic == "trade":
        actions = [
            "Check token safety",
            "Review current market price",
            "View Portfolio",
            "What's trending in crypto right now?",
        ]
    elif (_NEWS.search(message) or topic == "news") and "equity_research" not in capability_set:
        actions = [
            "Show more recent developments",
            "Summarize the key risks",
            "Compare the strongest sources",
            "What's trending in crypto right now?",
        ]
    elif topic == "equity" or "equity_research" in capability_set:
        ticker_match = _TICKER.search(message)
        if ticker_match:
            ticker = ticker_match.group(1)
            actions = [
                f"Show the latest earnings and guidance for {ticker}",
                f"Review valuation and fundamentals for {ticker}",
                f"Summarize recent news and catalysts for {ticker}",
                f"Compare {ticker} with its closest listed peers",
            ]
        else:
            actions = [
                "Show the latest earnings",
                "Review valuation and fundamentals",
                "Summarize recent company news",
                "Compare analyst expectations",
            ]
    elif topic == "architecture":
        actions = [
            "Show the request routing flow",
            "Explain provider fallback",
            "Explain the wallet security model",
            "Open the admin dashboard",
        ]
    else:
        actions = [
            "What's trending in crypto right now?",
            "Show the Bitcoin price now",
            "Research SOL token safety",
            "View Portfolio",
        ]

    normalized_message = " ".join(message.lower().split()).rstrip("?.!")
    return [
        action
        for action in actions
        if " ".join(action.lower().split()).rstrip("?.!") != normalized_message
    ][:4]


def structured_quick_actions(
    suggestions: list[str],
    context_revision: int,
    preferred_token: str | None = None,
    preferred_chain: str | None = None,
) -> list[QuickAction]:
    """Attach routing and entity data to UI actions instead of trusting labels."""
    actions: list[QuickAction] = []
    for index, prompt in enumerate(suggestions):
        lowered = prompt.lower()
        prompt_token = extract_token_reference(prompt)
        prompt_wallet = extract_wallet_reference(prompt)
        entity_value = preferred_token or (prompt_token.address if prompt_token else None)
        entity_chain = preferred_chain or (prompt_token.chain if prompt_token else None)
        if prompt_wallet:
            intent = "research"
            capabilities = ["wallet_intelligence"]
            wallet_scope = None
            entity_value = None
            entity_chain = prompt_wallet.chain
        elif (
            "portfolio" in lowered
            or "my recent transactions" in lowered
            or "my spl token holdings" in lowered
            or "my wallet health" in lowered
            or bool(re.search(r"\bmy\s+[a-z0-9._-]+\s+balance\b", lowered))
        ):
            intent = "portfolio"
            if "transactions" in lowered:
                capabilities = ["wallet_transactions", "wallet_intelligence"]
            elif "spl token holdings" in lowered:
                capabilities = ["token_holdings", "portfolio", "wallet_intelligence"]
            elif "wallet health" in lowered:
                capabilities = ["wallet_health", "portfolio", "wallet_intelligence"]
            elif "balance" in lowered:
                capabilities = ["token_balance", "portfolio", "wallet_intelligence"]
            else:
                capabilities = ["portfolio", "wallet_intelligence"]
            wallet_scope = "connected"
            entity_value = None
            entity_chain = None
        elif any(word in lowered for word in ("token safety", "liquidity", "holders", "dex trades", "current price", "trending", "new pairs", "bridge fees")):
            intent = "research"
            capabilities = ["token_security", "token_discovery"] if "safety" in lowered else ["market_data"]
            wallet_scope = None
        elif any(word in lowered for word in ("start a cross-chain swap", "start a swap")):
            intent = "cross_chain_swap"
            capabilities = ["cross_chain_swap", "token_resolve", "token_security"]
            wallet_scope = "connected"
        else:
            intent = "research"
            capabilities = ["web_research"]
            wallet_scope = None
        slug = re.sub(r"[^a-z0-9]+", ".", prompt.lower()).strip(".")[:55]
        actions.append(QuickAction(
            id=f"{slug or 'followup'}.{index}",
            prompt=prompt,
            intent=intent,
            capabilities=capabilities,
            chain=entity_chain,
            entity_kind="token" if entity_value else "wallet" if prompt_wallet else None,
            entity_label=entity_value if entity_value and len(entity_value) <= 20 else None,
            entity_address=entity_value if entity_value and len(entity_value) > 20 else (
                prompt_wallet.address if prompt_wallet else None
            ),
            wallet_scope=wallet_scope,
            context_revision=context_revision,
        ))
    return actions
