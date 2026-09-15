# Dopamint Web3 knowledge service

A knowledge service, not a vector database: a protocol registry, normalized
and versioned documents, structure-aware chunks with embeddings and
full-text vectors, canonical entities, temporal relationships, and hybrid
retrieval exposed to the existing router. Live data (TVL, prices, pools,
social) stays with the live tools; the KB answers *what is / how does / who
competes with* with citations.

```
Sources ── connectors ── normalize (html→md, hash, chunks) ── entity resolution
                                                                     │
                     documents ──── chunks (embedding + tsvector) ── entities ── relationships
                                              │
                          semantic ─┬─ lexical ─┬─ graph      (Postgres + pgvector + FTS)
                                    └── RRF ──┴── reranker ── context + citations ── router → LLM
```

## Layout (`app/knowledge/`)

| Module | Role |
|---|---|
| `models.py` | `Protocol`, `NormalizedDocument`, `Chunk`, `Entity`, `Relationship`, `Resolution`, `RetrievalHit`, `IngestionResult` |
| `schema.py` | `kb_protocols`, `kb_documents` (versioned: `valid_from`/`valid_to`, `content_hash`), `kb_chunks` (`embedding vector(N)` or `real[]`, generated `search_vector`), `kb_entities`, `kb_relationships` (temporal, unique per edge+`valid_from`), `kb_source_state`, `kb_ingestion_runs` |
| `normalize.py` | stdlib HTML→markdown (drops nav/footer/scripts, keeps headings/lists/code/links), boilerplate line filter, `content_hash` (whitespace/case-normalized SHA-256), `chunk_markdown` (sections by heading path, long sections split on paragraphs, short tails merged) |
| `entities.py` | canonical ids (`chain:ethereum`, `protocol:aave`, `token:solana:<mint>`, `category:lending`), `EntityResolver` with the confidence ladder (contract 1.0 → CoinGecko/DefiLlama id .98 → canonical name .90 → alias .75 → ticker .40), context disambiguation, `mentions()` for deterministic extraction |
| `embeddings.py` | `Embedder` interface: OpenAI `text-embedding-3-small` at `KNOWLEDGE_EMBEDDING_DIM` (1024 by default, Qwen-compatible), hashing fallback offline |
| `store.py` | `MemoryStore` (reference) and `PostgresStore`; `write_document` is hash-idempotent and versions on change, re-chunking only that document |
| `retrieval.py` | `plan_query` (entity recognition) → semantic + lexical (+ graph-expanded protocols) → RRF → `Reranker` (heuristic default) → `build_context` with numbered citations |
| `registry.py` | bootstrap from DefiLlama `/protocols` (top N by TVL, CEX/Chain skipped): protocol rows + entities + first edges `DEPLOYED_ON`, `IN_CATEGORY`, `TOKEN_OF` |
| `connectors/` | `base.Connector` (`applies`, `discover`, `fetch`) — `defillama` (overview page), `docs` (llms.txt → crawl, budgeted, same-host), `github` (top READMEs), `snapshot` (proposals via GraphQL) |
| `ingest.py` | `ingest_document`, `run_source`, `run_protocol`, `tick` (due pairs by refresh policy), `worker`; prose-derived edges are low-confidence and carry `source_document_id` |
| `tool.py` | `knowledge_base_search` ProviderTool (capability `knowledge`); matcher = knowledge-shaped ask + a registry entity − live-number words |

## Refresh policy

| Source | Refresh |
|---|---|
| DefiLlama metadata | 1h |
| Protocol docs | 12h |
| GitHub READMEs | 6h |
| Snapshot governance | 15 min |
| TVL / prices / pools / social | live tools, never the corpus |

Unchanged documents (same `content_hash`) are skipped before chunking, so a
re-run embeds nothing unless a page changed.

## Operating it

```
POST /admin/knowledge/bootstrap?limit=50        # registry + entities + edges from DefiLlama
POST /admin/knowledge/ingest/aave                # every applicable connector for one protocol
POST /admin/knowledge/tick?limit=5               # one worker pass over due pairs
GET  /knowledge/status                           # counts, backend, embedder, recent runs
GET  /knowledge/search?q=Aave%20E-mode           # hits, entities, graph expansion, citations
```

Set `KNOWLEDGE_INGEST_ENABLED=true` to run the worker continuously
(`KNOWLEDGE_INGEST_INTERVAL_SECONDS`, `KNOWLEDGE_INGEST_BATCH`). With
`DATABASE_URL` set the schema is created on first use; pgvector is used when
`CREATE EXTENSION vector` succeeds, otherwise embeddings are stored as
`real[]` and cosine runs in Python over lexical candidates.

## Routing

`knowledge` is a capability like any other: the tool's own matcher makes it
reachable through `ProviderRouter.matched_capabilities`, so "What is Pendle?"
→ KB, "Pendle TVL?" → DefiLlama, "Why is Pendle trending?" → the mixed
research path (KB + market + news + social) with no new classifier.

## Next steps

1. Run bootstrap + ingest for the first 50 protocols on Postgres; watch
   `kb_ingestion_runs` and the docs crawler's page yield per host.
2. Discourse forum connector (same `NormalizedDocument` shape) and
   `COMPETITOR_OF` edges derived from shared category + chain.
3. Model-based relation extraction behind `extract_relationships`, only
   raising confidence above 0.75 with two independent sources.
4. Cross-encoder reranker behind `set_reranker`.
5. Point-in-time queries (`neighbors(..., at=)` already exists) surfaced in
   the tool: "was USDC supported when proposal X passed".
