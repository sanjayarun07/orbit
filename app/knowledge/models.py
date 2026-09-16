"""Records shared across the knowledge service. Every connector emits a
NormalizedDocument; everything downstream consumes only that shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SOURCE_TYPES = ("protocol_docs", "github", "governance", "defillama", "incident", "funding", "token", "subgraph", "news")
ENTITY_TYPES = ("protocol", "token", "chain", "contract", "dao", "person", "exchange", "category", "organization", "incident")
RELATIONS = ("DEPLOYED_ON", "TOKEN_OF", "GOVERNED_BY", "INTEGRATES_WITH", "COMPETITOR_OF", "SUPPORTS_ASSET", "IN_CATEGORY", "AUDITED_BY", "FORK_OF",
             "USES_ORACLE", "FUNDED_BY", "PART_OF", "HAD_INCIDENT")

# Structured facts ride along in NormalizedDocument.metadata["facts"]: the
# connector states them, ingestion resolves/creates the target entity and
# writes the edge with the document as provenance. Shape:
#   {"relation": "FUNDED_BY", "target": {"id": "org:paradigm", "type": "organization", "name": "Paradigm"},
#    "confidence": 0.95, "valid_from": "2023-03-28", "direction": "out" | "in", "metadata": {...}}
# A target without an id is resolved by name against the entity index
# (protocols, chains) and dropped when it does not resolve at >= 0.9.


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Protocol:
    id: str                      # protocol:<slug>
    slug: str
    name: str
    symbol: str | None = None
    category: str | None = None
    description: str | None = None
    website: str | None = None
    docs_url: str | None = None
    github_org: str | None = None
    governance_url: str | None = None   # Snapshot space
    forum_url: str | None = None        # Discourse forum
    defillama_slug: str | None = None
    coingecko_id: str | None = None
    twitter_handle: str | None = None
    chains: list[str] = field(default_factory=list)
    contracts: list[dict] = field(default_factory=list)   # [{chain, address, label}]
    aliases: list[str] = field(default_factory=list)
    tvl_usd: float | None = None
    last_updated: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {**self.__dict__, "last_updated": self.last_updated.isoformat()}


@dataclass
class NormalizedDocument:
    """The one object every connector produces."""
    source: str                  # e.g. "aave_docs"
    source_type: str             # one of SOURCE_TYPES
    url: str
    protocol_id: str | None
    title: str
    content: str                 # markdown
    content_hash: str
    retrieved_at: datetime = field(default_factory=utcnow)
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    version: int = 1


@dataclass
class Chunk:
    id: str
    document_id: str
    protocol_id: str | None
    heading: str
    content: str
    position: int
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.heading}\n{self.content}" if self.heading else self.content


@dataclass
class Entity:
    id: str                      # canonical id, e.g. chain:ethereum, token:solana:<mint>
    entity_type: str
    canonical_name: str
    symbol: str | None = None
    chain: str | None = None
    address: str | None = None
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relationship:
    source_entity_id: str
    relation: str
    target_entity_id: str
    confidence: float = 0.8
    source_document_id: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Resolution:
    """Outcome of resolving a mention to a canonical entity."""
    entity: Entity
    confidence: float
    method: str                  # contract | chain_contract | coingecko_id | defillama_id | canonical_name | alias | ticker


@dataclass
class RetrievalHit:
    chunk: Chunk
    score: float
    sources: list[str]           # which searches surfaced it: semantic | lexical | graph
    document_title: str = ""
    document_url: str = ""
    protocol_name: str = ""


@dataclass
class IngestionResult:
    protocol_id: str
    source: str
    documents_seen: int = 0
    documents_changed: int = 0
    chunks_written: int = 0
    entities_written: int = 0
    relationships_written: int = 0
    errors: list[str] = field(default_factory=list)
