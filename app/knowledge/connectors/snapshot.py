"""Snapshot governance proposals (GraphQL hub, no key). Each proposal is a
document with its outcome, so "did X vote to ..." and "what parameter
changes passed" are answerable with a citation. Space ids are discovered from
the protocol's governance_url or aliases; Discourse forums are a later
connector with the same shape."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash

HUB = "https://hub.snapshot.org/graphql"
_QUERY = """
query Proposals($space: String!, $first: Int!) {
  proposals(first: $first, where: {space_in: [$space]}, orderBy: "created", orderDirection: desc) {
    id title body state start end created choices scores scores_total author link
  }
}
"""


class SnapshotConnector:
    name = "snapshot"
    source_type = "governance"
    refresh_every = timedelta(minutes=15)

    def __init__(self, proposals: int = 20):
        self.proposals = proposals

    @staticmethod
    def space_for(protocol: Protocol) -> str | None:
        url = protocol.governance_url or ""
        m = re.search(r"snapshot\.(?:org|box)/#?/?([a-z0-9.\-]+\.eth|s:[a-z0-9.\-]+)", url, re.I)
        if m:
            return m.group(1)
        return None

    def applies(self, protocol: Protocol) -> bool:
        return bool(self.space_for(protocol))

    def discover(self, protocol: Protocol):
        space = self.space_for(protocol)
        return [SourceRef(url=f"{HUB}?space={space}", title=f"{protocol.name} governance ({space})", kind="proposal", metadata={"space": space})] if space else []

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        # One fetch returns many proposals; ingestion splits documents by `documents()`.
        return None

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        with httpx.Client(timeout=25, headers={"User-Agent": "Dopamint-Knowledge/0.1"}) as client:
            resp = client.post(HUB, json={"query": _QUERY, "variables": {"space": ref.metadata["space"], "first": self.proposals}})
            resp.raise_for_status()
            proposals = ((resp.json().get("data") or {}).get("proposals")) or []
        return [d for d in (self.document(protocol, p) for p in proposals) if d]

    @staticmethod
    def document(protocol: Protocol, p: dict) -> NormalizedDocument | None:
        body = clean_markdown(str(p.get("body") or ""))
        if not p.get("title"):
            return None
        choices, scores = p.get("choices") or [], p.get("scores") or []
        tally = ", ".join(f"{c}: {float(s):,.0f}" for c, s in zip(choices, scores)) if scores else ", ".join(choices)
        created = datetime.fromtimestamp(int(p.get("created") or 0), tz=timezone.utc) if p.get("created") else None
        content = "\n".join([
            f"# {p['title']}", "",
            f"**Protocol**: {protocol.name} · **State**: {p.get('state')} · **Created**: {created.date().isoformat() if created else '?'} · **Author**: {p.get('author')}",
            f"**Choices / votes**: {tally}",
            "", "## Proposal", body or "(no body)",
        ])
        return NormalizedDocument(
            source=f"{protocol.slug}_snapshot", source_type="governance", url=p.get("link") or f"https://snapshot.org/#/{p.get('id')}", protocol_id=protocol.id,
            title=f"Governance: {p['title'][:140]}", content=content, content_hash=content_hash(content), published_at=created,
            metadata={"state": p.get("state"), "proposal_id": p.get("id"), "scores_total": p.get("scores_total")},
        )
