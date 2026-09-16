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
    changed, chunks, rels = asyncio.run(ingest.ingest_document(doc))
    assert changed and chunks == 3
    # The docs mention Ethereum/Arbitrum/Base. DefiLlama already asserted those
    # DEPLOYED_ON edges at 0.98; the mention is separate low-confidence evidence
    # carrying its document id, and never raises the structured edge's confidence.
    assert rels == 3
    eth = asyncio.run(store.neighbors("protocol:aave", relation="DEPLOYED_ON"))
    eth = [r for r in eth if r.target_entity_id == "chain:ethereum"]
    assert {r.confidence for r in eth} == {0.98, 0.55} and [r for r in eth if r.confidence == 0.55][0].source_document_id == doc.id
    assert asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML))) == (False, 0, 0)
    edited = AAVE_DOCS_HTML.replace("higher liquidation threshold", "much higher liquidation threshold")
    old_id = doc.id
    changed, chunks, rels = asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", edited)))
    assert changed and chunks == 3 and rels == 3
    live = asyncio.run(store.live_documents("protocol:aave"))
    assert len(live) == 1 and live[0].version == 2
    # The superseded version's evidence closes with it: live mention edges stay at one per chain.
    assert all(r.valid_to is not None for r in store.relationships if r.source_document_id == old_id)
    assert len([r for r in asyncio.run(store.neighbors("protocol:aave", relation="DEPLOYED_ON")) if r.target_entity_id == "chain:ethereum"]) == 2
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


# --- overrides, Discourse, derived edges, reranker seam -----------------------


def test_overrides_apply_docs_forum_and_snapshot_at_bootstrap(monkeypatch):
    from app.knowledge import overrides

    monkeypatch.setitem(overrides.OVERRIDES, "aave", {"docs_url": "https://aave.gitbook.io/docs", "forum_url": "https://governance.aave.com", "governance_url": "https://snapshot.org/#/aave.eth"})
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    assert aave.docs_url == "https://aave.gitbook.io/docs" and aave.forum_url == "https://governance.aave.com"
    assert registry.guess_docs_urls(aave)[0] == "https://aave.gitbook.io/docs"   # override outranks the guess
    from app.knowledge.connectors.discourse import DiscourseConnector
    from app.knowledge.connectors.snapshot import SnapshotConnector

    assert DiscourseConnector().applies(aave) and SnapshotConnector.space_for(aave) == "aave.eth"
    morpho = asyncio.run(store.get_protocol("protocol:morpho"))
    assert not DiscourseConnector().applies(morpho)


def test_discourse_topic_becomes_governance_document():
    from app.knowledge.connectors.discourse import DiscourseConnector

    aave = Protocol(id="protocol:aave", slug="aave", name="Aave", forum_url="https://governance.aave.com/")
    refs = DiscourseConnector().discover(aave)
    assert refs[0].kind == "proposal" and refs[0].metadata["forum"] == "https://governance.aave.com"
    topic = {"id": 12345, "title": "[ARFC] Raise USDC LTV on Base", "slug": "arfc-raise-usdc-ltv-on-base", "created_at": "2026-09-01T10:00:00.000Z",
             "last_posted_at": "2026-09-03T08:00:00Z", "posts_count": 3, "category_id": 7, "tags": ["arfc", "risk"]}
    detail = {"post_stream": {"posts": [
        {"username": "chaos-labs", "created_at": "2026-09-01T10:00:00Z", "cooked": "<p>We propose raising the <strong>USDC</strong> loan-to-value on Base from 75% to 78% given liquidity depth and the health factor distribution of current borrowers.</p>"},
        {"username": "gauntlet", "created_at": "2026-09-02T10:00:00Z", "cooked": "<p>Supportive; our simulations show liquidations stay manageable at 78% under a 30% drawdown.</p>"},
        {"username": "bot", "created_at": "2026-09-02T11:00:00Z", "cooked": "<p>+1</p>"},
    ]}}
    doc = DiscourseConnector.document(aave, "https://governance.aave.com", topic, detail)
    assert doc.source_type == "governance" and doc.source == "aave_forum"
    assert doc.url == "https://governance.aave.com/t/arfc-raise-usdc-ltv-on-base/12345" and doc.title.startswith("Forum: [ARFC] Raise USDC LTV")
    assert doc.published_at.isoformat().startswith("2026-09-01") and doc.metadata["topic_id"] == 12345 and doc.metadata["tags"] == ["arfc", "risk"]
    assert "## Post" in doc.content and "loan-to-value on Base" in doc.content
    assert "## Discussion" in doc.content and "**gauntlet**" in doc.content and "+1" not in doc.content   # short replies dropped
    assert DiscourseConnector.document(aave, "https://governance.aave.com", {"id": 1, "title": "x"}, {"post_stream": {"posts": [{"cooked": "<p>hi</p>"}]}}) is None


