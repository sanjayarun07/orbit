from app.nodes import runtime, routing, portfolio, research
from app.jupiter import WRAPPED_SOL_MINT
from app.trade_context import clean_mint, complete_swap_fields
from app import graph
import asyncio
from types import SimpleNamespace


ANSEM_MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def relay_context():
    return {"active_workflow": {"intent": "cross_chain_swap", "status": "collecting_details", "source_chain": "solana", "destination_chain": "base", "amount": "0.1", "input_token": "SOL", "output_token": "USDC", "slippage_bps": 10}}


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
    assert "Which chain is ANSEM on?" in answer["answer"]
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
    class Router:
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
