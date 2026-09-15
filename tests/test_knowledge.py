"""Knowledge service on the in-memory store: registry bootstrap from a
DefiLlama fixture, normalisation + structure-aware chunking, versioned
documents by content hash, entity resolution ladder, hybrid retrieval with
RRF + graph expansion, the router tool, and the admin/search endpoints."""
import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.knowledge import embeddings, entities as ent, ingest, normalize, registry, retrieval, store as kb_store, tool as kb_tool
from app.knowledge.connectors.base import SourceRef
from app.knowledge.connectors.defillama import DefiLlamaConnector
from app.knowledge.connectors.docs import DocsConnector
from app.knowledge.models import NormalizedDocument, Protocol
from app.settings import settings

LLAMA = [
    {"slug": "aave", "name": "Aave", "symbol": "AAVE", "category": "Lending", "tvl": 20e9, "chains": ["Ethereum", "Arbitrum", "Base"], "url": "https://aave.com", "gecko_id": "aave", "twitter": "aave", "github": ["aave"], "description": "Aave is a non-custodial liquidity protocol where users supply and borrow assets.", "address": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"},
    {"slug": "morpho", "name": "Morpho", "symbol": "MORPHO", "category": "Lending", "tvl": 6e9, "chains": ["Ethereum", "Base"], "url": "https://morpho.org", "description": "Morpho is a lending network."},
    {"slug": "jupiter", "name": "Jupiter", "symbol": "JUP", "category": "Dexs", "tvl": 3e9, "chains": ["Solana"], "url": "https://jup.ag", "description": "Jupiter is the aggregator on Solana; it integrates with Wormhole for bridging.", "address": "solana:JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"},
    {"slug": "binance-cex", "name": "Binance", "category": "CEX", "tvl": 100e9, "chains": []},
]

AAVE_DOCS_HTML = """<html><head><title>Liquidations | Aave Docs</title></head><body><nav>Home Docs</nav>
<main><h1>Liquidations</h1><p>A liquidation is a process that occurs when a borrower's health factor goes below 1 due to their collateral value not properly covering their loan.</p>
<h2>Health Factor</h2><p>The health factor is the numeric representation of the safety of your deposited assets against the borrowed assets. When it drops below 1, a position becomes eligible for liquidation on Ethereum, Arbitrum and Base.</p>
<h2>E-mode</h2><p>Efficiency mode (E-mode) allows a higher liquidation threshold for correlated assets such as stablecoins. USDC can be supplied on Base.</p></main><footer>Copyright</footer></body></html>"""


@pytest.fixture(autouse=True)
def memory_kb(monkeypatch):
    kb_store.reset()
    kb_tool.reset()
    embeddings.set_embedder(embeddings.HashingEmbedder(256))
    monkeypatch.setattr(settings, "knowledge_embedding_dim", 256)

    async def memory():
        if kb_store._store is None:
            kb_store._store = kb_store.MemoryStore()
        return kb_store._store

    monkeypatch.setattr(kb_store, "get_store", memory)
    monkeypatch.setattr(retrieval, "get_store", memory)
    monkeypatch.setattr(ingest, "get_store", memory)
    monkeypatch.setattr(kb_tool, "get_store", memory)
    monkeypatch.setattr(registry, "_fetch_protocols", lambda: LLAMA)
    yield
    kb_store.reset()
    kb_tool.reset()
    embeddings.set_embedder(None)


def test_registry_bootstrap_builds_entities_and_first_edges():
    result = asyncio.run(registry.bootstrap(limit=10))
    assert result["protocols"] == 3  # the CEX is skipped
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    assert aave.chains == ["ethereum", "arbitrum", "base"] and aave.github_org == "aave" and aave.coingecko_id == "aave"
    assert aave.contracts == [{"chain": "ethereum", "address": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "label": "token"}]
    rels = asyncio.run(store.neighbors("protocol:aave"))
    assert {(r.relation, r.target_entity_id) for r in rels if r.source_entity_id == "protocol:aave"} >= {("DEPLOYED_ON", "chain:base"), ("DEPLOYED_ON", "chain:ethereum"), ("IN_CATEGORY", "category:lending")}
    assert any(r.relation == "TOKEN_OF" and r.source_entity_id.startswith("token:ethereum:0x7fc665") for r in rels)
    jup = asyncio.run(store.neighbors("protocol:jupiter"))
    assert any(r.relation == "TOKEN_OF" and r.source_entity_id == "token:solana:JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN" for r in jup)
    assert registry.guess_docs_url(aave) == "https://docs.aave.com"


def test_docs_url_candidates_strip_app_hosts():
    """DefiLlama's url is usually the app (app.morpho.org, portal.arbitrum.io):
    the guess must fall back to the registrable domain and offer /docs paths."""
    from app.knowledge.models import Protocol

    def proto(website, docs_url=None):
        return Protocol(id="protocol:x", slug="x", name="X", website=website, docs_url=docs_url)

    assert registry.guess_docs_urls(proto("https://app.morpho.org")) == ["https://docs.morpho.org", "https://morpho.org/docs", "https://www.morpho.org/docs"]
    assert registry.guess_docs_urls(proto("https://portal.arbitrum.io/bridge"))[0] == "https://docs.arbitrum.io"
    assert registry.guess_docs_urls(proto("https://docs.base.org/base-chain/bridges")) == ["https://docs.base.org", "https://base.org/docs", "https://www.base.org/docs"]
    assert registry.guess_docs_urls(proto("https://kelpdao.xyz/restake/?utm_source=abc"))[0] == "https://docs.kelpdao.xyz"
    assert registry.guess_docs_urls(proto("https://app.sky.money/", docs_url="https://developers.sky.money/"))[0] == "https://developers.sky.money"
    assert registry.guess_docs_urls(proto(None)) == []


def test_entity_resolution_ladder():
    asyncio.run(registry.bootstrap(limit=10))
    resolver = asyncio.run(kb_tool.resolver())
    assert resolver.resolve("0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9").method == "contract"
    assert resolver.resolve("0x7FC66500C84A76AD7E9C93437BFC5AC33E2DDAE9", chain="ethereum").confidence == 1.0
    assert resolver.resolve("aave").method in ("canonical_name", "coingecko_id") and resolver.resolve("Aave").confidence >= 0.9
    assert resolver.resolve("eip155:1").entity.id == "chain:ethereum" and resolver.resolve("ETH").entity.id == "chain:ethereum"
    ticker = resolver.resolve("JUP")
    assert ticker is not None and ticker.confidence <= 0.75 and ticker.entity.entity_type in ("protocol", "token")  # ticker/alias never high confidence
    assert resolver.resolve("$AI") is None
    mentions = {r.entity.id for r in resolver.mentions("Users can supply USDC to Aave on Base; Morpho competes with Aave.")}
    assert {"protocol:aave", "protocol:morpho", "chain:base"} <= mentions


def test_html_to_structured_chunks_and_hash_stability():
    md = normalize.html_to_markdown(AAVE_DOCS_HTML)
    assert md.startswith("# Liquidations") and "Home Docs" not in md and "Copyright" not in md
    chunks = normalize.chunk_markdown(md, max_chars=400, min_chars=50)
    assert [c["heading"] for c in chunks] == ["Liquidations", "Liquidations > Health Factor", "Liquidations > E-mode"]
    assert normalize.content_hash(md) == normalize.content_hash(md.replace("\n", " ") + "  ")


def test_ingest_versions_documents_and_reindexes_only_changes():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    doc = DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)
    before = len(store.relationships)
    changed, chunks, rels = asyncio.run(ingest.ingest_document(doc))
    assert changed and chunks == 3
    # The docs mention Ethereum/Arbitrum/Base -- edges DefiLlama already gave us
    # with 0.98 confidence, so the low-confidence mention edges merge, not duplicate.
    assert rels == 0 and len(store.relationships) == before
    assert asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML))) == (False, 0, 0)
    edited = AAVE_DOCS_HTML.replace("higher liquidation threshold", "much higher liquidation threshold")
    changed, chunks, _ = asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", edited)))
    assert changed and chunks == 3
    live = asyncio.run(store.live_documents("protocol:aave"))
    assert len(live) == 1 and live[0].version == 2
    assert sum(1 for d in store.documents.values() if d.url.endswith("/liquidations")) == 2  # old version kept, closed
    assert len([c for c in store.chunks.values() if c.document_id == live[0].id]) == 3


