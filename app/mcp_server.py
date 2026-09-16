"""Orbit as an MCP server -- the whole copilot as a skill for Claude, ChatGPT
and any MCP host, with the same surface the web UI has.

Mounted at /mcp (Streamable HTTP) by app/main.py, and runnable over stdio with
`python -m app.mcp_server` for desktop hosts. Four families of tools:

- Conversation: the chat turn itself (research, portfolio, quotes), session
  controls (wallet binding, team desk, risk charter), history.
- Analytics: the deterministic endpoints the web UI calls directly (portfolio
  snapshot, wallet health, price-shock scenarios, market overview, deep dive,
  trade simulation, execution status, capabilities, route preview).
- Data: every read-only provider tool in the ProviderRouter, one MCP tool each
  (`orbit_data_<tool>`), plus a proxy for discovered MCP-server tools (Nansen).
- Resources/prompts: the skill playbook, capabilities, health, a session's
  history and policy.

Everything conversational goes through the same chat turn as POST /chat
(admission, per-session lock, budgets, validation, persistence), so an MCP
session is an ordinary Orbit session the web UI can open by id -- that is the
wallet hand-off: the host binds a public address for read-only work, and
anything that must be signed is confirmed by the user in the browser with
their own wallet. No key ever passes through this server, and nothing here can
sign, submit or move funds.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP

from app.settings import settings
from app.service_errors import ServiceError

_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "orbit" / "SKILL.md"
_SKILL_TEXT = _SKILL_PATH.read_text(encoding="utf-8") if _SKILL_PATH.exists() else "Orbit Web3 copilot."
_INSTRUCTIONS = _SKILL_TEXT.split("---", 2)[-1].strip() if _SKILL_TEXT.startswith("---") else _SKILL_TEXT

_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,96}$")

mcp = FastMCP(
    "orbit",
    instructions=_INSTRUCTIONS,
    streamable_http_path="/",
    json_response=True,
)


# --------------------------------------------------------------------------- helpers

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


async def _guard(session_id: str | None, claim: bool = False) -> None:
    """The ownership rule of app.session_access for the MCP caller (the
    identity the admission middleware resolved from the bearer key). A denied
    conversation is a 404 here as on HTTP, so ids cannot be probed."""
    from app.identity import current_identity
    from app.session_access import SessionAccessDenied, require_session_access

    try:
        await require_session_access(session_id, current_identity.get(), claim=claim)
    except SessionAccessDenied as exc:
        raise ServiceError(404, "Conversation not found") from exc


async def _context(session_id: str | None) -> dict:
    if not session_id:
        return {}
    await _guard(session_id)
    from app.sessions import get_session_context
    return await get_session_context(session_id)


async def _session_wallet(session_id: str | None) -> str | None:
    return (await _context(session_id)).get("mcp_wallet_address") or None


async def _resolve_wallet(session_id: str | None, wallet_address: str | None) -> str | None:
    wallet = wallet_address.strip() if wallet_address else await _session_wallet(session_id)   # raises 404 when not the caller's
    if wallet:
        wallet, _ = validate_address(wallet)
    return wallet


def _error(exc: Exception) -> dict:
    if isinstance(exc, (HTTPException, ServiceError)):
        return {"error": exc.detail, "status": exc.status_code}
    return {"error": str(exc) or exc.__class__.__name__}


async def _turn(message: str, session_id: str | None, wallet: str | None, **extra) -> dict:
    from app.execution_policy import execute_chat_turn
    from app.models import ChatRequest

    body = ChatRequest(message=message, session_id=session_id, wallet_address=wallet, **extra)
    try:
        await _guard(session_id, claim=True)    # a chat turn owns the conversation before any work
        return _response_payload(await execute_chat_turn(body, identity="mcp"))
    except (HTTPException, ServiceError) as exc:
        return _error(exc)


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
        "risk_charter_fields": data.get("risk_charter_fields"),
        "context_capsules": data.get("context_capsules") or [],
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


# ----------------------------------------------------------------- conversation

@mcp.tool()
async def orbit_chat(message: str, session_id: str | None = None, wallet_address: str | None = None) -> dict:
    """One turn of the Orbit Web3 copilot: research, portfolio, policy or a swap
    quote -- exactly what the web chat does. Reuse the returned session_id on
    follow-ups. wallet_address is an optional PUBLIC address for this turn;
    otherwise the session's bound wallet (orbit_connect_wallet) is used."""
    return await _turn(message, session_id, await _resolve_wallet(session_id, wallet_address))


