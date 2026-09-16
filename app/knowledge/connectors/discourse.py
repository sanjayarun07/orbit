"""Discourse governance forums (governance.aave.com, research.lido.fi,
gov.uniswap.org ...). Every Discourse site exposes the same JSON API without
a key: `/latest.json` lists topics, `/t/<id>.json` returns the posts. One
topic becomes one document: the opening post in full plus the first few
replies, so "what did the community say about raising the LTV" is answerable
with a link to the thread.

Budgeted like the docs crawler: `topics` newest-by-activity per run; unchanged
threads hash-match and cost nothing downstream."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import clean_markdown, content_hash, html_to_markdown

_UA = {"User-Agent": "Dopamint-Knowledge/0.1 (+governance reader)", "Accept": "application/json"}


class DiscourseConnector:
    name = "discourse"
    source_type = "governance"
    refresh_every = timedelta(hours=6)

    def __init__(self, topics: int = 30, replies: int = 5, timeout: float = 20.0):
        self.topics = topics
        self.replies = replies
        self.timeout = timeout

    @staticmethod
    def forum_for(protocol: Protocol) -> str | None:
        url = (protocol.forum_url or "").strip().rstrip("/")
        return url if re.match(r"^https?://", url) else None

    def applies(self, protocol: Protocol) -> bool:
        return bool(self.forum_for(protocol))

    def discover(self, protocol: Protocol):
        forum = self.forum_for(protocol)
        return [SourceRef(url=f"{forum}/latest.json", title=f"{protocol.name} forum", kind="proposal", metadata={"forum": forum})] if forum else []

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        return None  # one ref expands into many documents via documents()

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        forum = ref.metadata["forum"]
        out: list[NormalizedDocument] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=_UA) as client:
            resp = client.get(f"{forum}/latest.json", params={"order": "activity"})
            resp.raise_for_status()
            topics = ((resp.json().get("topic_list") or {}).get("topics")) or []
            for topic in topics[: self.topics]:
                if not topic.get("id") or topic.get("pinned_globally"):
                    continue
                try:
                    detail = client.get(f"{forum}/t/{topic['id']}.json")
                    if detail.status_code != 200:
                        continue
                    doc = self.document(protocol, forum, topic, detail.json(), replies=self.replies)
                except (httpx.HTTPError, ValueError):
                    continue
                if doc:
                    out.append(doc)
        return out

    @staticmethod
    def document(protocol: Protocol, forum: str, topic: dict, detail: dict, replies: int = 5) -> NormalizedDocument | None:
        posts = ((detail.get("post_stream") or {}).get("posts")) or []
        if not posts or not topic.get("title"):
            return None
        first, rest = posts[0], posts[1:]
        body = clean_markdown(html_to_markdown(first.get("cooked") or ""))
        if len(body) < 80:
            return None
        created = _ts(topic.get("created_at") or first.get("created_at"))
        last_posted = _ts(topic.get("last_posted_at"))
        # Some forums return tags as objects ({"name": ...}) rather than strings.
        tag_names = [t if isinstance(t, str) else str((t or {}).get("name") or "") for t in (topic.get("tags") or [])]
        tag_names = [t for t in tag_names if t]
        tags = ", ".join(tag_names)
        lines = [
            f"# {topic['title']}", "",
            f"**Protocol**: {protocol.name} · **Forum**: {forum} · **Posted**: {created.date().isoformat() if created else '?'} · **Author**: {first.get('username') or '?'}"
            + (f" · **Replies**: {topic.get('posts_count', 1) - 1}" if topic.get("posts_count") else "") + (f" · **Tags**: {tags}" if tags else ""),
            "", "## Post", body,
        ]
        discussion = []
        for post in rest[:replies]:
            text = clean_markdown(html_to_markdown(post.get("cooked") or "")).strip()
            if len(text) < 40:
                continue
            discussion.append(f"**{post.get('username') or 'reply'}** ({_ts(post.get('created_at')).date().isoformat() if _ts(post.get('created_at')) else '?'}): {text[:700]}")
        if discussion:
            lines += ["", "## Discussion", *discussion]
        content = "\n".join(lines)
        url = f"{forum}/t/{topic.get('slug') or 'topic'}/{topic['id']}"
        return NormalizedDocument(
            source=f"{protocol.slug}_forum", source_type="governance", url=url, protocol_id=protocol.id, title=f"Forum: {topic['title'][:140]}",
            content=content, content_hash=content_hash(content), published_at=created,
            metadata={"topic_id": topic["id"], "category_id": topic.get("category_id"), "posts_count": topic.get("posts_count"), "last_posted_at": last_posted.isoformat() if last_posted else None, "tags": tag_names},
        )


def _ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