def test_derived_competitor_and_corroborated_integration_edges():
    from app.knowledge import derive
    from app.knowledge.models import Relationship

    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    # COMPETITOR_OF: Aave and Morpho share Lending + Ethereum/Base; Jupiter (Dexs, Solana) has no peer.
    assert asyncio.run(derive.derive_competitors(store)) == 1
    edges = [r for r in asyncio.run(store.neighbors("protocol:aave", relation="COMPETITOR_OF"))]
    assert len(edges) == 1 and {edges[0].source_entity_id, edges[0].target_entity_id} == {"protocol:aave", "protocol:morpho"}
    assert edges[0].confidence == derive.COMPETITOR_CONFIDENCE and edges[0].metadata["shared_chains"] == ["base", "ethereum"]
    assert asyncio.run(store.neighbors("protocol:jupiter", relation="COMPETITOR_OF")) == []
    assert asyncio.run(derive.derive_competitors(store)) == 0   # idempotent
    # INTEGRATES_WITH: one mention stays at 0.5; mentions from both sides' docs are promoted to 0.75 with evidence.
    mention = lambda src, dst, doc, source: Relationship(src, "INTEGRATES_WITH", dst, confidence=0.5, source_document_id=doc, metadata={"method": "mention", "source": source})  # noqa: E731
    asyncio.run(store.upsert_relationship(mention("protocol:jupiter", "protocol:aave", "d1", "jupiter_docs")))
    asyncio.run(store.upsert_relationship(mention("protocol:aave", "protocol:morpho", "d2", "aave_docs")))
    asyncio.run(store.upsert_relationship(mention("protocol:morpho", "protocol:aave", "d3", "morpho_docs")))
    assert asyncio.run(derive.derive_integrations(store)) == 1
    promoted = [r for r in asyncio.run(store.list_relationships("INTEGRATES_WITH")) if r.metadata.get("method") == "corroborated_mentions"]
    assert len(promoted) == 1 and promoted[0].confidence == derive.INTEGRATION_CONFIDENCE and promoted[0].metadata["mutual"] is True
    assert set(promoted[0].metadata["documents"]) == {"d2", "d3"} and {promoted[0].source_entity_id, promoted[0].target_entity_id} == {"protocol:aave", "protocol:morpho"}
    jupiter_edges = asyncio.run(store.neighbors("protocol:jupiter", relation="INTEGRATES_WITH"))
    assert all(r.confidence == 0.5 for r in jupiter_edges)
    # graph expansion from a protocol prefers the derived protocol edges over "everything on Ethereum".
    resolver = ent.EntityResolver(asyncio.run(store.list_entities()))
    plan = asyncio.run(retrieval.plan_query("how does Aave compare", resolver))
    expanded = asyncio.run(retrieval.graph_expand(plan, store))
    assert expanded[0] == "protocol:morpho"


def test_reranker_seam_and_factory_fallback(monkeypatch):
    from app.knowledge import reranker as rr
    from app.knowledge.models import Chunk, RetrievalHit

    assert rr.build_reranker("bogus").name == "heuristic"
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert rr.build_reranker("llm").name == "heuristic"          # no key -> heuristic, never a crash
    monkeypatch.setattr(settings, "knowledge_reranker_model", "not-a-real-model/xyz")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    assert rr.build_reranker("cross-encoder").name == "heuristic"  # unavailable model -> heuristic
    # A custom reranker set on the seam is what search() uses.
    class Reverse:
        name = "reverse"

        def rerank(self, query, hits, plan):
            for i, hit in enumerate(hits):      # last becomes first
                hit.score = float(i)
            hits.sort(key=lambda h: -h.score)
            return hits

    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    retrieval.set_reranker(None)
    baseline, _ = asyncio.run(retrieval.search("Aave health factor", limit=3))
    retrieval.set_reranker(Reverse())
    try:
        reversed_hits, _ = asyncio.run(retrieval.search("Aave health factor", limit=3))
    finally:
        retrieval.set_reranker(None)
    assert retrieval.get_reranker().name == "heuristic"
    assert [h.chunk.heading for h in reversed_hits] != [h.chunk.heading for h in baseline]
    # scope boost is shared by every reranker: same-protocol passage wins ties.
    plan = retrieval.RetrievalPlan(query="q", protocol_ids=["protocol:aave"])
    inside = RetrievalHit(chunk=Chunk(id="1", document_id="d", protocol_id="protocol:aave", heading="", content="x" * 200, position=0), score=0.1, sources=[])
    outside = RetrievalHit(chunk=Chunk(id="2", document_id="d", protocol_id="protocol:morpho", heading="", content="x" * 200, position=0), score=0.1, sources=[])
    assert rr.HeuristicReranker().rerank("q", [outside, inside], plan)[0] is inside