@mcp.tool()
async def orbit_connect_wallet(session_id: str | None, address: str) -> dict:
    """Attach a PUBLIC wallet address (Solana base58 or EVM 0x) to the session
    for read-only use: balances, exposure, scenarios and quotes sized to real
    holdings. Never send a private key or seed phrase."""
    from uuid import uuid4
    from app.sessions import get_session_context, save_session_context

    canonical, family = validate_address(address)
    sid = session_id or str(uuid4())
    try:
        await _guard(sid, claim=True)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)
    context = await get_session_context(sid)
    context["mcp_wallet_address"] = canonical
    await save_session_context(sid, context)
    return {
        "session_id": sid, "wallet_address": canonical, "chain_family": family, "mode": "read_only",
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
    user confirms in their own wallet. Requires a bound wallet."""
    wallet = await _session_wallet(session_id)
    if not wallet:
        return {"error": "no_wallet",
                "detail": "Bind a public wallet first with orbit_connect_wallet, or ask the user to open the hand-off link and connect one.",
                "handoff_url": handoff_url(session_id) if session_id else None}
    route = f" on {source_chain}" if not destination_chain or destination_chain == source_chain else f" from {source_chain} to {destination_chain}"
    slip = f" with {slippage_bps} bps slippage" if slippage_bps else ""
    return await _turn(f"swap {amount} {input_token} to {output_token}{route}{slip}", session_id, wallet)


@mcp.tool()
async def orbit_trade_simulation(session_id: str | None, amount: str, input_token: str, output_token: str) -> dict:
    """Read-only 'what would I get' simulation against a real Jupiter quote for
    the bound wallet. Never creates a plan or a card."""
    wallet = await _session_wallet(session_id)
    if not wallet:
        return {"error": "no_wallet", "detail": "Bind a public wallet first with orbit_connect_wallet."}
    return await _turn(f"what would happen if I sold {amount} {input_token} for {output_token}", session_id, wallet)


@mcp.tool()
async def orbit_set_team_mode(session_id: str | None, enabled: bool) -> dict:
    """Turn the multi-agent trading desk on or off for this session (same as the
    web UI's Team desk switch). Slower and costlier per turn when on."""
    return await _turn("team status", session_id, await _session_wallet(session_id), team_mode=enabled)


@mcp.tool()
async def orbit_set_risk_charter(
    session_id: str | None,
    max_trade_usd: float | None = None,
    max_position_pct: float | None = None,
    max_slippage_bps: int | None = None,
    verified_only: bool = False,
    allowed_chains: list[str] | None = None,
    notes: str | None = None,
) -> dict:
    """Set the user's risk charter with exact rules (same card as the web UI).
    Fields are checked deterministically on every quote; notes are the only
    part interpreted by the Risk agent. Cannot exceed the built-in caps
    (orbit_risk_charter_limits)."""
    from app.models import RiskCharterFields

    try:
        fields = RiskCharterFields(max_trade_usd=max_trade_usd, max_position_pct=max_position_pct, max_slippage_bps=max_slippage_bps,
                                   verified_only=verified_only, allowed_chains=allowed_chains or [], notes=notes)
    except Exception as exc:
        return _error(exc)
    return await _turn("set my risk charter", session_id, await _session_wallet(session_id), risk_charter_fields=fields)


@mcp.tool()
async def orbit_clear_risk_charter(session_id: str) -> dict:
    """Remove the session's risk charter; trades return to advisory mode (the
    built-in caps still apply)."""
    return await _turn("clear my risk charter", session_id, await _session_wallet(session_id))


@mcp.tool()
async def orbit_risk_charter_limits() -> dict:
    """The built-in caps a charter can only tighten, and the chains it may name."""
    from app.models import TRADING_CHAINS
    return {"max_trade_usd": settings.max_trade_usd, "max_slippage_bps": settings.max_slippage_bps,
            "max_price_impact_pct": settings.max_price_impact_pct, "chains": list(TRADING_CHAINS)}


@mcp.tool()
async def orbit_policy(session_id: str | None = None) -> dict:
    """The risk rules Orbit enforces for this session: built-in caps and the
    user's risk charter if one is set."""
    from app.nodes.general import policy_summary

    try:
        context = await _context(session_id)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)
    return {"session_id": session_id, "policy": policy_summary({"session_context": context, "wallet_address": context.get("mcp_wallet_address") or ""})}


@mcp.tool()
async def orbit_history(session_id: str) -> dict:
    """The session's transcript and canonical context (revision, wallet, focus
    entity, risk charter, team mode) -- what GET /chat/history returns."""
    from app.sessions import get_messages, get_session_context

    try:
        await _guard(session_id, claim=True)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)
    messages = await get_messages(session_id)
    return {
        "session_id": session_id,
        "messages": [{k: m.get(k) for k in ("role", "content", "intent", "capabilities", "session_revision") if k in m} for m in messages],
        "context": await get_session_context(session_id),
        "handoff_url": handoff_url(session_id),
    }


