"""GitHub READMEs for a protocol's org: the org's most-starred repos' README
(via the REST API, unauthenticated is fine at this volume; a token raises the
rate limit). Code is not ingested -- READMEs and docs folders carry the
prose that answers questions."""

from __future__ import annotations

import base64
from datetime import timedelta

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash
from app.settings import settings


class GitHubConnector:
    name = "github"
    source_type = "github"
    refresh_every = timedelta(hours=6)

    def __init__(self, repos_per_org: int = 5):
        self.repos_per_org = repos_per_org

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Dopamint-Knowledge/0.1"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        return headers

    def applies(self, protocol: Protocol) -> bool:
        return bool(protocol.github_org)

    def discover(self, protocol: Protocol):
        with httpx.Client(timeout=20, headers=self._headers()) as client:
            resp = client.get(f"https://api.github.com/orgs/{protocol.github_org}/repos", params={"sort": "updated", "per_page": 30, "type": "public"})
            if resp.status_code == 404:
                resp = client.get(f"https://api.github.com/users/{protocol.github_org}/repos", params={"sort": "updated", "per_page": 30, "type": "owner"})
            if resp.status_code >= 400:
                return []
            repos = sorted((r for r in resp.json() if isinstance(r, dict) and not r.get("archived") and not r.get("fork")), key=lambda r: -(r.get("stargazers_count") or 0))
        return [SourceRef(url=f"https://api.github.com/repos/{r['full_name']}/readme", title=f"{r['full_name']} README", kind="readme", metadata={"html_url": r.get("html_url"), "stars": r.get("stargazers_count")}) for r in repos[: self.repos_per_org]]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        with httpx.Client(timeout=20, headers=self._headers()) as client:
            resp = client.get(ref.url)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        try:
            text = base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")
        except Exception:
            return None
        markdown = clean_markdown(text)
        if len(markdown) < 200:
            return None
        html_url = data.get("html_url") or ref.metadata.get("html_url") or ref.url
        return NormalizedDocument(
            source=f"{protocol.slug}_github", source_type="github", url=html_url, protocol_id=protocol.id, title=ref.title,
            content=markdown, content_hash=content_hash(markdown), metadata={"stars": ref.metadata.get("stars"), "path": data.get("path")},
        )