def test_graph_and_derive_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    client = TestClient(main.app)
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.post("/admin/knowledge/bootstrap?limit=10", headers=admin).json()["protocols"] == 3
    assert client.post("/admin/knowledge/derive").status_code in (401, 403)
    assert client.post("/admin/knowledge/derive", headers=admin).json() == {"competitors": 1, "integrations": 0}
    graph = client.get("/knowledge/graph/protocol:aave").json()
    relations = {e["relation"] for e in graph["edges"]}
    assert {"COMPETITOR_OF", "DEPLOYED_ON", "IN_CATEGORY"} <= relations
    competitors = client.get("/knowledge/graph/protocol:aave?relation=COMPETITOR_OF").json()["edges"]
    assert len(competitors) == 1 and competitors[0]["metadata"]["method"] == "category_chain"
    assert client.get("/knowledge/graph/protocol:nope").status_code == 404
    assert client.get("/knowledge/status").json()["reranker"] == "heuristic"
    # Dashboard overview: coverage per protocol with documents by source, runs, config.
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    asyncio.run(ingest.ingest_document(DocsConnector.document(aave, "https://docs.aave.com/liquidations", AAVE_DOCS_HTML)))
    overview = client.get("/admin/knowledge/overview", headers=admin).json()
    assert overview["totals"]["protocols"] == 3 and overview["config"]["reranker"] == "heuristic"
    row = next(c for c in overview["coverage"] if c["id"] == "protocol:aave")
    assert row["documents"] == 1 and row["chunks"] == 3 and row["by_source"] == {"protocol_docs": 1} and row["edges"] >= 4
    assert client.get("/admin/knowledge/overview").status_code in (401, 403)


def test_alias_case_variants_do_not_block_resolution():
    """DefiLlama gives both the parent slug and the ticker ("aave", "AAVE");
    case-folded they are one alias, so "Aave" must resolve, not look ambiguous."""
    from app.knowledge.models import Entity

    resolver = ent.EntityResolver([Entity(id="protocol:aave-v3", entity_type="protocol", canonical_name="Aave V3", symbol="AAVE", aliases=["aave", "AAVE", "Aave"])])
    assert resolver.resolve("Aave").entity.id == "protocol:aave-v3"
    assert [m.entity.id for m in resolver.mentions("what happens when an Aave health factor drops below 1")] == ["protocol:aave-v3"]
    assert resolver.resolve("$AAVE").entity.id == "protocol:aave-v3"


def test_clean_markdown_strips_nul_bytes():
    """Pendle's docs carried a NUL byte; Postgres rejects it in TEXT columns."""
    assert "\x00" not in normalize.clean_markdown("# Minting\x00\n\nYield\x00 tokenization")
    assert normalize.html_to_markdown("<h1>Min\x00ting</h1><p>text</p>") == "# Minting\n\ntext"


# --- DefiLlama facts, CoinGecko tokens, structured edges -----------------------