@mcp.tool()
async def orbit_delete_history(session_id: str) -> dict:
    """Delete the session's server-side history (same as the web UI's delete)."""
    from app.sessions import acquire_session_turn, clear_history

    try:
        await _guard(session_id)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)
    try:
        lease = await acquire_session_turn(session_id)
    except asyncio.TimeoutError:
        return {"error": "This chat is still processing a request. Wait for it to finish before deleting it.", "status": 409}
    try:
        await clear_history(session_id)
    finally:
        await lease.release()
    return {"session_id": session_id, "deleted": True}


@mcp.tool()
async def orbit_handoff_url(session_id: str) -> dict:
    """A link that opens this session in the Orbit web UI, where the user can
    connect a wallet and confirm any pending quote with their own signature."""
    try:
        await _guard(session_id)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)
    return {"session_id": session_id, "handoff_url": handoff_url(session_id),
            "note": "Wallet signing happens only in the browser; this conversation never holds keys."}


# -------------------------------------------------------------------- analytics

@mcp.tool()
async def orbit_portfolio(session_id: str | None = None, wallet_address: str | None = None) -> dict:
    """Wallet snapshot: SOL and SPL balances, DeFi and Hyperliquid positions,
    USD values (GET /portfolio/{wallet})."""
    from app.portfolio import build_portfolio_snapshot

    try:
        wallet = await _resolve_wallet(session_id, wallet_address)
    except (HTTPException, ServiceError) as exc:      # the session is not the caller's
        return _error(exc)
    if not wallet:
        return {"error": "no_wallet", "detail": "Pass wallet_address or bind one with orbit_connect_wallet."}
    try:
        return await build_portfolio_snapshot(wallet)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def orbit_wallet_health(session_id: str | None = None, wallet_address: str | None = None) -> dict:
    """Deterministic wallet diagnostics: concentration, dust, unpriced assets,
    stablecoin share (GET /wallet-health/{wallet})."""
    from app.portfolio import build_portfolio_snapshot
    from app.wallet_insights import wallet_health

    try:
        wallet = await _resolve_wallet(session_id, wallet_address)
    except (HTTPException, ServiceError) as exc:      # the session is not the caller's
        return _error(exc)
    if not wallet:
        return {"error": "no_wallet", "detail": "Pass wallet_address or bind one with orbit_connect_wallet."}
    try:
        return wallet_health(await build_portfolio_snapshot(wallet))
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def orbit_portfolio_scenario(change_pct: float, session_id: str | None = None, wallet_address: str | None = None, symbol: str | None = None) -> dict:
    """Price-shock simulation on the wallet's real holdings: change_pct applied
    to one symbol or to everything (POST /portfolio/{wallet}/scenario)."""
    from app.portfolio import build_portfolio_snapshot
    from app.wallet_insights import portfolio_scenario

    try:
        wallet = await _resolve_wallet(session_id, wallet_address)
    except (HTTPException, ServiceError) as exc:      # the session is not the caller's
        return _error(exc)
    if not wallet:
        return {"error": "no_wallet", "detail": "Pass wallet_address or bind one with orbit_connect_wallet."}
    try:
        return portfolio_scenario(await build_portfolio_snapshot(wallet), change_pct, symbol)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
