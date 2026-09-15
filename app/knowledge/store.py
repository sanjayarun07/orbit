"""KnowledgeStore: one interface, two implementations.

- MemoryStore: dictionaries + Python cosine/lexical scoring. Runs everywhere
  (tests, laptops without Postgres) and is the reference behaviour.
- PostgresStore: kb_* tables; tsvector for lexical search, pgvector for
  semantic search when the extension exists (otherwise Python cosine over the
  lexical candidates).

Documents are versioned: writing a document whose URL already has a live
version with the same content hash is a no-op; a changed hash closes the old
version (valid_to) and inserts the new one, re-chunking only that document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from app.knowledge.embeddings import cosine
from app.knowledge.models import Chunk, Entity, NormalizedDocument, Protocol, Relationship

logger = logging.getLogger(__name__)
_WORD = re.compile(r"[a-z0-9$#]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


# ----------------------------------------------------------------------------
# In-memory store
# ----------------------------------------------------------------------------

class MemoryStore:
    vector_native = False

    def __init__(self):
        self.protocols: dict[str, Protocol] = {}
        self.documents: dict[str, NormalizedDocument] = {}       # live + closed, by id
        self.doc_valid_to: dict[str, datetime | None] = {}
        self.chunks: dict[str, Chunk] = {}
        self.entities: dict[str, Entity] = {}
        self.relationships: list[Relationship] = []
        self.source_state: dict[tuple[str, str], dict] = {}

    # protocols
    async def upsert_protocol(self, protocol: Protocol) -> None:
        self.protocols[protocol.id] = protocol

    async def get_protocol(self, protocol_id: str) -> Protocol | None:
        return self.protocols.get(protocol_id)

    async def list_protocols(self, limit: int = 500) -> list[Protocol]:
        return sorted(self.protocols.values(), key=lambda p: -(p.tvl_usd or 0))[:limit]

    # documents
    async def live_document(self, url: str) -> NormalizedDocument | None:
        for doc in self.documents.values():
            if doc.url == url and self.doc_valid_to.get(doc.id) is None:
                return doc
        return None

    async def write_document(self, doc: NormalizedDocument, chunks: list[Chunk]) -> tuple[str, bool]:
        """(document_id, changed). Unchanged content is a no-op."""
        current = await self.live_document(doc.url)
        if current and current.content_hash == doc.content_hash:
            return current.id, False
        if current:
            self.doc_valid_to[current.id] = datetime.now(timezone.utc)
            for cid in [c.id for c in self.chunks.values() if c.document_id == current.id]:
                self.chunks.pop(cid, None)
            doc.version = current.version + 1
        doc.id = doc.id or str(uuid4())
        self.documents[doc.id] = doc
        self.doc_valid_to[doc.id] = None
        for chunk in chunks:
            chunk.document_id = doc.id
            self.chunks[chunk.id] = chunk
        return doc.id, True

    async def get_document(self, document_id: str) -> NormalizedDocument | None:
        return self.documents.get(document_id)

    async def live_documents(self, protocol_id: str | None = None) -> list[NormalizedDocument]:
        return [d for d in self.documents.values() if self.doc_valid_to.get(d.id) is None and (protocol_id is None or d.protocol_id == protocol_id)]

    # entities / relationships
    async def upsert_entity(self, entity: Entity) -> None:
        existing = self.entities.get(entity.id)
        if existing:
            existing.aliases = sorted(set(existing.aliases) | set(entity.aliases))
            existing.metadata = {**existing.metadata, **entity.metadata}
            existing.symbol = entity.symbol or existing.symbol
            existing.chain = entity.chain or existing.chain
            existing.address = entity.address or existing.address
        else:
            self.entities[entity.id] = entity

    async def list_entities(self) -> list[Entity]:
        return list(self.entities.values())

    async def upsert_relationship(self, rel: Relationship) -> bool:
        for existing in self.relationships:
            if (existing.source_entity_id, existing.relation, existing.target_entity_id, existing.valid_from) == (rel.source_entity_id, rel.relation, rel.target_entity_id, rel.valid_from) and existing.valid_to is None:
                existing.confidence = max(existing.confidence, rel.confidence)
                existing.observed_at = rel.observed_at
                return False
        self.relationships.append(rel)
        return True

    async def neighbors(self, entity_id: str, relation: str | None = None, direction: str = "both", at: datetime | None = None) -> list[Relationship]:
        out = []
        for rel in self.relationships:
            if relation and rel.relation != relation:
                continue
            if at and ((rel.valid_from and rel.valid_from > at) or (rel.valid_to and rel.valid_to <= at)):
                continue
            if not at and rel.valid_to is not None:
                continue
            if direction in ("out", "both") and rel.source_entity_id == entity_id:
                out.append(rel)
            elif direction in ("in", "both") and rel.target_entity_id == entity_id:
                out.append(rel)
        return out

    # search
    async def lexical_search(self, query: str, limit: int = 20, protocol_ids: list[str] | None = None) -> list[tuple[Chunk, float]]:
        terms = set(_tokens(query))
        if not terms:
            return []
        scored = []
        for chunk in self.chunks.values():
            if protocol_ids and chunk.protocol_id not in protocol_ids:
                continue
            words = _tokens(chunk.text)
            if not words:
                continue
            counts = {}
            for w in words:
                counts[w] = counts.get(w, 0) + 1
            score = sum(1 + min(counts.get(t, 0), 5) * 0.2 for t in terms if t in counts)
            if score:
                scored.append((chunk, score / (1 + len(words) / 400)))
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

    async def semantic_search(self, embedding: list[float], limit: int = 20, protocol_ids: list[str] | None = None) -> list[tuple[Chunk, float]]:
        scored = []
        for chunk in self.chunks.values():
            if protocol_ids and chunk.protocol_id not in protocol_ids:
                continue
            if chunk.embedding:
                scored.append((chunk, cosine(embedding, chunk.embedding)))
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

    async def chunks_for_protocols(self, protocol_ids: list[str], limit: int = 20) -> list[Chunk]:
        return [c for c in self.chunks.values() if c.protocol_id in protocol_ids][:limit]

    # source state
    async def record_source_state(self, source: str, protocol_id: str, ok: bool, documents: int, error: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        state = self.source_state.setdefault((source, protocol_id), {"last_success_at": None})
        state.update({"last_run_at": now, "last_error": error, "documents": documents})
        if ok:
            state["last_success_at"] = now

    async def get_source_state(self, source: str, protocol_id: str) -> dict | None:
        return self.source_state.get((source, protocol_id))

    async def stats(self) -> dict:
        live = [d for d in self.documents.values() if self.doc_valid_to.get(d.id) is None]
        return {"backend": "memory", "protocols": len(self.protocols), "documents": len(live), "chunks": len(self.chunks),
                "entities": len(self.entities), "relationships": len(self.relationships), "vector_native": False}


# ----------------------------------------------------------------------------
# Postgres store
# ----------------------------------------------------------------------------

class PostgresStore:
    def __init__(self, pool, vector_native: bool):
        self.pool = pool
        self.vector_native = vector_native

    @staticmethod
    def _protocol_row(row) -> Protocol:
        return Protocol(
            id=row["id"], slug=row["slug"], name=row["name"], symbol=row["symbol"], category=row["category"], description=row["description"],
            website=row["website"], docs_url=row["docs_url"], github_org=row["github_org"], governance_url=row["governance_url"],
            defillama_slug=row["defillama_slug"], coingecko_id=row["coingecko_id"], twitter_handle=row["twitter_handle"],
            chains=list(row["chains"] or []), contracts=json.loads(row["contracts"]) if isinstance(row["contracts"], str) else list(row["contracts"] or []),
            aliases=list(row["aliases"] or []), tvl_usd=row["tvl_usd"], last_updated=row["last_updated"],
        )

    async def upsert_protocol(self, p: Protocol) -> None:
        await self.pool.execute(
            """
            INSERT INTO kb_protocols (id, slug, name, symbol, category, description, website, docs_url, github_org, governance_url, defillama_slug, coingecko_id, twitter_handle, chains, contracts, aliases, tvl_usd, last_updated)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,NOW())
            ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, symbol=COALESCE(EXCLUDED.symbol, kb_protocols.symbol), category=COALESCE(EXCLUDED.category, kb_protocols.category),
              description=COALESCE(EXCLUDED.description, kb_protocols.description), website=COALESCE(EXCLUDED.website, kb_protocols.website), docs_url=COALESCE(EXCLUDED.docs_url, kb_protocols.docs_url),
              github_org=COALESCE(EXCLUDED.github_org, kb_protocols.github_org), governance_url=COALESCE(EXCLUDED.governance_url, kb_protocols.governance_url), defillama_slug=COALESCE(EXCLUDED.defillama_slug, kb_protocols.defillama_slug),
              coingecko_id=COALESCE(EXCLUDED.coingecko_id, kb_protocols.coingecko_id), twitter_handle=COALESCE(EXCLUDED.twitter_handle, kb_protocols.twitter_handle), chains=EXCLUDED.chains, contracts=EXCLUDED.contracts,
              aliases=EXCLUDED.aliases, tvl_usd=EXCLUDED.tvl_usd, last_updated=NOW()
            """,
            p.id, p.slug, p.name, p.symbol, p.category, p.description, p.website, p.docs_url, p.github_org, p.governance_url, p.defillama_slug, p.coingecko_id, p.twitter_handle, p.chains, json.dumps(p.contracts), p.aliases, p.tvl_usd,
        )

    async def get_protocol(self, protocol_id: str) -> Protocol | None:
        row = await self.pool.fetchrow("SELECT * FROM kb_protocols WHERE id = $1", protocol_id)
        return self._protocol_row(row) if row else None

    async def list_protocols(self, limit: int = 500) -> list[Protocol]:
        rows = await self.pool.fetch("SELECT * FROM kb_protocols ORDER BY tvl_usd DESC NULLS LAST LIMIT $1", limit)
        return [self._protocol_row(r) for r in rows]

    async def live_document(self, url: str) -> NormalizedDocument | None:
        row = await self.pool.fetchrow("SELECT * FROM kb_documents WHERE source_url = $1 AND valid_to IS NULL ORDER BY version DESC LIMIT 1", url)
        return self._doc_row(row) if row else None

    @staticmethod
    def _doc_row(row) -> NormalizedDocument:
        meta = row["metadata"]
        return NormalizedDocument(
            id=str(row["id"]), source=row["source"], source_type=row["source_type"], url=row["source_url"], protocol_id=row["protocol_id"],
            title=row["title"], content=row["content"], content_hash=row["content_hash"], retrieved_at=row["retrieved_at"], published_at=row["published_at"],
            metadata=json.loads(meta) if isinstance(meta, str) else dict(meta or {}), version=int(row["version"]),
        )

    async def write_document(self, doc: NormalizedDocument, chunks: list[Chunk]) -> tuple[str, bool]:
        current = await self.live_document(doc.url)
        if current and current.content_hash == doc.content_hash:
            return current.id, False
        doc.id = doc.id or str(uuid4())
        doc.version = (current.version + 1) if current else 1
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if current:
                    await conn.execute("UPDATE kb_documents SET valid_to = NOW() WHERE id = $1", current.id)
                    await conn.execute("DELETE FROM kb_chunks WHERE document_id = $1", current.id)
                await conn.execute(
                    "INSERT INTO kb_documents (id, protocol_id, source, source_type, source_url, title, content, content_hash, version, published_at, retrieved_at, metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                    doc.id, doc.protocol_id, doc.source, doc.source_type, doc.url, doc.title, doc.content, doc.content_hash, doc.version, doc.published_at, doc.retrieved_at, json.dumps(doc.metadata),
                )
                for chunk in chunks:
                    chunk.document_id = doc.id
                if chunks:
                    # Rows are built off the loop (a 1,500-chunk docs page means
                    # 1.5M float formats) and inserted in one batch.
                    rows = await asyncio.to_thread(self._chunk_rows, chunks)
                    if self.vector_native:
                        await conn.executemany("INSERT INTO kb_chunks (id, document_id, protocol_id, heading, content, position, embedding, metadata) VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8)", rows)
                    else:
                        await conn.executemany("INSERT INTO kb_chunks (id, document_id, protocol_id, heading, content, position, embedding, metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)", rows)
        return doc.id, True

    def _chunk_rows(self, chunks: list[Chunk]) -> list[tuple]:
        rows = []
        for chunk in chunks:
            if self.vector_native:
                embedding = "[" + ",".join(f"{v:.6f}" for v in chunk.embedding) + "]" if chunk.embedding else None
            else:
                embedding = chunk.embedding
            rows.append((chunk.id, chunk.document_id, chunk.protocol_id, chunk.heading, chunk.content, chunk.position, embedding, json.dumps(chunk.metadata)))
        return rows

    async def get_document(self, document_id: str) -> NormalizedDocument | None:
        row = await self.pool.fetchrow("SELECT * FROM kb_documents WHERE id = $1", document_id)
        return self._doc_row(row) if row else None

    async def live_documents(self, protocol_id: str | None = None) -> list[NormalizedDocument]:
        rows = await self.pool.fetch("SELECT * FROM kb_documents WHERE valid_to IS NULL AND ($1::text IS NULL OR protocol_id = $1) ORDER BY retrieved_at DESC LIMIT 500", protocol_id)
        return [self._doc_row(r) for r in rows]

    async def upsert_entity(self, e: Entity) -> None:
        await self.pool.execute(
            """
            INSERT INTO kb_entities (id, entity_type, canonical_name, symbol, chain, address, aliases, metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (id) DO UPDATE SET canonical_name=EXCLUDED.canonical_name, symbol=COALESCE(EXCLUDED.symbol, kb_entities.symbol), chain=COALESCE(EXCLUDED.chain, kb_entities.chain),
              address=COALESCE(EXCLUDED.address, kb_entities.address), aliases=(SELECT ARRAY(SELECT DISTINCT unnest(kb_entities.aliases || EXCLUDED.aliases))), metadata=kb_entities.metadata || EXCLUDED.metadata, updated_at=NOW()
            """,
            e.id, e.entity_type, e.canonical_name, e.symbol, e.chain, e.address, e.aliases, json.dumps(e.metadata),
        )

    async def list_entities(self) -> list[Entity]:
        rows = await self.pool.fetch("SELECT * FROM kb_entities")
        return [Entity(id=r["id"], entity_type=r["entity_type"], canonical_name=r["canonical_name"], symbol=r["symbol"], chain=r["chain"], address=r["address"], aliases=list(r["aliases"] or []),
                       metadata=json.loads(r["metadata"]) if isinstance(r["metadata"], str) else dict(r["metadata"] or {})) for r in rows]

    async def upsert_relationship(self, rel: Relationship) -> bool:
        row = await self.pool.fetchrow(
            """
            INSERT INTO kb_relationships (id, source_entity_id, relation, target_entity_id, confidence, source_document_id, valid_from, valid_to, observed_at, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (source_entity_id, relation, target_entity_id, valid_from) DO UPDATE SET confidence = GREATEST(kb_relationships.confidence, EXCLUDED.confidence), observed_at = EXCLUDED.observed_at
            RETURNING (xmax = 0) AS inserted
            """,
            str(uuid4()), rel.source_entity_id, rel.relation, rel.target_entity_id, rel.confidence, rel.source_document_id, rel.valid_from, rel.valid_to, rel.observed_at, json.dumps(rel.metadata),
        )
        return bool(row and row["inserted"])

    async def neighbors(self, entity_id: str, relation: str | None = None, direction: str = "both", at: datetime | None = None) -> list[Relationship]:
        clauses = ["(valid_to IS NULL)" if at is None else "((valid_from IS NULL OR valid_from <= $4) AND (valid_to IS NULL OR valid_to > $4))"]
        params: list = [entity_id, relation, direction]
        if at is not None:
            params.append(at)
        rows = await self.pool.fetch(
            f"""
            SELECT * FROM kb_relationships WHERE {clauses[0]} AND ($2::text IS NULL OR relation = $2)
              AND ((($3 = 'out' OR $3 = 'both') AND source_entity_id = $1) OR (($3 = 'in' OR $3 = 'both') AND target_entity_id = $1))
            """,
            *params,
        )
        return [Relationship(source_entity_id=r["source_entity_id"], relation=r["relation"], target_entity_id=r["target_entity_id"], confidence=r["confidence"],
                             source_document_id=str(r["source_document_id"]) if r["source_document_id"] else None, valid_from=r["valid_from"], valid_to=r["valid_to"], observed_at=r["observed_at"],
                             metadata=json.loads(r["metadata"]) if isinstance(r["metadata"], str) else dict(r["metadata"] or {})) for r in rows]

    @staticmethod
    def _chunk_row(row) -> Chunk:
        meta = row["metadata"]
        embedding = row.get("embedding") if hasattr(row, "get") else None
        if isinstance(embedding, str):
            embedding = [float(v) for v in embedding.strip("[]").split(",") if v]
        return Chunk(id=str(row["id"]), document_id=str(row["document_id"]), protocol_id=row["protocol_id"], heading=row["heading"], content=row["content"], position=row["position"],
                     embedding=list(embedding) if embedding else None, metadata=json.loads(meta) if isinstance(meta, str) else dict(meta or {}))

    async def lexical_search(self, query: str, limit: int = 20, protocol_ids: list[str] | None = None) -> list[tuple[Chunk, float]]:
        # OR the terms: AND-semantics (websearch_to_tsquery) drops every chunk that
        # lacks one word of a long question; ranking + RRF keep precision.
        terms = [t for t in _tokens(query) if len(t) > 1 and t not in ("the", "and", "for", "what", "how", "does", "is", "are", "of", "to", "in", "on", "a", "an")]
        if not terms:
            return []
        tsquery = " | ".join(t.replace("'", "") for t in dict.fromkeys(terms))
        rows = await self.pool.fetch(
            """
            SELECT c.*, ts_rank_cd(c.search_vector, q) AS rank FROM kb_chunks c, to_tsquery('english', $1) q
            WHERE c.search_vector @@ q AND ($2::text[] IS NULL OR c.protocol_id = ANY($2)) ORDER BY rank DESC LIMIT $3
            """,
            tsquery, protocol_ids, limit,
        )
        return [(self._chunk_row(r), float(r["rank"])) for r in rows]

    async def semantic_search(self, embedding: list[float], limit: int = 20, protocol_ids: list[str] | None = None) -> list[tuple[Chunk, float]]:
        if self.vector_native:
            vector = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
            rows = await self.pool.fetch(
                "SELECT c.*, 1 - (c.embedding <=> $1::vector) AS sim FROM kb_chunks c WHERE c.embedding IS NOT NULL AND ($2::text[] IS NULL OR c.protocol_id = ANY($2)) ORDER BY c.embedding <=> $1::vector LIMIT $3",
                vector, protocol_ids, limit,
            )
            return [(self._chunk_row(r), float(r["sim"])) for r in rows]
        rows = await self.pool.fetch("SELECT c.* FROM kb_chunks c WHERE c.embedding IS NOT NULL AND ($1::text[] IS NULL OR c.protocol_id = ANY($1)) LIMIT 5000", protocol_ids)
        scored = [(self._chunk_row(r), cosine(embedding, list(r["embedding"]))) for r in rows]
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

    async def chunks_for_protocols(self, protocol_ids: list[str], limit: int = 20) -> list[Chunk]:
        rows = await self.pool.fetch("SELECT * FROM kb_chunks WHERE protocol_id = ANY($1) ORDER BY position LIMIT $2", protocol_ids, limit)
        return [self._chunk_row(r) for r in rows]

    async def record_source_state(self, source: str, protocol_id: str, ok: bool, documents: int, error: str | None = None) -> None:
        await self.pool.execute(
            """
            INSERT INTO kb_source_state (source, protocol_id, last_run_at, last_success_at, last_error, documents) VALUES ($1,$2,NOW(),CASE WHEN $3 THEN NOW() END,$4,$5)
            ON CONFLICT (source, protocol_id) DO UPDATE SET last_run_at = NOW(), last_success_at = CASE WHEN $3 THEN NOW() ELSE kb_source_state.last_success_at END, last_error = $4, documents = $5
            """,
            source, protocol_id, ok, error, documents,
        )

    async def get_source_state(self, source: str, protocol_id: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM kb_source_state WHERE source = $1 AND protocol_id = $2", source, protocol_id)
        return dict(row) if row else None

    async def stats(self) -> dict:
        row = await self.pool.fetchrow(
            "SELECT (SELECT COUNT(*) FROM kb_protocols) AS p, (SELECT COUNT(*) FROM kb_documents WHERE valid_to IS NULL) AS d, (SELECT COUNT(*) FROM kb_chunks) AS c, (SELECT COUNT(*) FROM kb_entities) AS e, (SELECT COUNT(*) FROM kb_relationships) AS r"
        )
        return {"backend": "postgres", "protocols": row["p"], "documents": row["d"], "chunks": row["c"], "entities": row["e"], "relationships": row["r"], "vector_native": self.vector_native}


# ----------------------------------------------------------------------------
# Store selection
# ----------------------------------------------------------------------------

_store = None


async def get_store():
    """Postgres when configured (schema ensured once), memory otherwise."""
    global _store
    if _store is not None:
        return _store
    from app.db import get_pg_pool
    from app.knowledge.schema import ensure_schema
    from app.settings import settings

    pool = await get_pg_pool()
    if pool is not None:
        # A schema failure is a real error (permissions, wrong loop, outage):
        # surface it rather than silently answering from an empty memory store.
        native = await ensure_schema(pool, settings.knowledge_embedding_dim)
        _store = PostgresStore(pool, native)
        return _store
    _store = MemoryStore()
    return _store


def reset() -> None:
    global _store
    _store = None
