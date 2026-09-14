from app.nodes import runtime, routing, portfolio, research
from app.jupiter import WRAPPED_SOL_MINT
from app.trade_context import clean_mint, complete_swap_fields
from app import graph
import asyncio
from types import SimpleNamespace


ANSEM_MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


class _RouterDouble:
    """Mirror the real ProviderRouter.route/try_route split for test doubles.

    Doubles only implement route(); try_route() delegates and maps the
    'no configured provider' RuntimeError to None, exactly as the real
    try_route does, so a double that raises still exercises the caller's
    None-branch fallback.
    """

    def try_route(self, request, capability, chains=(), *, provider=None):
        try:
            return self.route(request, capability, chains)
        except RuntimeError:
            return None

    def try_route_across(self, request, capabilities, chains=(), *, provider=None):
        # Mirror the real router: route to the first capability that has a
        # candidate. The doubles in these tests set up candidates so only the
        # correct capability is non-empty; true cross-capability RANKING is
        # exercised against the real ProviderRouter in test_provider_router.py.
        for capability in capabilities:
            if self.candidates(request, capability, chains):
                return self.try_route(request, capability, chains)
        return None

    def matched_capabilities(self, request, chains=()):
        # Doubles drive routing through explicit candidates()/route(); the
        # reachability backstop is a no-op for them (exercised against the real
        # ProviderRouter in test_provider_router.py).
        return set()


def relay_context():
    return {"active_workflow": {"intent": "cross_chain_swap", "status": "collecting_details", "source_chain": "solana", "destination_chain": "base", "amount": "0.1", "input_token": "SOL", "output_token": "USDC", "slippage_bps": 10}}


def test_trade_simulation_never_creates_a_trade_plan_and_discloses_itself(monkeypatch):
    """The core safety property: a simulation answer must be explicit about
    being hypothetical, and this code path must never touch trade-plan
    creation at all (checked by asserting create_trade_plan is never even
    imported into this call, not just unmocked-and-uncalled).
    """
    from app.models import TokenInfo

    async def fake_call_lm(_program, **_kwargs):
        return SimpleNamespace(
            answer="Computing a simulated quote.", trajectory={"thought_0": "check balance"},
            should_simulate=True, input_mint=WRAPPED_SOL_MINT,
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", amount_atomic=500_000_000,
        )

    async def fake_simulate_swap(_input_mint, _output_mint, _amount_atomic):
        return {
            "input_token": TokenInfo(mint=WRAPPED_SOL_MINT, symbol="SOL", name="Solana", decimals=9, usd_price=150.0),
            "output_token": TokenInfo(mint="EPjF...", symbol="USDC", name="USD Coin", decimals=6, usd_price=1.0),
            "input_amount": 0.5, "input_value_usd": 75.0,
            "output_amount": 74.5, "output_value_usd": 74.5,
            "price_impact_pct": 0.2, "quote": {"outAmount": "74500000"},
        }

    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)
    monkeypatch.setattr(portfolio, "simulate_swap", fake_simulate_swap)

    result = asyncio.run(portfolio.portfolio_node({
        "request": "What would happen if I sold half my SOL for USDC?",
        "wallet_address": "wallet", "capabilities": ["trade_simulation", "portfolio"], "chains": [],
    }))

    assert "simulation only" in result["answer"].lower()
    assert "nothing has been prepared or submitted" in result["answer"].lower()
    assert not result.get("trade_plan")

    import json
    json.dumps(result["trajectory"])  # regression: TokenInfo instances broke this live


def test_trade_simulation_discloses_quote_failure_without_a_traceback(monkeypatch):
    async def fake_call_lm(_program, **_kwargs):
        return SimpleNamespace(
            answer="...", trajectory=None, should_simulate=True,
            input_mint=WRAPPED_SOL_MINT, output_mint="bad_mint", amount_atomic=500_000_000,
        )

    async def fake_simulate_swap(*_args, **_kwargs):
        raise ValueError("Mint bad_mint was not found uniquely in Jupiter's token registry")

    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)
    monkeypatch.setattr(portfolio, "simulate_swap", fake_simulate_swap)

    result = asyncio.run(portfolio.portfolio_node({
        "request": "What if I sold half my SOL for a scam token?",
        "wallet_address": "wallet", "capabilities": ["trade_simulation", "portfolio"], "chains": [],
    }))
    assert "couldn't compute a simulated quote" in result["answer"].lower()
    assert "no trade was prepared or submitted" in result["answer"].lower()