def test_hybrid_retrieval_fuses_semantic_lexical_and_graph():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    for slug in ("aave", "morpho", "jupiter"):
        protocol = asyncio.run(store.get_protocol(f"protocol:{slug}"))
        asyncio.run(ingest.ingest_document(DefiLlamaConnector.document(protocol, next(i for i in LLAMA if i["slug"] == slug), f"https://api.llama.fi/protocol/{slug}")))
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    hits, plan = asyncio.run(retrieval.search("Explain Aave E-mode liquidation threshold", limit=4, resolver=asyncio.run(kb_tool.resolver())))
    assert plan.protocol_ids == ["protocol:aave"]
    assert hits[0].chunk.heading == "Liquidations > E-mode" and {"lexical", "semantic"} & set(hits[0].sources)
    context, citations = retrieval.build_context(hits)
    assert context.startswith("[1] Aave · Liquidations › Liquidations > E-mode") and citations[0]["url"] == "https://docs.aave.com/liquidations"
    # Graph: a competitor question expands through the shared category edge.
    asyncio.run(store.upsert_relationship(retrieval_rel("protocol:morpho", "COMPETITOR_OF", "protocol:aave")))
    hits, plan = asyncio.run(retrieval.search("What protocols compete with Aave on Base?", limit=6, resolver=asyncio.run(kb_tool.resolver())))
    assert "protocol:morpho" in plan.graph_expanded
    assert any(h.protocol_name == "Morpho" and "graph" in h.sources for h in hits)