AAVE_LLAMA_DETAIL = {
    "name": "Aave V3", "symbol": "AAVE", "category": "Lending", "chains": ["Ethereum", "Base"], "url": "https://aave.com", "twitter": "aave",
    "description": "Aave is a non-custodial liquidity protocol.", "methodology": "Counts tokens locked as collateral.", "audits": "2", "audit_links": ["https://aave.com/security"],
    "oraclesBreakdown": [{"name": "Chainlink", "type": "Primary", "proof": [], "startDate": "2023-01-15"}], "forkedFrom": ["Morpho"], "parentProtocol": "parent#aave",
    "otherProtocols": ["Aave", "Aave V2", "Aave V3"], "listedAt": 1648776877,
    "hallmarks": [[1650412800, "Start AVAX Rewards"], [1776470400, "KelpDAO hack"]],
    "hacks": [{"date": 1773273600, "name": "Aave V3", "classification": "Oracle Manipulation", "technique": "Oracle Misconfiguration", "amount": 862000, "chain": ["Ethereum"], "returnedFunds": 862000, "language": "Solidity"}],
    "raises": [
        {"date": 1679961600, "name": "Aave", "round": "Series A", "amount": 50, "chains": ["Ethereum"], "leadInvestors": ["Blockchain Capital"], "otherInvestors": ["Coinbase Ventures", "Polychain Capital"], "valuation": "500"},
        {"date": 1750118400, "name": "Aave", "round": None, "amount": 70, "chains": ["Ethereum"], "leadInvestors": ["a16z crypto"], "otherInvestors": []},
    ],
}