async def orbit_market_overview(session_id: str | None = None) -> dict:
    """The crypto market overview card: BTC/ETH/SOL quotes, total market cap,
    Fear & Greed, dominance, DEX volume, TVL, top movers, trending."""
    return await _turn("how is the crypto market today", session_id, None)


@mcp.tool()
async def orbit_token_deep_dive(token: str, chain: str | None = None, session_id: str | None = None) -> dict:
    """Ten-dimension token due diligence with anti-hallucination rules and
    role-memory lessons. token is a symbol, name or contract; chain narrows an
    ambiguous symbol (otherwise Orbit asks which chain)."""
    return await _turn(f"deep dive on {token}" + (f" on {chain}" if chain else ""), session_id, await _session_wallet(session_id))


@mcp.tool()
async def orbit_trade_plan(plan_id: str) -> dict:
    """A quoted plan by id: tokens, amounts, route, warnings, status, expiry."""
    from app.plans import get_plan

    try:
        plan = await get_plan(plan_id)
    except KeyError as exc:
        return {"error": str(exc), "status": 404}
    return plan.model_dump(mode="json")


@mcp.tool()
async def orbit_execution_status(signature: str) -> dict:
    """Status of a submitted Solana transaction by signature (GET /executions/solana/{signature})."""
    if not _SIGNATURE.match(signature or ""):
        return {"error": "Invalid Solana transaction signature", "status": 400}
    from app.execution_policy import solana_execution_status

    try:
        return await solana_execution_status(signature)
    except (HTTPException, ServiceError) as exc:
        return _error(exc)


@mcp.tool()
async def orbit_relay_status(request_id: str) -> dict:
    """Tracked status of a Relay cross-chain execution by request id."""
    from app import relay_tracking

    status = await relay_tracking.get_status(request_id)
    return status or {"request_id": request_id, "status": "unknown"}


@mcp.tool()
async def orbit_capabilities() -> dict:
    """Every provider and MCP tool Orbit can route to, with configuration,
    quota, reliability and latency (GET /capabilities)."""
    from app.mcp_tools import get_mcp_registry
    from app.provider_registry import get_provider_router

    registry = get_mcp_registry()
    return {"tools": [*get_provider_router().catalog(), *registry.catalog()], "mcp_discovery_ready": registry.ready}


@mcp.tool()
async def orbit_route_preview(request: str, capability: str, chains: list[str] | None = None) -> dict:
    """Which provider tools Orbit would try, in order, for a request and
    capability -- a dry run, no provider is called."""
    from app.provider_registry import get_provider_router

    return {"capability": capability, "candidates": get_provider_router().preview(request, capability, tuple(chains or ()))}


@mcp.tool()
async def orbit_health() -> dict:
    """Service counters: provider calls, cache hits, rate limits, validation warnings, estimated spend."""
    from app.metrics import snapshot

    return {"status": "ok", "counters": snapshot()}


# ------------------------------------------------------------------- data tools

