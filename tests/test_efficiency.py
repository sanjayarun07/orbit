import asyncio

from app.mcp_tools import MCPToolRegistry, _placeholder_values, normalize_tool_arguments
from app.sessions import history_text
from app.settings import settings
from app.tool_results import compact_tool_result


def _tool(name: str, description: str):
    def call(**kwargs):
        return kwargs

    call.__name__ = f"mcp_test_{name}"
    call.mcp_tool_name = name
    call.mcp_description = description
    return call


def test_tool_results_are_bounded_and_preserve_both_ends():
    result = compact_tool_result("start-" + "x" * 200 + "-finish", max_chars=80)
    assert len(result) == 80
    assert result.startswith("start-")
    assert result.endswith("-finish")
    assert "compacted" in result


def test_registry_shortlists_relevant_future_tools_without_hardcoding():
    registry = MCPToolRegistry.__new__(MCPToolRegistry)
    portfolio = _tool("address_portfolio", "Get wallet holdings and balances")
    chart = _tool("token_ohlcv", "Get price candles")
    labels = _tool("address_labels", "Get wallet entity labels")
    registry._tools = [portfolio, chart, labels]

    selected = registry.select("show wallet portfolio holdings", limit=2)
    assert portfolio in selected
    assert chart not in selected


def test_native_bitcoin_does_not_select_contract_ohlcv_tool():
    registry = MCPToolRegistry.__new__(MCPToolRegistry)
    chart = _tool("token_ohlcv", "Get token price candles and OHLCV")
    search = _tool("general_search", "Search market data")
    registry._tools = [chart, search]

    selected = registry.select("What was the bitcoin BTC price?", limit=5)
    assert chart not in selected


def test_capability_ranking_prefers_provider_independent_match():
    registry = MCPToolRegistry.__new__(MCPToolRegistry)
    wallet = _tool("account_overview", "General account lookup")
    wallet.mcp_capabilities = ("wallet_intelligence",)
    market = _tool("market_overview", "General market lookup")
    market.mcp_capabilities = ("market_data",)
    registry._tools = [market, wallet]

    selected = registry.select("analyze this account", capabilities=("wallet_intelligence",), limit=1)
    assert selected == [wallet]


def test_execution_tools_are_not_exposed_to_research_agent():
    registry = MCPToolRegistry.__new__(MCPToolRegistry)
    execute = _tool("relay_execute", "Sign and submit a cross-chain swap")
    execute.mcp_capabilities = ("cross_chain_swap",)
    execute.mcp_risk = "financial_execution"
    quote = _tool("relay_quote", "Read a cross-chain quote")
    quote.mcp_capabilities = ("cross_chain_swap",)
    quote.mcp_risk = "financial_quote"
    registry._tools = [execute, quote]

    selected = registry.select("bridge funds", capabilities=("cross_chain_swap",), limit=5)
    assert quote in selected
    assert execute not in selected


def test_single_request_envelope_is_added_for_flattened_model_arguments():
    schema = {"properties": {"request": {"type": "object"}}, "required": ["request"]}
    arguments = {"chain": "bnb", "tokenAddress": "0xabc", "date": {"from": "1D_AGO", "to": "NOW"}}
    assert normalize_tool_arguments(schema, arguments) == {"request": arguments}


def test_existing_request_envelope_is_preserved():
    schema = {"properties": {"request": {"type": "object"}}, "required": ["request"]}
    arguments = {"request": {"chain": "bnb"}}
    assert normalize_tool_arguments(schema, arguments) is arguments


def test_history_context_is_character_bounded(monkeypatch):
    messages = [
        {"role": "user", "content": "old " * 500},
        {"role": "assistant", "content": "recent answer"},
    ]

    async def fake_messages(_session_id):
        return messages

    monkeypatch.setattr("app.sessions.get_messages", fake_messages)
    monkeypatch.setattr(settings, "max_context_chars", 80)
    text = asyncio.run(history_text("test"))
    assert len(text) <= 80
    assert "recent answer" in text


def test_mcp_placeholders_only_include_explicitly_allowed_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("NANSEN_API_KEY", "allowed")
    monkeypatch.setattr(settings, "mcp_env_allowlist", "NANSEN_API_KEY")
    values = _placeholder_values()
    assert values["NANSEN_API_KEY"] == "allowed"
    assert "OPENAI_API_KEY" not in values
