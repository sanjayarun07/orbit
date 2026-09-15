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
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_entity_id, relation, target_entity_id, valid_from)
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
"""

DDL_VECTOR = "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding vector({dim});"
DDL_VECTOR_INDEX = "CREATE INDEX IF NOT EXISTS kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops);"
DDL_FLOAT_ARRAY = "ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding real[];"


async def ensure_schema(pool, embedding_dim: int) -> bool:
    """Create the tables. Returns True when pgvector is available (native
    vector search), False when embeddings live in a float array."""
    async with pool.acquire() as conn:
        await conn.execute(DDL_BASE)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
