import asyncio

from app import graph, sessions
from app.experience import advance_session_context
from app.context_entities import resolve_contextual_request
from app.models import ContextCapsule, IntentLock
from app.suggestions import suggested_actions, structured_quick_actions


def test_session_context_persists_independently_of_prompt_history(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr(sessions, "get_redis", no_redis)
    session_id = "structured-state-test"
    context = {
        "revision": 3,
        "focus": {"kind": "token", "label": "ANSEM", "address": "mint", "chain": "solana"},
        "active_workflow": None,
    }
    asyncio.run(sessions.save_session_context(session_id, context))
    assert asyncio.run(sessions.get_session_context(session_id)) == context
    asyncio.run(sessions.clear_history(session_id))
    assert asyncio.run(sessions.get_session_context(session_id))["revision"] == 0


def test_atomic_turn_commit_and_recovery_keep_same_token_focus(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr(sessions, "get_redis", no_redis)
    session_id = "atomic-context-test"
    context = {
        "revision": 1,
        "focus": {"kind": "token", "label": "ANSEM", "address": "mint", "chain": "solana"},
        "active_workflow": None,
    }
    metadata = {
        "intent": "research",
        "capabilities": ["market_data"],
        "context_capsules": [
            {"kind": "token", "label": "ANSEM", "address": "mint", "chain": "solana"},
            {"kind": "wallet", "label": "Wallet", "address": "wallet", "chain": "solana"},
        ],
    }
    asyncio.run(sessions.commit_turn(session_id, "ANSEM price", "answer", metadata, context))
    messages = asyncio.run(sessions.get_messages(session_id))
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert asyncio.run(sessions.get_session_context(session_id))["focus"]["label"] == "ANSEM"
    sessions._contexts.pop(session_id)
    assert asyncio.run(sessions.get_session_context(session_id))["focus"]["label"] == "ANSEM"
    asyncio.run(sessions.clear_history(session_id))


def test_local_session_turns_are_serialized(monkeypatch):
    async def no_redis():
        return None

    monkeypatch.setattr(sessions, "get_redis", no_redis)

    async def scenario():
        first = await sessions.acquire_session_turn("serialized-test")
        waiter = asyncio.create_task(sessions.acquire_session_turn("serialized-test"))
        await asyncio.sleep(0)
        assert not waiter.done()
        await first.release()
        second = await waiter
        await second.release()

    asyncio.run(scenario())


def test_context_snapshot_tracks_focus_workflow_revision_and_cancel():
    lock = IntentLock(
        action="bridge",
        source_chain="solana",
        destination_chain="base",
        amount="0.1",
        input_token="SOL",
        output_token="USDC",
        max_slippage_bps=10,
        fingerprint="abc123",
    )
    context = advance_session_context(
        {},
        "Swap 0.1 SOL on Solana to USDC on Base",
        "wallet",
        "cross_chain_swap",
        ["cross_chain_swap"],
        [ContextCapsule(kind="token", label="USDC", chain="base")],
        lock,
    )
    assert context["revision"] == 1
    assert context["focus"]["label"] == "USDC"
    assert context["active_workflow"]["destination_chain"] == "base"

    cancelled = advance_session_context(
        context, "cancel it", "wallet", "general", [], [], None
    )
    assert cancelled["revision"] == 2
    assert cancelled["active_workflow"] is None


def test_explicit_research_switch_clears_old_execution_context_and_focus():
    previous = {
        "revision": 2,
        "focus": {"kind": "token", "label": "USDC", "chain": "base"},
        "active_workflow": {
            "intent": "cross_chain_swap",
            "status": "collecting_details",
            "source_chain": "solana",
            "destination_chain": "base",
            "amount": "0.1",
            "input_token": "SOL",
            "output_token": "USDC",
        },
    }
    context = advance_session_context(
        previous,
        "What's trending in crypto right now?",
        "wallet",
        "research",
        ["web_research"],
        [],
        None,
    )
    assert context["focus"] is None
    assert context["active_workflow"] is None


def test_cross_chain_modifier_persists_merged_workflow_draft():
    draft = graph.CrossChainSwapDraft(
        source_chain="solana",
        destination_chain="base",
        amount="0.1",
        input_token="SOL",
        output_token="USDC",
        slippage_bps=25,
    )
    context = advance_session_context(
        {"revision": 1, "active_workflow": {"intent": "cross_chain_swap", "status": "collecting_details"}},
        "use 25 bps instead",
        "wallet",
        "cross_chain_swap",
        ["cross_chain_swap"],
        [],
        None,
        draft,
    )
    assert context["active_workflow"]["slippage_bps"] == 25
    assert context["active_workflow"]["destination_chain"] == "base"


def test_chain_followup_merges_incomplete_trade_without_losing_token():
    previous = {
        "revision": 1,
        "active_workflow": {
            "intent": "trade",
            "status": "collecting_details",
            "source_chain": None,
            "destination_chain": None,
            "amount": None,
            "input_token": None,
            "output_token": "ANSEM",
            "recipient": None,
            "slippage_bps": None,
        },
    }
    context = advance_session_context(
        previous, "on Solana", "wallet", "trade",
        ["swap", "token_resolve", "token_security"], [], None,
    )
    workflow = context["active_workflow"]
    assert workflow["source_chain"] == "solana"
    assert workflow["destination_chain"] == "solana"
    assert workflow["output_token"] == "ANSEM"


def test_typed_quick_action_routes_without_reclassifying_label():
    action = structured_quick_actions(
        ["Show my recent transactions"], 4
    )[0].model_dump()
    state = {
        "request": "Show my recent transactions",
        "history": "user: old unrelated stock news",
        "session_context": {"revision": 4},
        "quick_action": action,
    }
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "portfolio"
    assert route["capabilities"] == ["wallet_transactions", "wallet_intelligence"]
    assert route["route_source"] == "quick_action"


def test_wallet_followup_buttons_have_explicit_portfolio_capabilities():
    expected = {
        "Show my SPL token holdings": "token_holdings",
        "Check my wallet health": "wallet_health",
        "Show my SOL balance": "token_balance",
    }
    for prompt, capability in expected.items():
        action = structured_quick_actions(
            [prompt], 4, "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "solana"
        )[0]
        assert action.intent == "portfolio"
        assert capability in action.capabilities
        assert action.wallet_scope == "connected"
        assert action.entity_kind is None


def test_cross_chain_modifier_uses_canonical_workflow_without_text_history():
    state = {
        "request": "use 25 bps instead",
        "wallet_address": "wallet",
        "history": "",
        "chains": [],
        "session_context": {
            "revision": 2,
            "active_workflow": {
                "intent": "cross_chain_swap",
                "source_chain": "solana",
                "destination_chain": "base",
                "amount": "0.1",
                "input_token": "SOL",
                "output_token": "USDC",
                "max_slippage_bps": 10,
            },
        },
    }
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["route_source"] == "session_context"
    state["chains"] = route["chains"]
    update = asyncio.run(graph.cross_chain_swap_node(state))
    assert update["cross_chain_swap"].source_chain == "solana"
    assert update["cross_chain_swap"].destination_chain == "base"
    assert update["cross_chain_swap"].slippage_bps == 25


def test_new_incomplete_trade_replaces_old_active_workflow():
    previous = {
        "revision": 5,
        "active_workflow": {
            "intent": "cross_chain_swap",
            "amount": "0.1",
            "input_token": "SOL",
            "output_token": "USDC",
            "source_chain": "solana",
            "destination_chain": "base",
        },
    }
    context = advance_session_context(
        previous,
        "buy BONK token",
        "wallet",
        "cross_chain_swap",
        ["cross_chain_swap", "token_resolve"],
        [ContextCapsule(kind="token", label="BONK", chain="solana")],
        None,
    )
    active = context["active_workflow"]
    assert active["status"] == "collecting_details"
    assert active["output_token"] == "BONK"
    assert active["amount"] is None
    assert active["output_token"] != previous["active_workflow"]["output_token"]


def test_parameter_answer_continues_collecting_workflow_without_model_classifier():
    state = {
        "request": "0.02 ETH on Base with 30 bps",
        "history": "",
        "session_context": {
            "revision": 1,
            "active_workflow": {
                "intent": "cross_chain_swap",
                "status": "collecting_details",
                "output_token": "BONK",
            },
        },
    }
    route = asyncio.run(graph.resolve_intent_node(state))
    assert route["intent"] == "cross_chain_swap"
    assert route["route_source"] == "session_context"


def test_named_buy_binds_matching_solana_token_focus_and_uses_jupiter():
    context = {
        "revision": 2,
        "focus": {
            "kind": "token",
            "label": "ANSEM",
            "address": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump",
            "chain": "solana",
        },
    }
    resolved = resolve_contextual_request("buy ANSEM", "", context)
    assert "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" in resolved
    route = asyncio.run(graph.resolve_intent_node({
        "request": "buy ANSEM",
        "contextual_request": resolved,
        "history": "",
        "session_context": context,
        "quick_action": None,
    }))
    assert route["intent"] == "trade"
    assert route["capabilities"][0] == "swap"


def test_wallet_transactions_and_token_market_actions_do_not_fall_into_news():
    wallet_actions = suggested_actions(
        "Show my recent transactions",
        "portfolio",
        capabilities=["wallet_transactions", "wallet_intelligence"],
    )
    assert "Show more recent developments" not in wallet_actions
    assert "Show my SPL token holdings" in wallet_actions

    token_actions = suggested_actions(
        "Show recent DEX trades for 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump on Solana",
        "research",
        capabilities=["market_data"],
    )
    assert "Show more recent developments" not in token_actions
    assert any("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" in action for action in token_actions)
