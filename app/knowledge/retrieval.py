"""Hybrid retrieval: entity recognition -> semantic + lexical + graph
candidates -> reciprocal-rank fusion -> reranker -> context with citations.

The reranker is an interface with a heuristic default (entity overlap and
exact-term boosts) so a cross-encoder can be dropped in later without
touching callers.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Protocol as TypingProtocol

from app.knowledge.embeddings import get_embedder
from app.knowledge.entities import EntityResolver
from app.knowledge.models import Chunk, RetrievalHit
from app.knowledge.store import get_store

_WORD = re.compile(r"[a-z0-9$#-]+")
_STOP = {"the", "a", "an", "of", "on", "in", "to", "for", "and", "or", "is", "are", "what", "how", "does", "do", "explain", "me", "about", "tell", "with", "vs", "versus", "compare"}


@dataclass
class RetrievalPlan:
    query: str
    entities: list = field(default_factory=list)          # Resolutions
    protocol_ids: list[str] = field(default_factory=list)
    graph_expanded: list[str] = field(default_factory=list)


class Reranker(TypingProtocol):
    def rerank(self, query: str, hits: list[RetrievalHit], plan: RetrievalPlan) -> list[RetrievalHit]: ...


class HeuristicReranker:
    """Boosts chunks that mention resolved entities and exact query terms in
    their heading; demotes very short chunks. Deterministic and cheap."""

    def rerank(self, query: str, hits: list[RetrievalHit], plan: RetrievalPlan) -> list[RetrievalHit]:
        terms = [t for t in _WORD.findall(query.lower()) if t not in _STOP and len(t) > 2]
        names = [r.entity.canonical_name.lower() for r in plan.entities]
        for hit in hits:
            text = hit.chunk.text.lower()
            heading = hit.chunk.heading.lower()
            boost = 0.0
            boost += 0.15 * sum(1 for t in terms if t in heading)
            boost += 0.05 * sum(1 for t in terms if t in text)
            boost += 0.2 * sum(1 for n in names if n in text)
            if len(hit.chunk.content) < 120:
                boost -= 0.2
            if hit.chunk.protocol_id and hit.chunk.protocol_id in plan.protocol_ids:
                boost += 0.25
            hit.score += boost
        hits.sort(key=lambda h: -h.score)
        return hits


_reranker: Reranker = HeuristicReranker()


def set_reranker(reranker: Reranker) -> None:
    global _reranker
    _reranker = reranker


async def plan_query(query: str, resolver: EntityResolver) -> RetrievalPlan:
    plan = RetrievalPlan(query=query)
    plan.entities = resolver.mentions(query)
    for token in _WORD.findall(query):
        if token.lower() in _STOP:
            continue
        resolution = resolver.resolve(token.lstrip("$"), context=query)
        if resolution and resolution.confidence >= 0.4 and resolution.entity.id not in {r.entity.id for r in plan.entities}:
            plan.entities.append(resolution)
    plan.protocol_ids = [r.entity.id for r in plan.entities if r.entity.entity_type == "protocol"]
    return plan


def rrf(rankings: list[list[tuple[Chunk, str]]], k: int = 60) -> dict[str, tuple[Chunk, float, list[str]]]:
    fused: dict[str, tuple[Chunk, float, list[str]]] = {}
    for ranking in rankings:
        for rank, (chunk, source) in enumerate(ranking):
            prev = fused.get(chunk.id)
            score = 1.0 / (k + rank + 1)
            if prev:
                fused[chunk.id] = (chunk, prev[1] + score, prev[2] + [source])
            else:
                fused[chunk.id] = (chunk, score, [source])
    return fused


async def graph_expand(plan: RetrievalPlan, store, max_hops: int = 1) -> list[str]:
    """Protocol ids reachable from the query's entities over live edges:
    competitors, protocols on a chain, protocols in a category."""
    found: list[str] = []
    for resolution in plan.entities:
        entity = resolution.entity
        rels = await store.neighbors(entity.id)
        for rel in rels:
            other = rel.target_entity_id if rel.source_entity_id == entity.id else rel.source_entity_id
            if other.startswith("protocol:") and other not in found and other not in plan.protocol_ids:
                found.append(other)
    return found[:12]


async def search(query: str, limit: int = 8, resolver: EntityResolver | None = None) -> tuple[list[RetrievalHit], RetrievalPlan]:
    store = await get_store()
    if resolver is None:
        resolver = EntityResolver(await store.list_entities())
    plan = await plan_query(query, resolver)
    plan.graph_expanded = await graph_expand(plan, store)
    scope = plan.protocol_ids + plan.graph_expanded
    embedder = get_embedder()
    embedding = (await asyncio.to_thread(embedder.embed, [query]))[0]
    semantic, lexical = await asyncio.gather(
        store.semantic_search(embedding, limit=limit * 3, protocol_ids=scope or None),
        store.lexical_search(query, limit=limit * 3, protocol_ids=scope or None),
    )
    rankings = [[(c, "semantic") for c, _ in semantic], [(c, "lexical") for c, _ in lexical]]
    if plan.graph_expanded:
        graph_chunks = await store.chunks_for_protocols(plan.graph_expanded, limit=limit * 2)
        rankings.append([(c, "graph") for c in graph_chunks])
    # When the scope is empty (no entity recognised) the searches ran corpus-wide already.
    fused = rrf(rankings)
    hits = [RetrievalHit(chunk=chunk, score=score, sources=sorted(set(sources))) for chunk, score, sources in fused.values()]
    hits = _reranker.rerank(query, hits, plan)[: limit * 2]
    # Hydrate document titles / urls / protocol names for citations.
    docs: dict[str, object] = {}
    for hit in hits:
        if hit.chunk.document_id not in docs:
            docs[hit.chunk.document_id] = await store.get_document(hit.chunk.document_id)
        doc = docs[hit.chunk.document_id]
        if doc:
            hit.document_title, hit.document_url = doc.title, doc.url
        if hit.chunk.protocol_id:
            protocol = await store.get_protocol(hit.chunk.protocol_id)
            hit.protocol_name = protocol.name if protocol else hit.chunk.protocol_id
    # One chunk per (document, heading) to keep the context diverse.
    seen: set[tuple[str, str]] = set()
    unique: list[RetrievalHit] = []
    for hit in hits:
        key = (hit.chunk.document_id, hit.chunk.heading)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique[:limit], plan


def build_context(hits: list[RetrievalHit], max_chars: int = 6000) -> tuple[str, list[dict]]:
    """Numbered passages + a citation list, ready for the LLM prompt."""
    parts: list[str] = []
    citations: list[dict] = []
    used = 0
    for index, hit in enumerate(hits, start=1):
        passage = f"[{index}] {hit.protocol_name or ''} · {hit.document_title}{' › ' + hit.chunk.heading if hit.chunk.heading else ''}\n{hit.chunk.content.strip()}"
        if used + len(passage) > max_chars:
            break
        parts.append(passage)
        used += len(passage)
        citations.append({"n": index, "title": hit.document_title, "url": hit.document_url, "heading": hit.chunk.heading, "protocol": hit.protocol_name, "sources": hit.sources})
    return "\n\n".join(parts), citations