def _register_provider_tools() -> list[str]:
    """One MCP tool per read-only provider tool, so a host can call a specific
    data source directly -- through the router's quota, circuit breaker, cache,
    budget and outcome accounting, with the request->tool matchers bypassed
    because the host chose the tool."""
    from app.provider_registry import get_provider_router

    names: list[str] = []
    for entry in get_provider_router().catalog():
        if entry.get("risk") != "read_only":
            continue
        tool_name = entry["name"]

        def make(name: str):
            async def call(request: str, chains: list[str] | None = None) -> dict:
                # Resolve the router per call: it is a rebuildable singleton
                # (admin overrides, tests), never something to capture at import.
                try:
                    result = await asyncio.to_thread(get_provider_router().invoke, name, request, tuple(chains or ()))
                except KeyError:
                    return {"error": f"unknown tool {name}", "status": 404}
                if result is None:
                    return {"error": "The provider is unavailable, unconfigured, over quota, or failed for this request.", "tool": name}
                return {"tool": result.tool, "provider": result.provider, "cached": result.cached, "output": result.output}
            return call

        chains = ", ".join(entry.get("chains") or []) or "any"
        caps = ", ".join(entry.get("capabilities") or [])
        description = (
            f"[{entry['provider']}] {entry.get('description') or tool_name.replace('_', ' ')}. "
            f"Capabilities: {caps}. Chains: {chains}. Read-only; call it with a natural-language request "
            f"(token symbol or address, wallet, chain) and optional chains filter."
        )
        mcp.add_tool(make(tool_name), name=f"orbit_data_{tool_name}", description=description)
        names.append(f"orbit_data_{tool_name}")
    return names


PROVIDER_TOOL_NAMES = _register_provider_tools()


@mcp.tool()
async def orbit_mcp_catalog() -> dict:
    """Tools discovered from Orbit's own MCP servers (e.g. Nansen): name,
    capabilities, chains, risk, reliability. Call one with orbit_mcp_call."""
    from app.mcp_tools import get_mcp_registry

    registry = get_mcp_registry()
    return {"ready": registry.ready, "tools": registry.catalog()}


@mcp.tool()
async def orbit_mcp_call(tool_name: str, arguments: dict | None = None) -> dict:
    """Call a discovered MCP-server tool by its full name (e.g.
    mcp_nansen_token_info) with a JSON arguments object. Read-only tools only;
    execution-risk tools are refused."""
    from app.nodes import runtime

    tool = runtime._mcp_registry.get(tool_name)
    if tool is None:
        return {"error": f"unknown MCP tool {tool_name}", "status": 404}
    if getattr(tool, "mcp_risk", "read_only") != "read_only":
        return {"error": "Only read-only MCP tools can be called from here.", "status": 403}
    try:
        output = await asyncio.to_thread(tool, **(arguments or {}))
    except Exception as exc:
        return _error(exc)
    return {"tool": tool_name, "output": output}


# --------------------------------------------------------- resources & prompts

@mcp.resource("orbit://skill")
def skill_document() -> str:
    """The Orbit skill playbook (SKILL.md): tools, working rules, wallet flow."""
    return _SKILL_TEXT


@mcp.resource("orbit://capabilities")
async def capabilities_resource() -> str:
    """JSON catalog of every provider and MCP tool Orbit can route to."""
    return json.dumps(await orbit_capabilities(), default=str)


@mcp.resource("orbit://health")
async def health_resource() -> str:
    """JSON service counters."""
    return json.dumps(await orbit_health(), default=str)


@mcp.resource("orbit://history/{session_id}")
async def history_resource(session_id: str) -> str:
    """JSON transcript and context of one session."""
    await _guard(session_id)          # a resource has no error envelope: the 404 propagates
    return json.dumps(await orbit_history(session_id), default=str)


@mcp.resource("orbit://policy/{session_id}")
async def policy_resource(session_id: str) -> str:
    """The trading policy in force for one session."""
    await _guard(session_id)
    return (await orbit_policy(session_id))["policy"]


@mcp.prompt()
def orbit_skill() -> str:
    """Load the Orbit skill guidance into the conversation."""
    return _INSTRUCTIONS


@mcp.prompt()
def orbit_deep_dive(token: str, chain: str | None = None) -> str:
    """Ask Orbit for a full due-diligence deep dive on a token."""
    where = f" on {chain}" if chain else ""
    return (f"Use the orbit_token_deep_dive tool for {token}{where}. Report the dimensions Orbit covered, the "
            "evidence gaps it named, its confidence and the single variable that would flip its rating. Do not add figures Orbit did not return.")


@mcp.prompt()
def orbit_market_brief() -> str:
    """Ask Orbit for the current crypto market overview."""
    return "Call orbit_market_overview and summarise the regime, leaders and laggards, and the data time it reports."


def main() -> None:
    """Run over stdio for desktop MCP hosts (Claude Desktop, IDEs)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
