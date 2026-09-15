"""Orbit as an MCP server -- the copilot as a skill for Claude, ChatGPT and any
MCP host.

Mounted at /mcp (Streamable HTTP) by app/main.py, and runnable over stdio with
`python -m app.mcp_server` for desktop hosts. Every tool goes through the same
chat turn as POST /chat (admission, per-session lock, budgets, validation,
persistence), so an MCP session is an ordinary Orbit session that the web UI
can open by id -- that is the wallet hand-off: the host binds a public address
for read-only work, and anything that must be signed is confirmed by the user in
the browser with their own wallet. No key ever passes through this server.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.settings import settings

_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "orbit" / "SKILL.md"
_SKILL_TEXT = _SKILL_PATH.read_text(encoding="utf-8") if _SKILL_PATH.exists() else "Orbit Web3 copilot."
_INSTRUCTIONS = _SKILL_TEXT.split("---", 2)[-1].strip() if _SKILL_TEXT.startswith("---") else _SKILL_TEXT

_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

mcp = FastMCP(
    "orbit",
    instructions=_INSTRUCTIONS,
    streamable_http_path="/",
    json_response=True,
)


def handoff_url(session_id: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/ui/?session={session_id}"


def validate_address(address: str) -> tuple[str, str]:
    """Return (canonical address, chain family) or raise ValueError. Public
    addresses only; anything that looks like key material is refused."""
    value = (address or "").strip()
    if _EVM.match(value):
        return value, "evm"
    if _BASE58.match(value):
        try:
            import base58
            if len(base58.b58decode(value)) == 32:
                return value, "solana"
        except Exception:
            pass
    if len(value) >= 60 or value.count(" ") >= 11:
        raise ValueError("That looks like key material, not a public address. Orbit only accepts public wallet addresses.")
    raise ValueError("Not a recognised public wallet address (expected Solana base58 or EVM 0x…).")


async def _session_wallet(session_id: str | None) -> str | None:
    if not session_id:
        return None
    from app.sessions import get_session_context
    context = await get_session_context(session_id)
    return context.get("mcp_wallet_address") or None


def _response_payload(response: Any) -> dict:
    data = response.model_dump(mode="json")
    plan = data.get("trade_plan")
    draft = data.get("cross_chain_swap")
    out = {
        "answer": data["answer"],
        "session_id": data["session_id"],
        "session_revision": data["session_revision"],
        "intent": data["intent"],
        "capabilities": data.get("capabilities") or [],
        "providers": (data.get("evidence") or {}).get("providers") or [],
        "validation": (data.get("validation") or {}).get("status"),
        "risk_assessment": data.get("risk_assessment"),
        "team_mode": data.get("team_mode", False),
        "risk_charter": data.get("risk_charter"),
    }
    if plan or draft:
        out["quote"] = {
            "kind": "solana_swap" if plan else "cross_chain_swap",
            "plan_id": plan.get("plan_id") if plan else None,
            "expires_at": plan.get("expires_at") if plan else None,
            "draft": draft,
            "warnings": (plan or {}).get("warnings") or [],
        }
        out["handoff_url"] = handoff_url(data["session_id"])
        out["next_step"] = (
            "Nothing has been signed. Show the user handoff_url: it opens this session in the "
            "Orbit web UI where they connect their wallet, review the card and confirm."
        )
    return out


@mcp.tool()
async def orbit_chat(message: str, session_id: str | None = None, wallet_address: str | None = None) -> dict:
    """One turn of the Orbit Web3 copilot: research, portfolio, policy or a swap
    quote. Reuse the returned session_id on follow-ups. wallet_address is an
    optional PUBLIC address for this turn; otherwise the session's bound wallet
    (orbit_connect_wallet) is used."""
    from app.main import execute_chat_turn
    from app.models import ChatRequest

    wallet = wallet_address.strip() if wallet_address else await _session_wallet(session_id)
    if wallet:
        wallet, _ = validate_address(wallet)
    body = ChatRequest(message=message, session_id=session_id, wallet_address=wallet)
    response = await execute_chat_turn(body, identity="mcp")
    return _response_payload(response)


@mcp.tool()
async def orbit_connect_wallet(session_id: str | None, address: str) -> dict:
    """Attach a PUBLIC wallet address (Solana base58 or EVM 0x) to the session
    for read-only use: balances, exposure, scenarios and quotes sized to real
    holdings. Never send a private key or seed phrase."""
    from uuid import uuid4
    from app.sessions import get_session_context, save_session_context

    canonical, family = validate_address(address)
    sid = session_id or str(uuid4())
    context = await get_session_context(sid)
    context["mcp_wallet_address"] = canonical
    await save_session_context(sid, context)
    return {
        "session_id": sid,
        "wallet_address": canonical,
        "chain_family": family,
        "mode": "read_only",
        "signing": "Only in the user's own wallet via the web UI; use orbit_handoff_url when a quote needs confirming.",
    }


@mcp.tool()
async def orbit_prepare_swap(
    session_id: str | None,
    amount: str,
    input_token: str,
    output_token: str,
    source_chain: str,
    destination_chain: str | None = None,
    slippage_bps: int | None = None,
) -> dict:
    """Quote a swap: Jupiter for Solana-only, Relay for cross-chain or EVM. The
    result is a reviewed quote plus handoff_url; nothing executes until the
    user confirms in their own wallet. Requires a bound or supplied wallet."""
    wallet = await _session_wallet(session_id)
    if not wallet:
        return {
            "error": "no_wallet",
            "detail": "Bind a public wallet first with orbit_connect_wallet, or ask the user to open the hand-off link and connect one.",
            "handoff_url": handoff_url(session_id) if session_id else None,
        }
    route = f" on {source_chain}" if not destination_chain or destination_chain == source_chain else f" from {source_chain} to {destination_chain}"
    slip = f" with {slippage_bps} bps slippage" if slippage_bps else ""
    message = f"swap {amount} {input_token} to {output_token}{route}{slip}"
    return await orbit_chat(message=message, session_id=session_id, wallet_address=wallet)


@mcp.tool()
async def orbit_policy(session_id: str | None = None) -> dict:
    """The risk rules Orbit enforces for this session: built-in caps and the
    user's risk charter if one is set."""
    from app.nodes.general import policy_summary
    from app.sessions import get_session_context

    context = await get_session_context(session_id) if session_id else {}
    wallet = context.get("mcp_wallet_address") or ""
    return {"session_id": session_id, "policy": policy_summary({"session_context": context, "wallet_address": wallet})}


@mcp.tool()
async def orbit_handoff_url(session_id: str) -> dict:
    """A link that opens this session in the Orbit web UI, where the user can
    connect a wallet and confirm any pending quote with their own signature."""
    return {"session_id": session_id, "handoff_url": handoff_url(session_id),
            "note": "Wallet signing happens only in the browser; this conversation never holds keys."}


@mcp.resource("orbit://skill")
def skill_document() -> str:
    """The Orbit skill playbook (SKILL.md): tools, working rules, wallet flow."""
    return _SKILL_TEXT


@mcp.prompt()
def orbit_skill() -> str:
    """Load the Orbit skill guidance into the conversation."""
    return _INSTRUCTIONS


def main() -> None:
    """Run over stdio for desktop MCP hosts (Claude Desktop, IDEs)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
