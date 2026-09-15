"""Connector interface. A connector turns one source into NormalizedDocuments
for one protocol; it never touches the store. Ingestion (app/knowledge/ingest.py)
owns hashing, chunking, embedding and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Protocol as TypingProtocol

from app.knowledge.models import NormalizedDocument, Protocol


@dataclass
class SourceRef:
    url: str
    title: str = ""
    kind: str = "page"          # page | readme | proposal | metadata
    metadata: dict = field(default_factory=dict)


class Connector(TypingProtocol):
    """Implement `name`, `source_type`, `refresh_every`, `discover`, `fetch`."""

    name: str
    source_type: str
    refresh_every: timedelta

    def applies(self, protocol: Protocol) -> bool: ...

    def discover(self, protocol: Protocol) -> Iterable[SourceRef]: ...

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None: ...
