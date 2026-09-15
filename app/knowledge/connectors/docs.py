"""Generic protocol-docs crawler. Bounded and polite: starts at the docs
root, prefers llms.txt / sitemap when present, follows same-host links up to
a page budget, extracts main content to markdown, skips 404s and near-empty
pages. GitBook, Mintlify, Docusaurus and plain HTML all reduce to the same
NormalizedDocument."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash, html_to_markdown
from app.knowledge.registry import guess_docs_urls

logger = logging.getLogger(__name__)
_HREF = re.compile(r'href=["\']([^"\'#]+)', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip", ".js", ".css", ".xml", ".json", ".ico", ".woff", ".woff2")
_SKIP_PATH = re.compile(r"/(?:changelog|release-notes|blog|legal|privacy|terms|search|tags?|api-reference/[^/]+/[^/]+/)", re.I)


class DocsConnector:
    name = "protocol_docs"
    source_type = "protocol_docs"
    refresh_every = timedelta(hours=12)

    def __init__(self, page_budget: int = 40, timeout: float = 15.0):
        self.page_budget = page_budget
        self.timeout = timeout

    def applies(self, protocol: Protocol) -> bool:
        return bool(guess_docs_urls(protocol))

    def discover(self, protocol: Protocol):
        """First docs root that serves a real site wins. A root counts when it
        returns pages with same-host links; an app shell or a 404 is skipped."""
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "Dopamint-Knowledge/0.1 (+docs crawler)"}) as client:
            for root in guess_docs_urls(protocol):
                refs = self._discover_root(client, root)
                if len(refs) >= self.MIN_PAGES:
                    logger.info("knowledge: %s docs root %s (%d pages)", protocol.slug, root, len(refs))
                    return refs
        return []

    MIN_PAGES = 2

    def _discover_root(self, client: httpx.Client, root: str) -> list[SourceRef]:
        refs: list[SourceRef] = []
        # llms.txt: curated markdown links -- best case.
        for candidate in (f"{root}/llms.txt", f"{root}/llms-full.txt"):
            try:
                resp = client.get(candidate)
                if resp.status_code == 200 and "http" in resp.text:
                    for url in re.findall(r"\((https?://[^)\s]+)\)", resp.text):
                        if self._same_host(root, url) and len(refs) < self.page_budget:
                            refs.append(SourceRef(url=url, kind="page"))
                    if refs:
                        return refs
            except httpx.HTTPError:
                pass
        try:
            resp = client.get(root)
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400 or "html" not in (resp.headers.get("content-type") or ""):
            return []
        final_root = str(resp.url)
        refs.append(SourceRef(url=final_root, kind="page"))
        seen = {final_root}
        queue = [u for u in self._links(final_root, resp.text) if u not in seen]
        while queue and len(refs) < self.page_budget:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            refs.append(SourceRef(url=url, kind="page"))
        return refs

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "Dopamint-Knowledge/0.1 (+docs crawler)"}) as client:
            try:
                resp = client.get(ref.url)
            except httpx.HTTPError:
                return None
        if resp.status_code >= 400 or "html" not in (resp.headers.get("content-type") or "") and not ref.url.endswith((".md", ".txt")):
            return None
        return self.document(protocol, ref.url, resp.text)

    @staticmethod
    def document(protocol: Protocol, url: str, html: str) -> NormalizedDocument | None:
        if url.endswith((".md", ".txt")):
            markdown = clean_markdown(html)
            title = markdown.split("\n", 1)[0].lstrip("# ").strip()[:160] or url
        else:
            markdown = html_to_markdown(html)
            m = _TITLE.search(html)
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:160] if m else url
            title = re.split(r"\s+[|\-–—]\s+", title)[0] or title
        if len(markdown) < 200:
            return None
        return NormalizedDocument(
            source=f"{protocol.slug}_docs", source_type="protocol_docs", url=url, protocol_id=protocol.id, title=title,
            content=markdown, content_hash=content_hash(markdown), metadata={"host": urlparse(url).netloc},
        )

    @staticmethod
    def _same_host(root: str, url: str) -> bool:
        return urlparse(url).netloc == urlparse(root).netloc

    def _links(self, base: str, html: str) -> list[str]:
        out: list[str] = []
        for href in _HREF.findall(html):
            url = urljoin(base, href).split("#", 1)[0].split("?", 1)[0]
            if not self._same_host(base, url) or url.lower().endswith(_SKIP_EXT) or _SKIP_PATH.search(url):
                continue
            if url not in out:
                out.append(url)
        return out