def retrieval_rel(src, rel, dst):
    from app.knowledge.models import Relationship

    return Relationship(src, rel, dst, confidence=0.9)


def test_router_tool_matches_knowledge_asks_not_live_numbers():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    assert not kb_tool.matches("What is Aave?")  # no snapshot yet: the matcher never does I/O
    asyncio.run(kb_tool.resolver(force=True))
    assert kb_tool.matches("What is Aave and how do liquidations work?")
    assert not kb_tool.matches("Aave TVL today?")            # live number -> DefiLlama tool
    assert not kb_tool.matches("what is a liquidity pool?")  # nothing in the registry
    card = kb_tool.knowledge_base_search("How does the Aave health factor work?")
    assert card.startswith("# Knowledge base") and "[1] Aave" in card and "docs.aave.com/liquidations" in card and "cite them as [n]" in card


def test_run_source_uses_connector_interface_and_records_state():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))

    class FakeDocs:
        name, source_type, refresh_every = "fake_docs", "protocol_docs", timedelta(hours=1)

        def applies(self, protocol):
            return protocol.slug == "aave"

        def discover(self, protocol):
            return [SourceRef(url="https://docs.aave.com/liquidations"), SourceRef(url="https://docs.aave.com/broken")]

        def fetch(self, protocol, ref):
            if ref.url.endswith("broken"):
                raise RuntimeError("500 upstream")
            return DocsConnector.document(protocol, ref.url, AAVE_DOCS_HTML)

    result = asyncio.run(ingest.run_source(FakeDocs(), aave))
    assert result.documents_seen == 1 and result.documents_changed == 1 and result.chunks_written == 3 and len(result.errors) == 1
    assert asyncio.run(store.get_source_state("fake_docs", "protocol:aave"))["documents"] == 1
    assert not asyncio.run(ingest.due(FakeDocs(), aave, store))
    assert asyncio.run(ingest.run_protocol("protocol:aave", connectors=[FakeDocs()]))[0].documents_changed == 0  # unchanged -> no re-embed


def test_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    client = TestClient(main.app)
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.post("/admin/knowledge/bootstrap?limit=10").status_code in (401, 403)
    assert client.post("/admin/knowledge/bootstrap?limit=10", headers=admin).json()["protocols"] == 3
    status = client.get("/knowledge/status").json()
    assert status["backend"] == "memory" and status["protocols"] == 3 and status["embedder"] == "hashing"
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    search = client.get("/knowledge/search?q=Aave%20health%20factor").json()
    assert search["entities"][0]["id"] == "protocol:aave" and search["hits"][0]["heading"] == "Liquidations > Health Factor"
    assert client.get("/knowledge/search?q=").status_code in (400, 422)
    assert client.post("/admin/knowledge/ingest/nope", headers=admin).status_code == 404


def test_research_node_synthesizes_over_knowledge_passages(monkeypatch):
    from app.nodes import research as research_mod, runtime
    from app.provider_router import ProviderResult

    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    asyncio.run(kb_tool.resolver(force=True))
    passages = kb_tool.knowledge_base_search("How does the Aave health factor work?")
    monkeypatch.setattr(research_mod.get_provider_router(), "try_route_across", lambda *a, **k: ProviderResult(passages, "knowledge_base_search", "knowledge"))
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: type("R", (), {"try_route_across": lambda self, *a, **k: ProviderResult(passages, "knowledge_base_search", "knowledge"), "matched_capabilities": lambda self, *a, **k: {"knowledge"}})())

    async def fake_lm(program, **kwargs):
        assert "[1] Aave" in kwargs["passages"]
        return type("Out", (), {"answer": "The health factor is a safety ratio; below 1 a position can be liquidated [1]."})()

    monkeypatch.setattr(runtime, "_call_lm", fake_lm)
    out = asyncio.run(research_mod.research_node({"request": "How does the Aave health factor work?", "capabilities": ["knowledge"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"].startswith("The health factor is a safety ratio") and "## Sources" in out["answer"] and "docs.aave.com/liquidations" in out["answer"]
    assert out["trajectory"]["tool_name_0"] == "knowledge_base_search"
