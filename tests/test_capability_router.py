from app.capability_router import (
    extract_cross_chain_draft,
    infer_tool_capabilities,
    infer_tool_risk,
    is_trade_cancellation,
    is_trade_confirmation,
    is_trade_modifier,
    route_capabilities,
)
import asyncio

from app.nodes import research as research_node_mod
from app.nodes.research import direct_mcp_request, wallet_portfolio_answer


def test_resolve_named_token_injects_mint_for_holders_query(monkeypatch):
    # BONK has a verified Solana canonical and no serious same-ticker EVM token
    # -> resolve to Solana (authoritative; immune to DEX Screener pollution).
    monkeypatch.setattr(research_node_mod, "search_verified_tokens",
                        lambda q: [{"mint": "BONKmint1111", "symbol": "BONK", "tags": ["verified"]}])
    monkeypatch.setattr(research_node_mod, "token_candidates", lambda symbol, chains=(): [])
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup",
                        lambda symbol, chain: (_ for _ in ()).throw(AssertionError("no EVM lookup when Solana is authoritative")))
    caps = {"token_discovery", "token_security"}
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK token", caps))
    assert "BONKmint1111" in out.request and "on solana" in out.request
    assert out.chain == "solana" and out.clarification is None
    # "trending tokens on solana" must NOT be read as a "TRENDING" ticker.
    out2 = asyncio.run(research_node_mod._resolve_named_token("trending tokens on solana", {"token_discovery", "market_data"}))
    assert out2.request == "trending tokens on solana" and out2.chain is None
    # Non-token requests and already-addressed requests are left untouched.
    # (a holders ask resolves whatever the capabilities since 2026-09-24 -- "Any whales
    # in ANSEM?" routed as wallet intelligence had no mint; a price ask still waits for them)
    out3 = asyncio.run(research_node_mod._resolve_named_token("price of BONK", set()))
    assert out3.request == "price of BONK" and out3.chain is None
    addressed = "top holders of 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump on solana"
    assert asyncio.run(research_node_mod._resolve_named_token(addressed, caps)).request == addressed


def test_resolve_named_token_asks_when_symbol_is_ambiguous_across_chains(monkeypatch):
    # No verified Solana canonical for PEPE -> DEX Screener enumerates the chains,
    # Bitquery validates each with real volume. Comparable -> ask.
    monkeypatch.setattr(research_node_mod, "search_verified_tokens", lambda q: [])
    bq = {
        "ethereum": [{"chain": "ethereum", "address": "0x6982", "symbol": "PEPE", "liquidity_usd": 3.9e6, "traders": 120}],
        "base": [{"chain": "base", "address": "0x6921", "symbol": "PEPE", "liquidity_usd": 1.5e6, "traders": 80}],
    }
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup", lambda symbol, chain: (bq.get(chain, []), True))
    monkeypatch.setattr(research_node_mod, "token_candidates",
                        lambda symbol, chains=(): [
                            {"chain": "ethereum", "address": "0x6982", "symbol": "PEPE", "liquidity_usd": 310_000_000.0},
                            {"chain": "base", "address": "0x6921", "symbol": "PEPE", "liquidity_usd": 120_000_000.0},
                        ])
    caps = {"token_discovery", "token_security"}
    # Comparable volume on Base and Ethereum -> ask, don't guess.
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of PEPE", caps))
    assert out.clarification is not None and out.pending is not None
    assert out.pending["symbol"] == "PEPE"
    assert {c["chain"] for c in out.pending["candidates"]} == {"ethereum", "base"}
    # Naming the chain resolves it directly (Bitquery), no question.
    out2 = asyncio.run(research_node_mod._resolve_named_token("top holders of PEPE on Base", caps))
    assert out2.clarification is None
    assert "0x6921" in out2.request and out2.chain == "base"