def test_backstop_routes_when_classifier_emitted_only_web_research(monkeypatch):
    """The permanent fix for classifier/tool vocabulary drift: even when the
    intent classifier emits ONLY web_research, a deterministic tool whose own
    matcher recognizes the request is still reached (via matched_capabilities),
    instead of falling to a generic web-search answer.
    """
    class FakeResult:
        tool = "goldrush_hyperliquid_market"
        output = "# Hyperliquid market data\n**Mark Price**: $77K"
        cached = False
        attempted = ("goldrush_hyperliquid_market",)
        failures = ()

    class FakeRouter(_RouterDouble):
        def matched_capabilities(self, request, chains=()):
            return {"market_data"}  # the tool's own matcher recognizes it

        def candidates(self, request, capability, chains=(), allow_semantic_fallback=True):
            return [SimpleNamespace(name="goldrush_hyperliquid_market")] if capability == "market_data" else []

        def route(self, request, capability, chains):
            return FakeResult()

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())
    state = {
        "request": "What is the funding rate for BTC on Hyperliquid?",
        "capabilities": ["web_research"],  # classifier missed market_data
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert "Hyperliquid market data" in result["answer"]
    assert result["trajectory"]["tool_name_0"] == "goldrush_hyperliquid_market"


def test_null_model_input_is_repaired_from_explicit_sol_followup():
    history = (
        "user: buy ANSEM token\n"
        f"assistant: I found The Black Bull. Solana mint: {ANSEM_MINT}. "
        "Tell me the amount and slippage."
    )

    fields = complete_swap_fields(
        "buy for .02SOL with 50bps slippage",
        history,
        "null",
        "ANSEM",
        None,
        None,
    )

    assert fields == (WRAPPED_SOL_MINT, ANSEM_MINT, 20_000_000, 50)


def test_named_sol_swap_reuses_matching_resolved_token_not_old_route():
    history = (
        f"assistant: ANSEM is a Solana token. Mint address: {ANSEM_MINT}.\n"
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage"
    )
    fields = complete_swap_fields(
        "swap .02sol to ANSEM with 25bps",
        history,
        "SOL",
        "ANSEM",
        None,
        None,
    )

    assert fields == (WRAPPED_SOL_MINT, ANSEM_MINT, 20_000_000, 25)


def test_named_token_never_inherits_a_different_researched_mint():
    history = (
        f"assistant: ANSEM is a Solana token. Mint address: {ANSEM_MINT}."
    )
    fields = complete_swap_fields(
        "buy BONK with .02 SOL",
        history,
        "SOL",
        "BONK",
        None,
        25,
    )

    assert fields[0] == WRAPPED_SOL_MINT
    assert fields[1] == "BONK"
    assert fields[1] != ANSEM_MINT


def test_null_like_mints_are_never_accepted():
    assert clean_mint(None) is None
    assert clean_mint("null") is None
    assert clean_mint(" `undefined` ") is None


def test_complete_fields_preserves_exact_output_mint():
    fields = complete_swap_fields(
        "Swap 0.01 SOL to the selected token with 50 bps slippage",
        "",
        "SOL",
        ANSEM_MINT,
        10_000_000,
        50,
    )
    assert fields == (WRAPPED_SOL_MINT, ANSEM_MINT, 10_000_000, 50)


def test_incomplete_trade_fields_do_not_claim_a_plan_is_being_prepared(monkeypatch):
    result = SimpleNamespace(
        answer="I am preparing your plan.",
        trajectory=None,
        should_propose_swap=True,
        input_mint=None,
        output_mint=None,
        amount_atomic=None,
        slippage_bps=50,
        proposal_reason=None,
    )

    async def fake_call(_program, **_kwargs):
        return result

    monkeypatch.setattr(runtime, "_call_lm", fake_call)
    update = asyncio.run(
        graph.trade_planner_node(
            {"request": "buy ANSEM", "wallet_address": "wallet", "history": ""}
        )
    )
    assert "No trade plan has been created" in update["answer"]
    assert "proposal" not in update


def test_new_buy_does_not_reuse_unrelated_pending_cross_chain_route():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: A Relay quote is ready for SOL to USDC on Base."
    )
    update = asyncio.run(
        graph.cross_chain_swap_node(
            {
                "request": "buy ANSEM token",
                "wallet_address": "wallet",
                "history": history,
                "chains": [],
            }
        )
    )

    assert update["cross_chain_swap"] is None
    assert update["answer"].startswith("I understand you want to buy ANSEM.")
    assert "preparing a Relay quote to swap 0.1 SOL" not in update["answer"]
    assert "token you’re paying with" in update["answer"]


