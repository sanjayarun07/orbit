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
| `connectors/` | `base.Connector` (`applies`, `discover`, `fetch` or `documents`) — `defillama` (overview + one document per recorded hack + funding rounds, with structured facts), `docs` (llms.txt → crawl, budgeted, same-host), `github` (top READMEs), `snapshot` (proposals via GraphQL), `discourse` (forum threads), `coingecko` (token page + contract addresses on every chain) |
| `overrides.py` | per-slug `docs_url` / `forum_url` / `governance_url` where DefiLlama's website is not enough |
| `derive.py` | `COMPETITOR_OF` (category + shared chain) and corroborated `INTEGRATES_WITH` after each pass |
| `reranker.py` | heuristic / cross-encoder / llm behind `retrieval.set_reranker` |
| `ingest.py` | `ingest_document`, `run_source`, `run_protocol`, `tick` (due pairs by refresh policy), `worker`; prose-derived edges are low-confidence and carry `source_document_id` |
| `tool.py` | `knowledge_base_search` ProviderTool (capability `knowledge`); matcher = knowledge-shaped ask + a registry entity − live-number words |

## Refresh policy

| Source | Refresh |
|---|---|
| DefiLlama detail (overview, hacks, raises, oracles, forks, family) | 24h |
| Protocol docs | 12h |
| GitHub READMEs | 6h |
| Snapshot governance | 15 min |
| Discourse forums | 6h |
| CoinGecko token page + addresses | 7d |
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

Bulk ingestion belongs in its own process. Crawling, chunking and mention
extraction are CPU-bound, and running fifty docs sites inside the API process
pushed chat latency to ~30 s. Use the standalone ingester, which shares the
database (the API refreshes its resolver snapshot every two minutes):

```bash
.venv/bin/python scripts/kb_ingest.py --parallel 4        # every due (protocol, source) pair
.venv/bin/python scripts/kb_ingest.py --slug aave-v3 lido # specific protocols, timers ignored
.venv/bin/python scripts/kb_ingest.py --loop              # worker mode
```

Docs roots are guessed from the DefiLlama website by stripping app-style
labels (`app.`, `portal.`, `data.`) and trying `docs.<domain>`, `<domain>/docs`
and `www.<domain>/docs` in order; the first root that serves at least two
pages wins. Where docs live elsewhere (GitBook subdomains), or a protocol has
a Discourse forum or Snapshot space, `app/knowledge/overrides.py` says so by
DefiLlama slug; bootstrap applies it and the crawler still verifies.

### Running it as a background job

```bash
mkdir -p ~/Library/Logs/orbit
nohup .venv/bin/python scripts/kb_ingest.py --loop --parallel 3 >> ~/Library/Logs/orbit/kb_ingest.log 2>&1 &
```

Each pass ingests every due (protocol, source) pair (docs every 12 h, GitHub
6 h, forums 6 h, Snapshot 15 min, DefiLlama daily, CoinGecko weekly), then
recomputes derived edges, then sleeps `KNOWLEDGE_INGEST_INTERVAL_SECONDS`.
A source whose last run failed (rate limit, timeout) is retried after an
hour rather than its full interval. Unchanged pages hash-match and cost
nothing. Stop it with `pkill -f "kb_ingest.py --loop"`; restart it after
code changes, it does not reload.
The admin dashboard (`/ui/admin.html`, "Knowledge base" panel) shows totals,
per-protocol coverage by source, failing sources, recent runs, a search box
and the graph around any entity.

### Governance: Snapshot and Discourse

`SnapshotConnector` reads a space's proposals (title, state, tally, body);
`DiscourseConnector` reads a forum's latest topics through the JSON API every
Discourse site exposes (`/latest.json`, `/t/<id>.json`): the opening post in
full plus the first replies, one document per thread. Both are
`source_type = governance`, so "what did the community say about raising the
LTV" is answered with a link to the thread.

### Structured facts from DefiLlama and CoinGecko

`/protocol/<slug>` carries more than a description, and each part becomes
both text and graph:

