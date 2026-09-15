"""Dopamint Web3 knowledge service.

Not a vector database: a protocol registry, normalized documents versioned by
content hash, structure-aware chunks with embeddings + full-text vectors,
canonical entities with confidence-scored resolution, temporal relationships
(the beginning of the knowledge graph), and hybrid retrieval (semantic +
lexical + graph, fused with reciprocal-rank fusion) exposed to the existing
provider router as the `knowledge_base_search` tool.

Layout
- models.py       shared records (NormalizedDocument, Chunk, Entity, Relationship, ...)
- schema.py       Postgres DDL (pgvector when available, float[] otherwise)
- normalize.py    HTML -> markdown, cleaning, content hash, heading-based chunking
- entities.py     canonical ids + the resolver with its confidence ladder
- embeddings.py   Embedder interface: OpenAI when configured, hashing fallback
- store.py        KnowledgeStore: Postgres + in-memory implementations
- retrieval.py    hybrid search, RRF, reranker interface, context builder
- registry.py     protocol registry bootstrapped from DefiLlama
- connectors/     source adapters producing NormalizedDocument
- ingest.py       ingestion jobs, refresh policies, the worker loop
- tool.py         router tool + rendering with citations

Live data (DefiLlama TVL, The Graph, prices, social) stays in the existing
provider tools; the KB answers "what is / how does" questions with citations.
"""