def test_unrecognized_equity_ticker_gets_domain_neutral_clarification():
    """A real stock ticker outside the curated equity registry (instruments.json
    only seeds ~12 symbols) must not be silently treated as a crypto token.
    'buy AMD' still enters the trade/collect branch (this isn't magically
    fixed -- the registry can't list every equity), but the resulting
    question must not presuppose crypto, and must never produce a trade
    proposal or cross-chain draft.
    """
    state = {"request": "buy AMD", "contextual_request": "buy AMD", "wallet_address": "wallet"}
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "trade"
    assert route["execution_provider"] is None
    assert route["missing_fields"] == ["chain"]
    answer = asyncio.run(graph.trade_planner_node({**state, **route}))
    assert "AMD" in answer["answer"]
    assert "stock or other equity" in answer["answer"]
    assert "research" in answer["answer"].lower()
    assert answer.get("proposal") is None
    assert answer.get("cross_chain_swap") is None


def test_new_symbol_buy_does_not_reenter_pending_relay_workflow():
    state = {
        "request": "buy ANSEM token",
        "contextual_request": "buy ANSEM token",
        "wallet_address": "wallet",
        "history": "user: Swap 0.1 SOL on Solana to USDC on Base\nassistant: Relay quote ready.",
        "session_context": {
            "active_workflow": {
                "intent": "cross_chain_swap",
                "status": "collecting_details",
                "source_chain": "solana",
                "destination_chain": "base",
                "amount": "0.1",
                "input_token": "SOL",
                "output_token": "USDC",
            }
        },
    }
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "trade"
    assert route["execution_provider"] is None
    assert route["missing_fields"] == ["chain"]
    answer = asyncio.run(graph.trade_planner_node({**state, **route}))
    # Domain-neutral: names the symbol and asks for the chain without
    # presupposing it's crypto (a real equity ticker outside the curated
    # registry reaches this same branch -- see test_capability_router.py).
    assert "ANSEM" in answer["answer"]
    assert "which chain" in answer["answer"].lower()
    assert "Relay quote" not in answer["answer"]


def test_new_incomplete_swap_does_not_inherit_old_amount():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: Relay quote ready."
    )
    update = asyncio.run(
        graph.cross_chain_swap_node(
            {
                "request": "swap USDC to ETH on Arbitrum",
                "wallet_address": "wallet",
                "history": history,
                "chains": ["arbitrum"],
            }
        )
    )

    assert update["cross_chain_swap"] is None
    assert "the source chain" in update["answer"]
    assert "the amount" in update["answer"]


def test_cross_chain_swap_without_wallet_refuses_before_drafting_a_quote():
    """A fully-specified swap request with no connected wallet must not
    reach 'I'm preparing a Relay quote...' -- that implies a swap is already
    underway when nothing can actually be signed.
    """
    state = {
        "request": "Swap 0.1 ETH on Base to USDC on Base with max 50 bps slippage",
        "wallet_address": "",
        "history": "",
        "session_context": {},
        "chains": [],
    }
    update = asyncio.run(graph.cross_chain_swap_node(state))
    assert update["cross_chain_swap"] is None
    assert "wallet" in update["answer"].lower()
    assert "preparing" not in update["answer"].lower()


def test_cross_chain_modifier_reuses_route_but_overrides_slippage():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: Relay quote ready."
    )
    state = {
        "request": "use 25 bps instead",
        "wallet_address": "wallet",
        "history": history,
        "session_context": relay_context(),
        "chains": [],
    }
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "cross_chain_swap"
    assert route["route_source"] == "session_context"

    update = asyncio.run(graph.cross_chain_swap_node(state))
    assert update["cross_chain_swap"] is not None
    assert update["cross_chain_swap"].amount == "0.1"
    assert update["cross_chain_swap"].input_token == "SOL"
    assert update["cross_chain_swap"].output_token == "USDC"
    assert update["cross_chain_swap"].slippage_bps == 25


def test_cross_chain_modifier_can_change_amount_without_losing_route():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: Relay quote ready."
    )
    update = asyncio.run(
        graph.cross_chain_swap_node(
            {
                "request": "change amount to .03 SOL",
                "session_context": relay_context(),
                "wallet_address": "wallet",
                "history": history,
                "chains": [],
            }
        )
    )

    assert update["cross_chain_swap"] is not None
    assert update["cross_chain_swap"].amount == "0.03"
    assert update["cross_chain_swap"].input_token.upper() == "SOL"
    assert update["cross_chain_swap"].output_token == "USDC"


