"""Deterministic completion of safe, multi-turn Solana swap fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from app.context_entities import extract_token_reference
from app.jupiter import WRAPPED_SOL_MINT, normalize_mint


_NULL_LIKE = {"", "null", "none", "nil", "n/a", "undefined"}
_SOLANA_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SOL_AMOUNT = re.compile(r"(?<![\w.])((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*SOL\b", re.IGNORECASE)
_SLIPPAGE_BPS = re.compile(r"\b(\d{1,3})\s*(?:bps?|basis\s+points?)\b", re.IGNORECASE)
_CONTEXTUAL_TOKEN = re.compile(
    r"\b(?:this|that|the|selected|it)\s+(?:token|coin|memecoin)?\b",
    re.IGNORECASE,
)
_OUTPUT_SYMBOL = re.compile(
    r"\b(?:buy|to|into|for)\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b",
    re.IGNORECASE,
)


def _history_binds_symbol_to_address(history: str, symbol: str, address: str) -> bool:
    """Require a named token and mint to occur in the same chat exchange."""
    turns = re.split(r"(?m)(?=^(?:user|assistant):\s)", history)
    symbol_pattern = re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])", re.IGNORECASE)
    for index, turn in enumerate(turns):
        if address not in turn:
            continue
        exchange = (turns[index - 1] if index else "") + "\n" + turn
        if symbol_pattern.search(exchange):
            return True
    return False


def clean_mint(value: object) -> str | None:
    """Reject model null sentinels and normalize harmless formatting."""
    if value is None:
        return None
    mint = normalize_mint(str(value))
    return None if mint.lower() in _NULL_LIKE else mint


def complete_swap_fields(
    request: str,
    conversation_history: str,
    input_mint: object,
    output_mint: object,
    amount_atomic: int | None,
    slippage_bps: int | None,
) -> tuple[str | None, str | None, int | None, int | None]:
    """Fill only explicit facts from the current request or resolved token context.

    The model still identifies the trade, while this boundary prevents literal
    ``null`` values and reliably carries a previously resolved token mint into a
    short follow-up such as "buy for .02 SOL with 50 bps slippage".
    """
    resolved_input = clean_mint(input_mint)
    resolved_output = clean_mint(output_mint)

    sol_amount = _SOL_AMOUNT.search(request)
    if sol_amount:
        resolved_input = WRAPPED_SOL_MINT
        if amount_atomic is None:
            try:
                amount_atomic = int(Decimal(sol_amount.group(1)) * Decimal(1_000_000_000))
            except (InvalidOperation, ValueError):
                pass

    # Tickers are not safe execution identifiers. Prefer the exact token mint
    # already established in the conversation whenever the extracted output is
    # absent or is not itself a Solana mint.
    if not resolved_output or not _SOLANA_MINT.fullmatch(resolved_output):
        reference = extract_token_reference(request, conversation_history)
        symbol_match = _OUTPUT_SYMBOL.search(request)
        symbol = symbol_match.group(1) if symbol_match else (
            resolved_output
            if resolved_output and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{1,15}", resolved_output)
            else None
        )
        # In an elliptical follow-up such as "buy for .02 SOL", ``for`` is
        # grammar, not the requested ticker. Fall back to the model-resolved
        # symbol so it can be checked against the mint established in history.
        if symbol and symbol.lower() in {
            "for", "with", "using", "on", "the", "a", "an", "some", "token", "coin"
        }:
            symbol = (
                resolved_output
                if resolved_output and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{1,15}", resolved_output)
                else None
            )
        contextual = bool(_CONTEXTUAL_TOKEN.search(request))
        matches_named_token = bool(
            symbol
            and reference
            and _history_binds_symbol_to_address(
                conversation_history, symbol, reference.address
            )
        )
        if reference and reference.chain == "solana" and (contextual or matches_named_token):
            resolved_output = reference.address

    if slippage_bps is None:
        slippage = _SLIPPAGE_BPS.search(request)
        if slippage:
            slippage_bps = int(slippage.group(1))

    return resolved_input, resolved_output, amount_atomic, slippage_bps
