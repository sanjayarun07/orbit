"""Fallback metadata inference for third-party MCP tools.

Curated provider registrations remain authoritative. These heuristics are used
only when a newly discovered MCP tool does not publish explicit metadata.
"""

from __future__ import annotations

import re

from .lexicon import CHAIN_ALIASES


def infer_tool_capabilities(name: str, description: str) -> tuple[str, ...]:
    normalized = name.lower()
    text = f"{normalized} {description[:500].lower()}"
    capabilities: list[str] = []
    if re.search(r"(?:price|ohlcv|candle|market|volume|dex_trades|flows?|indicators?|orderbook)", text):
        capabilities.append("market_data")
    if re.search(r"(?:discover|screener|search|sectors?|top_tokens|token_info|trending|new_pairs)", normalized):
        capabilities.append("token_discovery")
    if re.search(r"(?:security|safety|risk|holders?|liquidity|quant_scores?|token_info)", normalized):
        capabilities.append("token_security")
    if re.search(r"(?:wallet|address|portfolio|pnl|counterpart|labels?|transactions?|balances?|positions?)", normalized):
        capabilities.append("wallet_intelligence")
    if re.search(r"(?:portfolio|holdings?|balances?|positions?)", normalized):
        capabilities.append("portfolio")
    if re.search(r"(?:^|_)(?:swap|quote|exchange)(?:_|$)", normalized):
        capabilities.append("swap")
    if re.search(r"(?:cross_chain|bridge|route)", normalized):
        capabilities.append("cross_chain_swap")
    if re.search(r"(?:web|news|general_search)", normalized):
        capabilities.append("web_research")
    return tuple(capabilities or ("research",))


def infer_tool_risk(name: str, description: str) -> str:
    normalized = name.lower()
    description = description[:500].lower()
    if re.search(r"(?:^|_)(?:execute|submit|sign|send_transaction|place_order|create_order|buy|sell)(?:_|$)", normalized) or re.search(r"\b(?:sign and submit|execute (?:a |the )?transaction|place (?:an |the )?order|submit (?:a |the )?transaction)\b", description):
        return "financial_execution"
    if re.search(r"(?:^|_)(?:swap|bridge|transfer|quote|route)(?:_|$)", normalized):
        return "financial_quote"
    return "read_only"


def infer_tool_chains(description: str, schema: dict) -> tuple[str, ...]:
    values: list[str] = []

    def visit(value, chain_context: bool = False):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, chain_context or key.lower() in {"chain", "chains", "chainid", "chain_id"})
        elif isinstance(value, list):
            for item in value:
                visit(item, chain_context)
        elif chain_context and isinstance(value, str):
            values.append(value.lower())

    visit(schema)
    return tuple(dict.fromkeys(
        canonical for value in values for alias, canonical in CHAIN_ALIASES.items() if value == alias
    ))