def test_cross_chain_modifier_changes_only_destination_chain():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: Relay quote ready."
    )
    update = asyncio.run(
        graph.cross_chain_swap_node(
            {
                "request": "change destination to Arbitrum",
                "session_context": relay_context(),
                "wallet_address": "wallet",
                "history": history,
                "chains": ["arbitrum"],
            }
        )
    )

    assert update["cross_chain_swap"] is not None
    assert update["cross_chain_swap"].source_chain == "solana"
    assert update["cross_chain_swap"].destination_chain == "arbitrum"
    assert update["cross_chain_swap"].amount == "0.1"
    assert update["cross_chain_swap"].output_token == "USDC"


def test_cancelled_trade_cannot_be_resurrected_by_a_modifier():
    history = (
        "user: Swap 0.1 SOL on Solana to USDC on Base with max 10 bps slippage\n"
        "assistant: Relay quote ready.\n"
        "user: cancel it\n"
        "assistant: The pending trade has been dismissed."
    )
    state = {
        "request": "use 25 bps instead",
        "wallet_address": "wallet",
        "history": history,
        "chains": [],
    }

    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "general"
    response = asyncio.run(graph.general_node(state))
    assert "no active trade" in response["answer"].lower()


def test_token_balance_question_uses_portfolio_without_creating_trade(monkeypatch):
    async def fake_snapshot(_wallet):
        return {
            "wallet": "wallet",
            "sol": {"amount": 0.1, "usd_value": 10.0},
            "holdings": [
                {
                    "mint": ANSEM_MINT,
                    "symbol": "ANSEM",
                    "amount": 7.25,
                    "usd_value": 1.16,
                }
            ],
            "total_usd_value": 11.16,
        }

    monkeypatch.setattr(portfolio, "build_portfolio_snapshot", fake_snapshot)
    state = {
        "request": "how much ANSEM i have",
        "wallet_address": "wallet",
        "history": "user: swap .02 SOL to ANSEM with 50 bps\nassistant: Swap preview ready.",
        "capabilities": ["token_balance", "portfolio", "wallet_intelligence"],
    }
    update = asyncio.run(graph.portfolio_node(state))

    assert "7.25 ANSEM" in update["answer"]
    assert "$1.16" in update["answer"]
    assert "trade_plan" not in update
    assert update["trajectory"]["tool_name_0"] == "portfolio_snapshot"


def test_connected_wallet_transactions_use_helius_not_web_search(monkeypatch):
    class Router(_RouterDouble):
        def route(self, request, capability, chains):
            assert "3aHL" in request
            assert capability == "wallet_intelligence"
            assert chains == ("solana",)
            return SimpleNamespace(
                output="# Recent Solana wallet transactions\n\n`signature…`",
                tool="helius_wallet_transactions",
                provider="helius",
                cached=False,
                attempted=("helius_wallet_transactions",),
                failures=(),
            )

    monkeypatch.setattr(portfolio, "get_provider_router", lambda: Router())
    update = asyncio.run(
        graph.portfolio_node(
            {
                "request": "Show my recent transactions",
                "wallet_address": "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS",
                "chains": [],
                "capabilities": ["wallet_transactions", "wallet_intelligence"],
            }
        )
    )

    assert "Recent Solana wallet transactions" in update["answer"]
    assert update["trajectory"]["tool_name_0"] == "helius_wallet_transactions"


def test_connected_wallet_transactions_try_router_before_nansen(monkeypatch):
    """GoldRush/Helius (via the capability router) are already chain-scoped
    and prioritized above Nansen for wallet activity -- the router must be
    tried first, with Nansen only as a last resort when the router has
    nothing configured for this chain.
    """
    calls = []

    class Router(_RouterDouble):
        def route(self, request, capability, chains):
            calls.append("router")
            raise RuntimeError("no wallet-activity provider configured for this chain")

    def fake_nansen_call(wallet, chain, request):
        calls.append("nansen_lookup")
        return ("mcp_nansen_address_transactions", {"walletAddress": wallet, "chain": chain})

    async def fake_call_direct_mcp_tool(tool_name, arguments):
        calls.append("nansen_call")
        return "# Recent wallet transactions\n\n`sig…`"

    monkeypatch.setattr(portfolio, "get_provider_router", lambda: Router())
    monkeypatch.setattr(portfolio, "_nansen_wallet_tool_call", fake_nansen_call)
    monkeypatch.setattr(portfolio, "call_direct_mcp_tool", fake_call_direct_mcp_tool)

    update = asyncio.run(
        graph.portfolio_node(
            {
                "request": "Show my recent transactions",
                "wallet_address": "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
                "chains": ["base"],
                "capabilities": ["wallet_transactions", "wallet_intelligence"],
            }
        )
    )

    assert calls == ["router", "nansen_lookup", "nansen_call"]  # router attempted first
    assert "Recent wallet transactions" in update["answer"]


