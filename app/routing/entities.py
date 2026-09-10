"""Deterministic extraction of chain and address evidence."""

from __future__ import annotations

import re

from .lexicon import CHAIN_ALIASES


EVM_ADDRESS = re.compile(r"(?<![A-Za-z0-9])0x[0-9a-fA-F]{40}(?![A-Za-z0-9])")
SOLANA_ADDRESS = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")


def extract_chains(request: str) -> tuple[str, ...]:
    """Return canonical chains in mention order, without duplicates."""
    matches: list[tuple[int, str]] = []
    lowered = request.lower()
    for alias, canonical in CHAIN_ALIASES.items():
        for match in re.finditer(rf"\b{re.escape(alias)}\b", lowered):
            matches.append((match.start(), canonical))
    return tuple(dict.fromkeys(canonical for _, canonical in sorted(matches)))


def has_evm_address(request: str) -> bool:
    return bool(EVM_ADDRESS.search(request))


def has_solana_address(request: str) -> bool:
    return bool(SOLANA_ADDRESS.search(request))
