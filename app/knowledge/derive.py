"""Edges derived from what the graph already knows. Runs after ingestion.

COMPETITOR_OF   two protocols in the same DefiLlama category with at least
                one chain in common. Structured inputs, so confidence 0.6;
                the shared chains and category are kept as evidence.
INTEGRATES_WITH prose mentions arrive from ingestion at 0.5, one edge per
                source document. Here they are consolidated: a pair mentioned
                in two or more independent documents (different sources, or
                each protocol's own docs naming the other) is promoted to a
                single 0.75 edge that lists its evidence. One mention never
                gets above 0.5 on its own, per the design.

Both are idempotent (the store upserts on the edge key) and cheap: a few
hundred protocols is a few thousand comparisons.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from app.knowledge.models import Protocol, Relationship
from app.knowledge.store import get_store

logger = logging.getLogger(__name__)

COMPETITOR_CONFIDENCE = 0.6
INTEGRATION_CONFIDENCE = 0.75
MAX_COMPETITORS = 10


def competitor_pairs(protocols: list[Protocol], max_per_protocol: int = MAX_COMPETITORS) -> list[tuple[Protocol, Protocol, list[str]]]:
    """(a, b, shared_chains) for protocols sharing category + chain; each
    protocol keeps its `max_per_protocol` largest peers so a crowded category
    (Lending) does not turn graph expansion into a category listing."""
    by_category: dict[str, list[Protocol]] = defaultdict(list)
    for p in protocols:
        if p.category and p.chains:
            by_category[p.category].append(p)
    kept: dict[str, list[tuple[Protocol, list[str]]]] = defaultdict(list)
    for members in by_category.values():
        members = sorted(members, key=lambda p: -(p.tvl_usd or 0))
        for a in members:
            peers = []
            for b in members:
                if a.id == b.id:
                    continue
                shared = sorted(set(a.chains) & set(b.chains))
                if shared:
                    peers.append((b, shared))
            kept[a.id] = peers[:max_per_protocol]
    pairs: dict[tuple[str, str], tuple[Protocol, Protocol, list[str]]] = {}
    for a_id, peers in kept.items():
        for b, shared in peers:
            key = tuple(sorted((a_id, b.id)))
            if key not in pairs:
                a = next(p for p in protocols if p.id == a_id)
                pairs[key] = (a, b, shared) if a.id == key[0] else (b, a, shared)
    return list(pairs.values())


async def derive_competitors(store=None) -> int:
    store = store or await get_store()
    now = datetime.now(timezone.utc)
    written = 0
    for a, b, shared in competitor_pairs(await store.list_protocols(limit=5000)):
        rel = Relationship(a.id, "COMPETITOR_OF", b.id, confidence=COMPETITOR_CONFIDENCE, observed_at=now,
                           metadata={"method": "category_chain", "category": a.category, "shared_chains": shared})
        written += int(await store.upsert_relationship(rel))
    return written


async def derive_integrations(store=None) -> int:
    """Promote mention edges with two or more independent sources."""
    store = store or await get_store()
    now = datetime.now(timezone.utc)
    evidence: dict[tuple[str, str], list[Relationship]] = defaultdict(list)
    for rel in await store.list_relationships("INTEGRATES_WITH"):
        if rel.metadata.get("method") != "mention" or not rel.source_document_id:
            continue
        key = tuple(sorted((rel.source_entity_id, rel.target_entity_id)))
        evidence[key].append(rel)
    written = 0
    for (a, b), rels in evidence.items():
        documents = {r.source_document_id for r in rels}
        sources = {r.metadata.get("source") for r in rels}
        directions = {r.source_entity_id for r in rels}
        independent = len(documents) >= 2 and (len(sources) >= 2 or len(directions) >= 2)
        if not independent:
            continue
        rel = Relationship(a, "INTEGRATES_WITH", b, confidence=INTEGRATION_CONFIDENCE, observed_at=now,
                           metadata={"method": "corroborated_mentions", "documents": sorted(documents)[:10], "sources": sorted(s for s in sources if s), "mutual": len(directions) >= 2})
        written += int(await store.upsert_relationship(rel))
    return written


async def derive_all(store=None) -> dict:
    store = store or await get_store()
    competitors = await derive_competitors(store)
    integrations = await derive_integrations(store)
    logger.info("knowledge: derived edges competitors=%d integrations=%d", competitors, integrations)
    return {"competitors": competitors, "integrations": integrations}