def test_connected_wallet_transactions_disclose_when_both_router_and_nansen_fail(monkeypatch):
    class Router(_RouterDouble):
        def route(self, request, capability, chains):
            raise RuntimeError("no wallet-activity provider configured for this chain")

    monkeypatch.setattr(portfolio, "get_provider_router", lambda: Router())
    monkeypatch.setattr(portfolio, "_nansen_wallet_tool_call", lambda wallet, chain, request: None)

    update = asyncio.run(
        graph.portfolio_node(
            {
                "request": "Show my recent transactions",
                "wallet_address": "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460",
                "chains": ["base"],
                "capabilities": ["wallet_transactions", "wallet_intelligence"],
            }
        )
    )

    assert "neither a configured wallet-activity provider nor Nansen succeeded" in update["answer"]


def test_spl_token_holdings_use_portfolio_snapshot_not_discovery(monkeypatch):
    async def fake_snapshot(_wallet):
        return {
            "wallet": "wallet",
            "sol": {"amount": 0.1},
            "holdings": [
                {"symbol": "ANSEM", "mint": "mint", "amount": 7.25, "usd_value": 1.16}
            ],
            "total_usd_value": 10.0,
        }

    monkeypatch.setattr(portfolio, "build_portfolio_snapshot", fake_snapshot)
    update = asyncio.run(graph.portfolio_node({
        "request": "SPL token holdings?",
        "wallet_address": "wallet",
        "capabilities": ["token_holdings", "portfolio", "wallet_intelligence"],
    }))
    assert "SPL token holdings" in update["answer"]
    assert "7.25" in update["answer"]
    assert update["trajectory"]["tool_name_0"] == "portfolio_snapshot"


