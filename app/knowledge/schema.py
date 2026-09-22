"""Postgres schema for the knowledge service.

Documents are versioned (valid_from / valid_to) and de-duplicated by content
hash; chunks carry both an embedding and a tsvector so one table serves
semantic and lexical search; relationships are temporal from day one. The
embedding column is `vector(N)` when the pgvector extension can be created
and `real[]` otherwise (vector search then runs in Python over lexical
candidates -- slower, but the service still works).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DDL_BASE = """
CREATE TABLE IF NOT EXISTS kb_protocols (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    symbol TEXT,
    category TEXT,
    description TEXT,
    website TEXT,
    docs_url TEXT,
    github_org TEXT,
    governance_url TEXT,
    defillama_slug TEXT,
    coingecko_id TEXT,
    twitter_handle TEXT,
    chains TEXT[] NOT NULL DEFAULT '{}',
    contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    tvl_usd DOUBLE PRECISION,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS kb_protocols_category ON kb_protocols (category);

CREATE TABLE IF NOT EXISTS kb_documents (
    id UUID PRIMARY KEY,
    protocol_id TEXT REFERENCES kb_protocols(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    published_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS kb_documents_url_live ON kb_documents (source_url) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS kb_documents_protocol ON kb_documents (protocol_id) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS kb_chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    protocol_id TEXT,
    heading TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading, '') || ' ' || content)) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS kb_chunks_fts ON kb_chunks USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS kb_chunks_document ON kb_chunks (document_id);
CREATE INDEX IF NOT EXISTS kb_chunks_protocol ON kb_chunks (protocol_id);

CREATE TABLE IF NOT EXISTS kb_entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    symbol TEXT,
    chain TEXT,
    address TEXT,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS kb_entities_type ON kb_entities (entity_type);
CREATE INDEX IF NOT EXISTS kb_entities_symbol ON kb_entities (upper(symbol));
CREATE INDEX IF NOT EXISTS kb_entities_address ON kb_entities (chain, lower(address)) WHERE address IS NOT NULL;

CREATE TABLE IF NOT EXISTS kb_relationships (
    id UUID PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES kb_entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    target_entity_id TEXT NOT NULL REFERENCES kb_entities(id) ON DELETE CASCADE,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.8,
    source_document_id UUID REFERENCES kb_documents(id) ON DELETE SET NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS kb_relationships_source ON kb_relationships (source_entity_id, relation) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS kb_relationships_target ON kb_relationships (target_entity_id, relation) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS kb_source_state (
    source TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    last_run_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    documents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, protocol_id)
);

CREATE TABLE IF NOT EXISTS kb_ingestion_runs (
    id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    protocol_id TEXT,
    source TEXT,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_changed INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,
    errors TEXT[] NOT NULL DEFAULT '{}'
);

-- additive migrations (safe to re-run)
ALTER TABLE kb_protocols ADD COLUMN IF NOT EXISTS forum_url TEXT;

-- Edge key: one row per (edge, validity window, evidence document). A mention
-- in each of two documents is two evidence rows; a derived or structured fact
-- has no document and is one row. NULLs are coalesced so the key is total --
-- the original UNIQUE constraint let NULL valid_from rows duplicate on every
-- bootstrap, so duplicates are collapsed (highest confidence wins) first.
ALTER TABLE kb_relationships DROP CONSTRAINT IF EXISTS kb_relationships_source_entity_id_relation_target_entity_id_key;
DELETE FROM kb_relationships r USING kb_relationships k
 WHERE r.source_entity_id = k.source_entity_id AND r.relation = k.relation AND r.target_entity_id = k.target_entity_id
   AND coalesce(r.valid_from, '-infinity'::timestamptz) = coalesce(k.valid_from, '-infinity'::timestamptz)
   AND coalesce(r.source_document_id, '00000000-0000-0000-0000-000000000000'::uuid) = coalesce(k.source_document_id, '00000000-0000-0000-0000-000000000000'::uuid)
   AND (r.confidence < k.confidence OR (r.confidence = k.confidence AND r.id > k.id));
CREATE UNIQUE INDEX IF NOT EXISTS kb_relationships_edge_key ON kb_relationships
    (source_entity_id, relation, target_entity_id, coalesce(valid_from, '-infinity'::timestamptz), coalesce(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid));
"""

EDGE_CONFLICT_KEY = "(source_entity_id, relation, target_entity_id, coalesce(valid_from, '-infinity'::timestamptz), coalesce(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid))"

DDL_VECTOR = "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding vector({dim});"
DDL_VECTOR_INDEX = "CREATE INDEX IF NOT EXISTS kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops);"
DDL_FLOAT_ARRAY = "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding real[];"
# A database that started life without pgvector (embeddings as real[]) upgrades in
# place once the extension is installed: the cast keeps every stored embedding.
DDL_MIGRATE_ARRAY = "ALTER TABLE kb_chunks ALTER COLUMN embedding TYPE vector({dim}) USING embedding::vector({dim});"
SQL_EMBEDDING_TYPE = "SELECT udt_name FROM information_schema.columns WHERE table_name = 'kb_chunks' AND column_name = 'embedding'"


async def ensure_schema(pool, embedding_dim: int) -> bool:
    """Create the tables. Returns True when pgvector is available (native
    vector search), False when embeddings live in a float array."""
    from app.db import schema_lock

    async with pool.acquire() as conn:
      async with schema_lock(conn):          # one worker at a time (app/db.py)
        await conn.execute(DDL_BASE)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            if await conn.fetchval(SQL_EMBEDDING_TYPE) == "_float4":
                await conn.execute(DDL_MIGRATE_ARRAY.format(dim=int(embedding_dim)))
                logger.info("knowledge: migrated kb_chunks.embedding from real[] to vector(%d)", int(embedding_dim))
            await conn.execute(DDL_VECTOR.format(dim=int(embedding_dim)))
            try:
                await conn.execute(DDL_VECTOR_INDEX)
            except Exception:
                logger.info("knowledge: hnsw index unavailable (older pgvector); relying on sequential scan")
            return True
        except Exception:
            logger.warning("knowledge: pgvector not available; storing embeddings as real[] and searching in Python")
            await conn.execute(DDL_FLOAT_ARRAY)
            return False
