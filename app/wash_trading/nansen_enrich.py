"""Nansen entity-label enrichment for flagged wallets.

Reuses the already-registered `address_labels` Nansen MCP tool exactly as
app/nodes/research.py's `_nansen_wallet_tool_call` does for chat requests --
no new Nansen integration work, just a batched wrapper for a list of
wallets instead of one wallet per chat turn.
"""

from __future__ import annotations

from app.nodes import runtime


def label_wallet(address: str, chain: str | None = "solana") -> str:
    """Return Nansen's raw formatted label/entity text for one wallet, or a
    short placeholder if the address_labels tool isn't discovered or the
    call fails. Synchronous -- call via asyncio.to_thread from an async
    context, same convention as the rest of this codebase's MCP tool calls.
    """
    tool = runtime._mcp_registry.get("address_labels")
    if tool is None:
        return "Nansen address_labels tool not discovered."
    payload = {"address": address}
    if chain:
        payload["chain"] = chain
    try:
        return tool(request=payload)
    except Exception as exc:  # noqa: BLE001 -- best-effort enrichment, never fatal to the run
        return f"Nansen label lookup failed: {exc}"


def label_wallets(addresses: list[str], chain: str | None = "solana") -> dict[str, str]:
    return {address: label_wallet(address, chain) for address in addresses}
