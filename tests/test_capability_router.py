from app.capability_router import (
    extract_cross_chain_draft,
    infer_tool_capabilities,
    infer_tool_risk,
    is_trade_cancellation,
    is_trade_confirmation,
    is_trade_modifier,
    route_capabilities,
)
from app.nodes.research import direct_mcp_request, wallet_portfolio_answer


def test_cross_chain_chat_routes_to_relay_workflow():
    route = route_capabilities("Swap USDC on Base to WIF on Solana")
    assert route is not None
    assert route.intent == "cross_chain_swap"
    assert route.capabilities[0] == "cross_chain_swap"
    assert route.chains == ("base", "solana")


def test_relay_explanation_does_not_enter_execution_workflow():
    for request in (
        "how relay bridge works?",
        "How does Relay bridging work?",
        "Explain how Relay cross-chain swap works",
    ):
        route = route_capabilities(request)
        assert route is not None
        assert route.intent == "general"
        assert route.capabilities == ()


def test_trending_chat_requests_market_and_discovery_capabilities():
    route = route_capabilities("What's trending in crypto right now?")
    assert route is not None
    assert route.intent == "research"
    assert set(route.capabilities) == {"market_data", "token_discovery", "web_research"}


def test_latest_token_profiles_use_discovery_capability():
    route = route_capabilities("Show latest token profiles on Base")
    assert route is not None
    assert "token_discovery" in route.capabilities
    assert route.chains == ("base",)


def test_us_and_india_equities_use_restricted_equity_capability():
    for request in (
        "Research Apple stock",
        "What's happening in the India stock market?",
        "Analyze NSE:RELIANCE",
        "Give me a report on $AAPL",
    ):
        route = route_capabilities(request)
        assert route is not None
        assert route.capabilities == ("equity_research",)


def test_recent_equity_move_is_not_mistaken_for_a_cross_chain_transfer():
    route = route_capabilities("What is driving $NVDA's recent move and what is the outlook?")
    assert route.intent == "research"
    assert route.capabilities == ("equity_research",)


def test_named_wallet_routes_to_wallet_intelligence():
    route = route_capabilities("Analyze 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460")
    assert route is not None
    assert route.capabilities == ("wallet_intelligence",)


