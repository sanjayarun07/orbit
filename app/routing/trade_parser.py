"""Typed, side-effect-free extraction of execution fields from one message."""

from __future__ import annotations

import re

from .contracts import ExecutionDraft
from .lexicon import CHAIN_ALIASES, CHAIN_PATTERN

TOKEN = r"(?:0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}|[A-Za-z][A-Za-z0-9._-]{1,15})"
CHAIN = rf"(?:{CHAIN_PATTERN})"
AMOUNT = r"((?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))"
_NOT_A_TOKEN = {"bp", "bps", "basis", "point", "points", "slippage", "percent", "pct", "x", "of", "min", "mins", "minutes", "hours", "days"}
# Native gas tokens that identify exactly one supported chain. Multi-chain
# assets (ETH, USDC, USDT, BTC, ...) are deliberately excluded: assigning them
# a chain here would silently override an explicit or still-unstated chain.
_NATIVE_CHAIN = {
    "SOL": "solana", "BNB": "bnb", "AVAX": "avalanche",
    "POL": "polygon", "MATIC": "polygon", "SUI": "sui", "TRX": "tron",
}


def _canonical(value: str | None) -> str | None:
    return CHAIN_ALIASES.get(value.lower()) if value else None


def parse_execution_draft(request: str, chains: tuple[str, ...]) -> ExecutionDraft:
    """Extract only fields evidenced by the current user message."""
    source_token = output_token = swap_amount = None
    buy_match = re.search(
        rf"\bbuy\s+({TOKEN})(?:\s+(?:token|coin|memecoin|meme\s+coin))?"
        rf"(?:\s+on\s+[A-Za-z]+)?.{{0,50}}?\b(?:with|using|for)\s+{AMOUNT}\s*({TOKEN})\b",
        request, re.I,
    )
    if buy_match:
        output_token, swap_amount, source_token = buy_match.group(1), buy_match.group(2), buy_match.group(3)
        swap_amount = "0" + swap_amount if swap_amount.startswith(".") else swap_amount
    else:
        simple_buy = re.search(rf"\bbuy\s+({TOKEN})(?:\s+(?:token|coin|memecoin|meme\s+coin))?\b", request, re.I)
        if simple_buy and simple_buy.group(1).lower() not in {"for", "with", "on", "some", "the", "token", "coin"}:
            output_token = simple_buy.group(1)
        source_match = re.search(rf"\b(?:swap|bridge|send|exchange|convert|move|sell)\s+{AMOUNT}\s*({TOKEN})\b", request, re.I)
        if source_match:
            swap_amount, source_token = source_match.group(1), source_match.group(2)
            swap_amount = "0" + swap_amount if swap_amount.startswith(".") else swap_amount
        output_match = re.search(rf"\b(?:to|into|for)\s+({TOKEN})\s+on\s+{CHAIN}\b", request, re.I)
        if not output_match:
            output_match = re.search(rf"\b(?:to|into|for)\s+({TOKEN})(?:\s+(?:token|coin|memecoin|meme\s+coin))?\b", request, re.I)
        if output_match:
            output_token = output_match.group(1)
        if output_token is None and re.search(r"\bbridge\b", request, re.I):
            output_token = source_token

    if swap_amount is None:
        modifier_amount = re.search(rf"\b(?:amount(?:\s+to)?|make\s+it|use|set(?:\s+it)?(?:\s+to)?)\s+{AMOUNT}\s*({TOKEN})\b", request, re.I)
        if modifier_amount:
            candidate = modifier_amount.group(2)
            if candidate.lower() not in _NOT_A_TOKEN:
                swap_amount, source_token = modifier_amount.group(1), candidate
                swap_amount = "0" + swap_amount if swap_amount.startswith(".") else swap_amount
    if swap_amount is None:
        # A message that opens with the amount and no verb: ".01sol", "0.5 ETH
        # to USDC on Base with 50 bps". Common in follow-ups that amend a quote.
        bare = re.match(rf"^\s*{AMOUNT}\s*({TOKEN})\b(?=\s*$|\s+(?:on|from|to|into|for|with|max(?:imum)?)\b)", request, re.I)
        if bare and bare.group(2).lower() not in _NOT_A_TOKEN:
            swap_amount, source_token = bare.group(1), bare.group(2)
            swap_amount = "0" + swap_amount if swap_amount.startswith(".") else swap_amount

    recipient = re.search(r"\b(?:recipient|receive at|send to(?:\s+address)?)\s*[:=]?\s*(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b", request, re.I)
    slippage = re.search(r"\b(?:max(?:imum)?\s+)?(\d{1,5})\s*(?:bps?|basis\s+points?)\b", request, re.I)
    source_chain = destination_chain = None

    source_modifier = re.search(rf"\b(?:source|from)(?:\s+chain)?\s*(?:to|=|:)?\s*({CHAIN_PATTERN})\b", request, re.I)
    destination_modifier = re.search(rf"\b(?:destination|target)(?:\s+chain)?\s*(?:to|=|:)?\s*({CHAIN_PATTERN})\b", request, re.I)
    if source_modifier:
        source_chain = _canonical(source_modifier.group(1))
    if destination_modifier:
        destination_chain = _canonical(destination_modifier.group(1))
    if (source_modifier or destination_modifier) and output_token and _canonical(output_token):
        output_token = None

    explicit_source = re.search(rf"\bfrom\s+({CHAIN_PATTERN})\b", request, re.I)
    if explicit_source and source_chain is None:
        source_chain = _canonical(explicit_source.group(1))
    if source_token:
        token_source = re.search(rf"{re.escape(source_token)}\s+on\s+({CHAIN_PATTERN})\b", request, re.I)
        if token_source and not buy_match:
            source_chain = _canonical(token_source.group(1))
    if output_token:
        token_destination = re.search(rf"{re.escape(output_token)}(?:\s+(?:token|coin|memecoin|meme\s+coin))?\s+on\s+({CHAIN_PATTERN})\b", request, re.I)
        if token_destination:
            destination_chain = _canonical(token_destination.group(1))

    # A single-chain native asset (e.g. paying with SOL) is unambiguous chain
    # evidence on its own. Only applied when nothing more explicit already set
    # the field, so an explicit "TOKEN on CHAIN" mention always wins.
    if source_chain is None and source_token:
        source_chain = _NATIVE_CHAIN.get(source_token.upper())
    if destination_chain is None and output_token:
        destination_chain = _NATIVE_CHAIN.get(output_token.upper())

    if len(chains) > 1:
        source_chain = source_chain or chains[0]
        destination_chain = destination_chain or next((item for item in chains if item != source_chain), chains[-1])
    elif len(chains) == 1:
        only_chain = chains[0]
        if source_chain is None and destination_chain is None and not destination_modifier:
            source_chain = only_chain
        if buy_match or (output_token and re.search(r"\bbuy\b", request, re.I)):
            destination_chain = destination_chain or only_chain
            source_chain = source_chain or destination_chain
        elif re.search(r"\bsell\b", request, re.I) and destination_chain:
            source_chain = destination_chain

    if source_token is None:
        proportional = re.search(rf"\b(?:swap|bridge|send|exchange|convert|move|sell)\s+(?:all|half|max|maximum)\s+({TOKEN})\b", request, re.I)
        if proportional:
            source_token = proportional.group(1)

    return ExecutionDraft(
        source_chain, destination_chain, swap_amount, source_token, output_token,
        recipient.group(1) if recipient else None,
        int(slippage.group(1)) if slippage else None,
    )


def extract_cross_chain_draft(request: str, chains: tuple[str, ...]) -> dict:
    """Compatibility adapter for existing callers and API payloads."""
    return parse_execution_draft(request, chains).as_dict()
