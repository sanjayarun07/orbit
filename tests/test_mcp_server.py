import asyncio

import pytest
from fastapi.testclient import TestClient

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

    monkeypatch.setattr(main, "run_agent", fake_run)
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
