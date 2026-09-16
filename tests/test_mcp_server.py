import asyncio

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import main, mcp_server
from app.graph import AgentRun
from app.settings import settings

SOL = "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"
EVM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def test_tools_prompt_and_resource_are_registered():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"orbit_chat", "orbit_connect_wallet", "orbit_prepare_swap", "orbit_policy", "orbit_handoff_url"} <= names
    assert any(p.name == "orbit_skill" for p in asyncio.run(mcp_server.mcp.list_prompts()))
    body = "".join(c.content for c in asyncio.run(mcp_server.mcp.read_resource("orbit://skill")))
    assert "How wallet connection works" in body
    assert "never" in mcp_server.mcp.instructions.lower()


def test_address_validation_accepts_public_addresses_and_refuses_key_material():
    assert mcp_server.validate_address(SOL) == (SOL, "solana")
    assert mcp_server.validate_address(EVM) == (EVM, "evm")
    with pytest.raises(ValueError, match="key material"):
        mcp_server.validate_address("abandon " * 12)
    with pytest.raises(ValueError):
        mcp_server.validate_address("not-an-address")


def test_chat_tool_runs_a_real_turn_and_hands_off_quotes(monkeypatch):
    from app.models import SwapProposal, TokenInfo, TradePlan
    from datetime import datetime, timezone, timedelta

    seen = []

    async def fake_run(message, wallet, history, session_context, action):
        seen.append((message, wallet))
        plan = None
        if message.startswith("swap"):
            token = TokenInfo(mint="So11111111111111111111111111111111111111112", symbol="SOL", name="SOL", decimals=9, verified=True)
            now = datetime.now(timezone.utc)
            plan = TradePlan(plan_id="plan_mcp", status="pending_confirmation", created_at=now, expires_at=now + timedelta(seconds=120),
                             wallet_address=wallet, proposal=SwapProposal(input_mint=token.mint, output_mint=token.mint, amount_atomic=1, slippage_bps=50, reason="t"),
                             quote={}, input_token=token, output_token=token, simulation={}, confirmation_text="CONFIRM plan_mcp")
        return AgentRun(answer="ok", trajectory=None, trade_plan=plan, intent="trade" if plan else "general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    monkeypatch.setattr(settings, "public_base_url", "https://orbit.example.com")

    bound = asyncio.run(mcp_server.orbit_connect_wallet(None, SOL))
    sid = bound["session_id"]
    assert bound["mode"] == "read_only" and bound["chain_family"] == "solana"

    first = asyncio.run(mcp_server.orbit_chat("what do I hold", session_id=sid))
    assert first["session_id"] == sid and seen[-1] == ("what do I hold", SOL)  # bound wallet flows into the turn
    assert "handoff_url" not in first

    quote = asyncio.run(mcp_server.orbit_prepare_swap(sid, "0.1", "SOL", "USDC", "solana", slippage_bps=50))
    assert seen[-1][0] == "swap 0.1 SOL to USDC on solana with 50 bps slippage"
    assert quote["quote"]["plan_id"] == "plan_mcp" and quote["handoff_url"] == f"https://orbit.example.com/ui/?session={sid}"
    assert "Nothing has been signed" in quote["next_step"]

    unbound = asyncio.run(mcp_server.orbit_prepare_swap(None, "1", "SOL", "USDC", "solana"))
    assert unbound["error"] == "no_wallet"
    assert "Max per trade" in asyncio.run(mcp_server.orbit_policy(sid))["policy"]


def test_mcp_endpoint_requires_the_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "mcp_api_key", "mcp-secret")
    client = TestClient(main.app)
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/health").status_code == 200  # only /mcp is gated


def test_full_web_surface_is_exposed():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    core = {"orbit_chat", "orbit_connect_wallet", "orbit_prepare_swap", "orbit_trade_simulation", "orbit_set_team_mode",
            "orbit_set_risk_charter", "orbit_clear_risk_charter", "orbit_risk_charter_limits", "orbit_policy", "orbit_history",
            "orbit_delete_history", "orbit_handoff_url", "orbit_portfolio", "orbit_wallet_health", "orbit_portfolio_scenario",
            "orbit_market_overview", "orbit_token_deep_dive", "orbit_trade_plan", "orbit_execution_status", "orbit_relay_status",
            "orbit_capabilities", "orbit_route_preview", "orbit_health", "orbit_mcp_catalog", "orbit_mcp_call"}
    assert core <= set(tools)
    data_tools = [n for n in tools if n.startswith("orbit_data_")]
    assert len(data_tools) >= 30 and "orbit_data_birdeye_token_overview" in tools
    # The knowledge base is a data tool like any other: MCP clients can query it directly.
    assert "orbit_data_knowledge_base_search" in tools and "knowledge" in tools["orbit_data_knowledge_base_search"].description.lower()
    assert "birdeye" in tools["orbit_data_birdeye_token_overview"].description
    resources = {str(r.uri) for r in asyncio.run(mcp_server.mcp.list_resources())}
    assert {"orbit://skill", "orbit://capabilities", "orbit://health"} <= resources
    prompts = {p.name for p in asyncio.run(mcp_server.mcp.list_prompts())}
    assert {"orbit_skill", "orbit_deep_dive", "orbit_market_brief"} <= prompts


