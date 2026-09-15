"""DefiLlama protocol metadata as a document: the description, category,
chains and links -- the KB's baseline page for every protocol, so "what is X"
has an answer before any docs are crawled. TVL itself stays a live tool."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash

PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"


class DefiLlamaConnector:
    name = "defillama"
    source_type = "defillama"
    refresh_every = timedelta(hours=1)

    def applies(self, protocol: Protocol) -> bool:
        return bool(protocol.defillama_slug)

    def discover(self, protocol: Protocol):
        return [SourceRef(url=PROTOCOL_URL.format(slug=protocol.defillama_slug), title=f"{protocol.name} — overview", kind="metadata")]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        with httpx.Client(timeout=20, headers={"User-Agent": "Dopamint-Knowledge/0.1"}) as client:
            resp = client.get(ref.url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        return self.document(protocol, data, ref.url)

    @staticmethod
    def document(protocol: Protocol, data: dict, url: str) -> NormalizedDocument:
        chains = ", ".join(str(c) for c in (data.get("chains") or protocol.chains or []))
        audits = data.get("audit_links") or []
        lines = [
            f"# {protocol.name}",
            "",
            f"**Category**: {data.get('category') or protocol.category or 'unknown'}",
            f"**Chains**: {chains or 'unknown'}",
            f"**Token**: {data.get('symbol') or protocol.symbol or 'none'}",
            f"**Website**: {data.get('url') or protocol.website or ''}",
            f"**Twitter**: {data.get('twitter') or protocol.twitter_handle or ''}",
            "",
            "## Description",
            clean_markdown(str(data.get("description") or protocol.description or "No description provided.")),
        ]
        if data.get("methodology"):
            lines += ["", "## TVL methodology", clean_markdown(str(data["methodology"]))]
        if audits:
            lines += ["", "## Audits", *[f"- {a}" for a in audits[:10]]]
        if data.get("listedAt"):
            lines += ["", f"Listed on DefiLlama: {datetime.fromtimestamp(int(data['listedAt']), tz=timezone.utc).date().isoformat()}"]
        content = "\n".join(lines)
        return NormalizedDocument(
            source="defillama", source_type="defillama", url=url, protocol_id=protocol.id, title=f"{protocol.name} — overview",
            content=content, content_hash=content_hash(content), metadata={"category": data.get("category"), "chains": data.get("chains"), "symbol": data.get("symbol")},
        )