def test_verified_solana_token_is_not_made_ambiguous_by_dex_screener_pollution(monkeypatch):
    """Live regression (2026-09-16): DEX Screener search listed a 'BONK' on
    Ethereum with $342M of liquidity while Bitquery saw no token with real
    traders there. That is pollution, not a rival: BONK resolves to its
    verified Solana mint. Only when Bitquery cannot answer (outage, 402) does
    the DEX Screener entry still count, so a real rival is never dropped."""
    monkeypatch.setattr(research_node_mod, "search_verified_tokens",
                        lambda q: [{"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "symbol": "BONK", "tags": ["verified"]}])
    decoy = [
        {"chain": "ethereum", "address": "0x8955322D800D17BD2561Bb3B59c88171a9980456", "symbol": "BONK", "liquidity_usd": 342_036_373.0, "volume_24h_usd": 0.0},
        {"chain": "solana", "address": "56Lc5By6bBmewoLZLy2QWs9L9nd8aQHFZrYCSaX5KDCm", "symbol": "BONK", "liquidity_usd": 249_356_112.0, "volume_24h_usd": 4.0},
    ]
    monkeypatch.setattr(research_node_mod, "token_candidates", lambda symbol, chains=(): decoy)
    caps = {"token_discovery", "token_security"}
    # Zero 24h volume behind $342M "liquidity": not a rival, whatever Bitquery says or cannot say.
    for verdict in (([], True), ([], False)):
        monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup", lambda symbol, chain, v=verdict: v)
        out = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK", caps))
        assert out.clarification is None and out.chain == "solana" and "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" in out.request
    # Real volume on Ethereum but Bitquery answered "no token with real traders": still pollution.
    traded = [{**decoy[0], "volume_24h_usd": 5_000_000.0}, decoy[1]]
    monkeypatch.setattr(research_node_mod, "token_candidates", lambda symbol, chains=(): traded)
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup", lambda symbol, chain: ([], True))
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK", caps))
    assert out.clarification is None and out.chain == "solana"
    # Real volume and Bitquery unavailable: no verdict, so the question is still asked (a real rival is never dropped).
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup", lambda symbol, chain: ([], False))
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK", caps))
    assert out.clarification is not None and {c["chain"] for c in out.pending["candidates"]} == {"solana", "ethereum"}
    # Real volume and Bitquery sees traders: a genuine ambiguity.
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup",
                        lambda symbol, chain: ([{"chain": "ethereum", "address": "0x8955", "symbol": "BONK", "liquidity_usd": 2e6, "traders": 300}], True))
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK", caps))
    assert out.clarification is not None


def test_resolve_named_token_asks_when_verified_solana_has_evm_rival(monkeypatch):
    # A verified Solana PEPE AND a large Ethereum PEPE share the ticker -> ask,
    # listing the verified Solana option first, rather than silently picking it.
    monkeypatch.setattr(research_node_mod, "search_verified_tokens",
                        lambda q: [{"mint": "SolPEPEmint", "symbol": "PEPE", "tags": ["verified"]}])
    # Bitquery confirms a real Ethereum PEPE; Base has none.
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup",
                        lambda symbol, chain: ([{"chain": "ethereum", "address": "0x6982", "symbol": "PEPE",
                                                 "liquidity_usd": 3.9e6, "traders": 120}] if chain == "ethereum" else [], True))
    monkeypatch.setattr(research_node_mod, "token_candidates",
                        lambda symbol, chains=(): [
                            {"chain": "ethereum", "address": "0x6982", "symbol": "PEPE", "liquidity_usd": 3.1e8},
                            {"chain": "solana", "address": "SolPEPEmint", "symbol": "PEPE", "liquidity_usd": 40_000.0},
                        ])
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of PEPE", {"token_discovery", "token_security"}))
    assert out.clarification is not None
    chains = [c["chain"] for c in out.pending["candidates"]]
    assert chains[0] == "solana" and "ethereum" in chains

    # A verified Solana token with no real same-ticker EVM activity resolves.
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup", lambda symbol, chain: ([], True))
    monkeypatch.setattr(research_node_mod, "token_candidates",
                        lambda symbol, chains=(): [
                            {"chain": "solana", "address": "BONKmint", "symbol": "BONK", "liquidity_usd": 5e6},
                            {"chain": "base", "address": "0xdust", "symbol": "BONK", "liquidity_usd": 500.0},
                        ])
    monkeypatch.setattr(research_node_mod, "search_verified_tokens",
                        lambda q: [{"mint": "BONKmint", "symbol": "BONK", "tags": ["verified"]}])
    out2 = asyncio.run(research_node_mod._resolve_named_token("top holders of BONK", {"token_discovery", "token_security"}))
    assert out2.clarification is None and out2.chain == "solana" and "BONKmint" in out2.request


def test_resolve_named_token_picks_evm_when_no_verified_solana(monkeypatch):
    # AERO: not verified on Solana; DEX Screener's inflated "Solana AERO" must be
    # ignored, and Bitquery's real Base volume wins -> resolve to Base.
    monkeypatch.setattr(research_node_mod, "search_verified_tokens", lambda q: [])
    monkeypatch.setattr(research_node_mod, "bitquery_evm_lookup",
                        lambda symbol, chain: ([{"chain": "base", "address": "0x9401", "symbol": "AERO",
                                                 "liquidity_usd": 5.2e7, "traders": 318}] if chain == "base" else [], True))
    monkeypatch.setattr(research_node_mod, "token_candidates",
                        lambda symbol, chains=(): [
                            {"chain": "solana", "address": "93Wvz", "symbol": "AERO", "liquidity_usd": 1.0e9},
                            {"chain": "base", "address": "0x9401", "symbol": "AERO", "liquidity_usd": 5.1e7},
                        ])
    out = asyncio.run(research_node_mod._resolve_named_token("top holders of AERO", {"token_discovery", "token_security"}))
    assert out.clarification is None and out.chain == "base" and "0x9401" in out.request


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


def test_connected_wallet_this_wallet_transactions_route_to_portfolio():
    # "this wallet's recent transactions" is the connected-wallet quick action
    # the app itself emits; it must route to the wallet path, not web search.
    for request in (
        "Show this wallet's recent transactions",
        "the wallet's recent transactions",
    ):
        route = route_capabilities(request)
        assert route is not None, request
        assert route.intent == "portfolio", request
        assert "wallet_transactions" in route.capabilities, request


def test_hyperliquid_market_queries_classify_as_market_data_without_a_price_word():
    # "funding rate for BTC on Hyperliquid" has no generic market word; it must
    # still reach market_data so goldrush_hyperliquid_market can serve it,
    # rather than falling through to web_research.
    for request in (
        "What is the funding rate for BTC on Hyperliquid?",
        "Show open interest for ETH on Hyperliquid",
        "BTC perps funding rate",
    ):
        route = route_capabilities(request)
        assert route is not None, request
        assert "market_data" in route.capabilities, request


def test_trending_tokens_on_launchpad_classifies_as_discovery_not_general():
    for request in (
        "latest trending tokens on pump.fun",
        "trending tokens on robinhood",
    ):
        route = route_capabilities(request)
        assert route is not None, request
        assert route.intent == "research", request
        assert "token_discovery" in route.capabilities, request
        assert "market_sentiment" not in route.capabilities, request


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


def test_evm_wallet_analysis_never_picks_solana_even_if_it_appears_in_the_request(monkeypatch):
    """A 0x address can never exist on Solana. The chain picker used to take
    extract_chains(request)[0] unconditionally -- if 'solana' happened to
    appear anywhere in the (possibly multi-turn) effective request text, an
    EVM wallet's Nansen call (and everything keyed off its payload, like the
    Bitquery fallback) got pinned to the wrong chain entirely.
    """
    def portfolio_tool(**_kwargs):
        return ""

    portfolio_tool.__name__ = "mcp_nansen_address_portfolio"
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.get", lambda name: portfolio_tool if name == "address_portfolio" else None)
    wallet = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    # "solana" appears in the text (e.g. carried over from a prior turn)
    # alongside a real EVM chain -- the EVM chain must win, never solana.
    request = direct_mcp_request(f"portfolio for {wallet} on ethereum, previously discussed on solana")
    assert request[1]["request"].get("chain") == "ethereum"
    # And with no other chain word at all, "solana" must not be picked either.
    request = direct_mcp_request(f"portfolio for {wallet}, previously discussed on solana")
    assert request[1]["request"].get("chain") != "solana"


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


def test_wallet_portfolio_summary_does_not_claim_leverage_for_an_all_zero_wallet():
    """A wallet with no DeFi assets or debt ('0' for both) must not produce
    'debt materially offsets assets' -- the string '0' is truthy, so the
    naive check for that sentence needs a real numeric comparison.
    """
    answer = wallet_portfolio_answer(
        "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        """## DeFi Summary
**Net Value (USD)**: 0
**Total Assets (USD)**: 0
**Total Debts (USD)**: 0
""",
    )
    assert "materially offsets" not in answer
    assert "No DeFi assets or debt were reported" in answer


def test_wallet_portfolio_summary_discloses_sections_that_failed_on_credit_exhaustion():
    """When Nansen's multi-section address_portfolio response has some
    sections fail (e.g. 403 insufficient credits) while others succeed, the
    answer must disclose which sections failed rather than opening with
    'has current portfolio data shown below', which implies full success
    while genuine 403 failures sit right below the answer in the evidence
    panel.
    """
    answer = wallet_portfolio_answer(
        "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
        """# Token Holdings
**Error retrieving token balances**: API request to profiler/address/current-balance failed with status 403: Insufficient credits remaining to call this endpoint.. The request body was

{
  "address": "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
}

# DeFi Positions
**Error retrieving DeFi positions**: API request to portfolio/defi-holdings failed with status 403: Insufficient credits remaining to call this endpoint.. The request body was

{
  "wallet_address": "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
}

# Hyperliquid Positions
## Account Summary
**Account Value (USD)**: 5.4k

**Total Notional (USD)**: 53.9k
## Perp Positions
""",
    )
    assert "has current portfolio data shown below" not in answer
    assert "Token Holdings" in answer
    assert "DeFi Positions" in answer
    assert "credits are exhausted" in answer
    assert "**$5.4k**" in answer
    assert "**$53.9k total notional**" in answer


def test_wallet_portfolio_summary_stays_unqualified_when_all_sections_succeed():
    answer = wallet_portfolio_answer(
        "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
        """## DeFi Summary
**Net Value (USD)**: 258.4
**Total Assets (USD)**: 3.1k
**Total Debts (USD)**: 2.9k
""",
    )
    assert "has current portfolio data shown below" in answer
    assert "could not be retrieved" not in answer


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


def test_hypothetical_trade_questions_route_to_trade_simulation():
    """These previously fell through to generic web_research (which can't
    know the user's real balance or fetch a real quote) -- verified live
    this session. Past-tense ("sold") is the important case: TRADE's word
    list doesn't match it at all, so before this fix it skipped straight to
    the OPEN_QUESTION fallback without ever getting a chance at real
    handling.
    """
    for request in (
        "What would happen if I sold half my SOL?",
        "What if I sell my SOL now?",
        "Should I sell my SOL?",
        "Simulate selling half my SOL",
        "Simulate buying 1 ETH with SOL",
    ):
        route = route_capabilities(request)
        assert route is not None, request
        assert route.intent == "portfolio", request
        assert "trade_simulation" in route.capabilities, request


def test_hypothetical_trade_questions_never_reach_real_execution():
    """The existing execution safety net (has_competing_speech) must stay
    intact -- trade_simulation is a distinct, read-only outcome, not a
    replacement for that net or a new path into plan_execution_route.
    """
    for request in ("What would happen if I sold half my SOL?", "Should I sell my SOL?"):
        route = route_capabilities(request)
        assert route.intent != "trade"
        assert route.execution_provider is None


def test_real_execution_commands_still_route_to_trade():
    """Non-regression: a genuine imperative command must still reach the
    real execution path -- this new capability must not swallow it.
    """
    route = route_capabilities("Sell half my SOL now")
    assert route.intent == "trade"
    assert route.mode == "quote"


def test_should_i_buy_without_a_holding_is_advice_not_a_simulation():
    """'Should I buy HYPE?' names no amount and no position of the user's, so
    it is a recommendation question: it must not be turned into a trade
    simulation that invents a sell of the user's SOL. The rules layer stays
    out of the way (speech layer -> advice -> research). A 'should I' that
    does reference a holding or amount remains a simulation."""
    for request in ("should i buy HYPE token ?", "Should I buy HYPE?", "should I sell SOL"):
        route = route_capabilities(request)
        assert route is None or "trade_simulation" not in route.capabilities, request
        assert route is None or route.intent != "portfolio", request
    for request in ("Should I sell my SOL?", "should I buy 0.5 SOL of HYPE", "should I sell half"):
        route = route_capabilities(request)
        assert route is not None and "trade_simulation" in route.capabilities, request