def test_token_contract_address_is_not_routed_as_a_wallet():
    route = route_capabilities(
        "Show token contract details for 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    )
    assert route is not None
    assert "token_discovery" in route.capabilities
    assert "token_security" in route.capabilities
    assert "wallet_intelligence" not in route.capabilities


def test_named_wallet_analysis_starts_with_current_portfolio(monkeypatch):
    def portfolio_tool(**_kwargs):
        return ""

    portfolio_tool.__name__ = "mcp_nansen_address_portfolio"
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.get", lambda name: portfolio_tool if name == "address_portfolio" else None)
    request = direct_mcp_request("Analyze 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460")
    assert request == (
        "mcp_nansen_address_portfolio",
        {
            "request": {
                "walletAddress": "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
                "mode": "all",
            }
        },
    )


def test_token_contract_analysis_does_not_use_wallet_portfolio_fast_path(monkeypatch):
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.get", lambda _name: object())
    assert direct_mcp_request(
        "Analyze token contract 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    ) is None


def test_token_holder_request_uses_exact_nansen_tool(monkeypatch):
    def holder_tool(**_kwargs):
        return ""

    holder_tool.__name__ = "mcp_nansen_token_current_top_holders"
    monkeypatch.setattr(
        "app.nodes.runtime._mcp_registry.get",
        lambda name: holder_tool if name == "token_current_top_holders" else None,
    )
    mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    request = direct_mcp_request(f"Show top holders for {mint} on Solana")
    assert request[0] == "mcp_nansen_token_current_top_holders"
    assert request[1]["request"]["tokenAddress"] == mint
    assert request[1]["request"]["chain"] == "solana"


def test_wallet_portfolio_summary_exposes_leverage_and_debt_without_an_llm():
    answer = wallet_portfolio_answer(
        "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
        """## DeFi Summary
**Net Value (USD)**: 258.4
**Total Assets (USD)**: 3.1k
**Total Debts (USD)**: 2.9k
## Account Summary
**Account Value (USD)**: 6k
**Total Notional (USD)**: 56.2k
## Perp Positions
""",
    )
    assert "**$258.4**" in answer
    assert "**$2.9k in debt**" in answer
    assert "**$56.2k total notional**" in answer
    assert "substantial leveraged exposure" in answer


def test_tool_metadata_is_inferred_independently_of_provider():
    assert "wallet_intelligence" in infer_tool_capabilities(
        "address_portfolio", "Return wallet holdings and balances"
    )
    assert infer_tool_risk("relay_execute", "Sign and submit the route") == "financial_execution"


def test_read_only_market_language_is_not_mistaken_for_execution():
    assert infer_tool_risk("token_who_bought_sold", "Shows wallets that bought or sold") == "read_only"
    assert infer_tool_risk("token_discovery_screener", "Find tokens users may buy") == "read_only"


def test_natural_language_cross_chain_swap_is_prefilled():
    draft = extract_cross_chain_draft(
        "Swap 10 USDC on Base to WIF on Solana", ("base", "solana")
    )
    assert draft == {
        "source_chain": "base",
        "destination_chain": "solana",
        "amount": "10",
        "input_token": "USDC",
        "output_token": "WIF",
        "recipient": None,
        "slippage_bps": None,
    }


def test_natural_language_buy_is_prefilled():
    draft = extract_cross_chain_draft(
        "Buy WIF on Solana with 25 USDC from Base", ("solana", "base")
    )
    assert draft["amount"] == "25"
    assert draft["input_token"] == "USDC"
    assert draft["output_token"] == "WIF"
    assert draft["source_chain"] == "base"
    assert draft["destination_chain"] == "solana"


def test_incomplete_buy_preserves_new_token_subject():
    draft = extract_cross_chain_draft("buy ANSEM token", ())
    assert draft["output_token"] == "ANSEM"
    assert draft["amount"] is None
    assert draft["input_token"] is None


def test_symbol_only_buy_collects_chain_without_selecting_relay():
    route = route_capabilities("buy ANSEM token")
    assert route is not None
    assert route.intent == "trade"
    assert route.execution_provider is None
    assert route.mode == "collect"
    assert route.missing_fields == ("chain",)
    assert "cross_chain_swap" not in route.capabilities


def test_incomplete_buy_with_chain_preserves_token_and_destination():
    draft = extract_cross_chain_draft("buy ANSEM token on Solana", ("solana",))
    assert draft["output_token"] == "ANSEM"
    assert draft["destination_chain"] == "solana"


def test_convert_language_uses_cross_chain_route():
    route = route_capabilities("Convert 5 USDC from Base to WIF on Solana")
    assert route is not None
    assert route.intent == "cross_chain_swap"


def test_base_meme_buy_uses_relay_even_when_same_chain():
    route = route_capabilities(
        "Buy 0x1111111111111111111111111111111111111111 on Base with 0.01 ETH"
    )
    assert route is not None
    assert route.intent == "cross_chain_swap"
    assert route.chains == ("base",)


def test_evm_contract_without_chain_never_falls_back_to_jupiter():
    route = route_capabilities(
        "Buy 0x1111111111111111111111111111111111111111 with 0.01 ETH"
    )
    assert route is not None
    assert route.intent == "cross_chain_swap"


def test_solana_mint_without_chain_uses_jupiter():
    route = route_capabilities(
        "Buy 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump with 0.01 SOL"
    )
    assert route is not None
    assert route.intent == "trade"


def test_explicit_sol_payment_routes_symbol_trade_to_jupiter():
    route = route_capabilities("Buy ANSEM with 0.01 SOL")
    assert route is not None
    assert route.intent == "trade"
    assert route.chains == ("solana",)


def test_compact_decimal_sol_swap_routes_to_jupiter_and_parses_subject():
    request = "swap .02sol to ANSEM with 25bps"
    route = route_capabilities(request)
    assert route is not None
    assert route.intent == "trade"
    assert route.chains == ("solana",)
    draft = extract_cross_chain_draft(request, route.chains)
    assert draft["amount"] == "0.02"
    assert draft["input_token"].upper() == "SOL"
    assert draft["output_token"].upper() == "ANSEM"
    assert draft["slippage_bps"] == 25


def test_robinhood_chain_buy_is_recognized_and_prefilled():
    request = "Buy HOOD on Robinhood Chain with 0.02 ETH"
    route = route_capabilities(request)
    assert route is not None
    assert route.intent == "cross_chain_swap"
    assert route.chains == ("robinhood",)
    draft = extract_cross_chain_draft(request, route.chains)
    assert draft["source_chain"] == "robinhood"
    assert draft["destination_chain"] == "robinhood"
    assert draft["amount"] == "0.02"
    assert draft["input_token"] == "ETH"
    assert draft["output_token"] == "HOOD"


def test_cross_chain_route_extracts_max_slippage():
    request = "Swap 0.1 SOL on Solana to USDC on Base with max 10bps slippage"
    route = route_capabilities(request)
    assert route is not None
    assert route.chains == ("solana", "base")
    draft = extract_cross_chain_draft(request, route.chains)
    assert draft["source_chain"] == "solana"
    assert draft["destination_chain"] == "base"
    assert draft["amount"] == "0.1"
    assert draft["input_token"] == "SOL"
    assert draft["output_token"] == "USDC"
    assert draft["slippage_bps"] == 10


def test_trade_control_messages_do_not_start_new_quotes():
    for request in ("cancel the swap", "cancel it", "dismiss this quote", "never mind the trade"):
        assert is_trade_cancellation(request)
        assert route_capabilities(request).intent == "general"
    for request in ("confirm swap", "CONFIRM jEQ5hLJ-2wMTI1ziy6e--w", "proceed with the trade", "go ahead"):
        assert is_trade_confirmation(request)
        assert route_capabilities(request).intent == "general"


def test_token_overview_is_read_only_even_after_trade_context():
    for request in ("tell me about ANSEM token", "ANSEM token on Solana"):
        route = route_capabilities(request)
        assert route is not None
        assert route.intent == "research"
        assert "token_discovery" in route.capabilities


def test_own_token_balance_is_portfolio_not_previous_trade():
    for request in ("how much ANSEM i have", "my ANSEM balance", "do I have any ANSEM"):
        route = route_capabilities(request)
        assert route is not None
        assert route.intent == "portfolio"
        assert "token_balance" in route.capabilities


def test_own_recent_transactions_never_use_web_research():
    route = route_capabilities("Show my recent transactions")
    assert route is not None
    assert route.intent == "portfolio"
    assert route.capabilities == ("wallet_transactions", "wallet_intelligence")


def test_spl_token_holdings_is_connected_portfolio_not_token_discovery():
    for request in ("SPL token holdings?", "Show my SPL token holdings", "tokens in my wallet"):
        route = route_capabilities(request)
        assert route is not None
        assert route.intent == "portfolio"
        assert "token_holdings" in route.capabilities
        assert "token_discovery" not in route.capabilities


def test_parameter_followups_are_identified_without_becoming_fresh_trades():
    assert is_trade_modifier("use 25 bps instead")
    assert is_trade_modifier("change amount to .03 SOL")
    assert is_trade_modifier("send to 0x1111111111111111111111111111111111111111")


def test_directional_and_recipient_modifiers_only_change_the_named_field():
    destination = extract_cross_chain_draft("change destination to Base", ("base",))
    assert destination["destination_chain"] == "base"
    assert destination["source_chain"] is None
    assert destination["output_token"] is None

    source = extract_cross_chain_draft("change source chain to Arbitrum", ("arbitrum",))
    assert source["source_chain"] == "arbitrum"
    assert source["destination_chain"] is None
    assert source["output_token"] is None

    address = "0x1111111111111111111111111111111111111111"
    recipient = extract_cross_chain_draft(f"send to {address}", ())
    assert recipient["recipient"] == address


def test_market_move_language_is_research_not_execution():
    route = route_capabilities("what is driving the recent move in BTC")
    assert route is not None
    assert route.intent == "research"
    assert "web_research" in route.capabilities


def test_buy_for_and_sell_grammar_are_parsed():
    buy = extract_cross_chain_draft("buy HOOD on Base for .02 ETH", ("base",))
    assert buy["amount"] == "0.02"
    assert buy["input_token"] == "ETH"
    assert buy["output_token"] == "HOOD"
    assert buy["source_chain"] == buy["destination_chain"] == "base"

    sell = extract_cross_chain_draft(
        "sell 100 ANSEM for SOL on Solana with 50 bps", ("solana",)
    )
    assert sell["amount"] == "100"
    assert sell["input_token"] == "ANSEM"
    assert sell["output_token"] == "SOL"
    assert sell["source_chain"] == "solana"
    assert sell["slippage_bps"] == 50


def test_destination_only_chain_is_not_assigned_as_source():
    draft = extract_cross_chain_draft(
        "swap 10 USDC to ETH on Arbitrum", ("arbitrum",)
    )
    assert draft["source_chain"] is None
    assert draft["destination_chain"] == "arbitrum"


def test_proportional_sol_swap_is_solana_but_keeps_amount_missing():
    route = route_capabilities("swap all SOL to USDC")
    assert route.intent == "trade"
    assert route.chains == ("solana",)
    draft = extract_cross_chain_draft("swap all SOL to USDC", route.chains)
    assert draft["input_token"] == "SOL"
    assert draft["output_token"] == "USDC"
    assert draft["amount"] is None
def test_wallet_health_and_scenario_routes():
    health = route_capabilities("Check my wallet health")
    assert health.intent == "portfolio"
    assert "wallet_health" in health.capabilities

    scenario = route_capabilities("What if my portfolio drops 20%?")
    assert scenario.intent == "portfolio"
    assert "portfolio_scenario" in scenario.capabilities
