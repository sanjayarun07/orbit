"""Ingestion: connectors -> normalized documents -> (hash check) -> chunks ->
embeddings -> store; then deterministic entity/relationship extraction from
the new text. A background worker walks the registry and re-runs each
(connector, protocol) pair when its refresh interval has elapsed.

Cost control is structural: unchanged documents (same content hash) are
skipped before chunking, so re-runs embed nothing unless a page changed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.knowledge.connectors import DefiLlamaConnector, DocsConnector, GitHubConnector, SnapshotConnector
from app.knowledge.embeddings import get_embedder
from app.knowledge.entities import EntityResolver
from app.knowledge.models import Chunk, IngestionResult, NormalizedDocument, Protocol, Relationship
from app.knowledge.normalize import chunk_markdown
from app.knowledge.store import get_store
from app.settings import settings

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_status: dict = {"running": False, "last_run": None, "runs": []}


def default_connectors() -> list:
    return [DefiLlamaConnector(), DocsConnector(page_budget=settings.knowledge_docs_page_budget), GitHubConnector(), SnapshotConnector()]


async def _resolver(store) -> EntityResolver:
    return EntityResolver(await store.list_entities())


def extract_relationships(doc: NormalizedDocument, resolver: EntityResolver) -> list[Relationship]:
    """Deterministic, low-confidence edges from prose: protocol-mentions
    within a protocol's own documents become INTEGRATES_WITH / SUPPORTS_ASSET
    candidates; chains mentioned become DEPLOYED_ON candidates. Every edge
    carries the source document so it can be audited or revoked. Model-based
    extraction can add higher-precision edges later behind the same shape."""
    if not doc.protocol_id or not doc.id:
        return []
    now = datetime.now(timezone.utc)
    out: list[Relationship] = []
    for resolution in resolver.mentions(doc.content):
        entity = resolution.entity
        if entity.id == doc.protocol_id or resolution.confidence < 0.75:
            continue
        if entity.entity_type == "chain":
            out.append(Relationship(doc.protocol_id, "DEPLOYED_ON", entity.id, confidence=0.55, source_document_id=doc.id, observed_at=now, metadata={"source": doc.source, "method": "mention"}))
        elif entity.entity_type == "protocol":
            out.append(Relationship(doc.protocol_id, "INTEGRATES_WITH", entity.id, confidence=0.5, source_document_id=doc.id, observed_at=now, metadata={"source": doc.source, "method": "mention"}))
        elif entity.entity_type == "token":
            out.append(Relationship(doc.protocol_id, "SUPPORTS_ASSET", entity.id, confidence=0.5, source_document_id=doc.id, observed_at=now, metadata={"source": doc.source, "method": "mention"}))
    return out[:40]


async def ingest_document(doc: NormalizedDocument, store=None, resolver: EntityResolver | None = None) -> tuple[bool, int, int]:
    """(changed, chunks_written, relationships_written)."""
    store = store or await get_store()
    current = await store.live_document(doc.url)
    if current and current.content_hash == doc.content_hash:
        return False, 0, 0
    pieces = chunk_markdown(doc.content)
    if not pieces:
        return False, 0, 0
    embedder = get_embedder()
    vectors = await asyncio.to_thread(embedder.embed, [f"{p['heading']}\n{p['content']}" for p in pieces])
    chunks = [Chunk(id=str(uuid4()), document_id="", protocol_id=doc.protocol_id, heading=p["heading"], content=p["content"], position=p["position"], embedding=v,
                    metadata={"source_type": doc.source_type, "embedder": embedder.name}) for p, v in zip(pieces, vectors)]
    doc_id, changed = await store.write_document(doc, chunks)
    if not changed:
        return False, 0, 0
    doc.id = doc_id
    written = 0
    resolver = resolver or await _resolver(store)
    for rel in extract_relationships(doc, resolver):
        written += int(await store.upsert_relationship(rel))
    return True, len(chunks), written


async def run_source(connector, protocol: Protocol, store=None) -> IngestionResult:
    store = store or await get_store()
    result = IngestionResult(protocol_id=protocol.id, source=connector.name)
    resolver = await _resolver(store)
    try:
        refs = await asyncio.to_thread(list, connector.discover(protocol))
    except Exception as exc:
        result.errors.append(f"discover: {exc}"[:200])
        await store.record_source_state(connector.name, protocol.id, False, 0, str(exc)[:300])
        return result
    for ref in refs:
        try:
            if hasattr(connector, "documents") and ref.kind == "proposal":
                docs = await asyncio.to_thread(connector.documents, protocol, ref)
            else:
                doc = await asyncio.to_thread(connector.fetch, protocol, ref)
                docs = [doc] if doc else []
        except Exception as exc:
            result.errors.append(f"{ref.url}: {exc}"[:200])
            continue
        for doc in docs:
            result.documents_seen += 1
            try:
                changed, chunks, rels = await ingest_document(doc, store, resolver)
            except Exception as exc:
                result.errors.append(f"{doc.url}: {exc}"[:200])
                continue
            result.documents_changed += int(changed)
            result.chunks_written += chunks
            result.relationships_written += rels
    await store.record_source_state(connector.name, protocol.id, not result.errors or result.documents_seen > 0, result.documents_seen, "; ".join(result.errors[:3]) or None)
    return result


async def run_protocol(protocol_id: str, connectors: list | None = None, store=None) -> list[IngestionResult]:
    store = store or await get_store()
    protocol = await store.get_protocol(protocol_id)
    if protocol is None:
        raise ValueError(f"Unknown protocol {protocol_id}")
    results = []
    for connector in connectors or default_connectors():
        if not connector.applies(protocol):
            continue
        results.append(await run_source(connector, protocol, store))
    return results


async def due(connector, protocol: Protocol, store) -> bool:
    state = await store.get_source_state(connector.name, protocol.id)
    last = (state or {}).get("last_run_at")
    if not last:
        return True
    if isinstance(last, str):
        last = datetime.fromisoformat(last)
    return datetime.now(timezone.utc) - last >= connector.refresh_every


async def tick(limit: int = 5, connectors: list | None = None) -> list[IngestionResult]:
    """One worker pass: the first `limit` (protocol, connector) pairs that
    are due, most valuable (TVL) first."""
    if _lock.locked():
        return []
    async with _lock:
        store = await get_store()
        connectors = connectors or default_connectors()
        results: list[IngestionResult] = []
        for protocol in await store.list_protocols(limit=settings.knowledge_registry_limit):
            for connector in connectors:
                if len(results) >= limit:
                    break
                if connector.applies(protocol) and await due(connector, protocol, store):
                    results.append(await run_source(connector, protocol, store))
        _status["last_run"] = datetime.now(timezone.utc).isoformat()
        _status["runs"] = ([r.__dict__ for r in results] + _status["runs"])[:50]
        return results


async def worker() -> None:
    if not settings.knowledge_ingest_enabled:
        return
    _status["running"] = True
    while True:
        try:
            await tick(limit=settings.knowledge_ingest_batch)
        except Exception:
            logger.warning("knowledge: ingestion tick failed", exc_info=True)
        await asyncio.sleep(max(30, settings.knowledge_ingest_interval_seconds))


def status() -> dict:
    return dict(_status)