def test_data_tool_invokes_the_named_provider_tool_through_the_router(monkeypatch):
    from app.provider_registry import get_provider_router
    from app.provider_router import ProviderResult

    calls = []

    def fake_invoke(name, request, chains=()):
        calls.append((name, request, chains))
        return ProviderResult("**Price**: $1.23", name, "birdeye")

    monkeypatch.setattr(get_provider_router(), "invoke", fake_invoke)
    out = asyncio.run(mcp_server.mcp.call_tool("orbit_data_birdeye_token_overview", {"request": "price of BONK", "chains": ["solana"]}))
    payload = out[1] if isinstance(out, tuple) else out
    text = json.dumps(payload, default=str) if not isinstance(payload, list) else "".join(getattr(c, "text", "") for c in payload)
    assert calls == [("birdeye_token_overview", "price of BONK", ("solana",))]
    assert "$1.23" in text


def test_router_invoke_bypasses_matchers_but_refuses_non_read_only_and_unknown(monkeypatch):
    from app.provider_registry import get_provider_router
    router = get_provider_router()
    with pytest.raises(KeyError):
        router.invoke("no_such_tool", "x")
    tool = next(t for t in router._tools if t.name == "birdeye_token_overview")
    monkeypatch.setattr(router, "_route_ranked", lambda request, cap, cands: ("routed", cap, [t.name for t in cands()]))
    assert router.invoke("birdeye_token_overview", "anything at all")[2] == ["birdeye_token_overview"]
    monkeypatch.setattr(router, "_enabled", lambda t: False)
    assert router.invoke("birdeye_token_overview", "x") is None


def test_session_controls_go_through_the_chat_turn(monkeypatch):
    seen = []

    async def fake_run(message, wallet, history, session_context, action):
        seen.append((message, dict(session_context)))
        return AgentRun(answer=message, trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    monkeypatch.setattr(settings, "max_trade_usd", 25.0)
    on = asyncio.run(mcp_server.orbit_set_team_mode(None, True))
    sid = on["session_id"]
    assert on["team_mode"] is True and seen[-1][1]["team_mode"] is True
    charter = asyncio.run(mcp_server.orbit_set_risk_charter(sid, max_trade_usd=10, verified_only=True))
    assert charter["risk_charter"] == "max $10.00 per trade; only Jupiter-verified tokens"
    assert charter["risk_charter_fields"]["verified_only"] is True
    too_high = asyncio.run(mcp_server.orbit_set_risk_charter(sid, max_trade_usd=100))
    assert too_high["status"] == 400 and "built-in cap" in too_high["error"]
    cleared = asyncio.run(mcp_server.orbit_clear_risk_charter(sid))
    assert cleared["risk_charter"] is None
    history = asyncio.run(mcp_server.orbit_history(sid))
    assert [m["role"] for m in history["messages"]][:2] == ["user", "assistant"] and history["context"]["team_mode"] is True
    assert asyncio.run(mcp_server.orbit_delete_history(sid)) == {"session_id": sid, "deleted": True}
    assert asyncio.run(mcp_server.orbit_history(sid))["messages"] == []
    limits = asyncio.run(mcp_server.orbit_risk_charter_limits())
    assert limits["max_trade_usd"] == 25.0 and "solana" in limits["chains"]


def test_mcp_call_refuses_execution_tools_and_unknown_names(monkeypatch):
    from app.nodes import runtime

    def risky(**kwargs):
        return "should not run"

    risky.__name__ = "mcp_nansen_swap"
    risky.mcp_risk = "execution"
    registry_get = {"mcp_nansen_swap": risky}
    monkeypatch.setattr(runtime._mcp_registry, "get", lambda name: registry_get.get(name))
    assert asyncio.run(mcp_server.orbit_mcp_call("mcp_nansen_swap", {}))["status"] == 403
    assert asyncio.run(mcp_server.orbit_mcp_call("mcp_nansen_missing", {}))["status"] == 404