| DefiLlama field | Document | Edge |
|---|---|---|
| `hacks[]` | one `incident` document per event (date, loss, technique, returned funds) | `HAD_INCIDENT` → `incident:<slug>:<date>` (0.95, `valid_from` = date) |
| `raises[]` | one `funding` document (rounds, valuations, investors) | `FUNDED_BY` → `org:<investor>` (0.95 lead, 0.90 other) |
| `oraclesBreakdown[]` | in the overview | `USES_ORACLE` → `org:<oracle>` (0.95) |
| `forkedFrom[]` | in the overview | `FORK_OF` → protocol, only when the name resolves at ≥ 0.9 |
| `parentProtocol` | in the overview | `PART_OF` → `org:<family>` (0.98); graph expansion hops through it (Aave V2 ↔ V3) |
| `hallmarks[]` | a dated timeline in the overview | — |

CoinGecko's coin page adds the token description, categories, canonical
links and the contract address on every chain the token is deployed to,
each written as a `token:<chain>:<address>` entity with a `TOKEN_OF` edge:
a Base or Solana address pasted into chat then resolves at confidence 1.0.
Calls are throttled to one every 2.5 s process-wide (free tier).

Connectors state facts in `NormalizedDocument.metadata["facts"]`; ingestion
resolves or creates the target entity and writes the edge with the document
as provenance (`method = structured`). A fact whose named target does not
resolve is dropped, never invented.

### Derived edges

After a pass (`scripts/kb_ingest.py`, or `POST /admin/knowledge/derive`):

| Edge | Rule | Confidence |
|---|---|---|
| `COMPETITOR_OF` | same DefiLlama category and at least one shared chain; each protocol keeps its 10 largest peers | 0.60 |
| `INTEGRATES_WITH` (corroborated) | mention edges for the pair in two or more documents from different sources, or from both protocols' own docs | 0.75 |

Mention edges stay at 0.5 with their `source_document_id`; one mention never
promotes on its own. The edge key is (source, relation, target, validity,
evidence document), so two documents asserting the same fact are two evidence
rows, and a re-ingested document closes its previous version's edges
(`valid_to`) before asserting again. `GET /knowledge/graph/<entity id>`
folds evidence rows into one line per edge with a document count.

### Reranker

`KNOWLEDGE_RERANKER` picks the stage after RRF: `heuristic` (default, entity
and term boosts, no model), `cross-encoder` (sentence-transformers
`ms-marco-MiniLM-L-6-v2`, `pip install '.[rerank]'`, ~10 ms per pair on
CPU, downloads on first use) or `llm` (the chat model grades passages 0-10 in
one call). Every reranker keeps the retrieval-scope boost so a passage from
the protocol the question names still beats a lookalike from a competitor.

Set `KNOWLEDGE_INGEST_ENABLED=true` to run small in-process ticks
(`KNOWLEDGE_INGEST_INTERVAL_SECONDS`, `KNOWLEDGE_INGEST_BATCH`). With
`DATABASE_URL` set the schema is created on first use; pgvector is used when
`CREATE EXTENSION vector` succeeds (native `<=>` cosine + an HNSW index),
otherwise embeddings are stored as `real[]` and cosine runs in Python over
lexical candidates. A database that started as `real[]` migrates itself to
`vector(N)` the first time the extension is available; `GET /knowledge/status`
reports `vector_native`. On a Homebrew PostgreSQL 14 the bottled `pgvector`
only ships 17/18 builds, so build from source:

```bash
git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git
cd pgvector && make PG_CONFIG=$(which pg_config) && make install PG_CONFIG=$(which pg_config)
```

## Routing

`knowledge` is a capability like any other: the tool's own matcher makes it
reachable through `ProviderRouter.matched_capabilities`, so "What is Pendle?"
→ KB, "Pendle TVL?" → DefiLlama, "Why is Pendle trending?" → the mixed
research path (KB + market + news + social) with no new classifier.

## Next steps

1. Per-protocol `docs_url` overrides in the registry for the sites whose
   docs live on another domain (GitBook subdomains, `docs.kamino.finance`
   for `kamino.com`); the guess list should not grow to cover them.
2. Discourse forum connector (same `NormalizedDocument` shape) and
   `COMPETITOR_OF` edges derived from shared category + chain.
3. Model-based relation extraction behind `extract_relationships`, only
   raising confidence above 0.75 with two independent sources.
4. Cross-encoder reranker behind `set_reranker`.
5. Point-in-time queries (`neighbors(..., at=)` already exists) surfaced in
   the tool: "was USDC supported when proposal X passed".
