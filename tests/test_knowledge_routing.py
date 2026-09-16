"""The knowledge base is wired through every routing layer, not only its own
module: the anchored intent rule, the resolver's anchor list, the provider
registry (capability `knowledge`), the research node's direct/backstop lists,
the MCP surface and the credit meter. These tests pin each seam so a
refactor of one layer cannot silently drop the KB from the path."""

import asyncio

import pytest

from app import provider_registry
from app.knowledge import embeddings, ingest, registry, retrieval, store as kb_store, tool as kb_tool
from app.knowledge.connectors.docs import DocsConnector
from app.nodes import research
from app.routing import intent_router, resolver as routing_resolver
from app.settings import settings

LLAMA = [
    {"slug": "aave-v3", "name": "Aave V3", "symbol": "AAVE", "category": "Lending", "tvl": 20e9, "chains": ["Ethereum", "Base"], "url": "https://aave.com", "gecko_id": "aave", "description": "Aave is a liquidity protocol."},
    {"slug": "kamino-lend", "name": "Kamino Lend", "symbol": "KMNO", "category": "Lending", "tvl": 2e9, "chains": ["Solana"], "url": "https://kamino.com", "description": "Kamino is a Solana lending market."},
]
DOCS = (
    "<html><head><title>Liquidations</title></head><body><main><h1>Liquidations</h1>"
    "<p>A position is liquidated when its health factor drops below 1, meaning the borrowed value is no longer covered by the discounted collateral value.</p>"
    "<h2>E-mode</h2><p>Efficiency mode (E-mode) raises the liquidation threshold and loan-to-value for correlated assets such as stablecoins, so borrowers can take more leverage within a category.</p>"
    "</main></body></html>"
)


@pytest.fixture(autouse=True)
def memory_kb(monkeypatch):
    kb_store.reset()
    kb_tool.reset()
    embeddings.set_embedder(embeddings.HashingEmbedder(128))
    monkeypatch.setattr(settings, "knowledge_embedding_dim", 128)

    async def memory():
        if kb_store._store is None:
            kb_store._store = kb_store.MemoryStore()
        return kb_store._store

    for module in (kb_store, retrieval, ingest, kb_tool):
        monkeypatch.setattr(module, "get_store", memory)
    monkeypatch.setattr(registry, "_fetch_protocols", lambda: LLAMA)
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave-v3"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", DOCS)))
    asyncio.run(kb_tool.resolver(force=True))
    yield
    kb_store.reset()
    kb_tool.reset()
    embeddings.set_embedder(None)


def test_anchored_intent_rule_sends_protocol_asks_to_the_knowledge_capability():
    for ask in ("How does Aave V3's E-mode change the liquidation threshold?", "what is Kamino Lend", "has Aave been hacked?", "who are Kamino's competitors on Solana"):
        route = intent_router.route_capabilities(ask)
        assert route is not None and route.reason == "knowledge_base", ask
        assert route.intent == "research" and route.capabilities[0] == "knowledge"
    # Live numbers and unknown subjects never anchor to the KB.
    for ask in ("what is Aave's TVL right now", "what is a liquidity pool"):
        route = intent_router.route_capabilities(ask)
        assert route is None or route.reason != "knowledge_base", ask


def test_knowledge_base_is_an_anchor_the_speech_model_cannot_override():
    assert "knowledge_base" in routing_resolver._ANCHORED_REASONS


def test_provider_registry_exposes_the_knowledge_tool_under_its_own_capability():
    router = provider_registry.get_provider_router()
    entry = next((e for e in router.catalog() if e["name"] == "knowledge_base_search"), None)
    assert entry is not None and entry["capabilities"] == ["knowledge"] and entry["configured"] and entry["risk"] in ("read", "read_only", "low", "none")
    # Reachable through the tool's own matcher even when the classifier did not emit `knowledge`.
    assert "knowledge" in router.matched_capabilities("How does Aave V3's E-mode change the liquidation threshold?")
    assert "knowledge" not in router.matched_capabilities("what is Aave's TVL right now")


def test_research_node_prefers_the_knowledge_capability_and_keeps_it_as_a_backstop():
    assert research._DIRECT_CAPABILITY_ORDER[0] == "knowledge"
    assert "knowledge" in research._BACKSTOP_CAPABILITIES


def test_knowledge_tool_card_cites_sources_for_the_synthesizer():
    card = kb_tool.knowledge_base_search("How does Aave V3's E-mode change the liquidation threshold?")
    assert card.startswith("# Knowledge base") and "[1]" in card and "## Sources" in card and "docs.aave.com/liquidations" in card
    assert "cite them as [n]" in card


def test_knowledge_turn_is_metered_as_a_tool_call():
    from app import credits

    assert credits.turn_cost("research", {"tool_name_1": "knowledge_base_search", "tool_name_2": "finish"}, None) == (settings.credit_cost_tool_turn, "tool_turn")
    assert credits.turn_cost("research", {"tool_name_1": "finish"}, None) == (settings.credit_cost_chat_turn, "chat")
    assert settings.credit_cost_tool_turn > settings.credit_cost_chat_turn