GECKO_AAVE = {
    "id": "aave", "symbol": "aave", "name": "Aave", "genesis_date": "2020-10-02", "categories": ["Decentralized Finance (DeFi)", "Lending/Borrowing Protocols"],
    "description": {"en": "Aave is a decentralized money market protocol where users can <b>lend and borrow</b> cryptocurrency."},
    "links": {"homepage": ["https://aave.com/"], "whitepaper": "https://github.com/aave/aave-protocol/blob/master/docs/Aave_Protocol_Whitepaper_v1_0.pdf", "twitter_screen_name": "aave", "repos_url": {"github": ["https://github.com/aave/aave-protocol"]}},
    "platforms": {"ethereum": "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "solana": "AavE1kKKnesPw4MuRJmJ9jZs9QzEE8CPxQ3ViczUDfc1", "base": "0x63706e401c06ac8513145b7687a14804d17f814b", "hydration": "asset_registry%2F1000624"},
}


def test_defillama_detail_becomes_overview_incident_and_funding_documents():
    aave = Protocol(id="protocol:aave", slug="aave", name="Aave", symbol="AAVE", category="Lending", chains=["ethereum", "base"], defillama_slug="aave")
    docs = DefiLlamaConnector.build(aave, AAVE_LLAMA_DETAIL, "https://api.llama.fi/protocol/aave")
    assert [d.source_type for d in docs] == ["defillama", "incident", "funding"]
    overview, incident, funding = docs
    assert "**Forked from**: Morpho" in overview.content and "**Oracles**: Chainlink (Primary)" in overview.content and "**Family**: Aave" in overview.content
    assert "## Security incidents" in overview.content and "Oracle Manipulation via Oracle Misconfiguration, $862,000 lost, $862,000 returned" in overview.content
    assert "## Funding" in overview.content and "$120.0M raised in total" in overview.content and "led by a16z crypto" in overview.content
    assert "## Timeline" in overview.content and "2022-04-20: Start AVAX Rewards" in overview.content
    relations = {(f["relation"], f["target"].get("id") or f["target"]["name"]) for f in overview.metadata["facts"]}
    assert relations == {("USES_ORACLE", "org:chainlink"), ("FORK_OF", "Morpho"), ("PART_OF", "org:aave")}
    assert incident.title == "Aave V3 exploit — 2026-03-12" and incident.published_at.year == 2026 and incident.metadata["amount_usd"] == 862000
    assert incident.metadata["facts"][0]["relation"] == "HAD_INCIDENT" and incident.metadata["facts"][0]["target"]["id"] == "incident:aave:2026-03-12"
    assert funding.title == "Aave — funding rounds" and "**2023-03-28** · Series A · $50.0M at $500.0M valuation · lead: Blockchain Capital" in funding.content
    funded_by = {f["target"]["id"]: (f["confidence"], f["metadata"]["lead"]) for f in funding.metadata["facts"]}
    assert funded_by["org:a16z-crypto"] == (0.95, True) and funded_by["org:coinbase-ventures"] == (0.9, False)
    # No hacks or raises: only the overview.
    assert len(DefiLlamaConnector.build(aave, {"name": "Aave", "description": "x"}, "u")) == 1


def test_ingestion_applies_structured_facts_as_entities_and_dated_edges():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    docs = DefiLlamaConnector.build(aave, AAVE_LLAMA_DETAIL, "https://api.llama.fi/protocol/aave")
    written = sum(asyncio.run(ingest.ingest_document(d))[2] for d in docs)
    assert written >= 7   # oracle, fork, family, incident, 4 investors (+ mention edges)
    entities = {e.id: e for e in asyncio.run(store.list_entities())}
    assert entities["org:chainlink"].entity_type == "organization" and entities["org:aave"].canonical_name == "Aave family" and entities["org:aave"].aliases == []
    assert asyncio.run(kb_tool.resolver(force=True)).resolve("Aave").entity.id == "protocol:aave"   # the family never shadows the protocol
    assert entities["incident:aave:2026-03-12"].entity_type == "incident" and entities["incident:aave:2026-03-12"].metadata["technique"] == "Oracle Misconfiguration"
    edges = {(r.relation, r.target_entity_id): r for r in asyncio.run(store.neighbors("protocol:aave", direction="out"))}
    assert edges[("FORK_OF", "protocol:morpho")].confidence == 0.95            # resolved by name against the registry
    assert edges[("USES_ORACLE", "org:chainlink")].valid_from.date().isoformat() == "2023-01-15"
    assert edges[("HAD_INCIDENT", "incident:aave:2026-03-12")].valid_from.date().isoformat() == "2026-03-12"
    assert edges[("FUNDED_BY", "org:blockchain-capital")].metadata == {"source": "defillama", "lead": True, "rounds": ["2023-03-28"], "method": "structured"}
    assert all(r.source_document_id for r in edges.values() if r.metadata.get("method") == "structured")
    # Same facts again: nothing duplicated.
    assert sum(asyncio.run(ingest.ingest_document(d))[2] for d in DefiLlamaConnector.build(aave, AAVE_LLAMA_DETAIL, "https://api.llama.fi/protocol/aave")) == 0
    # An unresolvable fork target is dropped rather than inventing an entity.
    other = DefiLlamaConnector.document(aave, {**AAVE_LLAMA_DETAIL, "forkedFrom": ["Compound"], "hacks": [], "raises": []}, "https://api.llama.fi/protocol/aave-x")
    asyncio.run(ingest.ingest_document(other))
    assert not [r for r in asyncio.run(store.neighbors("protocol:aave", relation="FORK_OF")) if r.target_entity_id.endswith("compound")]
    assert "org:compound" not in {e.id for e in asyncio.run(store.list_entities())}


def test_coingecko_token_document_registers_addresses_on_every_chain():
    from app.knowledge.connectors.coingecko import CoinGeckoConnector

    aave = Protocol(id="protocol:aave", slug="aave", name="Aave", symbol="AAVE", coingecko_id="aave")
    assert CoinGeckoConnector().applies(aave) and not CoinGeckoConnector().applies(Protocol(id="p", slug="p", name="P"))
    doc = CoinGeckoConnector.document(aave, GECKO_AAVE)
    assert doc.source_type == "token" and doc.url == "https://www.coingecko.com/en/coins/aave" and doc.title == "Aave token (AAVE)"
    assert "lend and borrow" in doc.content and "<b>" not in doc.content and "- solana: `AavE1kKKnesPw4MuRJmJ9jZs9QzEE8CPxQ3ViczUDfc1`" in doc.content
    assert "hydration" not in doc.metadata["platforms"]          # not an address
    targets = {f["target"]["id"]: f for f in doc.metadata["facts"]}
    assert set(targets) == {"token:ethereum:0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", "token:solana:AavE1kKKnesPw4MuRJmJ9jZs9QzEE8CPxQ3ViczUDfc1", "token:base:0x63706e401c06ac8513145b7687a14804d17f814b"}
    assert all(f["relation"] == "TOKEN_OF" and f["direction"] == "in" for f in targets.values())
    # After ingestion the Base address resolves at 1.0 to the protocol's token.
    asyncio.run(registry.bootstrap(limit=10))
    asyncio.run(ingest.ingest_document(doc))
    resolver = asyncio.run(kb_tool.resolver(force=True))
    hit = resolver.resolve("0x63706e401c06ac8513145b7687a14804d17f814b", chain="base")
    assert hit.confidence == 1.0 and hit.entity.chain == "base" and hit.entity.symbol == "AAVE"
    store = asyncio.run(kb_store.get_store())
    assert any(r.relation == "TOKEN_OF" and r.source_entity_id.startswith("token:base:") for r in asyncio.run(store.neighbors("protocol:aave")))


def test_coingecko_calls_are_throttled_process_wide(monkeypatch):
    from app.knowledge.connectors import coingecko

    calls = []
    monkeypatch.setattr(coingecko, "MIN_INTERVAL", 0.05)
    monkeypatch.setattr(coingecko, "_last_call", 0.0)
    monkeypatch.setattr(coingecko.time, "sleep", lambda s: calls.append(round(s, 3)))

    class FakeClient:
        def get(self, url):
            return url

    coingecko._throttled_get(FakeClient(), "a")
    coingecko._throttled_get(FakeClient(), "b")
    assert calls and 0 < calls[-1] <= 0.05      # the second call waited for the interval


def test_knowledge_trigger_covers_incidents_funding_and_governance_asks():
    for ask in ("has Aave ever been hacked", "who are the investors in EigenLayer", "which oracle does Spark use", "is Morpho a fork of Aave", "what did the Lido community vote on"):
        assert kb_tool.TRIGGER.search(ask), ask
    assert not kb_tool.TRIGGER.search("price of AAVE right now") or kb_tool._LIVE.search("price of AAVE right now")


def test_graph_expansion_follows_the_protocol_family():
    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    morpho = asyncio.run(store.get_protocol("protocol:morpho"))
    for p in (aave, morpho):   # both declare the same family: one hop through org:aave-family links them
        asyncio.run(ingest.ingest_document(DefiLlamaConnector.document(p, {"name": p.name, "description": "x", "parentProtocol": "parent#aave-family", "otherProtocols": []}, f"u/{p.slug}")))
    resolver = ent.EntityResolver(asyncio.run(store.list_entities()))
    plan = asyncio.run(retrieval.plan_query("how does Aave handle liquidations", resolver))
    assert asyncio.run(retrieval.graph_expand(plan, store))[0] == "protocol:morpho"


def test_name_aliases_and_shared_alias_disambiguation_by_tvl():
    from app.knowledge.models import Entity

    assert registry.name_aliases("Kamino Lend") == {"Kamino"} and registry.name_aliases("Uniswap V3") == {"Uniswap"}
    assert registry.name_aliases("Morpho Blue") == {"Morpho"} and registry.name_aliases("Aave") == set()
    assert "The" not in registry.name_aliases("The Graph") and "Staked" not in registry.name_aliases("Staked ETH")
    # "Aave" is an alias on three registry protocols: context wins, then TVL.
    resolver = ent.EntityResolver([
        Entity(id="protocol:aave-v3", entity_type="protocol", canonical_name="Aave V3", aliases=["Aave", "AAVE"], metadata={"tvl_usd": 20e9}),
        Entity(id="protocol:aave-v4", entity_type="protocol", canonical_name="Aave V4", aliases=["Aave", "AAVE"], metadata={"tvl_usd": 1e9}),
        Entity(id="protocol:aave-horizon-rwa", entity_type="protocol", canonical_name="Aave Horizon RWA", aliases=["Aave"], metadata={"tvl_usd": 3e8}),
    ])
    assert resolver.resolve("Aave").entity.id == "protocol:aave-v3"
    assert resolver.resolve("Aave", context="how does Aave V4 differ").entity.id == "protocol:aave-v4"
    assert [m.entity.id for m in resolver.mentions("has Aave ever been hacked?")] == ["protocol:aave-v3"]
    assert {m.entity.id for m in resolver.mentions("compare Aave Horizon RWA with Aave V3")} == {"protocol:aave-horizon-rwa", "protocol:aave-v3"}


def test_failed_source_retries_after_an_hour_not_a_week():
    from datetime import datetime, timezone

    from app.knowledge.connectors.coingecko import CoinGeckoConnector

    asyncio.run(registry.bootstrap(limit=10))
    store = asyncio.run(kb_store.get_store())
    aave = asyncio.run(store.get_protocol("protocol:aave"))
    gecko = CoinGeckoConnector()                      # refresh_every = 7 days
    assert asyncio.run(ingest.due(gecko, aave, store))
    asyncio.run(store.record_source_state("coingecko", aave.id, False, 0, "coingecko rate limited (429)"))
    assert not asyncio.run(ingest.due(gecko, aave, store))             # just failed: back off
    state = store.source_state[("coingecko", aave.id)]
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
    assert asyncio.run(ingest.due(gecko, aave, store))                 # failed two hours ago: try again
    asyncio.run(store.record_source_state("coingecko", aave.id, True, 1, None))
    store.source_state[("coingecko", aave.id)]["last_run_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
    assert not asyncio.run(ingest.due(gecko, aave, store))             # succeeded two hours ago: wait the week