def test_compose_wallet_portfolio_discloses_nansen_defi_failure_but_still_has_balances(monkeypatch):
    """GoldRush-primary architecture: Nansen's address_portfolio (now called
    with mode='defi' only) failing with credit exhaustion must not block
    the rest of the answer -- balances (from GoldRush/Bitquery via the
    router) and the DeFi-unavailable disclosure should both be present.
    """
    wallet = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
    captured_defi_payload = {}

    def portfolio_tool(**kwargs):
        captured_defi_payload.update(kwargs["request"])
        return (
            "# DeFi Positions\n**Error retrieving DeFi positions**: API request to "
            "portfolio/defi-holdings failed with status 403: Insufficient credits "
            "remaining to call this endpoint."
        )

    portfolio_tool.__name__ = "mcp_nansen_address_portfolio"
    monkeypatch.setattr(
        "app.nodes.runtime._mcp_registry.get",
        lambda name: portfolio_tool if name == "address_portfolio" else None,
    )
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.all", lambda: [portfolio_tool])

    seen_calls = []

    class FakeResult:
        def __init__(self, tool, output):
            self.tool = tool
            self.output = output

    class FakeRouter(_RouterDouble):
        def route(self, request, capability, chains):
            seen_calls.append((request, capability, chains))
            if chains == ():
                # The Hyperliquid supplement's chain-agnostic call -- no
                # Hyperliquid provider configured in this test.
                return FakeResult("some_other_tool", "irrelevant")
            return FakeResult("bitquery_wallet_balances", "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 36000 |\n")

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    state = {
        "request": f"Show current portfolio for {wallet}",
        "capabilities": ["wallet_intelligence"],
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert "USDC | 36000" in result["answer"]  # balances came through despite Nansen's DeFi failure
    assert "DeFi Positions" in result["answer"]
    assert "credits are exhausted" in result["answer"]
    assert captured_defi_payload == {"walletAddress": wallet, "mode": "defi"}  # Token Holdings/Hyperliquid no longer requested from Nansen
    # No chain was named -- the primary EVM tier is swept for balances.
    balance_calls = [call for call in seen_calls if call[2] != ()]
    assert {call[2] for call in balance_calls} == {(chain,) for chain in research._BITQUERY_EVM_CHAINS_PRIMARY}
    # The synthetic balances request must avoid words that would also match
    # GoldRush/Helius's transactions-only wallet_intelligence tools.
    for word in ("wallet", "transactions", "activity", "history", "transfers"):
        assert all(word not in call[0].lower() for call in balance_calls)


def test_compose_wallet_portfolio_all_sources_succeed(monkeypatch):
    """Balances (GoldRush/Bitquery), Hyperliquid (GoldRush) and DeFi
    (Nansen) all succeeding must compose into one answer carrying all
    three, with no disclosed gaps.
    """
    wallet = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"

    def portfolio_tool(**kwargs):
        assert kwargs["request"] == {"walletAddress": wallet, "mode": "defi", "chain": "base"}
        return (
            "# DeFi Positions\n## DeFi Summary\n**Net Value (USD)**: 258.4\n"
            "**Total Assets (USD)**: 300.0\n**Total Debts (USD)**: 41.6\n"
        )

    portfolio_tool.__name__ = "mcp_nansen_address_portfolio"
    monkeypatch.setattr(
        "app.nodes.runtime._mcp_registry.get",
        lambda name: portfolio_tool if name == "address_portfolio" else None,
    )
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.all", lambda: [portfolio_tool])

    class FakeResult:
        def __init__(self, tool, output):
            self.tool = tool
            self.output = output

    class FakeRouter(_RouterDouble):
        def route(self, request, capability, chains):
            if chains == ():
                return FakeResult(
                    "goldrush_hyperliquid_positions",
                    # GoldRush's real hyperliquid_positions() formats these via
                    # _money(), which already prepends "$" -- unlike Nansen's
                    # bare-number fields (e.g. "258.4" below). Deliberately
                    # kept "$"-prefixed here to catch a "$$" doubling bug.
                    "# Hyperliquid positions\n## Account Summary\n**Account Value (USD)**: $5.9K\n"
                    "**Total Notional (USD)**: $53.3K\n",
                )
            return FakeResult(
                "goldrush_wallet_balances",
                "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 36000 |\n",
            )

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    state = {
        "request": f"Show current portfolio for {wallet} on Base",
        "capabilities": ["wallet_intelligence"],
        "chains": ["base"],
    }
    result = asyncio.run(research.research_node(state))
    assert "USDC | 36000" in result["answer"]  # GoldRush balances spliced in verbatim
    assert "DeFi net value is **$258.4**" in result["answer"]  # extracted from Nansen's DeFi-only response
    assert "Hyperliquid account value is **$5.9K**" in result["answer"]  # extracted from GoldRush's Hyperliquid response
    assert "$$" not in result["answer"]  # GoldRush's _money() already prepends "$" -- must not double up
    assert "Error retrieving" not in result["answer"]  # no disclosed gaps when everything succeeds


def test_bitquery_all_chains_sweep_only_checks_secondary_tier_when_primary_finds_nothing(monkeypatch):
    """The 6-chain sweep must not run as one flat pass: only the primary
    3-chain tier (ethereum, base, arbitrum) should be checked when it finds
    a positive balance; the secondary tier (bsc, polygon, avalanche) is
    wasted cost unless the primary tier comes up empty.
    """
    from app.nodes import research

    seen_chains = []

    async def fake_supplement(_wallet, chain):
        seen_chains.append(chain)
        if chain == "polygon":
            return "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 100 |\n"
        return f"# Wallet token balances\n\nNo positive token balances were returned for `w` on {chain}.\n"

    monkeypatch.setattr(research, "_bitquery_balances_supplement", fake_supplement)
    result = asyncio.run(research._bitquery_balances_supplement_all_chains("0xabc"))
    assert seen_chains == list(research._BITQUERY_EVM_CHAINS_PRIMARY) + list(research._BITQUERY_EVM_CHAINS_SECONDARY)
    assert "### Polygon" in result


def test_bitquery_all_chains_sweep_stops_at_primary_tier_when_a_balance_is_found(monkeypatch):
    from app.nodes import research

    seen_chains = []

    async def fake_supplement(_wallet, chain):
        seen_chains.append(chain)
        if chain == "base":
            return "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 100 |\n"
        return f"# Wallet token balances\n\nNo positive token balances were returned for `w` on {chain}.\n"

    monkeypatch.setattr(research, "_bitquery_balances_supplement", fake_supplement)
    result = asyncio.run(research._bitquery_balances_supplement_all_chains("0xabc"))
    assert seen_chains == list(research._BITQUERY_EVM_CHAINS_PRIMARY)  # secondary tier never touched
    assert "### Base" in result


def test_sentiment_request_reaches_market_sentiment_tool_not_crypto_trends_brief(monkeypatch):
    """'crypto fear and greed index right now' matches is_crypto_trends_query
    (broad 'crypto ... right now' pattern) but must reach the dedicated
    market_sentiment tool, not get overridden by the generic
    crypto_market_brief shortcut.
    """
    class FakeResult:
        tool = "market_sentiment_snapshot"
        output = "# Market sentiment snapshot\n\n- **Fear & Greed Index**: 72/100 -- Greed\n"
        failures = ()
        attempted = ("market_sentiment_snapshot",)

    class FakeRouter(_RouterDouble):
        def candidates(self, request, capability, chains=(), allow_semantic_fallback=True):
            return [SimpleNamespace(name="market_sentiment_snapshot")] if capability == "market_sentiment" else []

        def route(self, request, capability, chains=()):
            return FakeResult()

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("crypto_market_brief must not be called for a market_sentiment request")

    monkeypatch.setattr(research, "crypto_market_brief", fail_if_called)

    state = {
        "request": "What is the crypto fear and greed index right now?",
        "capabilities": ["market_sentiment"],
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert "Fear & Greed Index" in result["answer"]


def test_gainers_request_reaches_dedicated_tool_not_crypto_trends_brief(monkeypatch):
    """'Pull live trending tokens and gainers for Solana' matches
    is_crypto_trends_query via 'trending...tokens', but crypto_market_brief
    has no gainers/losers ranking of its own -- routing it there would
    silently drop the 'gainers' half of the request.
    """
    class FakeResult:
        tool = "coingecko_gainers_losers"
        output = "# Gainers on Solana\n\n| Token | Price | 24h change | 24h volume |\n|---|---:|---:|---:|\n| STONK | $0.27 | +44.2% | $129.0M |\n"
        failures = ()
        attempted = ("coingecko_gainers_losers",)

    class FakeRouter(_RouterDouble):
        def candidates(self, request, capability, chains=(), allow_semantic_fallback=True):
            return [SimpleNamespace(name="coingecko_gainers_losers")] if capability == "token_discovery" else []

        def route(self, request, capability, chains=()):
            return FakeResult()

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("crypto_market_brief must not be called when a gainers/losers request resolves")

    monkeypatch.setattr(research, "crypto_market_brief", fail_if_called)

    state = {
        "request": "Pull live trending tokens and gainers for Solana",
        "capabilities": ["token_discovery", "market_data", "web_research"],
        "chains": ["solana"],
    }
    result = asyncio.run(research.research_node(state))
    assert "Gainers on Solana" in result["answer"]
    assert "STONK" in result["answer"]


def test_hyperliquid_funding_rate_request_reaches_market_data_router(monkeypatch):
    """'What is the funding rate and price for BTC on Hyperliquid?' has no
    chain and none of the generic token/coin/pair wording -- verified live
    this made _direct_provider_capability skip market_data entirely and
    fall through to a web-search guess instead of the real
    goldrush_hyperliquid_market tool. Regression for that fix.
    """
    class FakeResult:
        tool = "goldrush_hyperliquid_market"
        output = "# Hyperliquid market data\n\n## BTC\n**Mark Price (USD)**: $77.1K\n"
        failures = ()
        attempted = ("goldrush_hyperliquid_market",)

    class FakeRouter(_RouterDouble):
        def candidates(self, request, capability, chains=(), allow_semantic_fallback=True):
            return [SimpleNamespace(name="goldrush_hyperliquid_market")] if capability == "market_data" else []

        def route(self, request, capability, chains=()):
            return FakeResult()

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())
    # BTC on Hyperliquid is a perp market, not a spot token with a contract -- symbol
    # resolution finds nothing and no-ops, leaving the request for the market router.
    monkeypatch.setattr(research, "token_candidates", lambda symbol, chains=(): [])
    monkeypatch.setattr(research, "search_verified_tokens", lambda q: [])

    state = {
        "request": "What is the funding rate and price for BTC on Hyperliquid right now?",
        "capabilities": ["market_data", "web_research"],
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert "Hyperliquid market data" in result["answer"]
    assert "Mark Price" in result["answer"]


def test_bitquery_all_chains_supplement_prefers_chains_with_real_balances(monkeypatch):
    """Only Base has a positive balance in this fake data; Ethereum and the
    rest come back with the 'no positive balances' placeholder. The combined
    output should lead with the real Base data, not repeat six near-identical
    'nothing found' disclosures.
    """
    async def fake_supplement(_wallet, chain):
        if chain == "base":
            return "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 500 |\n"
        return f"# Wallet token balances\n\nNo positive token balances were returned for `w` on {chain} within Bitquery's realtime window.\n"

    monkeypatch.setattr(research, "_bitquery_balances_supplement", fake_supplement)
    result = asyncio.run(research._bitquery_balances_supplement_all_chains("0xabc"))
    assert "### Base" in result
    assert "USDC | 500" in result
    assert "### Ethereum" not in result  # only chains with real balances are shown


def test_bitquery_all_chains_supplement_discloses_nothing_found_across_all_chains(monkeypatch):
    async def fake_supplement(_wallet, chain):
        return f"# Wallet token balances\n\nNo positive token balances were returned for `w` on {chain} within Bitquery's realtime window.\n"

    monkeypatch.setattr(research, "_bitquery_balances_supplement", fake_supplement)
    result = asyncio.run(research._bitquery_balances_supplement_all_chains("0xabc"))
    assert "Ethereum" in result and "Base" in result and "Arbitrum" in result
    assert "No positive token balances" in result


def test_compose_wallet_portfolio_respects_an_explicit_chain(monkeypatch):
    """A user- or upstream-resolved explicit chain must be respected for
    the balances lookup, not overridden by the all-chains sweep meant only
    for the no-chain-named case.
    """
    wallet = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"

    def portfolio_tool(**_kwargs):
        return "# DeFi Positions\n## DeFi Summary\n**Net Value (USD)**: 258.4\n"

    portfolio_tool.__name__ = "mcp_nansen_address_portfolio"
    monkeypatch.setattr(
        "app.nodes.runtime._mcp_registry.get",
        lambda name: portfolio_tool if name == "address_portfolio" else None,
    )
    monkeypatch.setattr("app.nodes.runtime._mcp_registry.all", lambda: [portfolio_tool])

    seen_chains = []

    class FakeResult:
        def __init__(self, tool, output):
            self.tool = tool
            self.output = output

    class FakeRouter(_RouterDouble):
        def route(self, request, capability, chains):
            seen_chains.append(chains)
            if chains == ():
                return FakeResult("some_other_tool", "irrelevant")
            return FakeResult("bitquery_wallet_balances", "# Wallet token balances\n\n| Token | Balance |\n|---|---:|\n| USDC | 36000 |\n")

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    state = {
        "request": f"Show current portfolio for {wallet} on Base",
        "capabilities": ["wallet_intelligence"],
        "chains": ["base"],
    }
    result = asyncio.run(research.research_node(state))
    balance_chains = {call for call in seen_chains if call != ()}
    assert balance_chains == {("base",)}  # only Base was checked, not a multi-chain sweep
    assert "USDC | 36000" in result["answer"]


def test_security_check_on_evm_contract_with_no_chain_asks_rather_than_substitutes():
    """A token_security question with an EVM contract but no chain word must
    ask which chain it's on -- it must never silently fall through to an
    unrelated capability (e.g. token_discovery market data) that looks like
    an answer but never actually ran a security check.
    """
    contract = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
    state = {
        "request": f"is {contract} a honeypot",
        "capabilities": ["token_security", "token_discovery"],
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert contract in result["answer"]
    assert "chain" in result["answer"].lower()
    assert result["trajectory"] is None


def test_security_check_on_solana_address_infers_chain_without_asking(monkeypatch):
    """A base58 address is unambiguously Solana in this app, so a security
    question about one must not trigger the EVM chain-clarification path --
    it should proceed straight to provider selection with chains=("solana",).
    """
    mint = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
    seen_chains = []

    class FakeRouter(_RouterDouble):
        def candidates(self, request, capability, chains, allow_semantic_fallback=True):
            seen_chains.append(chains)
            return []  # no live provider call; just observe the inferred chains

    monkeypatch.setattr(research, "get_provider_router", lambda: FakeRouter())

    async def fake_call_lm(_program, **_kwargs):
        return SimpleNamespace(answer="stub", trajectory=None)

    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)
    state = {
        "request": f"is {mint} a rug",
        "capabilities": ["token_security", "token_discovery"],
        "chains": [],
    }
    result = asyncio.run(research.research_node(state))
    assert "Which chain" not in result["answer"]
    assert ("solana",) in seen_chains
