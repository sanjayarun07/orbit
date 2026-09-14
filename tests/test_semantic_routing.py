from app.nodes import runtime, routing, portfolio, research
import asyncio
from types import SimpleNamespace

import pytest

from app import graph
from app.routing.semantic import EmbeddingRouter, SemanticMatch, SpeechUnderstanding, rank, unit
from app.settings import settings


@pytest.mark.parametrize("prompt", [
    "Should I buy BONK?", "Show buy and sell volume for BONK",
    "How do I trade meme coins?", "Recommend the best exchange for SOL",
    "Do not buy BONK", "If SOL drops below 100 buy BONK", "Buy NVDA",
])
def test_negative_speech_never_reaches_quote_nodes(monkeypatch, prompt):
    async def read_only(_state):
        return {"answer": "Read-only answer"}

    async def forbidden(_state):
        pytest.fail("Non-execution speech reached a quote planner")

    monkeypatch.setattr(graph, "general_node", read_only)
    monkeypatch.setattr(graph, "research_node", read_only)
    monkeypatch.setattr(graph, "trade_planner_node", forbidden)
    monkeypatch.setattr(graph, "cross_chain_swap_node", forbidden)
    result = asyncio.run(graph.build_graph().ainvoke({
        "request": prompt,
        "wallet_address": "wallet",
        "history": "user: Swap 0.1 SOL to USDC on Base",
        "session_context": {"active_workflow": {"intent": "cross_chain_swap", "status": "collecting_details", "source_chain": "solana", "destination_chain": "base"}},
    }))
    # "portfolio" is a third safe outcome alongside research/general -- a
    # hypothetical trade question ("Should I buy BONK?") can legitimately
    # resolve to the read-only trade_simulation capability (app/nodes/
    # portfolio.py), which never reaches trade_planner_node/
    # cross_chain_swap_node either (enforced below, same as before).
    assert result["intent"] in {"research", "general", "portfolio"}
    assert not result.get("trade_plan")
    assert not result.get("cross_chain_swap")
    assert "routing_decision" in result


def test_competing_neighbours_abstain_even_with_high_similarity():
    examples = (("quote", "crypto", "buy"), ("advice", "crypto", "should buy"))
    result = rank(unit([1, 0]), [unit([1, 0]), unit([1, .1])], examples)
    assert result.understanding is None
    assert result.reason == "low_margin"


def test_nearest_neighbour_resolves_read_only_label():
    examples = (("quote", "crypto", "buy"), ("advice", "crypto", "should buy"))
    result = rank(unit([0, 1]), [unit([1, 0]), unit([0, 1])], examples)
    assert result.understanding.speech_act == "advice"
    assert result.understanding.explicit_action is False


def test_low_margin_cannot_be_promoted_by_model(monkeypatch):
    monkeypatch.setattr(routing, "embedding_router", lambda _: SimpleNamespace(classify=lambda _: SemanticMatch(None, "embedding", .9, .01, "low_margin")))

    async def forbidden(*args, **kwargs):
        pytest.fail("Model must not promote an ambiguous embedding decision")

    monkeypatch.setattr(runtime, "_call_lm", forbidden)
    result = asyncio.run(graph.resolve_intent_node({"request": "Would you buy another coin?"}))
    assert result["intent"] == "general"
    assert result["clarification"]


@pytest.mark.parametrize("confidence,explicit", [(0.5, True), (.99, False)])
def test_model_cannot_propose_on_uncertainty_or_missing_action(monkeypatch, confidence, explicit):
    monkeypatch.setattr(routing, "embedding_router", lambda _: SimpleNamespace(classify=lambda _: SemanticMatch(None, "embedding", reason="provider_unavailable")))

    async def predict(*args, **kwargs):
        return SimpleNamespace(understanding=SpeechUnderstanding(speech_act="quote", domain="crypto", explicit_action=explicit, confidence=confidence))

    monkeypatch.setattr(runtime, "_call_lm", predict)
    result = asyncio.run(graph.resolve_intent_node({"request": "Could you buy another coin?"}))
    assert result["intent"] == "general"
    assert result["clarification"]


def test_embedding_cache_and_example_initialization_are_reused(monkeypatch):
    from app.routing import semantic
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.embeddings = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            calls.append(kwargs["input"])
            return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[1., 0.]) for i, _ in enumerate(kwargs["input"])])

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(semantic, "OpenAI", Client)
    router = EmbeddingRouter("test-model")
    router.classify("a held-out utterance")
    router.classify("a held-out utterance")
    router.classify("another held-out utterance")
    assert len(calls) == 2
    assert len(calls[0]) == len(semantic.EXAMPLES) + 1
    assert calls[1] == ["another held-out utterance"]


def test_provider_errors_are_sanitized_and_circuit_breaks(monkeypatch):
    from app.routing import semantic
    calls = []

    def broken(**kwargs):
        calls.append(1)
        raise RuntimeError("secret provider error")

    monkeypatch.setattr(settings, "openai_api_key", "test")
    monkeypatch.setattr(semantic, "OpenAI", broken)
    router = EmbeddingRouter("test-model")
    assert router.classify("unmatched one").reason == "provider_unavailable"
    assert router.classify("unmatched two").reason == "circuit_open"
    assert len(calls) == 1


def test_conceptual_topic_switch_clears_execution_context():
    from app.experience import advance_session_context

    result = advance_session_context(
        {"revision": 2, "active_workflow": {"intent": "cross_chain_swap", "status": "collecting_details"}},
        "How do I trade tokens?", "wallet", "general", [], [], None,
    )
    assert result["active_workflow"] is None


def test_clear_trade_routes_still_choose_correct_venue():
    for prompt, venue in (
        ("Buy WIF on Solana with .02 SOL", "jupiter"),
        ("Swap 0.1 SOL on Solana to USDC on Base", "relay"),
    ):
        result = asyncio.run(graph.resolve_intent_node({"request": prompt}))
        assert result["execution_provider"] == venue
